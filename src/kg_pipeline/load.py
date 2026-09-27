"""Load sections.py and datasheet.py output into Neo4j.

Reads out/<stem>.json (sections) and out/<stem>.datasheet.json for each PDF in ./pdfs, builds the
graph as a plan of nodes and relationships, then MERGEs it (rerunning is safe). Credentials come from
.env: NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE.

Graph:
  (:Document)-[:DESCRIBES]->(:Operation)
  (:Organization)-[:FINANCES {amount_usd, orig_currency, orig_amount}]->(:Operation)
  (:Organization)-[:IMPLEMENTS]->(:Operation)
  (:Operation)-[:HAS_RISK {rating}]->(:Risk)
  (:Operation)-[:LOCATED_IN]->(:Location)
  (:Program)-[:HAS_PHASE {phase}]->(:Phase)                MPA documents: every phase of the framework table
  (:Phase)-[:HAS_OPERATION]->(:Operation)                  phases with a project ID
  (:Document)-[:HAS_SECTION]->(:Section)                   top-level sections
  (:Section)-[:HAS_SUBSECTION]->(:Section)
  (:Section)-[:HAS_TABLE]->(:Table)
  (:Document)-[:DEFINES {expansion}]->(:Acronym)           the document's abbreviation list

Usage: python -m kg_pipeline.load [--dry-run] [--out out] [--pdfs pdfs]
"""

import argparse
import hashlib
import json
import re
import sys
import unicodedata

from kg_pipeline import glossary, paths
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

CONSTRAINTS = [
    ("Operation", "project_id"),
    ("Program", "program_id"),
    ("Phase", "phase_id"),
    ("Document", "hash"),
    ("Organization", "name_norm"),
    ("Location", "name_norm"),
    ("Risk", "name"),
    ("Acronym", "name"),
    # Not in the core list, but Section/Table are MERGEd too and need a unique key for that to be safe and fast.
    ("Section", "section_id"),
    ("Table", "table_id"),
]
KEY = dict(CONSTRAINTS)
ALIASES_PATH = paths.ALIASES
DATE_FIELDS = {"approval_date", "closing_date", "program_closing_date", "approval_date_api", "closing_date_api", "date"}
BATCH = 500


