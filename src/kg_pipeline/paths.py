"""Repository-relative paths. Everything resolves from this file's location, never from the working
directory, so the pipeline, the MCP server and the evals run from anywhere."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # src/kg_pipeline/paths.py -> repo root

CONFIG = ROOT / "config"
SECTION_TYPES = CONFIG / "section_types.json"
ALIASES = CONFIG / "aliases.json"
COUNTRIES = CONFIG / "countries.json"
SOURCES = CONFIG / "sources.json"

PDFS = ROOT / "pdfs"  # downloaded by scripts/fetch_pdfs.py (gitignored)
OUT = ROOT / "out"  # extraction output (gitignored)
CACHE = ROOT / "cache"  # API responses and embeddings (gitignored)
ENV = ROOT / ".env"  # Neo4j credentials (gitignored; see .env.example)
EVAL = ROOT / "eval"
SERVER = ROOT / "src" / "kg_server.py"
