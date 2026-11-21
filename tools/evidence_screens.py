"""Render evidence pages of what the Brain agent actually did, then screenshot them with headless Chrome.

Every number comes from the agent's saved state (state/brain_graph.json) and from the real stdout of
`python -m sentinel.brain bootstrap` / pytest. Nothing is typed in by hand.

  python tools/evidence_screens.py        -> docs/screenshots/*.png (+ the .html sources)
"""
from __future__ import annotations

import html
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
STATE = ROOT / "state" / "brain_graph.json"
CHROME = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
          r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]

# status palette (fixed roles) + neutral ink, from the dataviz reference palette
CSS = """
.viz-root{--surface-1:#fcfcfb;--surface-2:#f4f3f0;--text-primary:#0b0b0b;--text-secondary:#52514e;
 --text-muted:#8a8984;--grid:#e4e3df;--good:#0ca30c;--good-text:#006300;--warning:#fab219;--serious:#ec835a;
 --critical:#d03b3b;--neutral:#a3a29d;--accent:#2a78d6}
*{box-sizing:border-box}body{margin:0;background:var(--surface-1);font:14px/1.45 "Segoe UI",system-ui,sans-serif;
 color:var(--text-primary)}
.viz-root{padding:28px 36px}
header{border-bottom:1px solid var(--grid);padding-bottom:12px;margin-bottom:18px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:22px 0 8px;color:var(--text-secondary);font-weight:600}
.sub{color:var(--text-secondary);font-size:13px}
.tag{display:inline-block;font-size:12px;padding:1px 8px;border-radius:10px;background:var(--surface-2);
 color:var(--text-secondary);margin-right:6px}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;color:var(--text-secondary);font-weight:600;border-bottom:1px solid var(--grid);padding:6px 8px}
td{border-bottom:1px solid var(--grid);padding:6px 8px;vertical-align:top}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
code{font:12.5px Consolas,monospace;background:var(--surface-2);padding:1px 4px;border-radius:3px}
.chip{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:600;white-space:nowrap}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block}
.legend{display:flex;gap:18px;flex-wrap:wrap;font-size:12.5px;color:var(--text-secondary);margin:6px 0 10px}
.legend span{display:inline-flex;align-items:center;gap:6px}
pre{background:#1a1a19;color:#f0efec;padding:14px 16px;border-radius:6px;font:12.5px/1.5 Consolas,monospace;
 white-space:pre-wrap;margin:0}
.kpis{display:flex;gap:14px;margin:4px 0 6px}
.kpi{background:var(--surface-2);border-radius:8px;padding:10px 16px;min-width:150px}
.kpi b{display:block;font-size:24px}.kpi small{color:var(--text-secondary)}
"""

STATUS = {  # role -> (css var, icon, label)
    "validated": ("--good", "✓", "validated"),
    "seed": ("--neutral", "·", "seed (untested / weak)"),
    "needs_review": ("--serious", "!", "needs review"),
    "rejected": ("--critical", "✗", "rejected"),
    "proposed": ("--warning", "?", "proposed"),
}
HEADER_NOTE = ("Agent: Brain (person B) · step completed: build & validate the metric graph · "
               "data: dev stand-in for Data (dev/fake_data.py) following the Kickoff-spec baseline · "
               "monitoring/investigation not built yet")


def chip(status: str) -> str:
    var, icon, label = STATUS[status]
    return f'<span class="chip"><span class="dot" style="background:var({var})"></span>{icon} {label}</span>'


def page(title: str, sub: str, body: str) -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<style>{CSS}</style></head><body><div class='viz-root'><header><h1>{html.escape(title)}</h1>"
            f"<div class='sub'>{html.escape(sub)}</div><div class='sub' style='margin-top:6px'>"
            f"<span class='tag'>evidence</span>{html.escape(HEADER_NOTE)}</div></header>{body}</div></body></html>")


