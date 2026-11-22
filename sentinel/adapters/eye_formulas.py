"""Derive the agent's formula edges from Eye's process mining (PM4Py) and catalog definitions.

Nothing here is hand-written per metric: identities are read from what Eye discovered or declared,
then proposed to the graph and validated on data like any other formula.

  catalog components       a ratio metric declares [numerator, denominator]
                           -> numerator = denominator * ratio           (exact by definition)
  PM4Py process tree       an exclusive choice X(a, b, c) after activity p in one object's lifecycle
                           -> metric(p) = metric(a) + metric(b) + metric(c) (outcome partition)
  PM4Py OC-DFG (session)   the session lifecycle orders funnel activities a1 -> a2 -> ... ; each step
                           gets a virtual step-rate metric metric(a_k) / metric(a_k-1), and
                           metric(a_n) = metric(a_1) * step_2 * ... * step_n  (telescoping identity)
  PM4Py OC-DFG (objects)   payment_succeeded -> order_confirmed links payments to orders
                           -> candidate orders = successful_payments (validated, may be rejected)
  money chain              virtual avg order value and take rate give
                           GMV = orders * aov and fee revenue = GMV * take rate
"""
from __future__ import annotations

import re

from sentinel.brain.formula import Formula
from sentinel.contracts import MetricNode

COUNT_UNITS = {"count"}


def _q(db, sql, args=()):
    return db.execute(sql, args).fetchall()


def measures(db) -> dict[str, list[str]]:
    """activity -> metrics that measure it (count metrics first)."""
    out: dict[str, list[str]] = {}
    for r in _q(db, "select source, target from graph_edges where kind = 'measures'"):
        out.setdefault(r["target"].split(":", 1)[1], []).append(r["source"].split(":", 1)[1])
    return out


def session_chain(db) -> list[str]:
    """Follow the most frequent session-lifecycle successor from session_started (PM4Py OC-DFG)."""
    edges: dict[str, list[tuple[int, str]]] = {}
    for r in _q(db, "select source, target, properties from graph_edges where kind = 'precedes' "
                    "and provenance = 'pm4py.ocdfg'"):
        n = (r["properties"].get("object_types") or {}).get("session", 0)
        if n:
            edges.setdefault(r["source"].split(":", 1)[1], []).append((n, r["target"].split(":", 1)[1]))
    chain, cur = ["session_started"], "session_started"
    while cur in edges and len(chain) < 12:
        nxt = [t for n, t in sorted(edges[cur], reverse=True) if t not in chain]
        if not nxt:
            break
        cur = nxt[0]
        chain.append(cur)
    return chain


def parse_tree(text: str):
    """Parse PM4Py's process tree notation, e.g. ->( 'a', X( ->( 'b', 'c' ), 'd' ) ),
    into (operator, [children]) tuples and activity strings."""
    toks = re.findall(r"->|X|\+|\*|O|\(|\)|,|'[^']*'|tau", text)
    pos = 0

    def node():
        nonlocal pos
        t = toks[pos]
        pos += 1
        if t.startswith("'"):
            return t.strip("'")
        if t == "tau":
            return None
        assert toks[pos] == "("
        pos += 1
        kids = []
        while toks[pos] != ")":
            if toks[pos] == ",":
                pos += 1
                continue
            kids.append(node())
        pos += 1
        return (t, kids)
    return node() if toks else None


def _first(n):
    """First activity executed by a subtree."""
    if n is None or isinstance(n, str):
        return n
    op, kids = n
    return _first(kids[0]) if op == "->" and kids else None


def _choices(n, out):
    if n is None or isinstance(n, str):
        return
    op, kids = n
    if op == "->":
        for prev, cur in zip(kids, kids[1:]):
            if isinstance(prev, str) and isinstance(cur, tuple) and cur[0] == "X":
                alts = [_first(k) for k in cur[1]]
                if len(alts) >= 2 and all(alts):
                    out.append((prev, alts))
    for k in kids:
        _choices(k, out)


def process_tree_choices(db) -> list[tuple[str, list[str]]]:
    """(preceding activity, [exclusive alternatives]) from PM4Py inductive process trees."""
    out: list[tuple[str, list[str]]] = []
    for r in _q(db, "select id, properties from graph_nodes where kind = 'process'"):
        tree = (r["properties"] or {}).get("inductive_tree")
        if tree:
            _choices(parse_tree(tree), out)
    return out


class Proposer:
    def __init__(self, graph, data, bus):
        self.g, self.data, self.bus = graph, data, bus
        self.added = []

    def virtual(self, vid: str, name: str, num: str, den: str, desc: str, provenance: str):
        if vid in self.g.nodes or num not in self.g.nodes or den not in self.g.nodes:
            return False
        dims = [d for d in self.g.nodes[num].dimensions if d in self.g.nodes[den].dimensions]
        node = MetricNode(id=vid, name=name, description=desc, kind="ratio",
                          unit="share" if self.g.nodes[num].unit == self.g.nodes[den].unit else "score",
                          table=self.g.nodes[num].table, dimensions=dims, owner=provenance, status="proposed")
        self.g.add_node(node, actor=provenance)
        self.data.add_virtual_ratio(vid, num, den)
        self.bus.publish("GRAPH_MUTATION", provenance, {"kind": "virtual_metric", "metric": vid,
                         "summary": f"{vid} = {num} / {den} ({desc})"})
        return True

    def formula(self, output: str, expr: str, provenance: str, why: str):
        if output not in self.g.nodes or output in self.g.formulas:
            return False
        if not all(v in self.g.nodes for v in Formula.parse(expr).variables):
            return False
        try:
            self.g.add_formula(output, expr, status="proposed", actor=provenance)
        except ValueError as ex:
            self.bus.publish("META_RESULT", provenance, {"text": f"{output} = {expr} not added: {ex}"})
            return False
        self.added.append((output, expr, provenance))
        self.bus.publish("GRAPH_MUTATION", provenance, {"kind": "formula", "output": output, "expr": expr,
                         "summary": f"proposed {output} = {expr} — {why}"})
        return True


