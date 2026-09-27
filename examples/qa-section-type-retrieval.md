# What lessons from previous projects shaped the Iraq project's design?

*Retrieval filtered to one operation and one section type.*

Run `20260927-142623` · question `e08` (retrieve) · scored **PASS** · 20 s · 2 MCP tool call(s)

Test output from this corpus, not a benchmark; see the note in the [README](../README.md).

Claude Code first loaded the MCP tool definitions (ToolSearch).

## 1. `graph_query`

```cypher
MATCH (o:Operation)-[:LOCATED_IN]->(l:Location) WHERE l.iso2='IQ' OR toLower(l.name_norm) CONTAINS 'iraq' RETURN o.project_id, o.name, o.title LIMIT 20
```

**Result (trimmed):**

1 row(s):

| o.project_id | o.name | o.title |
|---|---|---|
| P175721 | Innovations Towards Learning in Lagging Iraqi Governorates | null |

## 2. `retrieve`

```json
{
  "question": "lessons learned from previous projects reflected in project design",
  "countries": [
    "Iraq"
  ],
  "section_types": [
    "lessons"
  ],
  "k": 15
}
```

**Result (trimmed):**

- #1 `P175721` · II. PROJECT DESCRIPTION > F. Lessons Learned and Reflected in the Project Design > B. Lessons learned from previous and ongoing Education Projects and Interventions in Iraq (lessons_learned, text) — 60. Flexibility and simplicity in project design need to be emphasized so that the outcomes are more achievable in a FCV context. Development progress is rarely predictable or linear, and challenges cannot be fully mitigated without relevant, quick adjustments (“Lessons learned” …
- #2 `P175721` · II. PROJECT DESCRIPTION > F. Lessons Learned and Reflected in the Project Design > B. Lessons learned from previous and ongoing Education Projects and Interventions in Iraq (lessons_learned, text) — at home. 64. Decentralization reforms, such as SBM, can help minimize risks and improve local services . UNICEF Iraq’s SBM programme supported school management committees in priority governorates to develop School Improvement Plans with parent-teacher associations . Day-to-day m…
- #3 `P175721` · II. PROJECT DESCRIPTION > F. Lessons Learned and Reflected in the Project Design > A. Global lessons and latest research on supporting foundational skills, teachers and school-focused interventions to (lessons_learned, text) — et al., 2016; Piper, Zuilkowski, & Mugenda, 2014). See also Darling-Hammond 2000; Hanushek 2005; Mourshed and others 2017; Leithwood, Harris, and Hopkins 2008; Leithwood and Mascall 2008. See Evans and Popova 2015 See for example Camminatiello et al 2012, Çankaya et al 2020. coor…
- #4 `P175721` · II. PROJECT DESCRIPTION > F. Lessons Learned and Reflected in the Project Design > A. Global lessons and latest research on supporting foundational skills, teachers and school-focused interventions to (lessons_learned, text) — of international evidence shows that teacher effectiveness is the most important school- related factor influencing student achievement, with some pedagogical techniques such as teaching-at-the-right-level, or adaptive instruction, being particularly effective in supporting stude…
- …1 more

## Answer

The Iraq operation in the knowledge graph is **Innovations Towards Learning in Lagging Iraqi Governorates (P175721)**. Section II.F of its appraisal document, "Lessons Learned and Reflected in the Project Design", lists lessons from earlier education projects in Iraq, each tied to a design choice:

| Lesson | Source project | How it shaped the design |
|---|---|---|
| **Keep the design flexible and simple.** In fragile and conflict-affected settings, progress is rarely predictable and quick adjustments are needed. | Iraq Emergency School Construction and Rehabilitation Project (P087907) | The scope is deliberately narrow: only primary schools, only in the three most lagging governorates, with three key outcomes in the project development objective. |
| **Keep the long-term goal of building capacity while also producing visible results.** | Emergency Textbook Provision Project (P088945) | School-based management (SBM) stays central, to encourage community engagement and strengthen the education system. Every activity includes capacity building along the delivery chain, from the Ministry of Education (MOED) to the directorates of education to schools. |
| **Quality measures like teacher training are very hard to carry out unless the MOED is actively involved.** | Iraq Third Emergency Education Project (P096234) | The project stresses capacity building for school leaders and the MOED, including designing the monitoring and evaluation system and training school management committees. |
| **Build on materials that have already been tested, to keep technical complexity low.** | EODP-AF project; Save the Children governorate pilot | Primary teacher training materials build on those being developed under EODP-AF. The project also reuses communication channels that the Save the Children pilot set up to support learning at home. |
| **Decentralization reforms like SBM reduce risk and improve local services.** | UNICEF Iraq SBM pilot (2016–2019, 1,027 schools) | The project scales this pilot up to more schools and governorates. The pilot's impact evaluation found a better learning environment, more parental support and better coordination. This led to school grants for urgent supplies, reading materials and communication with parents. |

The same section also draws on global research, which shaped the design in two more ways:
- **Evidence that teaching quality matters most.** Component 1 focuses on Arabic and mathematics teachers, with coaching, classroom support and peer learning.
- **Evidence that early literacy and numeracy are critical.** Baseline tests found that 29% of grade 3 students couldn't answer any reading comprehension question (EGRA), and 41% couldn't solve a single subtraction problem (EGMA).
