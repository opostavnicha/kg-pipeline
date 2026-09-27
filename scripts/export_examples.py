"""Write examples/qa-*.md from an end-to-end eval run: the question, each MCP tool call with its full arguments,
a trimmed tool result, and the final answer. Text is scrubbed the same way run_e2e.py scrubs transcripts.

Usage: python scripts/export_examples.py [--run eval/runs/<timestamp>] [--ids e11,e12,e08,e16]
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

import run_e2e  # noqa: E402  (scrubber, tool-name prefix)

EXAMPLES = {  # id -> (file name, what it shows)
    "e11": ("qa-graph-then-retrieve.md", "Graph query to find the operations, then retrieval for the reasons."),
    "e12": ("qa-per-project-k.md", "Several operations in one retrieve call with per_project_k, results grouped by operation."),
    "e08": ("qa-section-type-retrieval.md", "Retrieval filtered to one operation and one section type."),
    "e16": ("qa-not-in-corpus.md", "A question about a country that isn't in the corpus: the answer says so instead of guessing."),
}
NOTES = ROOT / "examples" / "notes.json"  # reviewer notes, keyed by run name and question id
MAX_ROWS = 10
MAX_HITS = 4
EXCERPT = 280


def parse(path: Path) -> tuple[list[dict], str]:
    calls, results, answer = [], {}, ""
    for line in path.read_text().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for b in ev["message"].get("content") or []:
                if b.get("type") == "tool_use":
                    calls.append({"id": b["id"], "name": b["name"], "input": b.get("input", {})})
        elif ev.get("type") == "user":
            for b in ev["message"].get("content") or []:
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    text = c if isinstance(c, str) else "".join(x.get("text", "") for x in (c or []) if isinstance(x, dict))
                    results[b["tool_use_id"]] = text
        elif ev.get("type") == "result":
            answer = ev.get("result") or ""
    for c in calls:
        c["result"] = results.get(c["id"], "")
    return calls, answer


def excerpt(text: str, n: int = EXCERPT) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    return t[:n] + ("…" if len(t) > n else "")


def trim_result(name: str, raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return "```\n" + excerpt(raw, 600) + "\n```"
    if name == "graph_query":
        if "error" in data:
            return f"Error: `{data['error']}`"
        rows = data.get("rows", [])
        cols = data.get("columns") or (list(rows[0]) if rows else [])
        out = [f"{data.get('row_count', len(rows))} row(s)" + (" (truncated)" if data.get("truncated") else "") + ":", "",
               "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        for r in rows[:MAX_ROWS]:
            out.append("| " + " | ".join(excerpt(json.dumps(r.get(c), ensure_ascii=False) if not isinstance(r.get(c), str) else r.get(c), 90).replace("|", "\\|") for c in cols) + " |")
        if len(rows) > MAX_ROWS:
            out.append(f"\n…and {len(rows) - MAX_ROWS} more rows")
        return "\n".join(out)
    if name == "retrieve":
        out = []
        if data.get("acronym_expansions"):
            out.append("Acronyms expanded: " + ", ".join(f"{a} = {'/'.join(e)}" for a, e in data["acronym_expansions"].items()))
        groups = data.get("groups") or [{"project_id": None, "results": data.get("results", [])}]
        for g in groups:
            if g.get("project_id"):
                out.append(f"\n**{g['project_id']} · {g.get('country')}** ({len(g['results'])} results)")
            for h in g["results"][:MAX_HITS]:
                out.append(f"- #{h['rank']} `{h['project_id']}` · {h['section_path']} ({h['section_subtype'] or h['section_type']}, {h['chunk_kind']}) — {excerpt(h['text'])}")
            if len(g["results"]) > MAX_HITS:
                out.append(f"- …{len(g['results']) - MAX_HITS} more")
        if data.get("projects_without_results"):
            out.append(f"\nNo results for: {', '.join(data['projects_without_results'])}")
        return "\n".join(out).strip()
    if name == "graph_schema":
        labels = ", ".join(f"{k} ({v['count']})" for k, v in data.get("node_labels", {}).items())
        return f"Labels: {labels}. {len(data.get('relationships', []))} relationship patterns, value vocabularies and example queries (trimmed)."
    return "```json\n" + excerpt(json.dumps(data, ensure_ascii=False), 600) + "\n```"


def render(qid: str, item: dict, result: dict, calls: list[dict], answer: str, run: Path, note: str | None = None) -> str:
    fname, what = EXAMPLES[qid]
    loaded = [c for c in calls if not c["name"].startswith(run_e2e.TOOL_PREFIX)]
    mcp = [c for c in calls if c["name"].startswith(run_e2e.TOOL_PREFIX)]
    lines = [
        f"# {item['question']}",
        "",
        f"*{what}*",
        "",
        f"Run `{run.name}` · question `{qid}` ({item.get('category')}) · scored **{'PASS' if result.get('pass') else 'FAIL'}** · "
        f"{result.get('seconds', 0):.0f} s · {len(mcp)} MCP tool call(s)",
        "",
        "Test output from this corpus, not a benchmark; see the note in the [README](../README.md).",
        "",
    ]
    if loaded:
        lines += [f"Claude Code first loaded the MCP tool definitions ({', '.join(c['name'] for c in loaded)}).", ""]
    for n, c in enumerate(mcp, 1):
        name = c["name"][len(run_e2e.TOOL_PREFIX):]
        lines.append(f"## {n}. `{name}`")
        lines.append("")
        args = dict(c["input"])
        cypher = args.pop("cypher", None)
        if cypher:
            lines += ["```cypher", cypher.strip(), "```", ""]
        if args:
            lines += ["```json", json.dumps(args, ensure_ascii=False, indent=2), "```", ""]
        if not cypher and not args:
            lines += ["(no arguments)", ""]
        lines += ["**Result (trimmed):**", "", trim_result(name, c["result"]), ""]
    lines += ["## Answer", "", answer.strip(), ""]
    if note:
        lines += ["---", "", f"> **Reviewer's note:** {note}", ""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", help="run directory (default: latest in eval/runs)")
    ap.add_argument("--ids", default=",".join(EXAMPLES))
    args = ap.parse_args()
    run = Path(args.run) if args.run else max((ROOT / "eval" / "runs").iterdir(), key=lambda p: p.name)
    from dotenv import dotenv_values

    scrub = run_e2e.scrubber(dotenv_values(ROOT / ".env"))
    summary = {r["id"]: r for r in json.loads((run / "summary.json").read_text())["results"]}
    import questions as Q

    items = {i["id"]: i for i in Q.load(ROOT / "eval" / "e2e.jsonl")}
    notes = json.loads(NOTES.read_text()).get(run.name, {}) if NOTES.exists() else {}
    for qid in [x.strip() for x in args.ids.split(",")]:
        calls, answer = parse(run / f"{qid}.jsonl")
        text = scrub(render(qid, items[qid], summary.get(qid, {}), calls, answer, run, notes.get(qid)))
        out = ROOT / "examples" / EXAMPLES[qid][0]
        out.write_text(text)
        print(f"{qid} -> {out.relative_to(ROOT)} ({len(text):,} chars, {len(calls)} tool calls)")


if __name__ == "__main__":
    main()
