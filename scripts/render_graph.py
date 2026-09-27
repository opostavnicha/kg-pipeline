"""Render the operations layer of the graph (operations, financiers, implementing agencies, countries, the MPA
program and its phases, risk categories) as an interactive HTML page and a static PNG in docs/img/.

The document layer (sections, tables, chunks, acronyms) is left out. HAS_RISK edges are drawn for High ratings
only unless --all-risks is given.

Requires the optional viz dependencies for the PNG:  uv sync --extra viz
Usage: python scripts/render_graph.py [--all-risks] [--out-dir docs/img]
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kg_pipeline import retrieve as R  # noqa: E402

STYLE = {  # kind -> (colour, size, shape for vis-network)
    "operation": ("#1f6feb", 26, "dot"),
    "stub": ("#8fb5f5", 14, "dot"),
    "financier": ("#2da44e", 18, "diamond"),
    "category": ("#a2d9b1", 12, "diamond"),
    "agency": ("#bf8700", 14, "square"),
    "country": ("#8250df", 18, "triangle"),
    "program": ("#cf222e", 24, "star"),
    "phase": ("#f5a3a3", 10, "dot"),
    "risk": ("#6e7781", 16, "hexagon"),
}
EDGE_COLOUR = {"FINANCES": "#2da44e", "IMPLEMENTS": "#bf8700", "LOCATED_IN": "#8250df", "HAS_PHASE": "#cf222e",
               "HAS_OPERATION": "#cf222e", "HAS_RISK": "#d1242f"}


def fetch(all_risks: bool) -> tuple[dict, list]:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def node(key, label, kind, title=""):
        nodes.setdefault(key, {"id": key, "label": label, "kind": kind, "title": title})

    with R.driver().session(database=R.database()) as s:
        for r in s.run("MATCH (o:Operation) RETURN o.project_id AS id, o.name AS name, o.country AS country, "
                       "coalesce(o.stub, false) AS stub, o.overall_risk AS risk"):
            label = r["id"] if r["stub"] else f"{r['id']}\n{r['country']}"
            node(f"op:{r['id']}", label, "stub" if r["stub"] else "operation",
                 f"{r['id']}: {r['name'] or 'MPA phase (ID only)'}" + (f" · overall risk {r['risk']}" if r["risk"] else ""))
        for r in s.run("MATCH (g:Organization)-[f:FINANCES]->(o:Operation) RETURN g.name AS g, coalesce(g.is_category,false) AS cat, "
                       "o.project_id AS o, f.amount_usd AS usd, f.orig_currency AS cur, f.orig_amount AS orig, f.detail AS detail"):
            counterpart = (r["detail"] or "") in ("Borrower/Recipient", "National Government", "Government")
            node(f"org:{r['g']}", short_org(r["g"]) + ("\n(counterpart)" if counterpart else ""), "category" if r["cat"] else "financier", r["g"])
            amount = f"US${r['usd'] / 1e6:,.1f}M" + (f" ({r['cur']} {r['orig'] / 1e6:,.1f}M)" if r["cur"] and r["cur"] != "USD" else "")
            edges.append({"from": f"org:{r['g']}", "to": f"op:{r['o']}", "type": "FINANCES", "label": amount, "title": f"{r['detail']}: {amount}"})
        for r in s.run("MATCH (g:Organization)-[:IMPLEMENTS]->(o:Operation) RETURN g.name AS g, o.project_id AS o"):
            node(f"org:{r['g']}", short_org(r["g"]), "agency", r["g"])
            edges.append({"from": f"org:{r['g']}", "to": f"op:{r['o']}", "type": "IMPLEMENTS"})
        for r in s.run("MATCH (o:Operation)-[:LOCATED_IN]->(l:Location) RETURN o.project_id AS o, l.name AS l, l.region AS region"):
            node(f"loc:{r['l']}", r["l"], "country", f"{r['l']} · {r['region']}")
            edges.append({"from": f"op:{r['o']}", "to": f"loc:{r['l']}", "type": "LOCATED_IN"})
        for r in s.run("MATCH (p:Program)-[:HAS_PHASE]->(ph:Phase) OPTIONAL MATCH (ph)-[:HAS_OPERATION]->(o:Operation) "
                       "RETURN p.program_id AS p, p.name AS pname, p.phases AS n, p.financing_envelope_usd AS env, "
                       "ph.phase_id AS ph, ph.phase AS num, ph.label AS label, ph.instrument AS instr, o.project_id AS o"):
            node(f"prog:{r['p']}", f"MPA {r['p']}\n{r['n']} phases", "program",
                 f"{r['pname']} · {r['n']} phases · envelope US${(r['env'] or 0) / 1e6:,.1f}M")
            node(f"ph:{r['ph']}", f"Phase {r['num']}", "phase", f"Phase {r['num']}: {r['label']} ({r['instr']})")
            edges.append({"from": f"prog:{r['p']}", "to": f"ph:{r['ph']}", "type": "HAS_PHASE"})
            if r["o"]:
                edges.append({"from": f"ph:{r['ph']}", "to": f"op:{r['o']}", "type": "HAS_OPERATION"})
        q = "MATCH (o:Operation)-[h:HAS_RISK]->(k:Risk) " + ("" if all_risks else "WHERE h.rating = 'High' ") + \
            "RETURN o.project_id AS o, k.name AS k, h.rating AS rating"
        for r in s.run(q):
            node(f"risk:{r['k']}", r["k"].replace(" and ", " & ")[:28], "risk", f"Risk category: {r['k']}")
            edges.append({"from": f"op:{r['o']}", "to": f"risk:{r['k']}", "type": "HAS_RISK", "label": r["rating"], "title": f"{r['k']}: {r['rating']}"})
    R.driver().close()
    return nodes, edges


def short_org(name: str) -> str:
    """'International Development Association (IDA)' -> 'IDA'; long names are shortened."""
    if name.endswith(")") and "(" in name:
        return name[name.rindex("(") + 1 : -1]
    return name if len(name) <= 30 else name[:28] + "…"


def write_html(nodes: dict, edges: list, path: Path) -> None:
    vis_nodes = [{"id": n["id"], "label": n["label"], "title": n["title"], "color": STYLE[n["kind"]][0],
                  "size": STYLE[n["kind"]][1], "shape": STYLE[n["kind"]][2], "group": n["kind"]} for n in nodes.values()]
    vis_edges = [{"from": e["from"], "to": e["to"], "label": e.get("label", ""), "title": e.get("title", e["type"]),
                  "color": EDGE_COLOUR[e["type"]], "arrows": "to", "font": {"size": 9, "align": "middle"}} for e in edges]
    legend = "".join(f'<span style="color:{c}">&#9679;</span> {k} &nbsp; ' for k, (c, _, _) in STYLE.items())
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Operations graph</title>
<script src="https://cdn.jsdelivr.net/npm/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>body{{margin:0;font-family:system-ui,sans-serif}}#g{{width:100vw;height:92vh}}header{{padding:8px 12px;font-size:13px}}</style>
</head><body>
<header><b>kg-pipeline · operations layer</b> — {len(nodes)} nodes, {len(edges)} relationships (hover for details). {legend}</header>
<div id="g"></div>
<script>
const nodes = new vis.DataSet({json.dumps(vis_nodes, ensure_ascii=False)});
const edges = new vis.DataSet({json.dumps(vis_edges, ensure_ascii=False)});
new vis.Network(document.getElementById("g"), {{nodes, edges}}, {{
  physics: {{solver: "forceAtlas2Based", stabilization: {{iterations: 400}}}},
  nodes: {{font: {{size: 12, multi: true}}}}, edges: {{smooth: {{type: "continuous"}}}},
  interaction: {{hover: true, tooltipDelay: 100}}
}});
</script></body></html>
"""
    path.write_text(html)


