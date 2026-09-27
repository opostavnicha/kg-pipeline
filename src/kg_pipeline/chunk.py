"""Chunk every Section's own text and tables, embed the chunks, and load them into Neo4j.

- Text: a section's own text only (never its subsections'), split into ~CHUNK_TOKENS-token windows with
  OVERLAP_TOKENS of overlap, ending at a sentence boundary when one is near. Chunks never cross sections.
- Tables: rendered as markdown and split by rows, repeating the header row in every piece.
- Glossary: the front matter's abbreviation list (parsed by glossary.py, the same pairs load.py stores as
  (:Document)-[:DEFINES]->(:Acronym)) becomes kind 'glossary' chunks, "ACR: expansion" per line.
- Cleaning: template markers (e.g. "@#&OPS~Doctype~OPS^dynamics@padrisk#doctemplate", "RESULT_FRAME_TBL_IO")
  and printed table-of-contents lines (dot leaders) are removed from chunk text and table cells.
- Each chunk keeps its raw `text`; `embed_text` prepends "country | project_id | section path".
- Embeddings: BAAI/bge-base-en-v1.5 (768-d, normalized), cached in cache/embeddings/ by embed_text hash.
- Graph: (:Section)-[:HAS_CHUNK]->(:Chunk)-[:NEXT_CHUNK]->(:Chunk); vector index chunk_vec (768, cosine)
  and full-text index chunk_ft on Chunk.text. Chunk IDs: {section_id}:c{n} for text and
  {table_id}:c{n} for tables (table_id = {section_id}:t{i}), so they follow from section ID and position.

Section IDs, paths and operation data come from load.py's plan, so run load.py first.

Usage: python -m kg_pipeline.chunk [--dry-run] [--out out] [--pdfs pdfs]
"""

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from kg_pipeline import load as L
from kg_pipeline import paths

MODEL = "BAAI/bge-base-en-v1.5"
DIM = 768
CHUNK_TOKENS = 400
OVERLAP_TOKENS = 50
SENTENCE_SNAP_TOKENS = 100  # end a chunk at a sentence boundary if one lies within this many tokens of the limit
MODEL_MAX_TOKENS = 512
MAX_HEADER_TOKENS = 100  # a longer first row is prose, not a header: it isn't repeated
EMBED_CACHE = paths.CACHE / "embeddings" / f"{MODEL.split('/')[-1]}.npz"
SENTENCE_END_RE = re.compile(r"[.;:!?][\"”')\]]*$")
# Document-template artifacts: the OPS doctemplate markers, and the older template's placeholders.
TEMPLATE_RE = re.compile(
    r"@#&OPS~Doctype~OPS\^dynamics\s*@\s*\w+\s*#\s*doctemplate"
    r"|\bRESULT[_ ]FRAME[_ ]TBL[_ ]\w+|\bBASIC[_ ]INFO[_ ]TABLE\b|\b(?:SUMMARY|DETAILS)\s*-\s*NewFin\w*|\b(?:PDO )?Table SPACE\b"
)
TOC_LINE_RE = re.compile(r"\.{4,}|(?:\. ){3,}")
BATCH = 200


@dataclass
class Chunk:
    chunk_id: str
    section_id: str
    order: int
    kind: str  # "text" | "table"
    text: str
    embed_text: str
    tokens: int
    embed_tokens: int
    table_id: str | None = None
    page: int | None = None


class Tokens:
    """Token counts with the embedding model's tokenizer (BERT WordPiece splits on whitespace first,
    so per-word counts add up to the count for the joined text)."""

    def __init__(self):
        from transformers import AutoTokenizer

        self.tok = AutoTokenizer.from_pretrained(MODEL)
        self.cache: dict[str, int] = {}

    def word(self, w: str) -> int:
        n = self.cache.get(w)
        if n is None:
            n = self.cache[w] = len(self.tok.tokenize(w))
        return n

    def text(self, t: str) -> int:
        return sum(self.word(w) for w in t.split())


# --- chunking ---------------------------------------------------------------


def split_text(text: str, tokens: Tokens) -> list[str]:
    words = re.sub(r"\s+", " ", text).strip().split(" ") if text.strip() else []
    if not words:
        return []
    counts = [tokens.word(w) for w in words]
    pieces = []
    start = 0
    while start < len(words):
        total, end = 0, start
        while end < len(words) and (total + counts[end] <= CHUNK_TOKENS or end == start):
            total += counts[end]
            end += 1
        if end < len(words):
            # Prefer to end at a sentence boundary near the limit.
            back, j = 0, end
            while j > start + 1 and back <= SENTENCE_SNAP_TOKENS:
                if SENTENCE_END_RE.search(words[j - 1]):
                    end = j
                    break
                j -= 1
                back += counts[j]
        pieces.append(" ".join(words[start:end]))
        if end >= len(words):
            break
        # Next chunk starts OVERLAP_TOKENS back from this chunk's end (always moving forward).
        back, nxt = 0, end
        while nxt > start + 1 and back + counts[nxt - 1] <= OVERLAP_TOKENS:
            nxt -= 1
            back += counts[nxt]
        start = max(nxt, start + 1)
    return pieces