def name_norm(name: str) -> str:
    """Case/accent/punctuation-insensitive key, e.g. 'Türkiye' -> 'turkiye'."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def neo4j_value(key: str, value):
    """Neo4j properties must be primitives or lists of primitives; dates become Date values."""
    if value is None:
        return None
    if key in DATE_FIELDS and isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list) and any(isinstance(v, (dict, list)) for v in value):
        return json.dumps(value, ensure_ascii=False)
    return value


def global_orgs() -> set[str]:
    """International organizations (aliases.json canonical names): one node across all countries."""
    orgs = json.loads(ALIASES_PATH.read_text())["organizations"]
    return {name_norm(canon) for canon in orgs}


def org_key(name: str, iso2: str | None, global_names: set[str]) -> str:
    """Government bodies are country-specific ("Ministry of Finance" in Somalia vs. elsewhere), so their key
    carries the country code; international organizations keep a global key."""
    norm = name_norm(name)
    return norm if norm in global_names or not iso2 else f"{norm} [{iso2}]"


# Personal contact details (datasheet "Contact/Title/Telephone/Email" rows) are not stored in the graph.
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"\+\d[\d\s-]{6,}\d")
CONTACT_LABEL_RE = re.compile(r"^(Contact|Title|Telephone No\.?|Tel\.?|Email|E-mail)\s*:?$", re.IGNORECASE)


def redact_rows(rows: list[list[str]]) -> list[list[str]]:
    """Drop contact rows: a Contact/Title/Telephone/Email label row, or any row with an email or phone number."""
    out = []
    for row in rows:
        cells = [c for c in row if c]
        if cells and CONTACT_LABEL_RE.match(cells[0].strip()):
            continue
        if any(EMAIL_RE.search(c) or PHONE_RE.search(c) for c in cells):
            continue
        out.append(row)
    return out


def redact_text(text: str) -> str:
    return PHONE_RE.sub("[phone redacted]", EMAIL_RE.sub("[email redacted]", text))


NUMBERING_RE = re.compile(r"^(?:[IVX]+\.|[A-H]\.|ANNEX\b\s*(?:\d+(?:\.\d+)*)?\s*[.:]?)\s*", re.IGNORECASE)
SLUG_MAX = 60


def heading_slug(title: str) -> str:
    """'B. Sectoral and Institutional Context' -> 'sectoral-and-institutional-context' (numbering dropped,
    so renumbered headings keep their IDs)."""
    return name_norm(NUMBERING_RE.sub("", title)).replace(" ", "-")[:SLUG_MAX].strip("-") or "untitled"


def section_ids(secs: list[dict], doc_hash: str) -> dict[int, tuple[str, str]]:
    """order -> (section_id, human path). section_id = {hash}:{slug path}#{n}; n tells apart identical
    paths within a document (in document order), so it is 0 unless two sections share a whole path."""
    by_order = {s["order"]: s for s in secs}
    seen = Counter()
    out = {}
    for s in secs:
        chain, cur = [], s
        while cur is not None:
            chain.append(cur)
            cur = by_order.get(cur["parent"]) if cur["parent"] is not None else None
        chain.reverse()
        slug_path = "/".join(heading_slug(c["title_raw"]) for c in chain)
        human_path = " > ".join(c["title_raw"] for c in chain)
        out[s["order"]] = (f"{doc_hash}:{slug_path}#{seen[slug_path]}", human_path)
        seen[slug_path] += 1
    return out


PDO_SECTION_TITLE = "Development Objective (from datasheet)"


def pdo_section_id(doc_hash: str) -> str:
    return f"{doc_hash}:{heading_slug(PDO_SECTION_TITLE)}#0"


def props(d: dict) -> dict:
    return {k: neo4j_value(k, v) for k, v in d.items() if v is not None}


# --- plan -------------------------------------------------------------------


@dataclass
class Plan:
    # label -> key value -> {"props": ..., "stub": bool}
    nodes: dict[str, dict] = field(default_factory=lambda: defaultdict(dict))
    # (type, start label, end label, rel-key names) -> {(start key, end key, rel-key values): props}
    rels: dict[tuple, dict] = field(default_factory=lambda: defaultdict(dict))
    notes: list[str] = field(default_factory=list)
    global_names: set[str] = field(default_factory=global_orgs)
    # document hash -> section ids loaded now (older sections of that document are removed on load)
    doc_sections: dict[str, set] = field(default_factory=lambda: defaultdict(set))

    def node(self, label: str, key: str, p: dict | None = None, stub: bool = False) -> str:
        entry = self.nodes[label].get(key)
        new = {KEY[label]: key, **props(p or {})}
        if entry is None:
            self.nodes[label][key] = {"props": new, "stub": stub}
        elif entry["stub"] and not stub:  # a full record replaces a stub
            self.nodes[label][key] = {"props": {**entry["props"], **new}, "stub": False}
        elif not stub:
            entry["props"].update(new)
        return key

    def rel(self, rtype: str, a: tuple[str, str], b: tuple[str, str], p: dict | None = None, merge_on: dict | None = None):
        merge_on = merge_on or {}
        k = (rtype, a[0], b[0], tuple(sorted(merge_on)))
        self.rels[k][(a[1], b[1], tuple(merge_on[m] for m in sorted(merge_on)))] = props({**(p or {}), **merge_on})


def build(plan: Plan, pdf: Path, sections_doc: dict, ds: dict) -> None:
    doc_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    op = ds["operation"]
    pid = op["project_id"]

    # Operation
    plan.node("Operation", pid, {
        "name": op.get("name"),
        "country": op.get("country_canonical") or op.get("country"),
        "region": op.get("region"), "region_source": op.get("region_source"),
        "status": op.get("status"), "status_source": op.get("status_source"),
        "instrument": op.get("instrument"), "instrument_text": op.get("instrument_text"),
        "is_cerp": op.get("cerp"), "has_ipf_component": op.get("has_ipf_component"),
        "modalities": op.get("modalities_ticked"),
        "approval_date": op.get("approval_date"), "closing_date": op.get("closing_date"),
        "program_closing_date": op.get("program_closing_date"),
        "approval_date_api": op.get("approval_date_api"), "closing_date_api": op.get("closing_date_api"),
        "overall_risk": op.get("overall_risk"), "es_risk_rating": op.get("es_risk_rating"),
        "practice_area_lead": op.get("practice_area_lead"),
        "practice_areas_contributing": op.get("practice_areas_contributing"),
        "borrower": next((a["name"] for a in ds["agencies"] if a["role"] == "borrower"), None),
        "total_financing_usd": _usd(op.get("financing_totals_usd_millions", {}).get("total_financing")),
        "financing_gap_usd": _usd(op.get("financing_totals_usd_millions", {}).get("financing_gap")),
        "pdo_text": ds.get("pdo_text"),
        "ida_commitment_usd_api": op.get("ida_commitment_usd_api"),
        "ibrd_commitment_usd_api": op.get("ibrd_commitment_usd_api"),
        "stub": False,
    })

    # Document
    plan.node("Document", doc_hash, {
        "filename": pdf.name, "doc_type": ds["document"]["doc_type"],
        "report_number": ds["document"]["report_number"], "date": ds["document"]["date"],
        "headings_from": sections_doc.get("headings_from"),
    })
    plan.rel("DESCRIBES", ("Document", doc_hash), ("Operation", pid))

    # Location (country)
    country = op.get("country_canonical")
    if country:
        loc = plan.node("Location", name_norm(country), {"name": country, "kind": "country", "iso2": op.get("country_iso2"),
                                                          "region": op.get("region_derived")})
        plan.rel("LOCATED_IN", ("Operation", pid), ("Location", loc))
    else:
        plan.notes.append(f"{pdf.name}: no mapped country, no LOCATED_IN")

    iso2 = op.get("country_iso2")

    # Financing
    for f in ds["financing"]:
        source = f["source"] or f["label_raw"]
        if f["source"]:
            org = plan.node("Organization", org_key(source, iso2, plan.global_names),
                            {"name": source, "country_iso2": None if name_norm(source) in plan.global_names else iso2})
        else:  # e.g. "Unguaranteed Commercial Financing": a category shared across operations, not an entity
            org = plan.node("Organization", name_norm(source), {"name": source, "is_category": True})
        if not f["source"]:
            plan.notes.append(f"{pdf.name}: financing row {f['label_raw']!r} has no named source; linked from a category node")
        orig = f["original"]
        plan.rel("FINANCES", ("Organization", org), ("Operation", pid), {
            "amount_usd": f["amount_usd"],
            "orig_currency": orig["currency"],
            "orig_amount": round(orig["amount_millions"] * 1_000_000),
            "orig_from": orig["from"],
            "category": f["category"], "group": f["group"],
        }, merge_on={"detail": f["detail"] or f["group"]})

    # Implementing agencies
    for a in ds["agencies"]:
        if a["role"] == "implementing_agency":
            org = plan.node("Organization", org_key(a["name"], iso2, plan.global_names),
                            {"name": a["name"], "country_iso2": None if name_norm(a["name"]) in plan.global_names else iso2})
            plan.rel("IMPLEMENTS", ("Organization", org), ("Operation", pid))

    # Risks: SORT categories only. The "Overall" rating is Operation.overall_risk (and the MPA-wide one
    # Program.overall_risk), not a Risk node.
    for r in ds["risks"]:
        if r["scope"] != "operation" or r["category"].lower() == "overall":
            continue
        risk = plan.node("Risk", r["category"])
        plan.rel("HAS_RISK", ("Operation", pid), ("Risk", risk), {"rating": r["rating"]})

    # MPA program and phases
    mpa = op.get("mpa")
    if mpa and mpa.get("program_id"):
        if any(ph["project_id"] == mpa["program_id"] for ph in mpa["phases"]):
            plan.notes.append(f"{pdf.name}: MPA program ID {mpa['program_id']} is also phase {next(ph['phase'] for ph in mpa['phases'] if ph['project_id'] == mpa['program_id'])}'s "
                              "project ID; it is both a Program and an Operation node")
        prog = plan.node("Program", mpa["program_id"], {
            "name": mpa.get("program_name"), "program_id_source": mpa.get("program_id_source"),
            "pdo_text": mpa.get("program_pdo_text"),
            "financing_envelope_usd": _usd(mpa.get("financing_envelope_usd_millions")),
            "overall_risk": next((r["rating"] for r in ds["risks"] if r["scope"] == "mpa_program"), None),
            "phases": mpa.get("phase_count", len(mpa["phases"])),
            "phases_with_project_id": sum(1 for ph in mpa["phases"] if ph.get("project_id")),
        })
        # Every phase of the framework table is a Phase node; phases with a project ID link to that Operation
        # (a stub unless it is this document's own operation).
        for ph in mpa["phases"]:
            phase_id = f"{mpa['program_id']}:phase:{ph['phase']}"
            plan.node("Phase", phase_id, {
                "program_id": mpa["program_id"], "phase": ph["phase"], "label": ph.get("label"),
                "project_id": ph.get("project_id"), "instrument": ph.get("instrument"), "sequencing": ph.get("sequencing"),
                "ibrd_usd": _usd(ph.get("ibrd_usd_millions")), "ida_usd": _usd(ph.get("ida_usd_millions")),
                "other_usd": _usd(ph.get("other_usd_millions")), "estimated_approval": ph.get("estimated_approval"),
                "es_risk_rating": ph.get("es_risk_rating"),
            })
            plan.rel("HAS_PHASE", ("Program", prog), ("Phase", phase_id), {"phase": ph["phase"]})
            if ph.get("project_id"):
                if not ph["is_this_operation"]:  # this document's own operation already has its full record
                    plan.node("Operation", ph["project_id"], {"instrument": ph.get("instrument"), "stub": True}, stub=True)
                plan.rel("HAS_OPERATION", ("Phase", phase_id), ("Operation", ph["project_id"]))
    elif mpa:
        plan.notes.append(f"{pdf.name}: MPA document without a program ID, no Program node")

    # Sections and tables
    secs = sections_doc["sections"]
    ids = section_ids(secs, doc_hash)
    for s in secs:
        sid, path = ids[s["order"]]
        plan.doc_sections[doc_hash].add(sid)
        plan.node("Section", sid, {
            "order": s["order"], "level": s["level"], "title": s["title_raw"], "path": path, "page": s["page"],
            "type": s["type"], "subtype": s["subtype"], "inherited": s["inherited"],
            "scope": s["scope"], "phase": s["phase"], "phase_project_id": s["phase_project_id"],
            "text": redact_text(s["text"]), "chars": len(s["text"]), "source": "pdf", "document_hash": doc_hash,
        })
        if s["parent"] is None:
            plan.rel("HAS_SECTION", ("Document", doc_hash), ("Section", sid))
        else:
            plan.rel("HAS_SUBSECTION", ("Section", ids[s["parent"]][0]), ("Section", sid))
        for i, t in enumerate(s["tables"]):
            tid = f"{sid}:t{i}"
            rows = redact_rows(t["rows"])
            plan.node("Table", tid, {
                "page": t["page"], "bbox": list(t["bbox"]), "n_rows": len(rows),
                "n_cols": max((len(r) for r in rows), default=0),
                "rows_json": json.dumps(rows, ensure_ascii=False), "document_hash": doc_hash,
            })
            plan.rel("HAS_TABLE", ("Section", sid), ("Table", tid))

    # Abbreviation list -> (:Document)-[:DEFINES {expansion}]->(:Acronym)
    front = next((s for s in secs if s["type"] == "front_matter"), None)
    pairs = glossary.parse(front) if front else []
    for acr, exp in pairs:
        plan.node("Acronym", acr)
        plan.rel("DEFINES", ("Document", doc_hash), ("Acronym", acr), {"expansion": exp})
    if not pairs:
        plan.notes.append(f"{pdf.name}: no abbreviation list parsed")

    # Synthetic PDO section when the document has none for this operation (e.g. MPA documents).
    own = [s for s in secs if s["scope"] == "main" or s["phase_project_id"] == pid]
    if not any(s["subtype"] == "development_objective" and not s["inherited"] for s in own):
        if ds.get("pdo_text"):
            sid = pdo_section_id(doc_hash)
            plan.doc_sections[doc_hash].add(sid)
            plan.node("Section", sid, {
                "order": -1, "level": 1, "title": PDO_SECTION_TITLE, "path": PDO_SECTION_TITLE,
                "type": "pdo", "subtype": "development_objective", "inherited": False, "scope": "main",
                "text": ds["pdo_text"], "chars": len(ds["pdo_text"]), "source": "datasheet", "document_hash": doc_hash,
            })
            plan.rel("HAS_SECTION", ("Document", doc_hash), ("Section", sid))
            plan.notes.append(f"{pdf.name}: no PDO section; added synthetic Section from the datasheet PDO")
        else:
            plan.notes.append(f"{pdf.name}: no PDO section and no datasheet PDO text")


def _usd(millions):
    return round(millions * 1_000_000) if millions is not None else None


def collect(out_dir: Path, pdf_dir: Path, only: list[str] | None = None) -> Plan:
    """Plan for every PDF with extraction output, or only the given stems."""
    plan = Plan()
    for ds_path in sorted(out_dir.glob("*.datasheet.json")):
        stem = ds_path.name.removesuffix(".datasheet.json")
        if only is not None and stem not in only:
            continue
        sec_path = out_dir / f"{stem}.json"
        pdf = pdf_dir / f"{stem}.pdf"
        if not sec_path.exists() or not pdf.exists():
            plan.notes.append(f"{stem}: skipped (missing {'sections JSON' if not sec_path.exists() else 'PDF'})")
            continue
        build(plan, pdf, json.loads(sec_path.read_text()), json.loads(ds_path.read_text()))
    return plan


# --- output / load ----------------------------------------------------------


def print_counts(plan: Plan) -> None:
    print("Constraints (created first, IF NOT EXISTS):")
    for label, key in CONSTRAINTS:
        print(f"  ({label}) REQUIRE {key} IS UNIQUE")
    print("\nNodes to MERGE:")
    total = 0
    for label in [l for l, _ in CONSTRAINTS]:
        entries = plan.nodes.get(label, {})
        stubs = sum(1 for e in entries.values() if e["stub"])
        total += len(entries)
        print(f"  {label:13} {len(entries):6}" + (f"   ({stubs} phase stubs: ID only)" if stubs else ""))
    print(f"  {'total':13} {total:6}")
    print("\nRelationships to MERGE:")
    by_type = Counter()
    for (rtype, a, b, _), rows in plan.rels.items():
        by_type[(rtype, a, b)] += len(rows)
    for (rtype, a, b), n in sorted(by_type.items(), key=lambda x: -x[1]):
        print(f"  ({a})-[:{rtype}]->({b}){'':{max(0, 44 - len(a) - len(rtype) - len(b))}} {n:6}")
    print(f"  {'total':50} {sum(by_type.values()):6}")
    if plan.notes:
        print("\nNotes:")
        for n in plan.notes:
            print(f"  - {n}")


def load(plan: Plan) -> None:
    from kg_pipeline import db

    database = db.database()
    with db.connect() as driver:
        driver.verify_connectivity()
        with driver.session(database=database) as session:
            for label, key in CONSTRAINTS:
                session.run(f"CREATE CONSTRAINT {label.lower()}_{key} IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE").consume()
            counters = Counter()
            # Schema migration: HAS_PHASE used to point from Program straight to Operation (phases with IDs only).
            counters += _count(session.run("MATCH (:Program)-[r:HAS_PHASE]->(:Operation) DELETE r").consume().counters)
            for label, entries in plan.nodes.items():
                key = KEY[label]
                full = [e["props"] for e in entries.values() if not e["stub"]]
                stubs = [e["props"] for e in entries.values() if e["stub"]]
                # Full records overwrite; stubs only fill a node that doesn't exist yet.
                q_full = f"UNWIND $rows AS r MERGE (n:{label} {{{key}: r.{key}}}) SET n += r"
                q_stub = f"UNWIND $rows AS r MERGE (n:{label} {{{key}: r.{key}}}) ON CREATE SET n += r"
                for q, rows in ((q_full, full), (q_stub, stubs)):
                    for i in range(0, len(rows), BATCH):
                        counters += _count(session.run(q, rows=rows[i : i + BATCH]).consume().counters)
            for (rtype, a, b, merge_keys), rows in plan.rels.items():
                on = ", ".join(f"{m}: r.p.{m}" for m in merge_keys)
                q = (f"UNWIND $rows AS r MATCH (a:{a} {{{KEY[a]}: r.a}}) MATCH (b:{b} {{{KEY[b]}: r.b}}) "
                     f"MERGE (a)-[x:{rtype}{' {' + on + '}' if on else ''}]->(b) SET x += r.p")
                data = [{"a": ka, "b": kb, "p": p} for (ka, kb, _), p in rows.items()]
                for i in range(0, len(data), BATCH):
                    counters += _count(session.run(q, rows=data[i : i + BATCH]).consume().counters)
            # Drop sections (with their tables and chunks) of these documents that are no longer in the
            # extraction output, e.g. after an ID scheme change or a heading-detection change.
            for doc_hash, keep in plan.doc_sections.items():
                res = session.run(
                    "MATCH (s:Section {document_hash: $h}) WHERE NOT s.section_id IN $keep "
                    "OPTIONAL MATCH (s)-[:HAS_TABLE]->(t:Table) OPTIONAL MATCH (s)-[:HAS_CHUNK]->(c:Chunk) "
                    "DETACH DELETE s, t, c",
                    h=doc_hash, keep=list(keep)).consume().counters
                counters += _count(res)
    print("\nDatabase changes:")
    for k, v in sorted(counters.items()):
        if v:
            print(f"  {k:24} {v}")


def _count(c) -> Counter:
    return Counter({k: getattr(c, k) for k in ("nodes_created", "nodes_deleted", "relationships_created",
                                               "relationships_deleted", "properties_set", "constraints_added")})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print node/relationship counts; don't connect to Neo4j")
    ap.add_argument("--out", default=str(paths.OUT), help="directory with sections/datasheet output")
    ap.add_argument("--pdfs", default=str(paths.PDFS), help="directory with the source PDFs (for document hashes)")
    args = ap.parse_args()

    plan = collect(Path(args.out), Path(args.pdfs))
    print_counts(plan)
    if args.dry_run:
        print("\n(dry run: nothing written)")
        return
    load(plan)


if __name__ == "__main__":
    sys.exit(main())
