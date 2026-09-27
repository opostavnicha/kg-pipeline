"""Ask the kg MCP server one question through Claude Code and print a readable trace of the tools Claude chose.

  uv run python scripts/test_kg.py "<question>"
  uv run python scripts/test_kg.py --replay traces/<timestamp>.jsonl       # re-format a saved trace (no API cost)

The report has four parts: QUESTION; TOOL CALLS (each kg tool call with its arguments and a trimmed result,
matched by id; Claude Code's internal calls such as ToolSearch are hidden unless --all); ANSWER; and a summary
line (route, status, turns, time, cost). Each live run's raw stream is scrubbed (emails, phone numbers, Neo4j
URI/host, home directory) and saved to traces/<timestamp>.jsonl, whose first line records the question and model.

Options:
  --full         don't truncate tool results
  --all          also show Claude Code's internal tool calls
  --model X      passed to claude --model
  --timeout N    seconds (default 300)
  --replay FILE  format a saved trace (a traces/ file or an eval/runs/<run>/<id>.jsonl transcript)
  --no-save      don't save the trace
"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kg_pipeline import trace  # noqa: E402

TRACES = ROOT / "traces"
META_TYPE = "kg_trace_meta"
MAX_ROWS = 15
MAX_CELL = 40
SNIPPET = 220
MAX_JSON = 1500


# --- colour -----------------------------------------------------------------


class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def head(self, t): return self._c("1;36", t)  # noqa: E704
    def bold(self, t): return self._c("1", t)  # noqa: E704
    def dim(self, t): return self._c("2", t)  # noqa: E704
    def ok(self, t): return self._c("32", t)  # noqa: E704
    def err(self, t): return self._c("1;31", t)  # noqa: E704
    def tool(self, t): return self._c("1;33", t)  # noqa: E704


# --- formatting ---------------------------------------------------------------


def clip(text: str, n: int | None) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if n is None or len(text) <= n else text[: n - 1] + "…"


def cell(value, full: bool) -> str:
    if value is None:
        return "null"
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return clip(text, None if full else MAX_CELL)


def table(columns: list[str], rows: list[dict], full: bool) -> list[str]:
    shown = rows if full else rows[:MAX_ROWS]
    grid = [[cell(r.get(c), full) for c in columns] for r in shown]
    widths = [max([len(c)] + [len(g[i]) for g in grid]) for i, c in enumerate(columns)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(columns, widths)), "  ".join("─" * w for w in widths)]
    lines += ["  ".join(v.ljust(w) for v, w in zip(g, widths)) for g in grid]
    return lines


def parse_json(text: str | None):
    try:
        return json.loads(text) if text else None
    except json.JSONDecodeError:
        return None


def payload_error(data) -> str | None:
    return data.get("error") if isinstance(data, dict) and data.get("error") else None


def format_graph_query(call: dict, data, full: bool, st: Style) -> list[str]:
    out = [st.dim("cypher:")] + ["  " + line for line in (call["input"].get("cypher") or "").strip().splitlines()]
    if call["input"].get("params"):
        out.append(st.dim("params: ") + json.dumps(call["input"]["params"], ensure_ascii=False))
    if not isinstance(data, dict) or "rows" not in data:
        return out
    rows, cols = data["rows"], data.get("columns") or (list(data["rows"][0]) if data["rows"] else [])
    out.append("")
    if cols:
        out += ["  " + line for line in table(cols, rows, full)]
    hidden = 0 if full else max(0, len(rows) - MAX_ROWS)
    note = f"{data.get('row_count', len(rows))} row(s)"
    if hidden:
        note = f"…{hidden} more rows · " + note
    if data.get("truncated"):
        note += " · server truncated the result: " + data["truncated"]
    out.append(st.dim("  " + note))
    return out


def format_retrieve(call: dict, data, full: bool, st: Style) -> list[str]:
    out = [st.dim("arguments:")] + ["  " + line for line in json.dumps(call["input"], ensure_ascii=False, indent=2).splitlines()]
    if not isinstance(data, dict):
        return out
    if data.get("acronym_expansions"):
        out.append(st.dim("acronyms expanded: ") + ", ".join(f"{a} = {' / '.join(e)}" for a, e in data["acronym_expansions"].items()))
    groups = data.get("groups")
    hits = [h for g in groups for h in g["results"]] if groups else data.get("results", [])
    out.append("")
    group = None
    for h in hits:
        if groups and h["project_id"] != group:
            group = h["project_id"]
            out.append(st.bold(f"  ── {group} · {h.get('country')}"))
        out.append(f"  #{h['rank']} {st.bold(h['project_id'])} · {h.get('country')} · {h.get('chunk_kind')}  {h.get('section_path')}")
        out.append("     " + st.dim(clip(h.get("text", ""), None if full else SNIPPET)))
    if data.get("projects_without_results"):
        out.append(st.dim(f"  no results for: {', '.join(data['projects_without_results'])}"))
    out.append(st.dim(f"  {len(hits)} hit(s)"))
    return out


def format_other(call: dict, data, raw: str | None, full: bool, st: Style) -> list[str]:
    out = []
    if call["input"]:
        out += [st.dim("arguments:")] + ["  " + line for line in json.dumps(call["input"], ensure_ascii=False, indent=2).splitlines()]
    body = json.dumps(data, ensure_ascii=False, indent=2) if data is not None else (raw or "")
    if not full and len(body) > MAX_JSON:
        body = body[:MAX_JSON] + f"\n… ({len(body) - MAX_JSON:,} more characters; --full to show)"
    if body:
        out += [st.dim("result:")] + ["  " + line for line in body.splitlines()]
    return out


def route(calls: list[dict]) -> str:
    names = [trace.short(c["name"]) for c in calls]
    parts: list[list] = []
    for n in names:
        if parts and parts[-1][0] == n:
            parts[-1][1] += 1
        else:
            parts.append([n, 1])
    return " → ".join(f"{n} ×{k}" if k > 1 else n for n, k in parts) or "(no kg tools)"


def report(question: str | None, model: str | None, parsed: dict, error: str | None, seconds: float | None,
           full: bool, show_all: bool, st: Style, source: str | None = None) -> str:
    calls = parsed["tools"]
    kg_calls = [c for c in calls if trace.is_kg_tool(c["name"])]
    shown = calls if show_all else kg_calls
    hidden = [c for c in calls if not trace.is_kg_tool(c["name"])] if not show_all else []
    lines = [st.head("━━ QUESTION " + "━" * 60), question or st.dim("(question not recorded in this trace)")]
    if source:
        lines.append(st.dim(f"replayed from {source}"))
    lines += ["", st.head("━━ TOOL CALLS " + "━" * 58)]
    if not kg_calls:
        lines.append(st.err("No kg tool was called."))
    for n, c in enumerate(shown, 1):
        name = trace.short(c["name"])
        data = parse_json(c["result"])
        err = c["is_error"] or bool(payload_error(data))
        mark = st.err("  ✗ ERROR") if err else ""
        internal = "" if trace.is_kg_tool(c["name"]) else st.dim("  (Claude Code internal)")
        lines.append("")
        lines.append(st.tool(f"{n}. {name}") + internal + mark)
        if c["result"] is None:
            lines.append(st.err("   no result received (the run may have been cut off)"))
        if err:
            lines.append(st.err("   " + clip(payload_error(data) or c["result"] or "", None if full else 400)))
        if name == "graph_query" and trace.is_kg_tool(c["name"]):
            body = format_graph_query(c, data, full, st)
        elif name == "retrieve" and trace.is_kg_tool(c["name"]):
            body = format_retrieve(c, data, full, st)
        else:
            body = format_other(c, data, c["result"], full, st)
        lines += ["   " + line if line else "" for line in body]
    if hidden:
        names = sorted({trace.short(c["name"]) for c in hidden})
        lines += ["", st.dim(f"({len(hidden)} internal Claude Code tool call(s) hidden: {', '.join(names)}; --all to show)")]
    lines += ["", st.head("━━ ANSWER " + "━" * 62), parsed["answer"].strip() or st.err("(no final answer)")]

    meta = parsed["meta"]
    problems = [p for p in (error,
                            f"kg server {parsed['server_status']}" if parsed["server_status"] not in (None, "connected") else None,
                            "result marked as error" if meta.get("is_error") else None,
                            "no final answer" if not parsed["answer"].strip() and not error else None) if p]
    status = st.err("✗ " + "; ".join(problems)) if problems else st.ok("✓ ok")
    secs = meta.get("duration_ms") / 1000 if meta.get("duration_ms") else seconds
    cost = meta.get("total_cost_usd")
    summary = (f"route: {route(kg_calls)}  ·  status: {status}  ·  turns: {meta.get('num_turns', '—')}  ·  "
               f"time: {f'{secs:.0f}s' if secs is not None else '—'}  ·  cost: {f'${cost:.3f}' if cost is not None else '—'}"
               + (f"  ·  model: {model}" if model else ""))
    lines += ["", st.head("━━ SUMMARY " + "━" * 61), summary]
    return "\n".join(lines)


# --- replay -------------------------------------------------------------------


def read_trace(path: Path) -> tuple[str, str | None, str | None]:
    """(raw stream, question, model). traces/ files start with a meta line; eval/runs transcripts don't, so their
    question is looked up in the eval question files by the transcript's id (e.g. e11.jsonl -> e11)."""
    raw = path.read_text()
    question = model = None
    first = raw.splitlines()[0] if raw else ""
    meta = parse_json(first)
    if isinstance(meta, dict) and meta.get("type") == META_TYPE:
        question, model = meta.get("question"), meta.get("model")
    else:
        for f in (ROOT / "eval" / "e2e.jsonl", ROOT / "eval" / "retrieval.jsonl"):
            if f.exists():
                for line in f.read_text().splitlines():
                    item = parse_json(line)
                    if isinstance(item, dict) and item.get("id") == path.stem:
                        question = item["question"]
    return raw, question, model