def md_row(cells: list[str]) -> str:
    return "| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |"


def split_table(rows: list[list[str]], tokens: Tokens) -> list[str]:
    """Markdown pieces of a table, split by rows, each repeating the header row."""
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    keep = [j for j in range(max(len(r) for r in rows)) if any(j < len(r) and r[j] for r in rows)]
    rows = [[r[j] if j < len(r) else "" for j in keep] for r in rows]
    header = md_row(rows[0]) + "\n" + md_row(["---"] * len(keep))
    body = rows[1:]
    if tokens.text(header) > MAX_HEADER_TOKENS:
        header = md_row([f"col {j + 1}" for j in range(len(keep))]) + "\n" + md_row(["---"] * len(keep))
        body = rows
    budget = CHUNK_TOKENS - tokens.text(header)
    pieces, current, used = [], [], 0
    for r in body:
        line = md_row(r)
        n = tokens.text(line)
        if n > budget:  # one oversized row: cut it by words, each part under the repeated header
            if current:
                pieces.append(current)
                current, used = [], 0
            for part in split_long_line(line, budget, tokens):
                pieces.append([part])
            continue
        if used + n > budget and current:
            pieces.append(current)
            current, used = [], 0
        current.append(line)
        used += n
    if current or not pieces:
        pieces.append(current)
    return [header + ("\n" + "\n".join(p) if p else "") for p in pieces]


def split_long_line(line: str, budget: int, tokens: Tokens) -> list[str]:
    parts, current, used = [], [], 0
    for w in line.split():
        n = tokens.word(w)
        if used + n > budget and current:
            parts.append(" ".join(current))
            current, used = [], 0
        current.append(w)
        used += n
    if current:
        parts.append(" ".join(current))
    return parts


def clean_text(text: str) -> str:
    """Drop template markers and printed-TOC lines (dot leaders), line by line."""
    lines = (L.redact_text(TEMPLATE_RE.sub("", line)).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line and not TOC_LINE_RE.search(line))


def clean_rows(rows: list[list[str]]) -> list[list[str]]:
    return [[re.sub(r"\s+", " ", TEMPLATE_RE.sub("", c or "")).strip() for c in row] for row in rows]


def split_lines(lines: list[str], tokens: Tokens) -> list[str]:
    """Pack whole lines into pieces of at most CHUNK_TOKENS (used for glossaries)."""
    pieces, current, used = [], [], 0
    for line in lines:
        n = tokens.text(line)
        if used + n > CHUNK_TOKENS and current:
            pieces.append("\n".join(current))
            current, used = [], 0
        current.append(line)
        used += n
    if current:
        pieces.append("\n".join(current))
    return pieces


def build_chunks(plan: L.Plan, tokens: Tokens) -> tuple[list[Chunk], dict]:
    ops = {k: e["props"] for k, e in plan.nodes["Operation"].items()}
    doc_op = {a: b for (a, b, _), _ in _rels(plan, "DESCRIBES").items()}
    tables_of = defaultdict(list)
    for (sid, tid, _), _ in _rels(plan, "HAS_TABLE").items():
        tables_of[sid].append(tid)
    section_doc = {}
    for (doc, sid, _), _ in _rels(plan, "HAS_SECTION").items():
        section_doc[sid] = doc
    sections = {k: e["props"] for k, e in plan.nodes["Section"].items()}
    for k, p in sections.items():
        section_doc.setdefault(k, p["document_hash"])

    chunks: list[Chunk] = []
    meta = {}
    for sid, s in sections.items():
        pid = doc_op[s["document_hash"]]
        op = ops[pid]
        # Sections inside an MPA phase annex describe that phase's project.
        project = s.get("phase_project_id") or pid
        head = f"{op.get('country') or '?'} | {project} | {s['path']}"
        meta[sid] = {"project_id": pid, "country": op.get("country")}
        order = 0
        for n, piece in enumerate(split_text(clean_text(s.get("text") or ""), tokens)):
            chunks.append(_chunk(f"{sid}:c{n}", sid, order, "text", piece, head, tokens))
            order += 1
        for tid in sorted(tables_of[sid], key=lambda t: int(t.rsplit(":t", 1)[1])):
            t = plan.nodes["Table"][tid]["props"]
            rows = clean_rows(json.loads(t["rows_json"]))
            for n, piece in enumerate(split_table(rows, tokens)):
                c = _chunk(f"{tid}:c{n}", sid, order, "table", piece, head, tokens)
                c.table_id, c.page = tid, t.get("page")
                chunks.append(c)
                order += 1
        if s["type"] == "front_matter":
            defs = sorted((acr, p["expansion"]) for (doc, acr, _), p in _rels(plan, "DEFINES").items() if doc == s["document_hash"])
            gloss_head = f"{op.get('country') or '?'} | {pid} | Abbreviations and acronyms"
            for n, piece in enumerate(split_lines([f"{a}: {e}" for a, e in defs], tokens)):
                chunks.append(_chunk(f"{sid}:g{n}", sid, order, "glossary", piece, gloss_head, tokens))
                order += 1
    return chunks, meta


