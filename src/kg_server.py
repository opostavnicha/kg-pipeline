"""MCP server for the World Bank operations knowledge graph (stdio).

Tools: graph_schema, graph_query (read-only Cypher), retrieve (hybrid chunk search), ingest_pdf.
Built on the MCP Python SDK 2.x MCPServer (the class that was FastMCP in SDK 1.x).

Run: python src/kg_server.py   (Claude Code launches it via `claude mcp add`)
"""

import contextlib
import json
import os
import re
import shutil
import sys
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

# Importable without installing the package: add the repo's src/ (this file's directory) to the path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("PYMUPDF_SUGGEST_LAYOUT_ANALYZER", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from kg_pipeline import paths  # noqa: E402
from kg_pipeline import retrieve as R  # noqa: E402

ROW_CAP = 200
QUERY_TIMEOUT_S = 30
MAX_STRING = 4000  # long property values (section text, table rows) are cut in graph_query results
WRITE_CLAUSES = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|FOREACH|LOAD\s+CSV)\b", re.IGNORECASE)
# Admin and write procedures. The read transaction already blocks writes; this rejects them up front.
WRITE_CALLS = re.compile(
    r"\bCALL\s+(dbms\.|db\.create|db\.index\.(fulltext|vector)\.create|db\.clearQueryCaches"
    r"|apoc\.(create|merge|refactor|periodic|do\.|nodes\.delete|atomic|trigger|schema\.assert|cypher\.(runWrite|runSchema|runMany|doIt)))",
    re.IGNORECASE)

INSTRUCTIONS = """\
Knowledge graph of World Bank operation documents (project/program appraisal documents and project papers):
datasheet facts, financing, risk ratings, MPA programs and phases, plus every document section and table,
split into searchable chunks.

SCHEMA (summary; call graph_schema for every property and value)
Labels: Operation {project_id}, Document {hash}, Organization {name_norm}, Location {name_norm, iso2},
  Risk {name}, Program {program_id}, Phase {phase_id}, Acronym {name}, Section {section_id}, Table {table_id},
  Chunk {chunk_id}.
Relationships:
  (Document)-[:DESCRIBES]->(Operation)
  (Organization)-[:FINANCES {amount_usd, orig_currency, orig_amount, detail}]->(Operation)
  (Organization)-[:IMPLEMENTS]->(Operation)
  (Operation)-[:HAS_RISK {rating}]->(Risk)
  (Operation)-[:LOCATED_IN]->(Location)
  (Program)-[:HAS_PHASE {phase}]->(Phase)-[:HAS_OPERATION]->(Operation)   (only phases with a project ID)
  (Document)-[:DEFINES {expansion}]->(Acronym)
  (Document)-[:HAS_SECTION]->(Section)-[:HAS_SUBSECTION*]->(Section)-[:HAS_TABLE|HAS_CHUNK]->(Table|Chunk)
  (Chunk)-[:NEXT_CHUNK]->(Chunk)
Risk names (SORT categories): Political and Governance, Macroeconomic, Sector Strategies and Policies,
  Technical Design of Project or Program, Institutional Capacity for Implementation and Sustainability,
  Fiduciary, Environment and Social, Stakeholders, Other. Ratings: Low, Moderate, Substantial, High.
  The overall rating is Operation.overall_risk (and Program.overall_risk), not a Risk node.
Section types (coarse): overview, context, pdo, design, lessons, implementation, appraisal, risks, results,
  mpa, front_matter, other. Common subtypes: datasheet, development_objective, components, partners,
  lessons_learned, institutional_arrangements, key_risks, fiduciary, environmental_social, results_framework,
  mpa_framework, mpa_phase.

HOW TO ROUTE A QUESTION
1. Facts, numbers, lists, comparisons of ratings/amounts/dates, "which operations ...", "who finances ...",
   "how many ...", MPA phases, acronym definitions -> graph_query (Cypher). Use the schema above; call
   graph_schema when you need a property name or value that isn't listed here.
2. Explanations -- why, how, what lessons, what risks involve, how implementation works, what a component does
   -> retrieve. Pass filters whenever the question names an operation, country or topic: project_ids,
   countries, section_types (coarse types or subtypes above), scope ('main'/'phase').
3. "Which X ... and why" / "... and what do they propose" -> graph_query to find the operations exactly
   (e.g. HAS_RISK {rating:'High'}), then ONE retrieve call with all of their project_ids, per_project_k
   (2-3 is usually enough) and the relevant section_types. That returns results grouped by operation;
   don't make one retrieve call per operation.
4. Acronyms: retrieve expands them automatically; for a definition query
   (:Document)-[:DEFINES {expansion}]->(:Acronym {name}).

KEY FACTS ABOUT THE DATA
- Operation has overall_risk, es_risk_rating, instrument (IPF/PforR/DPF), approval/closing dates (the PDF's
  *expected* dates; *_api fields are from the World Bank Projects API), status/region (+ *_source: 'api' or
  'derived'), pdo_text, total_financing_usd, modalities (e.g. fragile_state, conflict, cerc, mpa).
  stub:true operations are MPA phases known only by their project ID.
- FINANCES amounts are USD units (not millions); orig_* keep the loan currency (EUR, JPY, SDR).
- Program has phases (count), phases_with_project_id, financing_envelope_usd; Phase has phase, label,
  project_id (may be null), instrument, ibrd_usd/ida_usd/other_usd, estimated_approval, es_risk_rating.
- Sections have type/subtype/scope/path; scope 'phase' sections describe another MPA phase (phase_project_id).

ANSWERING
- Cite project_id and the section path (retrieve returns both) for every claim from text.
- Say when the graph or the retrieved text has no evidence rather than inferring.
- graph_query is read-only and returns at most 200 rows; aggregate in Cypher instead of pulling raw rows.
"""

