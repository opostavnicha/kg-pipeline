# Evaluation

Three ways to measure the system, from cheapest to most realistic:

| Mode | Script | What it measures | LLM? |
|---|---|---|---|
| **retrieval-only** (baseline) | `eval/eval.py --modes retrieval` | Unfiltered hybrid search: are the expected projects and section types in the top k? | no |
| **oracle** (upper bound) | `eval/eval.py --modes oracle` | The same search with filters taken from the answer key, i.e. what perfect routing could retrieve | no |
| **e2e** (realistic) | `eval/run_e2e.py` | Claude Code answering with only the `kg` MCP server: did it call the right tools, and does its answer contain the expected IDs and facts without invented ones? | yes |

The gap between retrieval-only and oracle is what routing is worth; the gap between oracle and e2e is what
the model loses (or gains) when it has to route, query and write the answer itself.

## Question files

Both scripts read the same format, documented in [`questions.py`](questions.py):

- [`retrieval.jsonl`](retrieval.jsonl) — 13 retrieval questions (`r01`–`r13`)
- [`e2e.jsonl`](e2e.jsonl) — 17 end-to-end questions (`e01`–`e17`): graph facts, retrieval, questions that need both, and edge
  cases (a question whose true answer is "no financing", and one about a country that isn't in the corpus)

`eval.py` scores every question that has `expect_section_types`, from either file. Each expected answer was
checked against Neo4j before it was written; the `note` field says where the answer lives.

## Prerequisites

- The graph loaded (see the top-level README: fetch PDFs → sections → datasheet → load → chunk)
- `.env` with the Neo4j connection (copy `.env.example`)
- For e2e only: [Claude Code](https://docs.claude.com/en/docs/claude-code) installed (`claude` on PATH) and logged in

## Running

```bash
# retrieval-only and oracle (about a minute, no API cost)
uv run python eval/eval.py

# end to end, all 17 questions
uv run python eval/run_e2e.py

# a subset, a specific model, parallel runs
uv run python eval/run_e2e.py --only e03,e15
uv run python eval/run_e2e.py --category edge --model sonnet    # any claude --model value
uv run python eval/run_e2e.py --jobs 3 --out-dir eval/runs/my-run
```

`run_e2e.py` checks its prerequisites first (`claude` on PATH, `.env`, Neo4j reachable with Operation nodes)
and exits with a message if one is missing. It writes a temporary MCP config pointing at `src/kg_server.py`
with the current Python, and runs each question as

```
claude -p <question> --mcp-config <tmp> --strict-mcp-config --allowedTools "mcp__kg__*" \
       --output-format stream-json --verbose --no-session-persistence
```

from an empty working directory, so no project instructions or other MCP servers are involved.

Output goes to `eval/runs/<timestamp>/` (gitignored):

- `<id>.jsonl` — the raw stream-json transcript
- `summary.json` — every score, tool call, answer, time and reported cost
- `results.md` — a results table, then per question: the tools called with their arguments, the pass/fail reasons
  and an answer excerpt

Emails, phone numbers, the Neo4j URI/host/password and your home directory path are scrubbed from all three
before they are written.

## What the e2e scores mean

| Score | Passes when |
|---|---|
| `route_ok` | every tool in `expect_route` was called (an entry that is a list means any one of those tools). Extra calls, such as `graph_schema` or Claude Code's own tool search, are fine. |
| `ids_found` | every project ID in `expect_ids` appears in the final answer |
| `facts_found` | every entry in `expect_facts` appears in the answer (a list entry means any one of the alternatives; `re:` entries are regular expressions) |
| `must_not_violated` | a `must_not` pattern appears in the answer — e.g. a dollar amount for the Somalia operation, which has no financing, or risk ratings for Kenya, which isn't in the corpus. This must be false to pass. |
| `pass` | all of the above hold and the run finished without an error or timeout |

The checks are string matches on the final answer, so they are strict about wording in one direction (an
answer that gives Ethiopia but not its project ID fails `ids_found`) and lenient in another (a correct fact
buried in an otherwise wrong answer still counts). Read the failing answers in `results.md` before drawing
conclusions.

## Cost and time

Measured on the 17-question set with Claude Code's default model and `--jobs 3`: **$4.01–$4.25 in total per
run** (reported cost), i.e. roughly **$0.15–$0.55 per question**, and **2–8 minutes wall clock** (111 s on
2026-09-27 after `per_project_k`; 473 s before, when one question made five `retrieve` calls and hit the
420 s timeout). Graph-only questions are the cheapest (≈$0.15–0.2, ≈10 s); questions that combine the graph
with retrieval cost the most (≈$0.25–0.55, 20–50 s). The per-question timeout is 420 s (`--timeout`).

Pick the model with `--model` (any value `claude --model` accepts, e.g. `sonnet`, `opus`, `haiku`, or a full
model ID); cost and time scale with it. `summary.json` records the cost Claude Code reports for every
question. Running more than 3–4 questions in parallel can hit rate limits. The retrieval-only and oracle modes
make no LLM calls and take about a minute.

## Scorer changes

Changes to the answer key or scoring after a run are logged here, so results from different dates can be
compared.

| Date | Question | Change | Reason |
|---|---|---|---|
| 2026-09-27 | e06 | Expected phase count 7 → 15 (before the first run) | Checked against the PDF: the MPA framework table (Table 1, pp. 27–28) lists 15 phases. |
| 2026-09-27 | e07 | Expected financiers IDA, IBRD → IDA, IBRD, ESMAP (before the first run) | Neo4j: ESMAP also funds two operations (India, Tanzania). |
| 2026-09-27 | all | `expect_facts` entries may be lists of alternatives (any one matches); documented in `questions.py` | Answers name the same thing in different ways. |
| 2026-09-27 | e04 | Each Moderate-rated operation may be named by country, operation name or project ID | The first run's answer listed the right three operations by ID and title, not country, and failed only on wording. |
| 2026-09-27 | e15 | `must_not` flags only dollar amounts attributed to the operation ("the project provides US$…", "US$… for the CERP", "total financing of US$…") | The first run's answer correctly said there is no financing amount, then quoted context figures from the document (US$129M/yr response cost, US$76.6M gap, SoDMA's US$3.1m budget) explicitly marked as not the project's funding; the old pattern flagged any dollar amount. Tested against the real answer and five invented-amount answers. |
| 2026-09-27 | e06 | `expect_route` graph_query + retrieve → graph_query | The graph now has every phase (`Program.phases`, one `Phase` node each), so the graph alone can answer; retrieval is optional. |
| 2026-09-27 | oracle mode | Multi-operation questions use `per_project_k` | That is what perfect routing does now that `retrieve` supports it; top-8 alone couldn't cover 5 operations (e12). |
| 2026-09-27 | all graph-category questions (e01–e05, e07) | Operations may be named by country, operation name or project ID (e01 and e02 changed; e04 already had it; e03, e05 and e07 have no operation-name facts) | The second run's e01 answer named the right operation by project ID and title ("P511478, Productive Safety Net Project 6") and failed only for not writing "Ethiopia" — the same false negative as e04. Applied to every graph question instead of fixing them one at a time. |

## Results are not deterministic

The same question can route differently between runs. Treat a single e2e run as a sample; rerun failing
questions with `--only` before concluding that something regressed.
