"""Hybrid retrieval over Chunk nodes: vector (chunk_vec) + full-text (chunk_ft), merged by reciprocal rank fusion.

Each candidate is traced back Chunk <- Section <- ... <- Document -> Operation -> Location, and the filters
apply along that path; each index is queried for a wide pool, filtered, then cut to its top 20.

- Front matter (cover, table of contents) is excluded by default; its glossary chunks (the abbreviation
  list) are kept. --include-front-matter keeps everything.
- Acronyms in the question (e.g. I3RF) are expanded from the documents' abbreviation lists
  ((:Document)-[:DEFINES]->(:Acronym)) and aliases.json: the vector query gets "I3RF: Iraq Reform, ..."
  appended, and the full-text query gets the expansion as an extra phrase.

Usage: python -m kg_pipeline.retrieve "<question>" [--country X[,Y]] [--project P123456[,..]] [--types lessons,key_risks]
                          [--scope main|phase] [--k 8] [--per-project-k 2] [--include-front-matter] [--no-expand]
"""

import argparse
import json
import os
import re
import sys
import textwrap
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

from kg_pipeline import paths
from kg_pipeline.load import name_norm

MODEL = "BAAI/bge-base-en-v1.5"
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "  # bge v1.5 query prefix
PER_METHOD = 20
POOL = 1000  # candidates fetched per index before filtering (the corpus has ~1k chunks)
RRF_K = 60
MAX_EXPANSIONS = 2  # per acronym, most common first
STOPWORDS = set("""a about above after again all also an and any are as at be been before being below between both but by can
could did do does doing down during each few for from further had has have having how i if in into is it its itself just
me more most my no nor not now of off on once only or other our out over own same should so some such than that the their
them then there these they this those through to too under until up very was we were what when where which while who whom
why will with would you your""".split())
LUCENE_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)')

# Chunk -> its Section -> up the section tree to the Document -> Operation -> Location.
CONTEXT = """
MATCH (s:Section)-[:HAS_CHUNK]->(c)
MATCH (d:Document)-[:HAS_SECTION]->(:Section)-[:HAS_SUBSECTION*0..]->(s)
MATCH (d)-[:DESCRIBES]->(o:Operation)
OPTIONAL MATCH (o)-[:LOCATED_IN]->(l:Location)
WITH c, score, s, o, l
WHERE ($countries IS NULL OR l.name_norm IN $countries OR l.iso2 IN $isos)
  AND ($project_ids IS NULL OR o.project_id IN $project_ids OR s.phase_project_id IN $project_ids)
  AND ($types IS NULL OR s.type IN $types OR s.subtype IN $types)
  AND ($scope IS NULL OR s.scope = $scope)
  AND ($include_front_matter OR s.type <> 'front_matter' OR c.kind = 'glossary')
RETURN c.chunk_id AS chunk_id, c.kind AS kind, c.text AS text, score,
       s.section_id AS section_id, s.path AS path, s.type AS type, s.subtype AS subtype, s.scope AS scope,
       s.phase_project_id AS phase_project_id, o.project_id AS project_id, o.name AS operation, l.name AS country
ORDER BY score DESC
LIMIT $per_method
"""


@lru_cache(maxsize=1)
def model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(MODEL)


@lru_cache(maxsize=1)
def driver():
    from dotenv import load_dotenv
    from neo4j import GraphDatabase

    load_dotenv(paths.ENV)
    return GraphDatabase.driver(os.environ["NEO4J_URI"], auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]))


def database() -> str | None:
    return os.environ.get("NEO4J_DATABASE") or None


@lru_cache(maxsize=1)
def acronym_map() -> dict[str, list[str]]:
    """Acronym -> expansions (most documents first), from DEFINES plus aliases.json."""
    counts: dict[str, Counter] = defaultdict(Counter)
    with driver().session(database=database()) as session:
        for r in session.run("MATCH (:Document)-[d:DEFINES]->(a:Acronym) RETURN a.name AS a, d.expansion AS e"):
            counts[r["a"]][r["e"]] += 1
    aliases = json.loads(paths.ALIASES.read_text())["organizations"]
    for canon, variants in aliases.items():
        for v in [canon, *variants]:
            if re.fullmatch(r"[A-Z0-9][A-Za-z0-9 &\-]{1,15}", v) and sum(c.isupper() for c in v) >= 2 and " " not in v.strip():
                counts[v][re.sub(r"\s*\([^)]*\)$", "", canon)] += 1
    return {a: [e for e, _ in c.most_common(MAX_EXPANSIONS)] for a, c in counts.items()}


def expansions_for(question: str) -> dict[str, list[str]]:
    words = set(re.findall(r"[A-Za-z0-9][A-Za-z0-9&\-]*", question))
    table = acronym_map()
    out = {}
    for w in words:
        if len(w) >= 2 and sum(c.isupper() for c in w) >= 2 and w in table:
            exps = [e for e in table[w] if e.lower() not in question.lower()]
            if exps:
                out[w] = exps
    return out


def fulltext_query(question: str, expansions: dict[str, list[str]]) -> str | None:
    """OR of the question's content words (Lucene-escaped, acronyms kept) plus acronym expansions as phrases."""
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9&'-]*", question)
    terms = [LUCENE_SPECIAL.sub(r"\\\1", w) for w in dict.fromkeys(words) if w.lower() not in STOPWORDS and (len(w) > 2 or w.isupper())]
    for exps in expansions.values():
        for e in exps:
            phrase = re.sub(r"[^\w\s-]", " ", e)
            terms.append('"' + re.sub(r"\s+", " ", phrase).strip() + '"')
    return " OR ".join(terms) if terms else None


