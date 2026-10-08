#!/usr/bin/env python3
"""Render the SQL-Agent architecture as a self-contained SVG."""
from pathlib import Path
from xml.sax.saxutils import escape

OUTPUT = Path(__file__).resolve().parents[2] / "docs/assets/workflow.svg"


def node(x, y, n, title, lines, tone="blue", w=190, h=106):
    colors = {"blue": ("#F7FAFF", "#B9D0F5", "#2457A7"), "amber": ("#FFFBF2", "#F0CD82", "#A95D08"), "violet": ("#FBF8FF", "#D8C5F2", "#7041A6"), "green": ("#F5FBF8", "#A9D8BF", "#217A4B")}
    fill, stroke, accent = colors[tone]
    out = [f'<g class="node"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill}" stroke="{stroke}"/>', f'<circle cx="{x+22}" cy="{y+23}" r="12" fill="{accent}"/>', f'<text x="{x+22}" y="{y+27}" text-anchor="middle" class="step">{n}</text>', f'<text x="{x+43}" y="{y+29}" class="node-title">{escape(title)}</text>']
    out += [f'<text x="{x+18}" y="{y+59+i*19}" class="node-copy">{escape(line)}</text>' for i, line in enumerate(lines)]
    return "".join(out) + "</g>"


def box(x, y, title, lines, tone="amber", w=190, h=76):
    colors = {"amber": ("#FFFBF2", "#F0CD82", "#A95D08"), "violet": ("#FBF8FF", "#D8C5F2", "#7041A6"), "green": ("#F5FBF8", "#A9D8BF", "#217A4B")}
    fill, stroke, accent = colors[tone]
    out = [f'<g><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" stroke="{stroke}"/>', f'<rect x="{x}" y="{y}" width="4" height="{h}" rx="2" fill="{accent}"/>', f'<text x="{x+17}" y="{y+27}" class="branch-title">{escape(title)}</text>']
    out += [f'<text x="{x+17}" y="{y+49+i*17}" class="branch-copy">{escape(line)}</text>' for i, line in enumerate(lines)]
    return "".join(out) + "</g>"


def edge(points, dashed=False):
    pts = " ".join(f"{x},{y}" for x, y in points)
    dash = ' stroke-dasharray="6 6"' if dashed else ""
    return f'<polyline points="{pts}" class="edge"{dash}/>'


