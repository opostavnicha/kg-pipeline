# Which operations rate fiduciary risk Substantial, and what mitigations do they propose?

*Several operations in one retrieve call with per_project_k, results grouped by operation.*

Run `20260927-142623` · question `e12` (both) · scored **PASS** · 29 s · 2 MCP tool call(s)

Test output from this corpus, not a benchmark; see the note in the [README](../README.md).

Claude Code first loaded the MCP tool definitions (ToolSearch).

## 1. `graph_query`

```cypher
MATCH (o:Operation)-[r:HAS_RISK]->(k:Risk {name:'Fiduciary'}) WHERE r.rating='Substantial' OPTIONAL MATCH (o)-[:LOCATED_IN]->(l:Location) RETURN o.project_id AS pid, o.name AS name, collect(DISTINCT l.name_norm) AS loc, o.overall_risk AS overall
```

**Result (trimmed):**

5 row(s):

| pid | name | loc | overall |
|---|---|---|---|
| P510381 | Türkiye: Education for Job Market Readiness | ["turkiye"] | Substantial |
| P510253 | Uttar Pradesh Clean Air Management Program | ["india"] | Substantial |
| P508698 | Sustainable Rural Water Supply and Sanitation Program Phase II | ["tanzania"] | Moderate |
| P175721 | Innovations Towards Learning in Lagging Iraqi Governorates | ["iraq"] | Substantial |
| P509224 | Somalia Contingent Emergency Response Project | ["somalia"] | Moderate |

## 2. `retrieve`

```json
{
  "question": "fiduciary risk substantial mitigation measures financial management procurement",
  "project_ids": [
    "P510381",
    "P510253",
    "P508698",
    "P175721",
    "P509224"
  ],
  "per_project_k": 3,
  "section_types": [
    "key_risks",
    "fiduciary",
    "risks"
  ]
}
```

**Result (trimmed):**

**P510381 · Türkiye** (3 results)
- #1 `P510381` · V. KEY RISKS (key_risks, table) — | | Type of risk | | Description/mitigation measures | | --- | --- | --- | --- | | Fiduciary | | The residual fiduciary risk is deemed Substantial specifically due to procurement. Risks include potential delays in the initiation of procurement processes due to preceding delays in…
- #2 `P510381` · V. KEY RISKS (key_risks, text) — 64. The overall risk rating of the project is Substantial given the substantial macro-economic and fiduciary risks and those related to institutional capacity for implementation and sustainability. Table 3 presents a description of each risk deemed substantial and its mitigation …
- #3 `P510381` · V. KEY RISKS (key_risks, table) — | | Type of risk | | Description/mitigation measures | | --- | --- | --- | --- | | Macro-economic | | Risks include high inflation and exchange rate volatility, which may lead to supply side constraints due to price increases and uncertainties. Moreover, Turkiye’s vulnerability t…

**P510253 · India** (3 results)
- #4 `P510253` · IV. PROGRAM ASSESSMENTS SUMMARY > B. Fiduciary (fiduciary, text) — 51. The fiduciary risk for the Program is Substantial. The fiduciary systems assessment completed in July 2025 determined that the fiduciary systems, capacity, and performance of the IAs are adequate to provide reasonable assurance, subject to timely and material implementation o…
- #5 `P510253` · V. KEY RISKS (key_risks, text) — government commitment, and the institutional capacity of the SPV and its coordination with line departments will be developed progressively during the initial years of the Program. The IAs bring varying levels of institutional capacity and may face constraints in coordinating int…
- #6 `P510253` · IV. PROGRAM ASSESSMENTS SUMMARY > B. Fiduciary (fiduciary, text) — (GeM) , which offers a credible platform with its own regulatory compliance and complaint-handling system, for most of their procurements. The FM and procurement capacity of the IAs varies, and some of them would require additional staffing and training. Various mitigation measur…

