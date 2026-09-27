# Which operations rate fiduciary risk High, and why?

*Graph query to find the operations, then retrieval for the reasons.*

Run `20260927-142623` · question `e11` (both) · scored **PASS** · 23 s · 3 MCP tool call(s)

Test output from this corpus, not a benchmark; see the note in the [README](../README.md).

Claude Code first loaded the MCP tool definitions (ToolSearch).

## 1. `graph_query`

```cypher
MATCH (o:Operation)-[r:HAS_RISK]->(:Risk {name:'Fiduciary'}) WHERE r.rating='High' OPTIONAL MATCH (o)-[:LOCATED_IN]->(l:Location) RETURN o.project_id AS pid, o.name AS name, o.overall_risk AS overall, collect(l.name_norm) AS loc
```

**Result (trimmed):**

1 row(s):

| pid | name | overall | loc |
|---|---|---|---|
| P511478 | Productive Safety Net Project 6 | Substantial | ["ethiopia"] |

## 2. `retrieve`

```json
{
  "question": "Why is fiduciary risk rated High?",
  "project_ids": [
    "P511478"
  ],
  "section_types": [
    "key_risks",
    "fiduciary",
    "risks"
  ],
  "k": 10
}
```

**Result (trimmed):**

- #1 `P511478` · V. KEY RISKS (key_risks, text) — The overall risk to project preparation and implementation is rated Substantial.
- #2 `P511478` · V. KEY RISKS (key_risks, table) — | Risk | Rating | Description | | Mitigation Measures | Mitigation Measures | | --- | --- | --- | --- | --- | --- | | | | | gaps in internal control systems. | | In cases of reported conflicts affecting existing | | | | | | | fiduciary arrangements, conduct a post-conflict | | | …
- #3 `P511478` · V. KEY RISKS (key_risks, table) — | Risk | Rating | Description | | Mitigation Measures | Mitigation Measures | | --- | --- | --- | --- | --- | --- | | Political and Governance | High | Ethiopia has faced volatile political situations over recent years. There are conflicts and social tensions in some parts of the…
- #4 `P511478` · IV. PROJECT APPRAISAL SUMMARY > B. Fiduciary (fiduciary, text) — in parallel. Project staff will receive training and capacity building support for using the FC. 77. The residual FM risk of the project is High. Details are noted in the Key Risks section. The proposed FM arrangements are considered acceptable to the World Bank. However, due to …
- …6 more

## 3. `graph_query`

```cypher
MATCH (o:Operation)-[r:HAS_RISK]->(:Risk {name:'Fiduciary'}) RETURN r.rating AS rating, count(*) AS n
```

**Result (trimmed):**

3 row(s):

| rating | n |
|---|---|
| Moderate | 1 |
| Substantial | 5 |
| High | 1 |

## Answer

Only one operation rates fiduciary risk High: **Ethiopia's Productive Safety Net Project 6 (PSNP 6, P511478)**. Seven operations in the knowledge graph have a fiduciary rating. Of the other six, five are Substantial and one is Moderate. PSNP 6's overall risk is Substantial.

**How the High rating breaks down:** financial management (FM) is rated High and procurement is rated Substantial. The appraisal says the "residual FM risk of the project is High." It still judges the FM arrangements acceptable to the World Bank, but only if the mitigation measures in the FM Action Plan (annex 2) are carried out.

**Why it's High (from the Key Risks table):**
- **Decentralized delivery.** Money moves through the government's Channel One system: from the Ministry of Finance to federal agencies, then to regional finance bureaus, then to district (woreda) finance offices. Procurement also happens at the federal, regional and woreda levels.
- **Manual cash distributions.** Beneficiaries are paid in cash by hand. This phase moves to cash only, where earlier phases also used food transfers.
- **Procurement weaknesses.** Procurements are complex, bidding is delayed, and staff have limited knowledge of the World Bank's Procurement Regulations.
- **Gaps in internal control systems.**
- **New implementing entities.** Two regions (Gambella and Benishangul Gumuz) have no PSNP experience, and new woredas and the Ministry of Labor and Skills (MoLS) are joining. The World Bank will only send them funds once it confirms minimum FM arrangements are in place.
- **Risks from the FundsChain rollout.** FundsChain is the World Bank's blockchain-based platform for tracking funds. The risks are connectivity problems in remote woredas, capacity gaps, data privacy, linking it to the government's existing FM systems, and tracking a large volume of beneficiary cash transfers. The government will run FundsChain and its current systems side by side until they are integrated.
- **Conflict.** Parts of the country are affected by conflict, and the political and governance risk is also rated High.

**Planned mitigations:**
- Keep improving the digital systems.
- Add a performance-based condition to improve reconciliation of beneficiary payments.
- Run regular trainings and increase monitoring and tracking.
- Carry out post-conflict fiduciary assessments before releasing more funds to affected areas.
- Roll out FundsChain in phases, with offline functionality and training.
- Make hiring qualified Regional Procurement Coordinators a dated covenant (a legal commitment with a deadline).