# --- main -----------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("question", nargs="?")
    ap.add_argument("--full", action="store_true", help="don't truncate tool results")
    ap.add_argument("--all", action="store_true", help="also show Claude Code's internal tool calls")
    ap.add_argument("--model", help="passed to claude --model")
    ap.add_argument("--timeout", type=int, default=300, help="seconds (default 300)")
    ap.add_argument("--replay", metavar="FILE", help="format a saved trace without calling Claude")
    ap.add_argument("--no-save", action="store_true", help="don't save the trace")
    args = ap.parse_args()
    st = Style(sys.stdout.isatty() and not os.environ.get("NO_COLOR"))

    if args.replay:
        path = Path(args.replay)
        if not path.exists():
            sys.exit(f"no such trace: {path}")
        raw, question, model = read_trace(path)
        print(report(question, model, trace.parse_stream(raw), None, None, args.full, args.all, st, source=str(path)))
        return 0

    if not args.question:
        ap.error("a question is required (or use --replay FILE)")
    env = trace.preflight()
    scrub = trace.scrubber(env)
    config = trace.mcp_config()
    workdir = Path(tempfile.mkdtemp(prefix="kg-test-"))  # neutral cwd: no project CLAUDE.md or settings
    print(st.dim(f"asking Claude Code with only the kg MCP server (timeout {args.timeout}s)…"), file=sys.stderr, flush=True)
    try:
        raw, error, seconds = trace.run_claude(args.question, config, args.model, args.timeout, workdir)
    finally:
        config.unlink(missing_ok=True)
        shutil.rmtree(workdir, ignore_errors=True)
    raw = scrub(raw)
    if not args.no_save:
        TRACES.mkdir(exist_ok=True)
        path = TRACES / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
        meta = {"type": META_TYPE, "question": scrub(args.question), "model": args.model,
                "started": datetime.now().isoformat(timespec="seconds"), "error": error}
        path.write_text(json.dumps(meta, ensure_ascii=False) + "\n" + raw)
    print(report(scrub(args.question), args.model, trace.parse_stream(raw), error, seconds, args.full, args.all, st))
    if not args.no_save:
        print(st.dim(f"trace saved: {path.relative_to(ROOT)}  (replay: uv run python scripts/test_kg.py --replay {path.relative_to(ROOT)})"))
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main())