# ====================================================================== page 1: the graph the agent built
def graph_page(g: dict) -> tuple[str, int]:
    nodes = {n["id"]: n for n in g["nodes"]}
    edges = [e for e in g["edges"] if e["type"] in ("formula", "causal")]
    # layer = shortest upstream distance from a root (formula outputs that feed nothing)
    fed = {e["src"] for e in edges if e["type"] == "formula"}
    outs = {e["dst"] for e in edges if e["type"] == "formula"}
    roots = sorted(outs - fed)
    depth = {r: 0 for r in roots}
    frontier = list(roots)
    while frontier:
        nxt = []
        for cur in frontier:
            for e in edges:
                if e["dst"] == cur and e["src"] not in depth:
                    depth[e["src"]] = depth[cur] + 1
                    nxt.append(e["src"])
        frontier = nxt
    for n in nodes:
        depth.setdefault(n, max(depth.values()) if depth else 0)
    layers: dict[int, list[str]] = {}
    for n, d in sorted(depth.items(), key=lambda x: (x[1], x[0])):
        layers.setdefault(d, []).append(n)
    W, BW, BH, VG = 1500, 168, 40, 92
    pos = {}
    for d, ids in layers.items():
        # keep children near their parents: order by mean x of their downstream nodes
        if d > 0:
            def px(i):
                xs = [pos[e["dst"]][0] for e in edges if e["src"] == i and e["dst"] in pos]
                return sum(xs) / len(xs) if xs else W
            ids.sort(key=px)
        gap = W / (len(ids) + 1)
        for k, i in enumerate(ids):
            pos[i] = (gap * (k + 1), 40 + d * VG)
    H = 40 + (max(layers) + 1) * VG + 20
    svg = [f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="Metric graph: formula and causal edges coloured by validation status">',
           '<defs>' + "".join(
               f'<marker id="a-{s}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
               f'orient="auto-start-reverse"><path d="M0,0L10,5L0,10z" fill="var({STATUS[s][0]})"/></marker>'
               for s in STATUS) + '</defs>']
    for e in edges:
        if e["src"] not in pos or e["dst"] not in pos:
            continue
        (x1, y1), (x2, y2) = pos[e["src"]], pos[e["dst"]]
        y1 -= BH / 2
        y2 += BH / 2
        st = e["status"]
        dash = ' stroke-dasharray="6 5"' if e["type"] == "causal" else ""
        my = (y1 + y2) / 2
        tip = html.escape(f'{e["type"]}: {e["src"]} → {e["dst"]} · {st} · {e.get("reason", "")}')
        svg.append(f'<path d="M{x1},{y1} C{x1},{my} {x2},{my} {x2},{y2}" fill="none" stroke="var({STATUS[st][0]})" '
                   f'stroke-width="2"{dash} marker-end="url(#a-{st})" opacity="0.9"><title>{tip}</title></path>')
    for i, (x, y) in pos.items():
        n = nodes[i]
        root = i in roots
        svg.append(f'<g><title>{html.escape(n["name"] + " — " + n["description"])}</title>'
                   f'<rect x="{x - BW / 2}" y="{y - BH / 2}" width="{BW}" height="{BH}" rx="6" fill="#fff" '
                   f'stroke="{"var(--text-primary)" if root else "var(--grid)"}" stroke-width="{2 if root else 1.5}"/>'
                   f'<text x="{x}" y="{y - 3}" text-anchor="middle" font-size="12.5" font-weight="600" '
                   f'fill="var(--text-primary)">{html.escape(i)}</text>'
                   f'<text x="{x}" y="{y + 12}" text-anchor="middle" font-size="11" fill="var(--text-secondary)">'
                   f'{n["kind"]} · {n["unit"]} · {len(n["dimensions"])} dims</text></g>')
    svg.append("</svg>")
    counts = {}
    for e in g["edges"]:
        counts.setdefault(e["type"], {}).setdefault(e["status"], 0)
        counts[e["type"]][e["status"]] += 1
    kpi = "".join(f'<div class="kpi"><b>{sum(v.values())}</b><small>{t} edges · '
                  + ", ".join(f"{c} {s}" for s, c in sorted(v.items())) + "</small></div>"
                  for t, v in counts.items())
    legend = ('<div class="legend"><span><svg width="34" height="8"><line x1="0" y1="4" x2="34" y2="4" '
              'stroke="var(--text-secondary)" stroke-width="2"/></svg>formula (exact identity)</span>'
              '<span><svg width="34" height="8"><line x1="0" y1="4" x2="34" y2="4" stroke="var(--text-secondary)" '
              'stroke-width="2" stroke-dasharray="6 5"/></svg>causal (hypothesis)</span>'
              + "".join(f"<span>{chip(s)}</span>" for s in ("validated", "seed", "needs_review")) +
              '<span>bold box = root of the driver tree</span><span>arrow = driver → driven</span></div>')
    body = (f'<div class="kpis"><div class="kpi"><b>{len(nodes)}</b><small>metrics</small></div>{kpi}</div>'
            f"{legend}{''.join(svg)}")
    return page("1 · The metric graph the agent built and validated",
                "29 seed metrics; every formula and causal edge coloured by the verdict the agent reached on data",
                body), H + 330


