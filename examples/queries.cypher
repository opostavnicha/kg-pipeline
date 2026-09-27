// Example read-only queries (run in Neo4j Browser, or pass to the graph_query MCP tool).

// Which operations rate a SORT risk category High?
MATCH (o:Operation)-[r:HAS_RISK]->(k:Risk)
WHERE r.rating = 'High'
RETURN k.name AS risk, collect(o.project_id + ' ' + o.country) AS operations
ORDER BY risk;

// Financing by source, with the original loan currency
MATCH (g:Organization)-[f:FINANCES]->(o:Operation)
RETURN o.project_id, o.country, g.name AS source, f.detail, f.amount_usd, f.orig_currency, f.orig_amount
ORDER BY o.project_id, f.amount_usd DESC;

// Financiers that fund more than one operation (category nodes excluded)
MATCH (g:Organization)-[:FINANCES]->(o:Operation)
WHERE NOT coalesce(g.is_category, false)
WITH g, collect(DISTINCT o.project_id) AS ops
WHERE size(ops) > 1
RETURN g.name, ops;

// MPA program, all its phases (with or without a project ID) and the envelope
MATCH (p:Program)-[:HAS_PHASE]->(ph:Phase)
OPTIONAL MATCH (ph)-[:HAS_OPERATION]->(o:Operation)
RETURN p.program_id, p.phases, p.financing_envelope_usd, ph.phase, ph.label, ph.instrument,
       ph.ida_usd, ph.ibrd_usd, ph.other_usd, ph.estimated_approval, o.project_id
ORDER BY ph.phase;

// What does an acronym stand for, per document?
MATCH (d:Document)-[x:DEFINES]->(a:Acronym {name: 'I3RF'})
MATCH (d)-[:DESCRIBES]->(o:Operation)
RETURN o.project_id, x.expansion;

// The section tree of one document
MATCH (o:Operation {project_id: 'P175721'})<-[:DESCRIBES]-(d:Document)
MATCH (d)-[:HAS_SECTION]->(top:Section)
OPTIONAL MATCH (top)-[:HAS_SUBSECTION*]->(sub:Section)
RETURN top.order, top.title, top.type, collect(sub.title) AS subsections
ORDER BY top.order;

// Chunks of one section, in reading order
MATCH (s:Section {subtype: 'key_risks'})<-[:HAS_SECTION|HAS_SUBSECTION*]-(:Document)-[:DESCRIBES]->(:Operation {project_id: 'P511478'})
MATCH (s)-[:HAS_CHUNK]->(c:Chunk)
RETURN c.order, c.kind, left(c.text, 200) AS text
ORDER BY c.order;