def main():
    items = []
    xs = [55, 275, 495, 715, 935, 1155, 1375]
    specs = [("Request", ["Browser · API · MCP", "durable job + worker"]), ("Ground", ["Allowlisted schema", "optional scoped RAG"]), ("Plan", ["SQL · clarify · stop", "structured decision"]), ("Authorize", ["Database + table scope", "read-only SQL + limits"]), ("Execute", ["Bounded DB call", "timeout + row limits"]), ("Verify", ["Independent SQL", "result agreement signal"]), ("Review", ["SQL · result · trace", "accept or reject"])]
    items += [node(x, 190, str(i+1), title, lines) for i, (x, (title, lines)) in enumerate(zip(xs, specs))]
    items += [edge([(x+190, 243), (x+220, 243)]) for x in xs[:-1]]

    items += [box(xs[2], 350, "Clarification", ["checkpoint · answer · resume"]), box(xs[4], 350, "Execution repair", ["eligible errors · bounded retries"]), box(xs[5], 350, "Semantic repair", ["optional · verifier-guided retry"])]
    items += [edge([(xs[2]+95,296),(xs[2]+95,350)], True), edge([(xs[4]+95,296),(xs[4]+95,350)], True), edge([(xs[5]+95,296),(xs[5]+95,350)], True)]
    items += [edge([(xs[4],388),(850,388),(850,316),(590,316),(590,296)], True), edge([(xs[5]+190,388),(1355,388),(1355,316),(1470,316),(1470,296)], True)]

    mx = [295, 625, 955, 1285]
    ms = [("Mutation policy", "validate operation and scope"), ("Impact preview", "show operation and affected scope"), ("Explicit approval", "approve before any write"), ("Transactional write", "idempotency · receipt · rollback")]
    items += [edge([(685,270),(700,270),(700,490),(500,490),(500,590)])]
    items += [box(x,590,t,[s],"violet",220,82) for x,(t,s) in zip(mx,ms)]
    items += [edge([(x+220,631),(x+330,631)]) for x in mx[:-1]]

    ex = [75, 375, 675, 975, 1275]
    es = [("Official Mini-Dev", "500 questions · evidence · schema"), ("Agent inference", "same guarded read workflow"), ("Candidate execution", "generated SQL result"), ("Execution scorer", "candidate result vs gold result"), ("Evidence", "accuracy · cost · p95 · pairs")]
    items += [box(x,850,t,[s],"green",230,82) for x,(t,s) in zip(ex,es)]
    items += [edge([(x+230,891),(x+300,891)]) for x in ex[:-1]]
    items += [box(760,965,"Isolated gold SQL",["available only after inference"],"green",230,72), edge([(875,965),(875,947),(1090,947),(1090,932)],True)]

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1620" height="1100" viewBox="0 0 1620 1100" role="img" aria-labelledby="title desc">
<title id="title">SQL-Agent system architecture</title><desc id="desc">A left-to-right LangGraph SQL agent with durable requests, schema grounding, planning, policy-gated execution, bounded recovery, advisory verification, human review, approved writes, and isolated BIRD evaluation.</desc>
<defs><filter id="shadow" x="-10%" y="-20%" width="120%" height="150%"><feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="#0F172A" flood-opacity="0.08"/></filter><marker id="arrow" markerWidth="9" markerHeight="7" refX="8" refY="3.5" orient="auto"><polygon points="0 0,9 3.5,0 7" fill="#718096"/></marker><style>
text{{font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}.title{{font-size:34px;font-weight:720;fill:#172033}}.subtitle{{font-size:15px;fill:#64748B}}.lane-title{{font-size:13px;font-weight:700;letter-spacing:1.8px;fill:#64748B}}.node{{filter:url(#shadow)}}.step{{font-size:12px;font-weight:700;fill:white}}.node-title{{font-size:16px;font-weight:700;fill:#172033}}.node-copy{{font-size:12.5px;fill:#536176}}.branch-title{{font-size:14px;font-weight:700;fill:#273449}}.branch-copy{{font-size:12px;fill:#64748B}}.edge{{fill:none;stroke:#718096;stroke-width:1.7;marker-end:url(#arrow)}}.note{{font-size:12px;fill:#64748B}}
</style></defs><rect width="1620" height="1100" fill="#FFF"/>
<text x="55" y="54" class="title">SQL-Agent</text><text x="55" y="82" class="subtitle">Grounded planning, controlled execution, bounded recovery, and reproducible evaluation</text>
<rect x="35" y="125" width="1550" height="610" rx="18" fill="#FCFDFF" stroke="#DCE6F5"/><text x="55" y="157" class="lane-title">ONLINE AGENT WORKFLOW</text>
<text x="55" y="178" class="note">Shared request and planning stages · reads execute before result review · writes require prior approval</text>
<rect x="55" y="525" width="1510" height="190" rx="14" fill="#FEFCFF" stroke="#E7DDF4"/><text x="75" y="557" class="lane-title">WRITE BRANCH · HUMAN APPROVAL REQUIRED</text>
<rect x="35" y="780" width="1550" height="275" rx="18" fill="#FBFEFC" stroke="#D5EBDD"/><text x="55" y="812" class="lane-title">OFFLINE BIRD MINI-DEV EVALUATION</text>
{''.join(items)}<text x="55" y="1078" class="note">Dashed lines show interrupts or bounded retry paths. Verifier agreement is evidence, not a correctness guarantee.</text></svg>'''
    OUTPUT.write_text(svg, encoding="utf-8")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