# ====================================================================== page 2: formula identities
def formula_page(g: dict) -> tuple[str, int]:
    rows = []
    for out, expr in g["formulas"].items():
        es = [e for e in g["edges"] if e["type"] == "formula" and e["dst"] == out]
        st = es[0].get("stats", {}) or {}
        status = es[0]["status"]
        reason = es[0].get("reason", "")
        if st:
            slices = f'{round(st["passing_share"] * st["slices"])}/{st["slices"]}'
            err = f'{st["worst_median_rel_err"]:.1e}'
            dims = ", ".join(st.get("slice_dims", [])) or "total"
        else:
            slices, err, dims = "–", "–", "–"
        rows.append(f"<tr><td><code>{html.escape(out)} = {html.escape(expr)}</code></td><td>{dims}</td>"
                    f"<td class='num'>{slices}</td><td class='num'>{err}</td><td>{chip(status)}"
                    f"<div class='sub'>{html.escape(reason)}</div></td></tr>")
    bd = [e for e in g["edges"] if e["type"] == "breakdown"]
    ok = sum(e["status"] == "validated" for e in bd)
    body = ("<table><thead><tr><th>Formula (seed, graph/formulas.yaml)</th><th>Slice dimensions tested</th>"
            "<th class='num'>Slices passing</th><th class='num'>Worst median rel. error</th><th>Agent verdict</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
            f"<h2>Breakdown edges</h2><div class='sub'>{ok}/{len(bd)} breakdown edges validated: for every metric and "
            "dimension, slice values (counts) or slice numerators and denominators (ratios) sum to the total.</div>"
            "<h2>Rule</h2><div class='sub'>Kickoff spec §4: both sides computed from data; median relative error "
            "&lt; 0.5% in ≥ 95% of slices. Window: the 7 days before the live start, excluding the 2 freshest "
            "hours.</div>")
    return page("2 · Formula identities checked on data", "Each seed formula recomputed in every slice of the "
                "dimensions its inputs share", body), 900


# ====================================================================== page 3: causal tests
def causal_page(g: dict) -> tuple[str, int]:
    cs = [e for e in g["edges"] if e["type"] == "causal"]
    W, L, R, RH, TOP = 1420, 380, 360, 46, 46
    plot_w = W - L - R
    x = lambda r: L + (r + 1) / 2 * plot_w
    H = TOP + len(cs) * RH + 50
    s = [f'<svg width="{W}" height="{H}" role="img" aria-label="Causal link tests: rank correlation per link">']
    s.append(f'<rect x="{x(-0.1)}" y="{TOP - 16}" width="{x(0.1) - x(-0.1)}" height="{len(cs) * RH + 8}" '
             f'fill="var(--surface-2)"/>')
    for t in (-1, -0.5, 0, 0.5, 1):
        s.append(f'<line x1="{x(t)}" y1="{TOP - 16}" x2="{x(t)}" y2="{TOP + len(cs) * RH - 8}" stroke="var(--grid)"'
                 f'{" stroke-width=2" if t == 0 else ""}/>'
                 f'<text x="{x(t)}" y="{TOP + len(cs) * RH + 12}" text-anchor="middle" font-size="12" '
                 f'fill="var(--text-muted)">{t:+.1f}</text>')
    s.append(f'<text x="{x(0)}" y="{TOP + len(cs) * RH + 32}" text-anchor="middle" font-size="12" '
             f'fill="var(--text-secondary)">rank correlation of residuals (best lag in the hypothesised range); '
             f'shaded = |ρ| &lt; 0.1, too weak to count</text>')
    s.append(f'<text x="{L - 10}" y="{TOP - 24}" text-anchor="end" font-size="12" font-weight="600" '
             f'fill="var(--text-secondary)">hypothesis (seed causal.yaml)</text>'
             f'<text x="{W - R + 16}" y="{TOP - 24}" font-size="12" font-weight="600" '
             f'fill="var(--text-secondary)">agent verdict</text>')
    for i, e in enumerate(cs):
        y = TOP + i * RH + 8
        st = e.get("stats", {}) or {}
        rho = st.get("rho", 0.0)
        reason = e.get("reason", "")
        verdict = reason.split(":")[0] if reason.startswith(("supported", "no clear")) else \
            ("confounded / overlap" if "co-moves" in reason else reason.split("(")[0])
        var = STATUS[e["status"]][0]
        lab = f'{e["src"]} → {e["dst"]}  ({e["sign"]})'
        s.append(f'<text x="{L - 10}" y="{y + 4}" text-anchor="end" font-size="13" fill="var(--text-primary)">'
                 f'{html.escape(lab)}</text>')
        if "rho_partial_on_numerator" in st:
            pr = st["rho_partial_on_numerator"]
            s.append(f'<line x1="{x(rho)}" y1="{y}" x2="{x(pr)}" y2="{y}" stroke="var(--serious)" stroke-width="2" '
                     f'stroke-dasharray="4 3"/><circle cx="{x(pr)}" cy="{y}" r="6" fill="#fff" stroke="var(--serious)" '
                     f'stroke-width="2"><title>partial ρ after controlling for the effect numerator: {pr:+.2f}</title>'
                     f'</circle><text x="{x(pr)}" y="{y - 11}" text-anchor="middle" font-size="11" '
                     f'fill="var(--text-secondary)">partial {pr:+.2f}</text>')
        s.append(f'<circle cx="{x(rho)}" cy="{y}" r="7" fill="var({var})" stroke="#fff" stroke-width="2">'
                 f'<title>{html.escape(reason)}</title></circle>'
                 f'<text x="{x(rho)}" y="{y - 11}" text-anchor="middle" font-size="11" fill="var(--text-secondary)">'
                 f'{rho:+.2f}</text>')
        s.append(f'<foreignObject x="{W - R + 10}" y="{y - 12}" width="{R - 10}" height="{RH}">'
                 f'<div xmlns="http://www.w3.org/1999/xhtml" style="font-size:12.5px">{chip(e["status"])} '
                 f'<span class="sub">conf {e["confidence"]:.2f} · lag {st.get("best_lag_h", "–")}h · '
                 f'{html.escape(st.get("mode", ""))}</span></div></foreignObject>')
    s.append("</svg>")
    notes = "".join(f"<tr><td><code>{html.escape(e['src'])} → {html.escape(e['dst'])}</code></td>"
                    f"<td>{html.escape(e.get('reason', ''))}</td></tr>" for e in cs)
    body = ('<div class="legend">' + "".join(f"<span>{chip(s_)}</span>" for s_ in ("validated", "seed", "needs_review"))
            + '<span><svg width="14" height="14"><circle cx="7" cy="7" r="5" fill="#fff" stroke="var(--serious)" '
            'stroke-width="2"/></svg>partial correlation after controlling for the effect\'s own volume</span></div>'
            + "".join(s) + "<h2>Agent's reasoning per link (verbatim from state)</h2><table><tbody>" + notes
            + "</tbody></table>")
    return page("3 · Causal hypotheses tested — supported vs. hypothesis vs. confounded",
                "Seed causal links are not trusted: each is tested on de-seasonalised history (local time) and its "
                "confidence updated", body), H + 560


