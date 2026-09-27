"""Retrieval eval without an LLM: does kg_pipeline.retrieve.search() put the expected projects and section types
in its top k?

Two modes (the third, end-to-end, is eval/run_e2e.py):
  retrieval  question only, default search settings: what unrouted retrieval finds (baseline)
  oracle     filters set from the answer key (expect_ids that are corpus operations, expect_section_types,
             expect_scope; per_project_k when several operations are expected): what perfect routing could
             find (upper bound)

Questions come from the shared files (eval/questions.py documents the fields); only questions with
expect_section_types are scored.

Per question:
  project_recall  share of expected corpus project IDs among the top-k results (n/a if none expected)
  type_hit        some result's section type or subtype is expected
  joint_rank      rank of the first result matching an expected project (if any), type and scope
  pass            project_recall = 1 (or n/a) and a joint match exists; MRR = mean of 1/joint_rank

Usage: python eval/eval.py [--k 8] [--modes retrieval,oracle] [--files eval/retrieval.jsonl,eval/e2e.jsonl] [--out out/eval_results.json]
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))

import questions as Q  # noqa: E402
from kg_pipeline import retrieve as R  # noqa: E402

DEFAULT_FILES = [ROOT / "eval" / "retrieval.jsonl", ROOT / "eval" / "e2e.jsonl"]


def corpus_operations() -> set[str]:
    with R.driver().session(database=R.database()) as s:
        return {r["id"] for r in s.run("MATCH (:Document)-[:DESCRIBES]->(o:Operation) RETURN o.project_id AS id")}


def score(item: dict, hits: list[dict], corpus: set[str]) -> dict:
    want_p = set(item["expect_ids"]) & corpus
    want_t = set(item["expect_section_types"])
    want_scope = item.get("expect_scope")
    found_p = set()
    for h in hits:
        found_p |= {h["project_id"], h.get("phase_project_id")} & want_p
    type_ok = lambda h: h["type"] in want_t or h["subtype"] in want_t
    proj_ok = lambda h: not want_p or h["project_id"] in want_p or h.get("phase_project_id") in want_p
    scope_ok = lambda h: not want_scope or h["scope"] == want_scope
    joint = next((i for i, h in enumerate(hits, 1) if type_ok(h) and proj_ok(h) and scope_ok(h)), None)
    recall = len(found_p) / len(want_p) if want_p else None
    return {"project_recall": recall, "missing_projects": sorted(want_p - found_p), "type_hit": any(type_ok(h) for h in hits),
            "joint_rank": joint, "rr": 1 / joint if joint else 0.0, "pass": (recall is None or recall == 1) and joint is not None}


def run(items: list[dict], k: int, mode: str, corpus: set[str]) -> list[dict]:
    rows = []
    for item in items:
        if mode == "oracle":
            ids = [p for p in item["expect_ids"] if p in corpus] or None
            # Perfect routing for a multi-operation question is one call with per_project_k.
            per_project = max(1, k // len(ids)) if ids and len(ids) > 1 else None
            hits, info = R.search(item["question"], project_ids=ids, types=item["expect_section_types"] or None,
                                  scope=item.get("expect_scope"), k=k, per_project_k=per_project)
        else:
            hits, info = R.search(item["question"], k=k)
        rows.append({"id": item["id"], "question": item["question"], "topic": item.get("topic"), **score(item, hits, corpus),
                     "results": [{"rank": i, "project_id": h["project_id"], "phase_project_id": h.get("phase_project_id"), "type": h["type"],
                                  "subtype": h["subtype"], "scope": h["scope"], "path": h["path"], "kind": h["kind"]} for i, h in enumerate(hits, 1)],
                     "expansions": info["expansions"]})
    return rows


def summarize(rows: list[dict]) -> dict:
    recalls = [r["project_recall"] for r in rows if r["project_recall"] is not None]
    return {"questions": len(rows), "pass": sum(r["pass"] for r in rows),
            "project_recall": sum(recalls) / len(recalls) if recalls else None,
            "type_hit": sum(r["type_hit"] for r in rows) / len(rows),
            "joint_hit": sum(r["joint_rank"] is not None for r in rows) / len(rows),
            "mrr": sum(r["rr"] for r in rows) / len(rows)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--modes", default="retrieval,oracle")
    ap.add_argument("--files", default=",".join(str(f.relative_to(ROOT)) for f in DEFAULT_FILES))
    ap.add_argument("--out", default=str(ROOT / "out" / "eval_results.json"))
    args = ap.parse_args()
    files = [ROOT / f if not Path(f).is_absolute() else Path(f) for f in args.files.split(",")]
    items = [i for i in Q.load(*files) if i["expect_section_types"]]
    modes = [m.strip() for m in args.modes.split(",")]
    corpus = corpus_operations()

    results = {m: run(items, args.k, m, corpus) for m in modes}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"k": args.k, "results": results, "summary": {m: summarize(r) for m, r in results.items()}},
                                         ensure_ascii=False, indent=1))

    print(f"Per question (top {args.k}; joint@ = rank of first result with expected project+type):")
    print(f"  {'id':4} {'topic':14}" + "".join(f"  {m[:9]:>9} proj  type" for m in modes))
    for i, item in enumerate(items):
        cells = ""
        for m in modes:
            r = results[m][i]
            rec = "n/a" if r["project_recall"] is None else f"{r['project_recall']:.2f}"
            cells += f"  {('PASS@' + str(r['joint_rank'])) if r['pass'] else 'fail':>9} {rec:>4} {'yes' if r['type_hit'] else 'no':>5}"
        print(f"  {item['id']:4} {(item.get('topic') or ''):14}{cells}")
    print(f"\nSummary (top {args.k}, {len(items)} questions):")
    print(f"  {'mode':10} {'pass':>7} {'project recall':>15} {'type hit':>9} {'joint hit':>10} {'MRR':>6}")
    for m in modes:
        s = summarize(results[m])
        print(f"  {m:10} {s['pass']:>3}/{s['questions']:<3} {s['project_recall']:15.2f} {s['type_hit']:9.2f} {s['joint_hit']:10.2f} {s['mrr']:6.2f}")
    by_topic = defaultdict(list)
    for r in results[modes[0]]:
        by_topic[r["topic"]].append(r)
    print(f"\nBy topic ({modes[0]}): " + "  ".join(f"{t} {sum(x['pass'] for x in rs)}/{len(rs)}" for t, rs in sorted(by_topic.items())))
    print(f"\n  -> {Path(args.out).relative_to(ROOT) if Path(args.out).is_relative_to(ROOT) else args.out}")
    R.driver().close()


if __name__ == "__main__":
    sys.exit(main())