def write_png(nodes: dict, edges: list, path: Path) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import networkx as nx
    except ImportError:
        print("  PNG skipped: install the viz extra (uv sync --extra viz)")
        return
    g = nx.DiGraph()
    for key, n in nodes.items():
        g.add_node(key, **n)
    for e in edges:
        g.add_edge(e["from"], e["to"], type=e["type"])
    # Kamada-Kawai spaces clusters evenly; a short spring pass from there separates overlapping labels.
    pos = nx.spring_layout(g, pos=nx.kamada_kawai_layout(g.to_undirected()), k=0.35, iterations=60, seed=7)
    fig, ax = plt.subplots(figsize=(18, 13), dpi=110)
    for etype, colour in EDGE_COLOUR.items():
        es = [(u, v) for u, v, d in g.edges(data=True) if d["type"] == etype]
        nx.draw_networkx_edges(g, pos, edgelist=es, edge_color=colour, alpha=0.55, arrows=True, arrowsize=8, width=1.1, ax=ax)
    for kind, (colour, size, _) in STYLE.items():
        ns = [k for k, n in nodes.items() if n["kind"] == kind]
        nx.draw_networkx_nodes(g, pos, nodelist=ns, node_color=colour, node_size=size * 22, label=kind, ax=ax, edgecolors="white")
    labels = {k: n["label"] for k, n in nodes.items() if n["kind"] not in ("phase", "stub")}
    nx.draw_networkx_labels(g, pos, labels, font_size=7.5, ax=ax)
    ax.legend(scatterpoints=1, fontsize=9, loc="lower left", frameon=False)
    ax.set_title(f"Operations layer: {len(nodes)} nodes, {len(edges)} relationships", fontsize=13)
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--all-risks", action="store_true", help="draw every HAS_RISK edge, not only High ratings")
    ap.add_argument("--out-dir", default=str(ROOT / "docs" / "img"))
    args = ap.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    nodes, edges = fetch(args.all_risks)
    write_html(nodes, edges, out / "operations_graph.html")
    write_png(nodes, edges, out / "operations_graph.png")
    print(f"{len(nodes)} nodes, {len(edges)} relationships -> {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}/operations_graph.(html|png)")


if __name__ == "__main__":
    main()
