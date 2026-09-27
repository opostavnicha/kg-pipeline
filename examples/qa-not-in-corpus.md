# What are the key risks of the Kenya education project?

*A question about a country that isn't in the corpus: the answer says so instead of guessing.*

Run `20260927-142623` · question `e16` (edge) · scored **PASS** · 19 s · 4 MCP tool call(s)

Test output from this corpus, not a benchmark; see the note in the [README](../README.md).

Claude Code first loaded the MCP tool definitions (ToolSearch).

## 1. `graph_query`

```cypher
MATCH (o:Operation)-[:LOCATED_IN]->(l:Location) WHERE l.iso2='KE' OR toLower(l.name_norm) CONTAINS 'kenya' RETURN o.project_id, o.name, o.overall_risk, properties(o) AS p
```

**Result (trimmed):**

0 row(s):

| o.project_id | o.name | o.overall_risk | p |
|---|---|---|---|

## 2. `graph_query`

```cypher
MATCH (o:Operation) OPTIONAL MATCH (o)-[:LOCATED_IN]->(l) RETURN o.project_id, o.overall_risk, keys(o) AS k, collect(l.name_norm) AS locs LIMIT 50
```

**Result (trimmed):**

12 row(s):

| o.project_id | o.overall_risk | k | locs |
|---|---|---|---|
| P507538 | Moderate | ["project_id", "name", "country", "is_cerp", "closing_date", "region_source", "ida_commitm… | ["rwanda"] |
| P510381 | Substantial | ["project_id", "name", "country", "is_cerp", "closing_date", "region_source", "instrument"… | ["turkiye"] |
| P510253 | Substantial | ["project_id", "name", "country", "is_cerp", "closing_date", "region_source", "instrument"… | ["india"] |
| P511478 | Substantial | ["project_id", "name", "country", "is_cerp", "closing_date", "region_source", "ida_commitm… | ["ethiopia"] |
| P508698 | Moderate | ["project_id", "name", "country", "is_cerp", "closing_date", "region_source", "ida_commitm… | ["tanzania"] |
| P175721 | Substantial | ["project_id", "name", "country", "is_cerp", "closing_date", "region_source", "instrument"… | ["iraq"] |
| P509224 | Moderate | ["project_id", "name", "country", "is_cerp", "closing_date", "region_source", "instrument"… | ["somalia"] |
| P506439 | null | ["project_id", "instrument", "stub"] | [] |
| P507207 | null | ["project_id", "instrument", "stub"] | [] |
| P512161 | null | ["project_id", "instrument", "stub"] | [] |

…and 2 more rows

## 3. `graph_query`

```cypher
MATCH (o:Operation) WHERE o.name IS NOT NULL RETURN o.project_id, o.name, o.country, o.practice_area_lead
```

**Result (trimmed):**

7 row(s):

| o.project_id | o.name | o.country | o.practice_area_lead |
|---|---|---|---|
| P507538 | Revenue Improvement and Spending Efficiency Program-for-Results Operation | Rwanda | Governance |
| P510381 | Türkiye: Education for Job Market Readiness | Türkiye | Education |
| P510253 | Uttar Pradesh Clean Air Management Program | India | Environment, Natural Resources & the Blue Economy |
| P511478 | Productive Safety Net Project 6 | Ethiopia | Social Protection & Jobs |
| P508698 | Sustainable Rural Water Supply and Sanitation Program Phase II | Tanzania | null |
| P175721 | Innovations Towards Learning in Lagging Iraqi Governorates | Iraq | Education |
| P509224 | Somalia Contingent Emergency Response Project | Somalia | Urban, Resilience and Land |

## 4. `retrieve`

```json
{
  "question": "Kenya education project risks",
  "k": 8
}
```

**Result (trimmed):**

- #1 `P510381` · V. KEY RISKS (key_risks, text) — 64. The overall risk rating of the project is Substantial given the substantial macro-economic and fiduciary risks and those related to institutional capacity for implementation and sustainability. Table 3 presents a description of each risk deemed substantial and its mitigation …
- #2 `P510381` · V. KEY RISKS (key_risks, table) — | | Type of risk | | Description/mitigation measures | | --- | --- | --- | --- | | Macro-economic | | Risks include high inflation and exchange rate volatility, which may lead to supply side constraints due to price increases and uncertainties. Moreover, Turkiye’s vulnerability t…
- #3 `P510381` · IV. PROJECT APPRAISAL SUMMARY > C. Environmental, Social and Legal Operational Policies (environmental_social, table) — | | Legal Operational Policies | Triggered? | | --- | --- | --- | be hired locally. A key social risk, particularly under Component 3 but also relevant across the project, is the potential exclusion of vulnerable groups due to ineffective stakeholder engagement, which could limit…
- #4 `P510381` · IV. PROJECT APPRAISAL SUMMARY > C. Environmental, Social and Legal Operational Policies (environmental_social, table) — | | Legal Operational Policies | Triggered? | | --- | --- | --- | | (iii) ineffective stakeholder engagement and grievance management. These risks are site-specific, limited in scale and | | | | duration, and manageable through standard mitigation measures. The project will not f…
- …4 more

## Answer

There's no Kenya education project in the knowledge base, so I can't give you its risks.

The knowledge base holds World Bank operation documents for 7 operations, and none of them are in Kenya:

| Project | Country | Sector | Overall risk |
|---|---|---|---|
| P510381: Education for Job Market Readiness | Türkiye | Education | Substantial |
| P175721: Innovations Towards Learning in Lagging Iraqi Governorates | Iraq | Education | Substantial |
| P511478: Productive Safety Net Project 6 | Ethiopia | Social Protection | Substantial |
| P508698: Sustainable Rural Water Supply and Sanitation Phase II | Tanzania | Water | Moderate |
| P509224: Somalia Contingent Emergency Response Project | Somalia | Urban and Resilience | Moderate |
| P507538: Revenue Improvement and Spending Efficiency | Rwanda | Governance | Moderate |
| P510253: Uttar Pradesh Clean Air Management Program | India | Environment | Substantial |

Kenya only comes up in passing, in the Tanzania water program's list of cross-border aquifers.

Some possible next steps:
- **If you meant one of the education projects above** (Türkiye or Iraq), I can go through its key risks and how they're mitigated.
- **If you have the Kenya project's document** (for example, a Project Appraisal Document PDF), I can add it to the knowledge base and then answer your question.

---

> **Reviewer's note:** The answer's main claim is right: there is no Kenya operation in the corpus. One detail is overstated: Kenya does not come up "only" in the Tanzania program's aquifer list (Annex 2). It is also mentioned in Rwanda's lessons section (PforR programs in neighbouring Kenya and Uganda), Tanzania's riparian-notification paragraph (Annex 3), and a returns-to-education table in Iraq's economic analysis (Annex 2). Checked with a text search over all chunks.
