# Lessons learned

Things that went wrong or surprised us while building this, and what changed as a result. Newest first.

## An extractor that keeps only what it can link undercounts silently (MPA phases)

**What happened.** Tanzania's appraisal document (P508698) is Phase 2 of a multiphase programmatic approach
(MPA, program P506439). Table 1, "Proposed MPA Framework" (pp. 27–28), lists **15 phases**. The graph had 6.
`datasheet.py` read the same table as the section extraction, but kept only rows whose "Operation ID" cell
contained a project ID (`P######`), because each phase became a `HAS_PHASE` edge to an `Operation` node and
there was nothing to point at without an ID. Nine planned phases (Somalia, Madagascar, Comoros, Malawi, Angola,
Zambia, Mozambique and two regional ones) were dropped with no warning. The eval answer key made it worse in the
other direction: it was first written as "7 phases" from memory, and a graph-only answer would have said 6.

**How it was found.** Writing the e2e answer key against Neo4j and the PDF instead of from memory: the table
text had rows 1–15, the graph had 6 `HAS_PHASE` edges.

**Fix.**
- Every framework-table row is parsed, with fields found by content rather than column position (the table's
  columns shift between pages): phase number, label, project ID if any, sequencing, instrument, IBRD/IDA/other
  amounts, estimated approval, E&S rating.
- Each phase is a `(:Phase)` node: `(:Program)-[:HAS_PHASE]->(:Phase)-[:HAS_OPERATION]->(:Operation)`, the last
  edge only when there is a project ID. `Program.phases` = 15 = the number of `HAS_PHASE` edges.
- A cross-check that would have caught it: the phases' IBRD, IDA and other amounts must sum to the datasheet's
  MPA financing breakdown (240 + 1,340 + 1,013.5 = US$2,593.5M). They now match to the decimal; a mismatch or a
  gap in the phase numbers is flagged.

**Lesson.** When a parser drops rows it can't link, count what it dropped. Prefer modelling the unlinked thing
(a `Phase` without an operation) over discarding it, and reconcile against a total the document states.

## Check the answer key against the data, not memory

Two of the 17 end-to-end expected answers were wrong before the first run: the MPA phase count (above), and
"financiers that fund more than one operation", where ESMAP (India and Tanzania) had been forgotten. Every
expected answer is now verified with a Cypher query or a text match, and the `note` field says where it lives.
Changes after a run are logged in [eval/README.md](../eval/README.md#scorer-changes).

## String-match scoring produces false negatives; read the failures

In the first e2e run, 2 of 3 failures were correct answers: one named operations by project ID instead of
country (e04), one quoted context figures from the document that a "no dollar amounts" rule caught (e15). In the
next run a third appeared the same way (e01: right project ID, no country name). Scoring rules need
alternatives (country, operation name or ID), and `must_not` rules must target the claim ("the project provides
US$…"), not the token ("US$"). Always read failing answers before counting them.

## Per-operation questions need per-operation retrieval

"Which operations rate fiduciary risk Substantial, and what mitigations do they propose?" timed out: the model
correctly found five operations in the graph, then made five separate `retrieve` calls and ran out of time.
Even a single unfiltered call can't help, because the global top 8 can't cover five operations. `retrieve` now
takes `per_project_k` (each project gets its own candidates and top n, results grouped by project), and the
server instructions tell the model to make one call with all the project IDs. The question now passes in ~30 s
with one call, and the oracle eval went from 21/22 to 22/22.

## Put the schema where the model will read it

The model often skipped `graph_schema` and guessed property names. A compact schema summary (labels,
relationship patterns, risk category names, section types) in the MCP server instructions cut those detours;
`graph_schema` remains for full property lists and value vocabularies.

## Detection rules generalize worse than they look; hold out documents

Heading detection tuned on three PDFs met a fourth template (a Project Paper for a contingent emergency
response project) with unnumbered annexes and different section names, and a fifth with unnumbered sub-headings
listed only in the printed table of contents. A holdout run (no rule changes allowed) surfaced both, including
one that silently folded a results framework into the Key Risks section. Validation checks (Key Risks with many
tables or results vocabulary; sections far above their subtype's median size; missing core sections per document
type) now catch that class of error.

## Filter extracted tables by how they are drawn

`find_tables()` also detects shaded prose (a filled box behind each line) as tables, which would remove body
text from sections. Keeping only tables drawn with thin ruling lines removed every false positive in the corpus
without losing real tables. A cells-per-row heuristic did not work: real tables with merged cells look sparse.

## Official APIs have gaps; cross-check, and mark derived values

The Projects API v2 had no record for most recent projects and only a ratings record for some older ones
(P149095); v3 had them all but lacks the lending instrument and practice area. The pipeline queries both, treats
zeros and blanks as missing rather than as conflicting values, and marks region and status as `derived` when
they come from the PDF instead of the API.

## Small cleanups matter for retrieval

Four of the top 8 results for a lessons question were table-of-contents lines. Removing dot-leader lines and
template markers, excluding front matter by default, and keeping the abbreviation lists as `glossary` chunks
fixed it. Expanding acronyms in the query from the documents' own abbreviation lists turned "What does I3RF
finance?" from unrelated vector hits into the right section at rank 1.

## Keep personal data out from the start

Datasheets list officials' names, phone numbers and emails, and the Projects API returns staff emails. Contact
rows are dropped at load, stray emails and phone numbers are masked, staff fields are stripped before API
responses are cached, and eval transcripts are scrubbed before they are written.
