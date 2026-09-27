# Examples

Excerpts are from publicly disclosed World Bank documents (documents.worldbank.org), © World Bank, used for illustration.

## Q&A traces

End-to-end traces from an eval run (`scripts/export_examples.py`): the question, every MCP tool call with its
arguments, a trimmed result, and Claude's answer.

| File | Shows |
|---|---|
| [qa-graph-then-retrieve.md](qa-graph-then-retrieve.md) | Graph query to find the operations, then retrieval for the reasons |
| [qa-per-project-k.md](qa-per-project-k.md) | Five operations in one `retrieve` call with `per_project_k`, grouped by operation |
| [qa-section-type-retrieval.md](qa-section-type-retrieval.md) | Retrieval filtered to one operation and one section type |
| [qa-not-in-corpus.md](qa-not-in-corpus.md) | A country that isn't in the corpus: the answer says so instead of guessing |

To ask your own question and see the same kind of trace: `uv run python scripts/test_kg.py "<question>"`;
re-read a saved one for free with `uv run python scripts/test_kg.py --replay traces/<timestamp>.jsonl`.

## Retrieval from the command line

```bash
# unfiltered hybrid search
uv run python -m kg_pipeline.retrieve "What lessons from previous projects shaped the design?"

# filtered: one operation, Key Risks and fiduciary sections
uv run python -m kg_pipeline.retrieve "Why is fiduciary risk rated High?" --project P511478 --types key_risks,fiduciary

# by country and coarse type; acronyms are expanded automatically
uv run python -m kg_pipeline.retrieve "What does I3RF finance?" --country Iraq
uv run python -m kg_pipeline.retrieve "What does Phase I support?" --country Tanzania --scope phase

# several operations at once: top 2 per operation, grouped by operation
uv run python -m kg_pipeline.retrieve "What mitigations are proposed for fiduciary risk?" \
    --project P510381,P510253,P508698,P175721,P509224 --types key_risks,fiduciary --per-project-k 2
```

Section types are the coarse types and subtypes in `config/section_types.json`, e.g. `lessons`,
`key_risks`, `fiduciary`, `development_objective`, `components`, `mpa_framework`.

## Cypher

[`queries.cypher`](queries.cypher) has read-only queries for ratings, financing, MPA phases, acronyms and
the section tree. The same queries work through the `graph_query` MCP tool.

## MCP

[`mcp.example.json`](mcp.example.json) is a config for `claude --mcp-config`; or register the server once with
`claude mcp add` (see the top-level README). Questions that exercise the routing:

- "Which operations rate fiduciary risk High, and why?" — graph for the operations, retrieval for the reasons
- "What is Türkiye's loan in its original currency and in USD?" — graph (`FINANCES.orig_currency`)
- "Which earlier World Bank program does the Rwanda operation build on?" — retrieval
- "How much does the Somalia operation finance?" — the honest answer is that it has no financing yet