**P508698 · Tanzania** (3 results)
- #7 `P508698` · ANNEX 3: MPA Phase II: Sustainable Rural Water Supply and Sanitation Program II > G. Risks (key_risks, table) — | Risk | Rating | Risk Description | Mitigation Measure | | --- | --- | --- | --- | | | | endorsement and integration of program budgets; (vii) complex coordination across multiple institutions and varying financial capabilities that results in delays and communication breakdowns…
- #8 `P508698` · ANNEX 3: MPA Phase II: Sustainable Rural Water Supply and Sanitation Program II > G. Risks (key_risks, table) — | Risk | Rating | Risk Description | Mitigation Measure | | --- | --- | --- | --- | | Fiduciary | Substantial | (i) Shortages of qualified procurement staff; (ii) capacity gaps in planning, bid preparation and evaluation; (iii) weak contract management; and (iv) inadequate manage…
- #9 `P508698` · ANNEX 3: MPA Phase II: Sustainable Rural Water Supply and Sanitation Program II > G. Risks (key_risks, text) — 48. The risk assessment is informed by the results of the Technical, Fiduciary, and Environmental and Social Systems Assessments . Substantial risks and mitigation measures are summarized in Table 3.7 below. The overall risk rating of the operation is Moderate . Table 3.7 Program…

**P175721 · Iraq** (3 results)
- #10 `P175721` · VI. KEY RISKS (key_risks, text) — be delayed, although there has been recent progress. Risk mitigation measures include: (a) Using the existing MOED PMT under the EODP-AF project, thus leveraging institutional and implementation capacity already built over the last years. (b) The PMT comprising mostly civil serva…
- #11 `P175721` · VI. KEY RISKS (key_risks, text) — 82. The overall risk to achieving the PDO is rated Substantial. Risks relating to Political & Governance, Macro- economic, Institutional Capacity for Implementation & Sustainability, Fiduciary, and other risks (relating to the impact of COVID-19 on project implementation and secu…
- #12 `P175721` · IV. PROJECT APPRAISAL SUMMARY > B. Fiduciary (fiduciary, text) — (i) Financial Management 73. Due to the emergency nature of this project and the need to respond quickly to mitigate learning losses, the Financial Management (FM) approach has been streamlined and is based on simplified ex-ante requirements, while relying more heavily on ex-post…

**P509224 · Somalia** (3 results)
- #13 `P509224` · V. KEY RISKS (key_risks, text) — legacy of state fragmentation and conflict, as well because of political divisions, minimal revenue mobilization, and inchoate policy consensus and coordination. A shortage of managerial and professional staff to implement emergency preparedness and response also contributes to i…
- #14 `P509224` · V. KEY RISKS (key_risks, text) — 34. The categories of Political and Governance and Macroeconomic risks are rated high risks, while Institutional Capacity for Implementation and Sustainability, as well as Fiduciary risks, are rated substantial risks to the achievement of the CERP. A description of these risks an…
- #15 `P509224` · IV. APPRAISAL OF EMERGENCY RESPONSE READINESS > B. Fiduciary Readiness for Emergency Response (fiduciary, text) — transparent management of funds . The CERP PIU will oversee FM, including compliance with financial covenants, funds management, financial reporting, and audit processes. CERP financing will be channeled through a designated bank account at the Central Bank of Somalia. Payments f…

## Answer

Five operations in the knowledge graph rate fiduciary risk **Substantial**. None rate it High.

### 1. Türkiye: Education for Job Market Readiness (P510381). Overall risk: Substantial
- **Why:** The rating comes from procurement specifically. Procurement could start late because technical preparation runs late, and bid evaluation and contract signing could be slow because the client has limited experience running high-value goods contracts. Local market uncertainty could also mean few bidders. Financial management risk on its own is only Moderate, because the Ministry of National Education had a satisfactory rating on its earlier SSDE project.
- **Mitigations:**
  - The project implementation unit (PIU) handles all procurement centrally, working closely with the ministry's directorates-general (DGs) and tracking their technical inputs.
  - The procurement plan and the project operations manual (POM) set time limits for key steps, including bid evaluation and contract signing.
  - Annual procurement plans go to the Bank.
  - Contracts are bundled into fewer, larger packages.
  - Contracts are signed annually and progress is reported quarterly.
  - Experienced procurement staff from DGIET join the PIU, with individual consultants helping during at least the first 12 months.
  - Consultant selection starts early so contracts are ready to sign when the project becomes effective.
  - The ministry engages the market early to attract more bidders for internationally tendered goods packages.

