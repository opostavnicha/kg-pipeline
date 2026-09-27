# Architecture

```
pdfs/*.pdf ──sections──▶ out/<pdf>.json ──┐
          └─datasheet──▶ out/<pdf>.datasheet.json ──load──▶ Neo4j graph ──chunk──▶ Chunk nodes + indexes
                 │                                                                     │
                 └── World Bank Projects API (cache/)                     retrieve / kg_server (MCP)
```

## Stages

| Module | Input → output | Notes |
|---|---|---|
| `sections` | PDF → sections, tables, types | Headings from the PDF outline when present, otherwise from typography (bold `I.`/`A.`/`ANNEX n` rows, plus unnumbered bold rows that appear in the printed table of contents). Headings map to a subtype by regex and a coarse type by group (`config/section_types.json`); unmatched headings inherit from a typed parent, else `other`. Tables come from `page.find_tables()`, kept only when drawn with ruling lines (shaded prose is rejected). Sections inside an MPA phase annex get `scope: phase`, the phase number and its project ID. A corpus-wide `validate()` flags oversized sections, suspicious Key Risks sections and missing core sections per document type. |
| `datasheet` | PDF → datasheet facts | Project ID (page header, confirmed in the datasheet), report number and date (cover), PDO, financing rows with original currency from the cover, borrower and implementing agencies, SORT ratings, E&S rating, modality checkboxes, MPA program and phases. Cross-checked against the Projects API (v3, with v2 for instrument and practice area); disagreements go to `conflicts`. Region and status are derived when the API has none. |
| `glossary` | front matter → acronym pairs | Abbreviation lists from 2-column tables or text (acronym and expansion on one line or on separate lines, in either order). |
| `load` | out/ → Neo4j | Builds a plan of nodes and relationships, then MERGEs it in batches. `--dry-run` prints the counts. Sections dropped from the extraction are deleted with their tables and chunks. Contact rows (names, phones, emails) are removed. |
| `chunk` | sections → chunks | A section's own text in ~400-token windows with 50 tokens of overlap, snapped to sentence ends, never crossing sections; tables as markdown split by rows with the header repeated; abbreviation lists as `glossary` chunks. Template markers and table-of-contents lines are stripped. `embed_text` = `country | project_id | section path` + text. Embeddings are cached by content hash. |
| `db` | — → Neo4j driver | The only place drivers are created. `notifications_min_severity = WARNING`; the `neo4j.notifications` logger writes to the real stderr and doesn't propagate, so server notifications never reach stdout (the MCP protocol stream) or tool results. |
| `retrieve` | question → ranked chunks | Vector (`chunk_vec`, queried with the Cypher 25 `SEARCH` clause) and full-text (`chunk_ft`) top 20 each after filtering, merged by reciprocal rank fusion (k = 60). With `per_project_k` and several project IDs, each project gets its own candidates and top n before the global top k, and results come back grouped by project. Filters follow the path Chunk → Section → Document → Operation → Location. Acronyms in the question are expanded from `DEFINES` and `config/aliases.json`. Front matter is excluded unless asked for (glossary chunks stay searchable). |

## Graph schema

```
(:Document {hash})-[:DESCRIBES]->(:Operation {project_id})
(:Organization {name_norm})-[:FINANCES {amount_usd, orig_currency, orig_amount, detail}]->(:Operation)
(:Organization)-[:IMPLEMENTS]->(:Operation)
(:Operation)-[:HAS_RISK {rating}]->(:Risk {name})
(:Operation)-[:LOCATED_IN]->(:Location {name_norm, iso2, region})
(:Program {program_id, phases})-[:HAS_PHASE {phase}]->(:Phase {phase_id})-[:HAS_OPERATION]->(:Operation)
(:Document)-[:DEFINES {expansion}]->(:Acronym {name})
(:Document)-[:HAS_SECTION]->(:Section {section_id})-[:HAS_SUBSECTION]->(:Section)
(:Section)-[:HAS_TABLE]->(:Table {table_id, rows_json})
(:Section)-[:HAS_CHUNK]->(:Chunk {chunk_id, kind, text, embedding})-[:NEXT_CHUNK]->(:Chunk)
```

- Amounts are in USD units (not millions); `orig_*` keep the loan currency (EUR, JPY, SDR).
- `Operation.overall_risk` holds the overall SORT rating; `Risk` nodes are the categories only.
- Every row of an MPA framework table is a `Phase` (label, instrument, IBRD/IDA/other amounts, estimated
  approval, E&S rating); `HAS_OPERATION` links the phases that have a project ID. `Program.phases` equals the
  number of `HAS_PHASE` relationships. `Operation.stub = true` marks phase operations known only by their ID.
- Government bodies have country-scoped keys (`ministry of finance [SO]`); international organizations
  (`config/aliases.json`) are global.
- Dates on Operation are the document's *expected* dates; `*_api` fields come from the Projects API.

## Identifiers

All IDs are deterministic, so rerunning any stage updates in place:

| Node | Key |
|---|---|
| Document | SHA-256 of the PDF bytes |
| Section | `{hash}:{slug of heading path}#{n}` — numbering prefixes dropped, so renumbering doesn't change it; `n` separates identical paths |
| Table | `{section_id}:t{i}` |
| Chunk | `{section_id}:c{n}` (text), `{table_id}:c{n}` (table), `{section_id}:g{n}` (glossary) |
| Phase | `{program_id}:phase:{n}` |

A heading-detection change that adds or removes a heading changes the IDs of the sections below it; `load`
then deletes the old sections with their tables and chunks.

## Known limits

- Heading detection assumes the World Bank templates (`I.`, `A.`, `ANNEX n`); other layouts need patterns.
- `ingest_pdf` validates the new document on its own, so size-outlier flags differ from a corpus run.
- The Projects API has no instrument or practice area in v3 and misses many projects in v2.

## Related work

- [Neo4j LLM Graph Builder](https://github.com/neo4j-labs/llm-graph-builder) turns unstructured documents into a
  Neo4j knowledge graph by extracting entities and relationships with an LLM, and chats over the result.
- [neo4j-graphrag-python](https://github.com/neo4j/neo4j-graphrag-python) is Neo4j's package for building graphs
  with LLM-based extraction pipelines and querying them with vector, hybrid and Cypher retrievers.
- [Microsoft GraphRAG](https://github.com/microsoft/graphrag) extracts an entity graph with an LLM, clusters it into
  communities and summarizes them, to answer both local and corpus-wide ("global") questions.
- [LightRAG](https://github.com/HKUDS/LightRAG) indexes text into an LLM-extracted entity/relation graph and
  combines low-level (entity) and high-level (theme) retrieval.

This project differs in two ways: it builds the graph by deterministic parsing of templated documents (section
patterns, datasheet tables, SORT ratings, financing rows) instead of LLM extraction, so every node and figure
traces back to a specific table cell or heading and reruns produce identical IDs; and it leaves the choice between
structured graph queries and text retrieval to the model at question time, through MCP tools and routing
instructions, rather than fixing one retrieval strategy in the pipeline.