def _chunk(cid, sid, order, kind, text, head, tokens: Tokens) -> Chunk:
    embed = f"{head}\n{text}"
    return Chunk(cid, sid, order, kind, text, embed, tokens.text(text), tokens.text(embed) + 2)  # +2: [CLS]/[SEP]


def _rels(plan: L.Plan, rtype: str) -> dict:
    out = {}
    for (t, _, _, _), rows in plan.rels.items():
        if t == rtype:
            out.update(rows)
    return out


# --- embeddings -------------------------------------------------------------


def embed(chunks: list[Chunk]) -> dict[str, np.ndarray]:
    cache: dict[str, np.ndarray] = {}
    if EMBED_CACHE.exists():
        with np.load(EMBED_CACHE) as z:
            cache = {k: z[k] for k in z.files}
    key = {c.chunk_id: hashlib.sha1(c.embed_text.encode()).hexdigest() for c in chunks}
    todo = [c for c in chunks if key[c.chunk_id] not in cache]
    if todo:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(MODEL)
        vecs = model.encode([c.embed_text for c in todo], batch_size=32, normalize_embeddings=True, show_progress_bar=True)
        for c, v in zip(todo, vecs):
            cache[key[c.chunk_id]] = v.astype(np.float32)
        EMBED_CACHE.parent.mkdir(parents=True, exist_ok=True)
        live = set(key.values())
        np.savez(EMBED_CACHE, **{k: v for k, v in cache.items() if k in live})
    print(f"embeddings: {len(chunks) - len(todo)} cached, {len(todo)} computed")
    return {c.chunk_id: cache[key[c.chunk_id]] for c in chunks}


# --- Neo4j ------------------------------------------------------------------