# ====================================================================== page 4: what the agent flagged
def review_page(g: dict, log_lines: str) -> tuple[str, int]:
    flagged = [e for e in g["edges"] if e["status"] == "needs_review"]
    rows = "".join(f"<tr><td>{chip('needs_review')}</td><td><code>{html.escape(e['type'])}: {html.escape(e['src'])} → "
                   f"{html.escape(e['dst'])}</code></td><td>{html.escape(e.get('reason', ''))}</td></tr>" for e in flagged)
    body = (f"<div class='sub'>{len(flagged)} relationships the agent refused to trust, each with its reason. "
            "These stay visible to people instead of being silently used.</div><br><table><thead><tr><th>Status</th>"
            "<th>Edge</th><th>Reason recorded by the agent</th></tr></thead><tbody>" + rows + "</tbody></table>"
            "<h2>Console: python -m sentinel.brain bootstrap</h2><pre>" + html.escape(log_lines) + "</pre>")
    return page("4 · Where the agent admits doubt", "Edges marked needs_review, with the agent's own reasons", body), 1000


def tests_page(pytest_out: str) -> tuple[str, int]:
    body = "<pre>" + html.escape(pytest_out) + "</pre>"
    return page("5 · Automated checks", "pytest -v tests/test_brain_graph.py (Brain graph + the Data stand-in)", body), 900


# ====================================================================== run
def shoot(html_path: Path, png: Path, height: int):
    chrome = next((c for c in CHROME if Path(c).exists()), None)
    if not chrome:
        sys.exit("Chrome/Edge not found")
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--screenshot={png}",
                    f"--window-size=1500,{height}", "--force-device-scale-factor=1", html_path.as_uri()],
                   check=True, capture_output=True, timeout=120)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    boot = subprocess.run([py, "-m", "sentinel.brain", "bootstrap"], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    if boot.returncode:
        sys.exit(boot.stderr)
    tests = subprocess.run([py, "-m", "pytest", "-v", "-p", "no:cacheprovider", "tests/test_brain_graph.py"],
                           cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    g = json.loads(STATE.read_text(encoding="utf-8"))
    pages = {"01_graph": graph_page(g), "02_formulas": formula_page(g), "03_causal": causal_page(g),
             "04_doubt": review_page(g, boot.stdout.strip()),
             "05_tests": tests_page("\n".join(l for l in tests.stdout.splitlines() if l.strip())[-6000:])}
    for name, (doc, h) in pages.items():
        hp = OUT / f"{name}.html"
        hp.write_text(doc, encoding="utf-8")
        shoot(hp, OUT / f"{name}.png", h)
        print("wrote", OUT / f"{name}.png")


if __name__ == "__main__":
    main()
