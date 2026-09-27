"""Running a question through Claude Code with only the kg MCP server, and reading the result.

Shared by eval/run_e2e.py (scored end-to-end eval) and scripts/test_kg.py (one question, readable trace):
preflight checks, the temporary MCP config, the `claude -p ... --output-format stream-json` call, parsing the
stream (tool calls matched to their results, the final answer, cost/turns), and scrubbing personal data and
connection details before anything is written or shown.
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from kg_pipeline import paths

ROOT = paths.ROOT
SERVER = paths.SERVER
ENV_FILE = paths.ENV

SERVER_NAME = "kg"
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"\+\d[\d\s-]{6,}\d")


# --- preflight ---------------------------------------------------------------


def fail(msg: str) -> None:
    sys.exit(f"preflight failed: {msg}")


def preflight() -> dict:
    """Check `claude` is on PATH, .env exists and Neo4j is reachable with Operation nodes; return the .env values."""
    if not shutil.which("claude"):
        fail("`claude` (Claude Code CLI) is not on PATH. Install it: https://docs.claude.com/en/docs/claude-code")
    if not ENV_FILE.exists():
        fail(f"{ENV_FILE.relative_to(ROOT)} not found. Copy .env.example to .env and fill in the Neo4j connection.")
    if not SERVER.exists():
        fail(f"{SERVER.relative_to(ROOT)} not found")
    from dotenv import dotenv_values

    from kg_pipeline import db

    env = dotenv_values(ENV_FILE)
    missing = [k for k in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD") if not env.get(k)]
    if missing:
        fail(f".env is missing {', '.join(missing)}")
    try:
        with db.connect(env) as driver:
            driver.verify_connectivity()
            with driver.session(database=db.database(env)) as s:
                n = s.run("MATCH (o:Operation) RETURN count(o) AS n").single()["n"]
    except Exception as e:
        fail(f"cannot reach Neo4j: {type(e).__name__}: {str(e)[:200]}")
    if n == 0:
        fail("Neo4j has no Operation nodes. Load the graph first (see README: sections -> datasheet -> load -> chunk).")
    return env


# --- scrubbing ---------------------------------------------------------------


def scrubber(env: dict):
    """A function that masks the Neo4j URI/host/instance/password, the home directory, emails and phone numbers."""
    secrets = []
    uri = env.get("NEO4J_URI") or ""
    if uri:
        secrets.append((uri, "<NEO4J_URI>"))
        host = re.sub(r"^[a-z0-9+.-]+://", "", uri, flags=re.IGNORECASE).split("/")[0].split(":")[0]  # neo4j+s://host:port
        if host:
            secrets.append((host, "<NEO4J_HOST>"))
            secrets.append((host.split(".")[0], "<NEO4J_INSTANCE>"))
    if env.get("NEO4J_PASSWORD"):
        secrets.append((env["NEO4J_PASSWORD"], "<NEO4J_PASSWORD>"))
    home = str(Path.home())

    def scrub(text: str) -> str:
        for value, placeholder in secrets:
            if value and len(value) >= 6:
                text = text.replace(value, placeholder)
        text = text.replace(home, "~")
        text = EMAIL_RE.sub("<email>", text)
        return PHONE_RE.sub("<phone>", text)

    return scrub


# --- running and parsing -------------------------------------------------------


def mcp_config() -> Path:
    """A temporary MCP config that starts src/kg_server.py with the current Python."""
    cfg = {"mcpServers": {SERVER_NAME: {"command": sys.executable, "args": [str(SERVER)]}}}
    f = tempfile.NamedTemporaryFile("w", suffix=".json", prefix="kg-mcp-", delete=False)
    json.dump(cfg, f)
    f.close()
    return Path(f.name)


def run_claude(question: str, config: Path, model: str | None, timeout: int, workdir: Path) -> tuple[str, str | None, float]:
    """Run one question; return (raw stream-json stdout, error or None, seconds)."""
    cmd = ["claude", "-p", question, "--mcp-config", str(config), "--strict-mcp-config",
           "--allowedTools", f"{TOOL_PREFIX}*", "--output-format", "stream-json", "--verbose", "--no-session-persistence"]
    if model:
        cmd += ["--model", model]
    start = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=workdir)
        error = None if proc.returncode == 0 else f"claude exited {proc.returncode}: {proc.stderr.strip()[-400:]}"
        return proc.stdout, error, time.time() - start
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        return out, f"timeout after {timeout}s", time.time() - start


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(c.get("text", "") for c in (content or []) if isinstance(c, dict))


def parse_stream(stdout: str) -> dict:
    """Parse Claude Code's stream-json output.

    Returns {"tools": [{"id", "name", "input", "result", "is_error"}, ...] in call order (each tool_use matched to its
    tool_result by id), "answer", "meta" (is_error, subtype, num_turns, duration_ms, total_cost_usd),
    "server_status" (the kg server's status at init)}.
    """
    tools, answer, meta, server_status = [], "", {}, None
    by_id: dict[str, dict] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            server_status = {s.get("name"): s.get("status") for s in ev.get("mcp_servers", [])}.get(SERVER_NAME)
        elif kind == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    call = {"id": block.get("id"), "name": block.get("name", ""), "input": block.get("input", {}),
                            "result": None, "is_error": False}
                    tools.append(call)
                    if call["id"]:
                        by_id[call["id"]] = call
        elif kind == "user":
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") in by_id:
                    call = by_id[block["tool_use_id"]]
                    call["result"] = _result_text(block.get("content"))
                    call["is_error"] = bool(block.get("is_error"))
        elif kind == "result":
            answer = ev.get("result") or ""
            meta = {"is_error": ev.get("is_error"), "subtype": ev.get("subtype"), "num_turns": ev.get("num_turns"),
                    "duration_ms": ev.get("duration_ms"), "total_cost_usd": ev.get("total_cost_usd")}
    return {"tools": tools, "answer": answer, "meta": meta, "server_status": server_status}


def short(name: str) -> str:
    """'mcp__kg__graph_query' -> 'graph_query'; other names unchanged."""
    return name[len(TOOL_PREFIX):] if name.startswith(TOOL_PREFIX) else name


def is_kg_tool(name: str) -> bool:
    return name.startswith(TOOL_PREFIX)