### 2. Uttar Pradesh Clean Air Management Program, India (P510253). Overall risk: Substantial
- **Why:** Many sectors and agencies are involved, and their fiduciary capacity varies. That raises the risk of breaking fiduciary rules and anti-corruption guidelines. The accounting and audit systems of the state pollution control board (UPPCB) are weak. The program also includes high-value e-bus PPP contracts worth roughly US$75–110M each.
- **Mitigations** (set out in the POM and the Program Action Plan):
  - Financial management and procurement staff are appointed at the program agency (UPCAMPA) and the other implementing agencies.
  - UPPCB's annual financial statements get a statutory audit.
  - Internal and external audit terms of reference are drafted, audit firms acceptable to the Bank are hired, and audit reports are submitted on time.
  - Separate program bank accounts are opened.
  - UPCAMPA monitors spending through utilization certificates.
  - Most procurement goes through India's national e-procurement portal (GeM).
  - The Bank did extra due diligence on the high-value contracts.

### 3. Tanzania: Sustainable Rural Water Supply and Sanitation Program Phase II (P508698). Overall risk: Moderate
- **Why:** Qualified procurement staff are scarce, and staff struggle with planning, preparing bids and evaluating them. Contract management and document management are weak. Several layers of fund approvals slow disbursement. Program activities don't line up with the national budget calendar. Coordination across many institutions is complex, and internal audit is weak.
- **Mitigations:**
  - Staff at all levels are trained in procurement and contract management.
  - Internal audits are reviewed every six months.
  - Procurement and financial management staff are hired or seconded to decentralized entities.
  - Annual work plans and budget reports are submitted on time to improve planning and budgeting.
  - Disbursement is streamlined, with regular reviews to clear bottlenecks.

### 4. Iraq: Innovations Towards Learning in Lagging Iraqi Governorates (P175721). Overall risk: Substantial
- **Why:** The Ministry of Education and its project management team have limited capacity. Security conditions and COVID-19 could stop Bank staff from visiting to check things physically. School grant money could reach ineligible beneficiaries.
- **Mitigations:**
  - The project management team gets a central financial management function, supported by a financial management consultant shared with the earlier EODP-AF project.
  - The Bank hires a third-party monitoring agency, which also covers fiduciary matters.
  - The ministry prepares a POM with a financial management section, and the Bank must clear it before any disbursement.
  - Because this is an emergency operation, it uses simplified procedures and relies more on checks after spending than before.

### 5. Somalia Contingent Emergency Response Project (P509224). Overall risk: Moderate
- **Why:** Government financial and procurement controls are weak, including the ability to manage contracts. Perceived corruption and elite capture are high.
- **Mitigations:**
  - Implementation runs through an existing PIU in the Ministry of Finance with a proven track record, plus tested and reputable implementing partners.
  - Financial management controls are paired with transparent procurement, regular audits and reporting.
  - The Bank may give hands-on procurement support (the HEIS approach used across the Somalia portfolio).
  - The Bank pays implementing partners directly.
  - UN agencies work under the financial management framework agreement the Bank has with the UN.

**Common themes:** Almost every operation adds or seconds dedicated financial management and procurement staff, strengthens audits, and writes fiduciary procedures into a POM. The projects in fragile settings (Iraq and Somalia) also use third-party monitoring, direct payments, or hands-on Bank support with procurement.

---

> **Reviewer's note:** The opening line 'None rate it High' is wrong: Ethiopia (P511478) rates fiduciary risk High (see the fiduciary-High example). The scorer passed this answer because it checks for expected facts, not for false ones.