mcp = MCPServer(name="kg-pipeline", instructions=INSTRUCTIONS)


@contextlib.contextmanager
def quiet_stdout():
    """stdout carries the MCP protocol; send any library printing to stderr."""
    with contextlib.redirect_stdout(sys.stderr):
        yield


def session():
    return R.driver().session(database=R.database())


def to_json(v: Any) -> Any:
    from neo4j.graph import Node, Path as GPath, Relationship

    if isinstance(v, Node):
        props = {k: to_json(x) for k, x in v.items() if k != "embedding"}
        return {"_labels": sorted(v.labels), **props}
    if isinstance(v, Relationship):
        return {"_type": v.type, **{k: to_json(x) for k, x in v.items()}}
    if isinstance(v, GPath):
        return {"_path": [to_json(n) for n in v.nodes], "_rels": [to_json(r) for r in v.relationships]}
    if isinstance(v, (list, tuple)):
        if len(v) > 50 and all(isinstance(x, float) for x in v):
            return f"<vector of {len(v)} floats>"
        return [to_json(x) for x in v]
    if isinstance(v, dict):
        return {k: to_json(x) for k, x in v.items()}
    if isinstance(v, str) and len(v) > MAX_STRING:
        return v[:MAX_STRING] + f"… [{len(v) - MAX_STRING} more chars]"
    if hasattr(v, "iso_format"):
        return v.iso_format()
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def strip_literals(cypher: str) -> str:
    """Remove string literals and comments so keywords inside them don't trip the write check."""
    cypher = re.sub(r"//[^\n]*|/\*.*?\*/", " ", cypher, flags=re.DOTALL)
    return re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|`[^`]*`", "''", cypher)


@lru_cache(maxsize=1)
def _schema() -> dict:
    with session() as s:
        labels = {r["l"]: r["n"] for r in s.run("MATCH (n) UNWIND labels(n) AS l RETURN l, count(*) AS n ORDER BY l")}
        props = {}
        for label in labels:
            props[label] = sorted({k for r in s.run(f"MATCH (n:`{label}`) WITH n LIMIT 300 RETURN keys(n) AS ks") for k in r["ks"]})
        patterns = s.run(
            "MATCH (a)-[r]->(b) WITH labels(a)[0] AS a, type(r) AS t, labels(b)[0] AS b, r "
            "WITH a, t, b, count(*) AS n, collect(keys(r))[..50] AS ks "
            "RETURN a, t, b, n, reduce(acc = [], k IN ks | acc + [x IN k WHERE NOT x IN acc]) AS props ORDER BY n DESC").data()
        vocab = {
            "Section.type": [r["v"] for r in s.run("MATCH (s:Section) RETURN DISTINCT s.type AS v ORDER BY v")],
            "Section.subtype": [r["v"] for r in s.run("MATCH (s:Section) WHERE s.subtype IS NOT NULL RETURN DISTINCT s.subtype AS v ORDER BY v")],
            "Section.scope": [r["v"] for r in s.run("MATCH (s:Section) RETURN DISTINCT s.scope AS v ORDER BY v")],
            "Chunk.kind": [r["v"] for r in s.run("MATCH (c:Chunk) RETURN DISTINCT c.kind AS v ORDER BY v")],
            "Risk.name": [r["v"] for r in s.run("MATCH (r:Risk) RETURN r.name AS v ORDER BY v")],
            "HAS_RISK.rating": [r["v"] for r in s.run("MATCH ()-[h:HAS_RISK]->() RETURN DISTINCT h.rating AS v ORDER BY v")],
            "Document.doc_type": [r["v"] for r in s.run("MATCH (d:Document) RETURN DISTINCT d.doc_type AS v ORDER BY v")],
            "Operation.instrument": [r["v"] for r in s.run("MATCH (o:Operation) WHERE o.instrument IS NOT NULL RETURN DISTINCT o.instrument AS v ORDER BY v")],
            "operations": s.run("MATCH (o:Operation) WHERE NOT coalesce(o.stub, false) "
                                "RETURN o.project_id AS project_id, o.name AS name, o.country AS country ORDER BY project_id").data(),
        }
    return {
        "node_labels": {l: {"count": n, "properties": props[l]} for l, n in labels.items()},
        "relationships": [{"pattern": f"(:{p['a']})-[:{p['t']}]->(:{p['b']})", "count": p["n"], "properties": p["props"]} for p in patterns],
        "vocabularies": vocab,
        "indexes": {"chunk_vec": "vector index on Chunk.embedding (768, cosine; BAAI/bge-base-en-v1.5)",
                    "chunk_ft": "full-text index on Chunk.text"},
        "examples": [
            "MATCH (o:Operation)-[r:HAS_RISK]->(:Risk {name:'Fiduciary'}) WHERE r.rating = 'High' RETURN o.project_id, o.name, o.country",
            "MATCH (g:Organization)-[f:FINANCES]->(o:Operation) RETURN o.project_id, g.name, f.detail, f.amount_usd, f.orig_currency, f.orig_amount ORDER BY o.project_id",
            "MATCH (p:Program)-[:HAS_PHASE]->(ph:Phase) OPTIONAL MATCH (ph)-[:HAS_OPERATION]->(o:Operation) "
            "RETURN p.program_id, p.phases, ph.phase, ph.label, ph.instrument, ph.ida_usd, o.project_id ORDER BY ph.phase",
            "MATCH (d:Document)-[x:DEFINES]->(a:Acronym {name:'I3RF'}) MATCH (d)-[:DESCRIBES]->(o) RETURN o.project_id, x.expansion",
        ],
    }


@mcp.tool(annotations=ToolAnnotations(title="Graph schema", read_only_hint=True, idempotent_hint=True, open_world_hint=False))
def graph_schema() -> dict:
    """Labels with property names and counts, relationship patterns with their properties, value vocabularies
    (section types/subtypes, risk names and ratings, instruments, the list of operations) and example queries.
    Call this before writing Cypher for graph_query."""
    with quiet_stdout():
        return _schema()


@mcp.tool(annotations=ToolAnnotations(title="Read-only Cypher", read_only_hint=True, idempotent_hint=True, open_world_hint=False))
def graph_query(cypher: str, params: dict | None = None) -> dict:
    """Run a read-only Cypher query and return up to 200 rows as JSON.

    Write clauses (CREATE, MERGE, DELETE, SET, REMOVE, DROP, FOREACH, LOAD CSV) and admin/write procedures
    (CALL dbms.*, db.create*, apoc writes) are rejected; the query also runs in a read transaction with a 30 s
    timeout. Embedding vectors are omitted and long strings are truncated. Use $params for values.
    """
    stripped = strip_literals(cypher)
    bad = WRITE_CLAUSES.search(stripped) or WRITE_CALLS.search(stripped)
    if bad:
        return {"error": f"rejected: '{bad.group(0).strip()}' is not allowed; graph_query is read-only"}
    from neo4j import unit_of_work

    @unit_of_work(timeout=QUERY_TIMEOUT_S)
    def work(tx):
        result = tx.run(cypher, params or {})
        rows, truncated = [], False
        for record in result:
            if len(rows) == ROW_CAP:
                truncated = True
                break
            rows.append({k: to_json(v) for k, v in record.items()})
        return rows, truncated, result.keys()

    with quiet_stdout():
        try:
            with session() as s:
                rows, truncated, columns = s.execute_read(work)
        except Exception as e:  # surface Cypher errors to the caller instead of failing the tool call
            return {"error": f"{type(e).__name__}: {e}"}
    out = {"columns": list(columns), "rows": rows, "row_count": len(rows)}
    if truncated:
        out["truncated"] = f"more than {ROW_CAP} rows; aggregate or add LIMIT/filters"
    return out


@mcp.tool(annotations=ToolAnnotations(title="Hybrid retrieval", read_only_hint=True, idempotent_hint=True, open_world_hint=False))
def retrieve(question: str, project_ids: list[str] | None = None, countries: list[str] | None = None,
             section_types: list[str] | None = None, scope: str | None = None, k: int | None = None,
             per_project_k: int | None = None, include_front_matter: bool = False) -> dict:
    """Hybrid search over document chunks: vector + full-text, merged by reciprocal rank fusion.

    Filters (all optional, combined with AND): project_ids (e.g. ["P175721"]); countries (names or ISO2 codes);
    section_types (coarse types or subtypes, e.g. ["key_risks", "fiduciary"] or ["lessons"]); scope ('main' or
    'phase'). k is the number of results (default 8, max 30).
    per_project_k (with project_ids): take the top n for EACH project first, so every operation is represented,
    then return them grouped by project (k defaults to per_project_k x projects). Use it for per-operation
    questions instead of one call per operation.
    Acronyms in the question are expanded from the documents' abbreviation lists. Front matter is excluded unless
    include_front_matter is true (abbreviation-list chunks are always searchable).
    Each result has project_id, operation, country, section_path, section type/subtype, chunk kind and text.
    """
    if scope not in (None, "main", "phase"):
        return {"error": "scope must be 'main' or 'phase'"}
    grouped = bool(per_project_k and project_ids)
    if k is None:
        k = min(per_project_k * len(project_ids), 30) if grouped else 8
    k = max(1, min(k, 30))
    with quiet_stdout():
        hits, info = R.search(question, countries, project_ids, section_types, scope, k, include_front_matter,
                              per_project_k=max(1, min(per_project_k, 10)) if grouped else None)
    rows = [{
        "rank": i, "score": round(h["rrf"], 5), "project_id": h["project_id"],
        "phase_project_id": h.get("phase_project_id"), "operation": h["operation"], "country": h["country"],
        "section_path": h["path"], "section_type": h["type"], "section_subtype": h["subtype"], "scope": h["scope"],
        "chunk_kind": h["kind"], "chunk_id": h["chunk_id"],
        "matched_by": [m for m in ("vector", "fulltext") if f"{m}_rank" in h], "text": h["text"],
    } for i, h in enumerate(hits, 1)]
    out = {"acronym_expansions": info["expansions"]}
    if grouped:
        groups: dict[str, dict] = {}
        for h, r in zip(hits, rows):
            g = groups.setdefault(h["group"], {"project_id": h["group"], "operation": h["operation"], "country": h["country"], "results": []})
            g["results"].append(r)
        out["groups"] = [groups[p] for p in project_ids if p in groups]
        out["projects_without_results"] = [p for p in project_ids if p not in groups]
    else:
        out["results"] = rows
    return out


@mcp.tool(annotations=ToolAnnotations(title="Ingest a PDF", read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                      open_world_hint=True))
def ingest_pdf(path: str) -> dict:
    """Add one World Bank operation PDF to the graph: copy it into pdfs/, extract sections and tables, parse the
    datasheet (and query the World Bank Projects API), load the operation, sections and acronyms, then chunk,
    embed and load its chunks. Rerunning on the same PDF updates it in place. Takes about 30-90 seconds.
    Returns the project ID, counts, unmatched headings, validation flags and PDF-vs-API conflicts."""
    src = Path(path).expanduser().resolve()
    if not src.is_file() or src.suffix.lower() != ".pdf":
        return {"error": f"not a PDF file: {src}"}
    with quiet_stdout():
        import pymupdf

        from kg_pipeline import chunk as C
        from kg_pipeline import datasheet as DS
        from kg_pipeline import load as L
        from kg_pipeline import sections as S

        pymupdf.set_messages(fd=2)
        pdfs, out = paths.PDFS, paths.OUT
        dest = pdfs / src.name
        if src != dest:
            shutil.copy2(src, dest)

        subtypes, group_of = S.load_types(paths.SECTION_TYPES)
        core = S.load_core(paths.SECTION_TYPES)
        with pymupdf.open(dest) as doc:
            secs, source = S.split_sections(doc)
            d = S.Doc(dest, S.document_project_id(doc), S.document_type(doc), source, secs)
        S.assign_types(secs, subtypes, group_of)
        S.assign_scope(secs)
        S.validate([d], core)  # single-document validation: size outliers fall back to this document only
        S.write_json(d, out)

        ds = DS.extract(dest, subtypes, group_of, DS.Aliases(DS.ALIASES_PATH), DS.Countries(DS.COUNTRIES_PATH), date.today().isoformat())
        (out / f"{dest.stem}.datasheet.json").write_text(json.dumps(ds, ensure_ascii=False, indent=1))

        plan = L.collect(out, pdfs, only=[dest.stem])
        L.load(plan)
        tokens = C.Tokens()
        chunks, _ = C.build_chunks(plan, tokens)
        C.load_chunks(chunks, C.embed(chunks), plan)
        R.acronym_map.cache_clear()
        _schema.cache_clear()

    op = ds["operation"]
    return {
        "pdf": dest.name, "project_id": op.get("project_id"), "name": op.get("name"), "country": op.get("country"),
        "doc_type": ds["document"]["doc_type"], "sections": len(secs), "tables": sum(len(s.tables) for s in secs),
        "chunks": len(chunks), "unmatched_headings": [s.title_raw for s in secs if s.type == "other"],
        "section_flags": d.flags, "datasheet_flags": ds["flags"], "api_conflicts": ds["conflicts"],
        "notes": plan.notes,
    }


if __name__ == "__main__":
    mcp.run()