def _fuse(vec: list[dict], ft: list[dict]) -> list[dict]:
    fused: dict[str, dict] = {}
    for method, results in (("vector", vec), ("fulltext", ft)):
        for rank, r in enumerate(results, 1):
            hit = fused.setdefault(r["chunk_id"], {**{k: v for k, v in r.items() if k != "score"}, "rrf": 0.0})
            hit["rrf"] += 1 / (RRF_K + rank)
            hit[f"{method}_rank"], hit[f"{method}_score"] = rank, r["score"]
    return sorted(fused.values(), key=lambda h: -h["rrf"])


def search(question: str, countries: list[str] | None = None, project_ids: list[str] | None = None,
           types: list[str] | None = None, scope: str | None = None, k: int = 8,
           include_front_matter: bool = False, expand: bool = True,
           per_project_k: int | None = None) -> tuple[list[dict], dict]:
    """Top-k hybrid results. With per_project_k and project_ids, each project gets its own vector and
    full-text candidates and its own top per_project_k (so one operation can't crowd out the others);
    the union is then cut to the global top k (default per_project_k x projects) and grouped by project,
    in the order of project_ids."""
    expansions = expansions_for(question) if expand else {}
    vector_text = question + ("  (" + "; ".join(f"{a}: {', '.join(e)}" for a, e in expansions.items()) + ")" if expansions else "")
    vector = model().encode(QUERY_INSTRUCTION + vector_text, normalize_embeddings=True).tolist()
    base = {
        "countries": [name_norm(c) for c in countries] if countries else None,
        "isos": [c.upper() for c in countries] if countries else None,
        "types": types or None, "scope": scope,
        "include_front_matter": include_front_matter, "per_method": PER_METHOD, "pool": POOL,
    }
    ftq = fulltext_query(question, expansions)
    grouped = bool(per_project_k and project_ids)
    scopes = [[p] for p in project_ids] if grouped else [project_ids or None]
    ranked, n_vec, n_ft = [], 0, 0
    with driver().session(database=database()) as session:
        for pids in scopes:
            params = {**base, "project_ids": pids}
            vec = session.run("CALL db.index.vector.queryNodes('chunk_vec', $pool, $vector) YIELD node AS c, score" + CONTEXT,
                              vector=vector, **params).data()
            ft = session.run("CALL db.index.fulltext.queryNodes('chunk_ft', $q, {limit: $pool}) YIELD node AS c, score" + CONTEXT,
                             q=ftq, **params).data() if ftq else []
            n_vec, n_ft = n_vec + len(vec), n_ft + len(ft)
            fused = _fuse(vec, ft)
            if grouped:
                for h in fused[:per_project_k]:
                    h["group"] = pids[0]
                ranked += fused[:per_project_k]
            else:
                ranked = fused
    if grouped:
        k = k if k is not None else per_project_k * len(project_ids)
        keep = sorted(ranked, key=lambda h: -h["rrf"])[:k]
        order = {p: i for i, p in enumerate(project_ids)}
        ranked = sorted(keep, key=lambda h: (order.get(h["group"], len(order)), -h["rrf"]))
    else:
        ranked = ranked[:k]
    return ranked, {"vector": n_vec, "fulltext": n_ft, "fulltext_query": ftq, "expansions": expansions, "grouped": grouped}


def show(question: str, hits: list[dict], info: dict, width: int = 110) -> None:
    print(f"Q: {question}")
    if info["expansions"]:
        print("   acronyms expanded: " + "; ".join(f"{a} = {' / '.join(e)}" for a, e in info["expansions"].items()))
    print(f"   candidates: vector {info['vector']}, full-text {info['fulltext']}\n")
    group = None
    for i, h in enumerate(hits, 1):
        if info.get("grouped") and h.get("group") != group:
            group = h.get("group")
            print(f"── {group} ({h['country']}) ──")
        v = f"v#{h['vector_rank']} {h['vector_score']:.3f}" if "vector_rank" in h else "v –"
        f = f"ft#{h['fulltext_rank']} {h['fulltext_score']:.2f}" if "fulltext_rank" in h else "ft –"
        project = h["project_id"] + (f" (phase {h['phase_project_id']})" if h.get("phase_project_id") and h["phase_project_id"] != h["project_id"] else "")
        print(f"{i}. rrf {h['rrf']:.4f}  [{v} | {f}]  {h['country']} | {project} | {h['kind']}")
        print(f"   {h['path']}  ({h['type']}/{h['subtype']}, {h['scope']})")
        snippet = re.sub(r"\s+", " ", h["text"]).strip()
        print(textwrap.indent(textwrap.fill(snippet[:320] + ("…" if len(snippet) > 320 else ""), width - 3), "   "))
        print()


def split_list(v: str | None) -> list[str] | None:
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("question")
    ap.add_argument("--country", help="comma-separated country names or ISO2 codes")
    ap.add_argument("--project", help="comma-separated project IDs")
    ap.add_argument("--types", help="comma-separated coarse types or subtypes")
    ap.add_argument("--scope", choices=["main", "phase"])
    ap.add_argument("--k", type=int, help="global top k (default 8; with --per-project-k, per-project-k x projects)")
    ap.add_argument("--per-project-k", type=int, help="with --project: top n per project before the global top k, grouped by project")
    ap.add_argument("--include-front-matter", action="store_true")
    ap.add_argument("--no-expand", action="store_true", help="don't expand acronyms")
    args = ap.parse_args()
    k = args.k if args.k is not None else (None if args.per_project_k and args.project else 8)
    hits, info = search(args.question, split_list(args.country), split_list(args.project), split_list(args.types),
                        args.scope, k, args.include_front_matter, not args.no_expand, args.per_project_k)
    show(args.question, hits, info)
    driver().close()


if __name__ == "__main__":
    sys.exit(main())
