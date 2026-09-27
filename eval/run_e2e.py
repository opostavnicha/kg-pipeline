"""End-to-end eval: ask Claude Code each question with only the kg MCP server available, then score the
tools it called and its final answer against eval/e2e.jsonl.

For each question this runs
  claude -p <question> --mcp-config <tmp> --strict-mcp-config --allowedTools "mcp__kg__*"
         --output-format stream-json --verbose [--model M]
and parses the tool calls and the final answer from the stream.

Scores per question:
  route_ok           every tool in expect_route was called (a list entry means any one of them)
  ids_found          every expect_ids project ID appears in the answer
  facts_found        every expect_facts entry appears in the answer
  must_not_violated  some must_not pattern appears in the answer (a made-up answer)
  pass               all of the above ok, and the run finished without error or timeout

Writes <out-dir>/<id>.jsonl (raw stream, scrubbed), summary.json and results.md.
Emails, phone numbers, the Neo4j URI/host and the home directory are scrubbed from everything written.

Usage: python eval/run_e2e.py [--only e03,e15] [--category edge] [--model sonnet] [--out-dir DIR]
                              [--timeout 420] [--jobs 1] [--file eval/e2e.jsonl]
"""

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "src"))

import questions as Q  # noqa: E402
# Shared with scripts/test_kg.py: preflight, MCP config, the claude call, stream parsing, scrubbing.
from kg_pipeline.trace import mcp_config, parse_stream, preflight, run_claude, scrubber, short  # noqa: E402


def score(item: dict, parsed: dict, error: str | None) -> dict:
    called = {short(t["name"]) for t in parsed["tools"]}
    answer = parsed["answer"]
    missing_route = [Q.label(e) for e in item["expect_route"] if not any(x in called for x in (e if isinstance(e, list) else [e]))]
    missing_ids = [i for i in item["expect_ids"] if i.lower() not in answer.lower()]
    missing_facts = [Q.label(e) for e in item["expect_facts"] if not Q.any_of(answer, e)]
    violations = [Q.label(e) for e in item["must_not"] if Q.any_of(answer, e)]
    errors = [e for e in (error,
                          "no final answer" if not answer and not error else None,
                          f"kg server status: {parsed['server_status']}" if parsed["server_status"] not in (None, "connected") else None,
                          "result marked as error" if parsed["meta"].get("is_error") else None) if e]
    s = {"route_ok": not missing_route, "ids_found": not missing_ids, "facts_found": not missing_facts,
         "must_not_violated": bool(violations), "error": "; ".join(errors) or None}
    s["pass"] = s["route_ok"] and s["ids_found"] and s["facts_found"] and not s["must_not_violated"] and not s["error"]
    reasons = ([f"route: missing {', '.join(missing_route)}"] if missing_route else []) + \
              ([f"ids: missing {', '.join(missing_ids)}"] if missing_ids else []) + \
              ([f"facts: missing {'; '.join(missing_facts)}"] if missing_facts else []) + \
              ([f"must_not: matched {'; '.join(violations)}"] if violations else []) + errors
    s["reasons"] = reasons
    return s


def evaluate(item: dict, config: Path, args, scrub, out_dir: Path, workdir: Path) -> dict:
    stdout, error, seconds = run_claude(item["question"], config, args.model, args.timeout, workdir)
    (out_dir / f"{item['id']}.jsonl").write_text(scrub(stdout))
    parsed = parse_stream(stdout)
    result = {
        "id": item["id"], "category": item.get("category"), "question": item["question"],
        **score(item, parsed, error),
        "tools_called": [{"name": short(t["name"]), "input": t["input"]} for t in parsed["tools"]],
        "answer": parsed["answer"], "seconds": round(seconds, 1),
        "cost_usd": parsed["meta"].get("total_cost_usd"), "num_turns": parsed["meta"].get("num_turns"),
    }
    print(f"  {item['id']}  {'PASS' if result['pass'] else 'fail'}  {seconds:5.0f}s  tools={[t['name'] for t in result['tools_called']]}"
          + (f"  ({'; '.join(result['reasons'])[:160]})" if result["reasons"] else ""), flush=True)
    return json.loads(scrub(json.dumps(result, ensure_ascii=False)))


# --- report ------------------------------------------------------------------


def yn(v: bool) -> str:
    return "✓" if v else "✗"


