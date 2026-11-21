"""Command line interface for the metric graph.

  python -m argus bootstrap                     register the initial 33-metric registry -> state/graph.json
  python -m argus register <contract.yaml>      a contributor adds a metric (no placement needed)
  python -m argus validate                      whole-graph structural checks
  python -m argus tree                          browse the graph as a tree (--json, --show-rejected)
  python -m argus show <metric>                 contract, edges, checks and reasons for one metric
  python -m argus log                           registrations and change log
  python -m argus export --format mermaid|json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .graph.graph import MetricGraph, load_contracts
from .graph.model import EDGE_REASONING, EdgeType
from .graph.tree import MARK, build_tree, render, to_mermaid
from .graph.validate import edge_summary, validate_structure

SOURCES = Path("catalog/sources.yaml")
REGISTRY = Path("catalog/contracts/registry.yaml")
OBJECTIVES = Path("catalog/objectives.yaml")
STATE = Path("state/graph.json")


def _load() -> MetricGraph:
    if not STATE.exists():
        sys.exit("no graph state yet: run `python -m argus bootstrap`")
    return MetricGraph.load(STATE)


def _print_reg(g: MetricGraph, r):
    mark = "✓" if r.state == "registered" else "✗"
    print(f"{mark} {r.registration_id} {r.metric_id} v{r.version}: {r.state}"
          + (f" (catalog revision {r.catalog_revision})" if r.state == "registered" else ""))
    for x in r.reasons:
        print(f"    reason : {x}")
    for x in r.warnings:
        print(f"    warning: {x}")
    for eid in r.edges_created:
        e = g.edges.get(eid)
        if e and e.type not in (EdgeType.BREAKDOWN, EdgeType.SOURCED):
            why = "; ".join(e.reasons) or "; ".join(c.detail for c in e.checks if c.passed is not True)
            print(f"    {MARK[e.status]} {e.type.value:20} {e.source} -> {e.target}  [{e.provenance}]"
                  + (f"  — {why}" if why and e.status != "validated" else ""))


def _print_validation(g):
    rep = validate_structure(g)
    print(f"edges: {json.dumps(edge_summary(g))}")
    for i in rep.issues:
        print("  " + str(i))
    return rep


def cmd_bootstrap(a):
    g, regs = MetricGraph.bootstrap(a.sources, a.registry, a.objectives)
    for r in regs:
        if a.verbose or r.state != "registered":
            _print_reg(g, r)
    ok = sum(r.state == "registered" for r in regs)
    print(f"registered {ok}/{len(regs)} contracts; catalog revision {g.revision}")
    _print_validation(g)
    g.save(STATE)
    print(f"saved {STATE}")


def cmd_register(a):
    g = _load()
    for path in a.files:
        for c in load_contracts(path, raw=True):
            _print_reg(g, g.register(c, actor=a.actor))
    g.save(STATE)


def cmd_validate(a):
    _print_validation(_load())


def cmd_tree(a):
    t = build_tree(_load(), max_depth=a.depth, show_rejected=a.show_rejected)
    print(json.dumps(t.to_dict(), indent=1, ensure_ascii=False) if a.json else render(t))


def cmd_show(a):
    g = _load()
    m = g.metric(a.metric)
    c = m.contract
    print(f"{c.name} ({m.id}) v{c.version}  [{m.status}, role={m.role}, unit={c.unit}, owner={c.owner}]")
    print(f"  {c.definition}")
    print(f"  population={c.population}  time={c.time_semantics}  aggregation={c.aggregation_policy}  "
          f"maturity={c.maturity_delay_seconds}s")
    print(f"  access: {'calculation ' + c.expression() if c.calculation else 'query_ref ' + c.query_ref}")
    print(f"  dimensions: {', '.join(m.canonical_dimensions)} | sources: {', '.join(c.source_ids)}")
    for title, direction in (("explained by", "in"), ("feeds into", "out")):
        es = [e for e in g.edges_of(m.id, None, direction, include_rejected=True)
              if e.type not in (EdgeType.BREAKDOWN, EdgeType.SOURCED)]
        if not es:
            continue
        print(f"  {title}:")
        for e in es:
            other = e.source if direction == "in" else e.target
            print(f"    {MARK[e.status]} {e.type.value:20} {other:30} [{e.provenance}, conf {e.confidence:.1f}]"
                  + (f"  {e.expression}" if e.expression else ""))
            for ck in e.checks:
                print(f"        {'✓' if ck.passed else '?' if ck.passed is None else '✗'} {ck.name}: {ck.detail}")
            for rs in e.reasons:
                print(f"        · {rs}")


def cmd_log(a):
    g = _load()
    for r in g.registrations[-a.n:]:
        print(f"{r.registration_id} {r.submitted_at} {r.actor:12} {r.metric_id} v{r.version} {r.state} "
              + ("; ".join(r.reasons)))
    print("--- change log")
    for c in g.changelog[-a.n:]:
        print(f"r{c.revision:<3} {c.ts} {c.actor:12} {c.action:22} {c.target} {c.detail}")


def cmd_export(a):
    g = _load()
    if a.format == "mermaid":
        out = to_mermaid(g)
    else:
        d = g.to_dict()
        d["edge_reasoning"] = {k.value: v for k, v in EDGE_REASONING.items()}
        d["tree"] = build_tree(g).to_dict()
        out = json.dumps(d, indent=1, ensure_ascii=False)
    if a.out:
        Path(a.out).write_text(out, encoding="utf-8")
        print(f"wrote {a.out}")
    else:
        print(out)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    p = argparse.ArgumentParser(prog="argus", description="Eye of the System — metric graph")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bootstrap")
    b.add_argument("--sources", default=SOURCES)
    b.add_argument("--registry", default=REGISTRY)
    b.add_argument("--objectives", default=OBJECTIVES)
    b.add_argument("-v", "--verbose", action="store_true")
    b.set_defaults(fn=cmd_bootstrap)
    r = sub.add_parser("register")
    r.add_argument("files", nargs="+")
    r.add_argument("--actor", default="contributor")
    r.set_defaults(fn=cmd_register)
    sub.add_parser("validate").set_defaults(fn=cmd_validate)
    t = sub.add_parser("tree")
    t.add_argument("--depth", type=int, default=8)
    t.add_argument("--show-rejected", action="store_true")
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=cmd_tree)
    s = sub.add_parser("show")
    s.add_argument("metric")
    s.set_defaults(fn=cmd_show)
    lg = sub.add_parser("log")
    lg.add_argument("-n", type=int, default=30)
    lg.set_defaults(fn=cmd_log)
    e = sub.add_parser("export")
    e.add_argument("--format", choices=["json", "mermaid"], default="json")
    e.add_argument("--out")
    e.set_defaults(fn=cmd_export)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