def load_chunks(chunks: list[Chunk], vectors: dict[str, np.ndarray], plan: L.Plan) -> None:
    from dotenv import load_dotenv
    from neo4j import GraphDatabase

    load_dotenv(paths.ENV)
    with GraphDatabase.driver(os.environ["NEO4J_URI"], auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"])) as driver:
        with driver.session(database=os.environ.get("NEO4J_DATABASE") or None) as session:
            session.run("CREATE CONSTRAINT chunk_chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE").consume()
            session.run(
                "CREATE VECTOR INDEX chunk_vec IF NOT EXISTS FOR (c:Chunk) ON (c.embedding) "
                f"OPTIONS {{indexConfig: {{`vector.dimensions`: {DIM}, `vector.similarity_function`: 'cosine'}}}}"
            ).consume()
            session.run("CREATE FULLTEXT INDEX chunk_ft IF NOT EXISTS FOR (c:Chunk) ON EACH [c.text]").consume()

            missing = session.run("UNWIND $ids AS id OPTIONAL MATCH (s:Section {section_id: id}) WITH id, s WHERE s IS NULL RETURN count(id) AS n",
                                  ids=sorted({c.section_id for c in chunks})).single()["n"]
            if missing:
                sys.exit(f"{missing} sections are not in Neo4j; run load.py first")

            counters = Counter()
            rows = [{
                "chunk_id": c.chunk_id, "section_id": c.section_id, "order": c.order, "kind": c.kind,
                "text": c.text, "embed_text": c.embed_text, "tokens": c.tokens, "embed_tokens": c.embed_tokens,
                "table_id": c.table_id, "page": c.page, "embedding": vectors[c.chunk_id].tolist(),
            } for c in chunks]
            q = ("UNWIND $rows AS r MATCH (s:Section {section_id: r.section_id}) "
                 "MERGE (c:Chunk {chunk_id: r.chunk_id}) "
                 "SET c.section_id = r.section_id, c.order = r.order, c.kind = r.kind, c.text = r.text, "
                 "c.embed_text = r.embed_text, c.tokens = r.tokens, c.embed_tokens = r.embed_tokens, "
                 "c.table_id = r.table_id, c.page = r.page "
                 "MERGE (s)-[:HAS_CHUNK]->(c) "
                 "WITH c, r CALL db.create.setNodeVectorProperty(c, 'embedding', r.embedding)")
            for i in range(0, len(rows), BATCH):
                counters += L._count(session.run(q, rows=rows[i : i + BATCH]).consume().counters)

            by_section = defaultdict(list)
            for c in chunks:
                by_section[c.section_id].append(c)
            pairs = [{"a": a.chunk_id, "b": b.chunk_id}
                     for cs in by_section.values() for a, b in zip(sorted(cs, key=lambda c: c.order), sorted(cs, key=lambda c: c.order)[1:])]
            for i in range(0, len(pairs), BATCH):
                counters += L._count(session.run(
                    "UNWIND $rows AS r MATCH (a:Chunk {chunk_id: r.a}) MATCH (b:Chunk {chunk_id: r.b}) MERGE (a)-[:NEXT_CHUNK]->(b)",
                    rows=pairs[i : i + BATCH]).consume().counters)

            # Remove chunks of these documents that are no longer produced (e.g. after text or table changes).
            keep = [c.chunk_id for c in chunks]
            for doc_hash in plan.doc_sections:
                counters += L._count(session.run(
                    "MATCH (:Section {document_hash: $h})-[:HAS_CHUNK]->(c:Chunk) WHERE NOT c.chunk_id IN $keep DETACH DELETE c",
                    h=doc_hash, keep=keep).consume().counters)
            session.run("CALL db.awaitIndexes(300)").consume()
    print("\nDatabase changes:")
    for k, v in sorted(counters.items()):
        if v:
            print(f"  {k:24} {v}")


# --- report -----------------------------------------------------------------


def report(chunks: list[Chunk], meta: dict) -> None:
    per_op = defaultdict(Counter)
    for c in chunks:
        m = meta[c.section_id]
        per_op[(m["project_id"], m["country"])][c.kind] += 1
    print("Chunks per operation:")
    kinds = ("text", "table", "glossary")
    print(f"  {'project':9} {'country':10}" + "".join(f"{k:>9}" for k in kinds) + f"{'total':>7}")
    for (pid, country), n in sorted(per_op.items()):
        print(f"  {pid:9} {country or '?':10}" + "".join(f"{n[k]:9}" for k in kinds) + f"{sum(n.values()):7}")
    print(f"  {'all':20}" + "".join(f"{sum(n[k] for n in per_op.values()):9}" for k in kinds) + f"{len(chunks):7}")

    print("\nToken lengths (bge tokenizer):")
    for label, attr in (("text (raw)", "tokens"), ("embed_text (+header, +2 special)", "embed_tokens")):
        for kind in ("text", "table", "glossary"):
            v = np.array([getattr(c, attr) for c in chunks if c.kind == kind])
            if len(v):
                p = np.percentile(v, [0, 10, 50, 90, 100]).astype(int)
                print(f"  {label:34} {kind:5}  n={len(v):5}  min={p[0]:3}  p10={p[1]:3}  median={p[2]:3}  p90={p[3]:3}  max={p[4]:3}  mean={v.mean():5.0f}")
    edges = [0, 50, 100, 200, 300, 350, 400, 450, 512, 10_000]
    hist = Counter()
    for c in chunks:
        for lo, hi in zip(edges, edges[1:]):
            if lo <= c.tokens < hi:
                hist[(lo, hi)] += 1
    print("\n  raw text tokens   " + "  ".join(f"{lo}-{hi - 1 if hi < 10_000 else '+'}" for lo, hi in zip(edges, edges[1:])))
    print("  chunks            " + "  ".join(f"{hist[(lo, hi)]:>{len(f'{lo}-{hi - 1 if hi < 10_000 else chr(43)}')}}" for lo, hi in zip(edges, edges[1:])))
    over = [c for c in chunks if c.embed_tokens > MODEL_MAX_TOKENS]
    print(f"\n  embed_text over the model's {MODEL_MAX_TOKENS}-token limit (would be truncated): {len(over)}")
    empty = sum(1 for c in chunks if not c.text.strip())
    if empty:
        print(f"  empty chunks: {empty}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="chunk and report only; no embedding, no Neo4j")
    ap.add_argument("--out", default=str(paths.OUT))
    ap.add_argument("--pdfs", default=str(paths.PDFS))
    args = ap.parse_args()

    plan = L.collect(Path(args.out), Path(args.pdfs))
    tokens = Tokens()
    chunks, meta = build_chunks(plan, tokens)
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids)), "duplicate chunk IDs"
    report(chunks, meta)
    if args.dry_run:
        print("\n(dry run: nothing embedded or written)")
        return
    vectors = embed(chunks)
    load_chunks(chunks, vectors, plan)


if __name__ == "__main__":
    main()
