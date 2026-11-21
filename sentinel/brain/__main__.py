"""Brain graph CLI (development; loop.py is the real entry point).

  python -m sentinel.brain bootstrap   seed graph -> validate against the dev data stand-in -> state/brain_graph.json
  python -m sentinel.brain tree        browse the driver tree
  python -m sentinel.brain show <id>   one metric: definition, edges, status and reasons
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from .graph import BrainGraph, edge_key
from .tree import MARK, render_tree

STATE = Path("state/brain_graph.json")
LIVE_START = datetime(2026, 10, 26)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    p = argparse.ArgumentParser(prog="sentinel.brain")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bootstrap")
    t = sub.add_parser("tree")
    t.add_argument("--no-context", action="store_true")
    s = sub.add_parser("show")
    s.add_argument("metric")
    a = p.parse_args(argv)

    if a.cmd == "bootstrap":
        from dev.fake_data import FakeData
        from .validate import validate_graph
        g = BrainGraph.from_seed()
        t0 = time.time()
        data = FakeData(list(g.nodes.values()), g.formulas)
        summary = validate_graph(g, data, LIVE_START)
        print(f"{len(g.nodes)} metrics, {len(g.edges)} edges; validated in {time.time() - t0:.1f}s")
        print("verdicts:", summary.counts())
        for k, e in g.edges.items():
            if e.status == "needs_review":
                print(f"  ! {k}: {g.notes[k]['reason']}")
        g.save(STATE)
        print(f"saved {STATE}")
        return
    if not STATE.exists():
        sys.exit("run `python -m sentinel.brain bootstrap` first")
    g = BrainGraph.load(STATE)
    if a.cmd == "tree":
        print(render_tree(g, show_context=not a.no_context))
    else:
        n = g.nodes[a.metric]
        print(f"{n.name} ({n.id}) [{n.kind}, {n.unit}, good={n.good_direction}, owner={n.owner}]")
        print(f"  {n.description}")
        sql = n.value_sql or f"{n.numerator_sql} / {n.denominator_sql}" if (n.value_sql or n.numerator_sql) else \
            f"formula: {g.formulas.get(n.id)}"
        print(f"  table={n.table or '-'}  {sql}")
        print(f"  dimensions: {', '.join(n.dimensions)}  services: {', '.join(n.services) or '-'}")
        for title, es in (("driven by", [e for e in g.edges.values() if e.dst == n.id]),
                          ("drives", [e for e in g.edges.values() if e.src == n.id and e.type != "breakdown"])):
            if es:
                print(f"  {title}:")
                for e in es:
                    other = e.src if e.dst == n.id else e.dst
                    print(f"    {MARK[e.status]} {e.type:8} {other:28} {e.status:12} conf {e.confidence:.2f}  "
                          f"{g.notes.get(edge_key(e), {}).get('reason', '')}")
        print(f"  upstream: {', '.join(g.upstream(n.id)) or '-'}")
        print(f"  impacts:  {', '.join(g.downstream(n.id)) or '-'}")


if __name__ == "__main__":
    main()
