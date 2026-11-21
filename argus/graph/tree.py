"""Tree projection of the metric graph for people to browse (the graph stays the storage model).

  objective
  ├── = formula                   arithmetic components (contract or inferred), recursive
  ├── ⊂ subsets                   populations nested inside this one (funnel stages), recursive
  ├── ~ possible drivers          provisional / validated mechanisms and associations, recursive
  └── ⊞ by dim(n) · ...           supported breakdowns
  Connected elsewhere             metrics linked to an objective but not upstream of it
  Data health                     data-quality metrics
  Unplaced                        not connected yet (awaiting inference)
  Unresolved objectives           configured but not registered

Every edge shows its status: ✓ validated · ? provisional · ✗ rejected (rejected edges hidden by default).
Shared nodes are expanded once; later occurrences are references (↗).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .graph import MetricGraph
from .model import EdgeType
from .validate import validate_structure

MARK = {"validated": "✓", "provisional": "?", "rejected": "✗"}


@dataclass
class TreeNode:
    label: str
    kind: str
    metric_id: str | None = None
    detail: str = ""
    children: list["TreeNode"] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {"label": self.label, "kind": self.kind}
        if self.metric_id:
            d["metric_id"] = self.metric_id
        if self.detail:
            d["detail"] = self.detail
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        return d


def build_tree(g: MetricGraph, max_depth: int = 8, show_rejected: bool = False) -> TreeNode:
    expanded: set[str] = set()

    def keep(e):
        return show_rejected or e.status != "rejected"

    def node(mid: str, depth: int, via: str = "") -> TreeNode:
        m = g.metric(mid)
        label = f"{m.contract.name} ({mid})"
        if mid in expanded:
            return TreeNode(label, "ref", mid, "↗ see above" + (f" · {via}" if via else ""))
        expanded.add(mid)
        n = TreeNode(label, "metric", mid, " · ".join(x for x in [m.contract.unit, m.role if m.role != m.contract.unit else "", via] if x))
        if depth >= max_depth:
            return n
        for expr, edges in g.components(mid).items():
            edges = [e for e in edges if keep(e)]
            if not edges:
                continue
            worst = min((e.status for e in edges), key=["rejected", "provisional", "validated"].index)
            grp = TreeNode(f"= {expr}", "group", detail=f"{MARK[worst]} {worst} · {edges[0].provenance}")
            for e in edges:
                grp.children.append(node(e.source, depth + 1, e.role or ""))
            n.children.append(grp)
        subs = [e for e in g.edges_of(mid, [EdgeType.SUBSET], include_rejected=show_rejected)]
        if subs:
            grp = TreeNode("⊂ subsets", "group", detail="nested populations")
            for e in subs:
                grp.children.append(node(e.source, depth + 1, f"{MARK[e.status]} {e.status}"))
            n.children.append(grp)
        drv = [e for e in g.edges_of(mid, [EdgeType.DRIVER, EdgeType.ASSOCIATED], include_rejected=show_rejected)]
        if drv:
            grp = TreeNode("~ possible drivers", "group", detail="hypotheses, not conclusions")
            for e in sorted(drv, key=lambda e: -e.confidence):
                grp.children.append(node(e.source, depth + 1,
                                         f"{MARK[e.status]} {e.status} · {e.type.value} · {e.provenance}"))
            n.children.append(grp)
        bds = g.edges_of(mid, [EdgeType.BREAKDOWN])
        if bds:
            n.children.append(TreeNode("⊞ by " + " · ".join(e.dimension for e in bds), "group"))
        return n

    root = TreeNode("Business", "group")
    for o in g.objective_ids():
        if o in g.metrics:
            root.children.append(node(o, 0, "objective"))
    issues = validate_structure(g).issues
    unplaced = {i.target for i in issues if i.code == "unplaced_metric"}
    dq = [m.id for m in g.active_metrics() if m.role == "data_quality"]
    rest = [m.id for m in g.active_metrics() if m.id not in expanded and m.id not in unplaced and m.id not in dq]
    sections = [("Connected elsewhere (downstream of or beside objectives)", rest),
                ("Data health", [x for x in dq if x not in expanded]),
                ("Unplaced (not connected yet — awaiting inference)", [x for x in unplaced if x not in expanded])]
    for title, ids in sections:
        ids = [i for i in ids if i not in expanded]
        if ids:
            grp = TreeNode(title, "group")
            for i in ids:
                if i not in expanded:
                    grp.children.append(node(i, 1))
            root.children.append(grp)
    if g.unresolved_objectives():
        root.children.append(TreeNode("Unresolved objectives", "group", children=[
            TreeNode(o, "missing", detail="configured objective, not registered") for o in g.unresolved_objectives()]))
    return root


def render(node: TreeNode, prefix: str = "", last: bool = True, top: bool = True) -> str:
    lines = []
    if top:
        lines.append(node.label)
    else:
        lines.append(prefix + ("└── " if last else "├── ") + node.label + (f"  ({node.detail})" if node.detail else ""))
        prefix += "    " if last else "│   "
    for i, c in enumerate(node.children):
        lines.append(render(c, prefix, i == len(node.children) - 1, False))
    return "\n".join(lines)


def to_mermaid(g: MetricGraph) -> str:
    """Solid = arithmetic, thick = subset, dashed = driver/association; rejected edges omitted."""
    lines = ["flowchart BT"]
    for m in g.active_metrics():
        shape = ("[[", "]]") if m.id in g.objective_ids() else ("([", "])") if m.role == "data_quality" else ("[", "]")
        lines.append(f'  {m.id}{shape[0]}"{m.contract.name}"{shape[1]}')
    arrows = {EdgeType.ARITHMETIC: "-->", EdgeType.SUBSET: "==>", EdgeType.DRIVER: "-.->", EdgeType.ASSOCIATED: "-.->"}
    for e in g.edges.values():
        if e.type in arrows and e.status != "rejected" and e.source in g.metrics and e.target in g.metrics:
            tag = e.role if e.type == EdgeType.ARITHMETIC else e.type.value
            lines.append(f"  {e.source} {arrows[e.type]}|{tag} {MARK[e.status]}| {e.target}")
    return "\n".join(lines)