def add_eye_formulas(graph, data, bus) -> list[tuple[str, str, str]]:
    db = data.db
    p = Proposer(graph, data, bus)
    meas = measures(db)
    # activity -> the count metric whose declared primary activity it is (catalog `activity` field);
    # Eye's `measures` edges also link a metric to every activity it reads, which is too broad here
    count_of = {}
    for mid, node in graph.nodes.items():
        act = data.catalog.get(mid, {}).get("calculation", {}).get("activity")
        if node.kind == "count" and act and act not in count_of:
            count_of[act] = mid
    for act, ms in meas.items():
        counts = [m for m in ms if m in graph.nodes and graph.nodes[m].kind == "count"]
        if counts and act not in count_of:
            count_of[act] = sorted(counts, key=len)[0]

    # 1. catalog components: numerator = denominator * ratio
    for mid in list(graph.nodes):
        comp = data.catalog.get(mid, {}).get("calculation", {}).get("components")
        if graph.nodes[mid].kind == "ratio" and comp and len(comp) == 2 and all(c in graph.nodes for c in comp):
            num, den = comp
            p.formula(num, f"{den} * {mid}", "catalog.components", f"{mid} is defined as {num} / {den}")

    # 2. PM4Py process tree: exclusive outcomes partition their parent. Written in RATE form so the rate
    #    of one outcome decomposes into the shares of the others, e.g.
    #    payment_success_rate = 1 - payment_timeouts_share - other_failed_payments_share
    ratio_of = {tuple(data.catalog.get(m, {}).get("calculation", {}).get("components") or ()): m
                for m in graph.nodes if graph.nodes[m].kind == "ratio"}
    for parent, alts in process_tree_choices(db):
        kids = [count_of.get(a) for a in alts]
        total = count_of.get(parent)
        if not (total and all(kids) and len(set(kids)) == len(kids) and total not in kids):
            continue
        rate_kid = next((k for k in kids if (k, total) in ratio_of), None)
        why = f"process tree: after '{parent}' exactly one of {alts} happens"
        if rate_kid:
            shares = []
            for k in kids:
                if k == rate_kid:
                    continue
                sid = ratio_of.get((k, total)) or f"{k}_share"
                if sid not in graph.nodes:
                    p.virtual(sid, f"{k} share of {total}", k, total, f"share of {total} ending in {k}",
                              "pm4py.process_tree")
                shares.append(sid)
            p.formula(ratio_of[(rate_kid, total)], "1 - " + " - ".join(shares), "pm4py.process_tree", why)
        else:
            p.formula(total, " + ".join(kids), "pm4py.process_tree", why)

    # 3. PM4Py OC-DFG session lifecycle -> funnel step rates and the telescoping identity
    chain = [a for a in session_chain(db) if a in count_of]
    metrics_chain = []
    for a in chain:
        m = count_of[a]
        if m not in metrics_chain and graph.nodes[m].table in ("sessions", "session_funnel"):
            metrics_chain.append(m)
    steps = []
    for prev, cur in zip(metrics_chain, metrics_chain[1:]):
        vid = f"{cur.replace('_sessions', '')}_step_rate"
        if p.virtual(vid, f"Step rate {prev} -> {cur}", cur, prev,
                     f"share of {prev} reaching {cur} (PM4Py session lifecycle)", "pm4py.ocdfg"):
            steps.append(vid)
        elif vid in graph.nodes:
            steps.append(vid)
    if len(metrics_chain) >= 2 and steps:
        p.formula(metrics_chain[-1], " * ".join([metrics_chain[0]] + steps), "pm4py.ocdfg",
                  f"session lifecycle {' -> '.join(chain)}")
        if "session_conversion_rate" in graph.nodes and "checkout_completion_rate" in graph.nodes \
                and metrics_chain[-1] == "checkout_sessions":
            p.formula("session_conversion_rate", f"checkout_sessions / {metrics_chain[0]} * checkout_completion_rate",
                      "pm4py.ocdfg", "converted sessions = checkout sessions x checkout completion")

    # 4. OC-DFG object link payment -> order: candidate equality (the validator decides)
    links = {(r["source"].split(":", 1)[1], r["target"].split(":", 1)[1])
             for r in _q(db, "select source, target from graph_edges where kind = 'precedes'")}
    if ("payment_succeeded", "order_confirmed") in links and "orders" in graph.nodes \
            and "successful_payments" in graph.nodes:
        p.formula("orders", "successful_payments", "pm4py.ocdfg",
                  "OC-DFG: every order_confirmed follows a payment_succeeded")

    # 5. money chain with virtual unit economics
    if "gross_merchandise_value" in graph.nodes and "orders" in graph.nodes:
        p.virtual("avg_order_value", "Average order value", "gross_merchandise_value", "orders",
                  "GMV per confirmed order", "catalog.economics")
        p.formula("gross_merchandise_value", "orders * avg_order_value", "catalog.economics",
                  "GMV = orders x average order value")
    if "platform_fee_revenue" in graph.nodes and "gross_merchandise_value" in graph.nodes:
        p.virtual("take_rate", "Take rate", "platform_fee_revenue", "gross_merchandise_value",
                  "fee revenue per GMV", "catalog.economics")
        p.formula("platform_fee_revenue", "gross_merchandise_value * take_rate", "catalog.economics",
                  "fee revenue = GMV x take rate")
    return p.added
