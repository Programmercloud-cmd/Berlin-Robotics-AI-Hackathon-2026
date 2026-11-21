"""Driver-tree projection of the graph for browsing (the graph stays the model).

  root (contribution)
  ├── = expr                 formula inputs, recursive
  ├── ~ possible causes      causal edges (sign, confidence, status, reason)
  ├── ⊞ by dims              breakdowns
  └── ⚙ services             context edges
Shared nodes are expanded once; later occurrences show ↗. Metrics outside the tree are listed at the end.
"""
from __future__ import annotations

from .graph import SERVICE, BrainGraph, edge_key

MARK = {"seed": "·", "validated": "✓", "proposed": "?", "needs_review": "!", "rejected": "✗"}


def render_tree(g: BrainGraph, roots: list[str] | None = None, show_context: bool = True) -> str:
    lines: list[str] = []
    seen: set[str] = set()

    def label(mid: str) -> str:
        n = g.nodes[mid]
        return f"{n.name} ({mid}) [{n.kind}, {n.unit}]"

    def walk(mid: str, prefix: str, via: str):
        if mid in seen:
            lines.append(f"{prefix}{label(mid)}  ↗{(' ' + via) if via else ''}")
            return
        seen.add(mid)
        lines.append(f"{prefix}{label(mid)}{('  ' + via) if via else ''}")
        child = prefix.replace("├── ", "│   ").replace("└── ", "    ")
        items = []
        ins = [e for e in g.edges.values() if e.type == "formula" and e.dst == mid]
        if ins:
            st = {e.status for e in ins}
            items.append(("group", f"= {g.formulas[mid]}  {''.join(MARK[s] for s in sorted(st))} {'/'.join(sorted(st))}",
                          [(e.src, e.op) for e in ins]))
        cs = sorted([e for e in g.edges.values() if e.type == "causal" and e.dst == mid], key=lambda e: -e.confidence)
        if cs:
            items.append(("group", "~ possible causes",
                          [(e.src, f"{e.sign} conf {e.confidence:.2f} {MARK[e.status]} {e.status}"
                            + (f" — {g.notes[edge_key(e)]['reason'][:70]}" if e.status == "needs_review" else ""))
                           for e in cs]))
        dims = [e.dimension for e in g.edges.values() if e.type == "breakdown" and e.src == mid]
        if dims:
            items.append(("leaf", "⊞ by " + " · ".join(dims), []))
        svc = [e for e in g.edges.values() if e.type == "context" and e.dst == mid and e.src.startswith(SERVICE)]
        if show_context and svc:
            items.append(("leaf", "⚙ " + ", ".join(f"{e.src[len(SERVICE):]}{MARK[e.status]}" for e in svc), []))
        for i, (kind, text, kids) in enumerate(items):
            last = i == len(items) - 1
            lines.append(f"{child}{'└── ' if last else '├── '}{text}")
            sub = child + ("    " if last else "│   ")
            for j, (k, via2) in enumerate(kids):
                walk(k, sub + ("└── " if j == len(kids) - 1 else "├── "), via2 or "")

    for r in roots or g.roots():
        walk(r, "", "")
    rest = [m for m in g.nodes if m not in seen]
    if rest:
        lines.append("Outside the driver tree")
        for i, m in enumerate(rest):
            walk(m, "└── " if i == len(rest) - 1 else "├── ", "")
    return "\n".join(lines)