def results_md(results: list[dict], meta: dict) -> str:
    passed = sum(r["pass"] for r in results)
    cost = sum(r["cost_usd"] or 0 for r in results)
    lines = [
        f"# End-to-end eval: {passed}/{len(results)} passed",
        "",
        f"Run {meta['started']} · model: {meta['model'] or 'CLI default'} · {meta['seconds']:.0f}s wall clock · "
        f"reported cost ${cost:.2f} · questions: {meta['file']}",
        "",
        "| id | category | pass | route | ids | facts | must_not | tools called | time | cost |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        tools = ", ".join(t["name"] for t in r["tools_called"]) or "—"
        lines.append(f"| {r['id']} | {r['category']} | **{'PASS' if r['pass'] else 'FAIL'}** | {yn(r['route_ok'])} | {yn(r['ids_found'])} | "
                     f"{yn(r['facts_found'])} | {'✗ violated' if r['must_not_violated'] else '✓'} | {tools} | {r['seconds']:.0f}s | "
                     f"{'$%.3f' % r['cost_usd'] if r['cost_usd'] is not None else '—'} |")
    by_cat = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r["pass"])
    lines += ["", "By category: " + " · ".join(f"{c} {sum(v)}/{len(v)}" for c, v in by_cat.items()), "", "## Per question", ""]
    for r in results:
        lines.append(f"### {r['id']} — {'PASS' if r['pass'] else 'FAIL'} ({r['category']})")
        lines.append(f"**Q:** {r['question']}")
        lines.append("")
        if r["tools_called"]:
            lines.append("Tools called:")
            for t in r["tools_called"]:
                arg = json.dumps(t["input"], ensure_ascii=False)
                lines.append(f"- `{t['name']}` {arg[:300] + ('…' if len(arg) > 300 else '')}")
        else:
            lines.append("Tools called: none")
        lines.append("")
        lines.append("Result: " + ("all checks passed" if r["pass"] else "; ".join(r["reasons"])))
        excerpt = re.sub(r"\s+", " ", r["answer"]).strip()
        lines.append("")
        lines.append(f"> {excerpt[:600] + ('…' if len(excerpt) > 600 else '')}" if excerpt else "> (no answer)")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--file", default=str(ROOT / "eval" / "e2e.jsonl"))
    ap.add_argument("--only", help="comma-separated question ids, e.g. e03,e15")
    ap.add_argument("--category", help="graph | retrieve | both | edge (comma-separated)")
    ap.add_argument("--model", help="passed to claude --model")
    ap.add_argument("--out-dir", help="default eval/runs/<timestamp>/")
    ap.add_argument("--timeout", type=int, default=420, help="seconds per question")
    ap.add_argument("--jobs", type=int, default=1, help="questions run in parallel")
    args = ap.parse_args()

    env = preflight()
    items = Q.load(Path(args.file))
    if args.only:
        wanted = {x.strip() for x in args.only.split(",")}
        items = [i for i in items if i["id"] in wanted]
    if args.category:
        cats = {x.strip() for x in args.category.split(",")}
        items = [i for i in items if i.get("category") in cats]
    if not items:
        sys.exit("no questions selected")

    started = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else ROOT / "eval" / "runs" / started
    out_dir.mkdir(parents=True, exist_ok=True)
    scrub = scrubber(env)
    config = mcp_config()
    workdir = Path(tempfile.mkdtemp(prefix="kg-e2e-"))  # neutral cwd: no project CLAUDE.md or settings
    print(f"{len(items)} questions, {args.jobs} at a time, timeout {args.timeout}s -> {out_dir}")
    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            results = list(pool.map(lambda it: evaluate(it, config, args, scrub, out_dir, workdir), items))
    finally:
        config.unlink(missing_ok=True)
        shutil.rmtree(workdir, ignore_errors=True)
    meta = {"started": started, "model": args.model, "seconds": time.time() - t0,
            "file": str(Path(args.file).resolve().relative_to(ROOT)) if Path(args.file).resolve().is_relative_to(ROOT) else args.file}
    summary = {
        **meta, "questions": len(results), "passed": sum(r["pass"] for r in results),
        "totals": {k: sum(bool(r[k]) for r in results) for k in ("route_ok", "ids_found", "facts_found", "must_not_violated")},
        "cost_usd": round(sum(r["cost_usd"] or 0 for r in results), 4), "results": results,
    }
    (out_dir / "summary.json").write_text(scrub(json.dumps(summary, ensure_ascii=False, indent=1)))
    (out_dir / "results.md").write_text(scrub(results_md(results, meta)))
    print(f"\n{summary['passed']}/{len(results)} passed · cost ${summary['cost_usd']:.2f} · {meta['seconds']:.0f}s")
    rel = out_dir.resolve().relative_to(ROOT) if out_dir.resolve().is_relative_to(ROOT) else out_dir
    print(f"  -> {rel}/results.md")


if __name__ == "__main__":
    main()
