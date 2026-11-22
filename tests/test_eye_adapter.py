"""Sentinel on Eye of the System (jacob/v1): adapter, PM4Py-derived formulas, failure mechanism.

Builds a throw-away Eye database with ONE simulated day of OCEL history (Eye's own simulator and
initialisation, incl. PM4Py discovery), plus two hours of Eye's payment fault for the mechanism test.
Skipped when the Eye checkout (.eye-jacob) or its dependencies (pgserver, pm4py, psycopg) are missing.

  .venv-eye/Scripts/python -m pytest -q tests/test_eye_adapter.py
"""
from __future__ import annotations

import warnings
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
EYE = ROOT / ".eye-jacob"
pytest.importorskip("pgserver")
pytest.importorskip("pm4py")
pytest.importorskip("psycopg")
if not (EYE / "eye").exists():
    pytest.skip("Eye checkout (.eye-jacob) not present", allow_module_level=True)
warnings.filterwarnings("ignore")

from sentinel.adapters.eye import EyeData, build_graph, import_eye  # noqa: E402

PAY = {"country": "DE", "platform": "mobile", "payment_provider": "Stripe"}


@pytest.fixture(scope="module")
def eye_db(tmp_path_factory):
    import pgserver
    import_eye(EYE)
    from eye import monitor, runtime, simulator
    from eye.db import connect, setup
    server = pgserver.get_server(str(tmp_path_factory.mktemp("eye_pg")), cleanup_mode="stop")
    dsn = server.get_uri()
    with connect(dsn) as db:
        setup(db)
        simulator.seed(db, days=1)                      # one day of history (24 closed windows)
    runtime.initialize(dsn)                             # registers metrics, PM4Py discovery, observations
    hist_end = simulator.DEMO_START
    with connect(dsn) as db:                            # two hours of Eye's payment fault
        for h in range(2):
            t = hist_end + timedelta(hours=h)
            simulator.emit(db, t, fault=True)
            monitor.materialize(db, window_start=t)
        # Eye's live runtime: a rolling one-hour window evaluated every five minutes (jacob/v1 b8e7f83)
        from eye.db import state
        baseline = datetime.fromisoformat(state(db, "baseline_through"))
        for k in (1, 2):
            monitor.materialize_rolling(db, hist_end + k * timedelta(minutes=5), baseline)
        # third live hour: Eye's faster-search rollout (an experiment: rollout vs holdout arm)
        from eye.scenario import Scenario
        t = hist_end + timedelta(hours=2)
        simulator.emit(db, t, scenarios=(Scenario(effect="search_rollout", transition="search", severity=.82,
                                                  duration_hours=1),))
        monitor.materialize(db, window_start=t)
    yield dsn, pd.Timestamp(hist_end).tz_convert("UTC").tz_localize(None)
    server.cleanup()


@pytest.fixture(scope="module")
def data(eye_db):
    dsn, _ = eye_db
    return EyeData(dsn, EYE)


# ====================================================================== 1. adapter
def test_adapter_totals_match_observations(data, eye_db):
    _, live = eye_db
    start, end = live - timedelta(hours=24), live
    df = data.metric("sessions", start, end)
    assert len(df) == 24 and df.complete.all()
    raw = data.db.execute("""select sum(value) as v from observations where metric_id = 'sessions'
                             and scope = '{}' and window_start >= %s and window_start < %s
                             and window_start = date_trunc('hour', window_start)""",
                          (start.tz_localize("UTC").to_pydatetime(), end.tz_localize("UTC").to_pydatetime())).fetchone()
    assert df.value.sum() == pytest.approx(raw["v"])


def test_adapter_breakdowns_reconcile(data, eye_db):
    _, live = eye_db
    start, end = live - timedelta(hours=24), live
    tot = data.metric("orders", start, end).set_index("ts").value
    by = data.metric("orders", start, end, group_by=["country"]).groupby("ts").value.sum()
    assert (tot - by).abs().max() == 0
    r_tot = data.metric("payment_success_rate", start, end)
    r_by = data.metric("payment_success_rate", start, end, group_by=["payment_provider"])
    assert r_by.numerator.sum() == pytest.approx(r_tot.numerator.sum())
    assert r_by.denominator.sum() == pytest.approx(r_tot.denominator.sum())


def test_adapter_reaggregates_unmaterialised_slices(data, eye_db):
    """country x platform is not materialised for payments; it is rebuilt from the finer scope and must
    equal Eye's own computation for that scope."""
    import_eye(EYE)
    from eye.metric_catalog import CATALOG
    from eye.metric_readings import MAX_MATURITY, Facts, metric_readings
    _, live = eye_db
    t = live - timedelta(hours=3)
    mine = data.metric("payment_success_rate", t, t + timedelta(hours=1),
                       filters={"country": "DE", "platform": "mobile"})
    assert len(mine) == 1
    aware = t.tz_localize("UTC").to_pydatetime()
    facts = Facts(data.db, aware, aware + timedelta(hours=1), aware + timedelta(hours=1) + MAX_MATURITY)
    (_, value, _, details), = list(metric_readings(CATALOG["payment_success_rate"], facts,
                                                   scope={"country": "DE", "platform": "mobile"}))
    assert mine.value.iloc[0] == pytest.approx(value)
    assert mine.denominator.iloc[0] == pytest.approx(details["denominator"])


def test_adapter_completeness_and_virtual_ratio(data, eye_db):
    _, live = eye_db
    assert data.completeness("payments", live - timedelta(hours=2)) == pytest.approx(1.0)
    assert data.completeness("payments", live + timedelta(days=1)) == 0.0        # future: not visible
    data.add_virtual_ratio("checkout_share_test", "checkout_sessions", "sessions")
    v = data.metric("checkout_share_test", live - timedelta(hours=5), live, group_by=["country"])
    c = data.metric("checkout_sessions", live - timedelta(hours=5), live, group_by=["country"])
    s = data.metric("sessions", live - timedelta(hours=5), live, group_by=["country"])
    assert v.value.sum() == pytest.approx((c.value.values / s.value.values).sum())


# ====================================================================== 1b. jacob/v1 b8e7f83 compatibility
def test_rolling_windows_are_not_read_as_hours(data, eye_db):
    """A1: Eye writes a rolling one-hour observation every five minutes; the agent's hourly series must
    contain each hour once, with the hour-aligned value."""
    _, live = eye_db
    aware = lambda t: t.tz_localize("UTC").to_pydatetime()  # noqa: E731
    rolling = data.db.execute("""select count(*) as n from observations where metric_id = 'sessions' and scope = '{}'
                                 and window_start > %s and window_start < %s""",
                              (aware(live), aware(live + timedelta(hours=1)))).fetchone()["n"]
    assert rolling >= 2                                   # the five-minute windows exist in Eye's table
    df = data.metric("sessions", live, live + timedelta(hours=2))
    assert list(df.ts) == [live, live + timedelta(hours=1)]
    hourly = data.db.execute("""select value from observations where metric_id = 'sessions' and scope = '{}'
                                and window_start = %s""", (aware(live),)).fetchone()["value"]
    assert df.value.iloc[0] == pytest.approx(hourly)


def test_event_evidence_respects_ingestion_cutoff(data, eye_db):
    """A2: event-level evidence only counts events Eye could have seen (ingested by the hour's end + GRACE)."""
    _, live = eye_db
    aware = lambda t: t.tz_localize("UTC").to_pydatetime()  # noqa: E731
    assert data.as_of() <= aware(data.published_cutoff())
    mix = data.activity_mix(live, live + timedelta(hours=1), {}, ["payment_attempted", "payment_succeeded"])
    raw = {r["activity"]: r["n"] for r in data.db.execute(
        """select activity, count(*) as n from events where activity = any(%s) and occurred_at >= %s
           and occurred_at < %s and ingested_at <= %s group by activity""",
        (["payment_attempted", "payment_succeeded"], aware(live), aware(live + timedelta(hours=1)), data.as_of()))}
    assert {m["activity"]: m["now"] for m in mix} == {k: raw.get(k, 0) for k in ("payment_attempted", "payment_succeeded")}


def test_release_measured_from_event_versions(data, eye_db):
    """A4: the search rollout is found as a release from the events' version field, at the right hour."""
    _, live = eye_db
    t = live + timedelta(hours=2)
    evs = data.events(t - timedelta(hours=1), t + timedelta(hours=1))
    rel = [e for e in evs if e.service == "search"]
    assert len(rel) == 1 and rel[0].type == "release" and "search-v2" in rel[0].description
    assert t <= pd.Timestamp(rel[0].ts) < t + timedelta(hours=1)
    assert data.events(live - timedelta(hours=6), live) == [] or all(e.service != "search" for e in
                                                                     data.events(live - timedelta(hours=6), live))


# ====================================================================== 2. PM4Py-derived formulas
@pytest.fixture(scope="module")
def graph(data, eye_db):
    from sentinel.adapters.eye_formulas import add_eye_formulas
    from sentinel.brain.validate import validate_graph
    from sentinel.bus import EventBus
    _, live = eye_db
    g = build_graph(data)
    bus = EventBus(None)
    added = add_eye_formulas(g, data, bus)
    validate_graph(g, data, live, formula_days=1, causal_days=1)
    return g, added, bus


def test_graph_uses_eye_catalog(graph):
    g, _, _ = graph
    assert {"payment_success_rate", "session_conversion_rate", "search_click_rate"} <= set(g.nodes)
    assert set(g.nodes["payment_success_rate"].dimensions) == {"country", "platform", "payment_provider",
                                                               "checkout_release_version"}


def test_formulas_discovered_from_pm4py_and_catalog(graph):
    g, added, bus = graph
    sources = {(o, prov) for o, _, prov in added}
    assert ("payment_success_rate", "pm4py.process_tree") in sources          # XOR outcome partition (rate form)
    assert g.formulas["payment_success_rate"].startswith("1 - ")
    assert "payment_timeouts_share" in g.nodes                                 # virtual outcome share
    assert ("successful_payments", "catalog.components") in sources           # numerator = den x rate
    assert any(prov == "pm4py.ocdfg" for _, _, prov in added)                 # session lifecycle funnel
    assert any(e.type == "GRAPH_MUTATION" for e in bus.history)


def test_discovered_formulas_validated_on_data(graph):
    g, _, _ = graph
    def status(output):
        return {e.status for e in g.edges.values() if e.type == "formula" and e.dst == output}
    assert status("payment_success_rate") == {"validated"}   # 1 - timeout share - decline share
    assert status("successful_payments") == {"validated"}
    tele = [o for o, ex in g.formulas.items() if "_step_rate" in ex]
    assert tele and all(status(o) == {"validated"} for o in tele)
    for out in g.formulas:                                   # every proposal was really tested on data
        assert status(out) == {"validated"}, (out, status(out))
        note = next(g.notes[k] for k, e in g.edges.items() if e.type == "formula" and e.dst == out)
        assert "identity holds" in note["reason"], note["reason"]          # not skipped "by construction"
    bad = {k: g.notes[k]["reason"] for k, e in g.edges.items() if e.type == "breakdown" and e.status != "validated"}
    # finding for Eye: stockout_sku_share does not partition by sku_id (slices sum to ~50x the total); the
    # validator flags it for review instead of trusting it for drill-down
    assert set(bad) <= {"breakdown:stockout_sku_share->stockout_sku_share@sku_id#sku_id"}, bad


# ====================================================================== 3. failure mechanism (event level)
def test_failure_mechanism_from_events(data, graph, eye_db):
    from sentinel.bus import EventBus
    from sentinel.contracts import Incident
    from sentinel.investigator.react import Case, Investigator
    from sentinel.monitor.engine import MonitorEngine
    g, _, _ = graph
    _, live = eye_db
    data.now = live + timedelta(hours=2)
    bus = EventBus(None)
    mon = MonitorEngine(g, data, bus, live - timedelta(hours=24), live)
    inv = Investigator(g, data, mon, bus)
    inc = Incident(id="INC-T", fingerprint="", state="investigating", opened_at=live, updated_at=live)
    case = Case(inc, {"metric": "payment_success_rate", "segment": "country=DE|payment_provider=Stripe|platform=mobile",
                      "ts": str(live)}, live, live + timedelta(hours=2))
    inv.mechanism(case, live, live + timedelta(hours=2))
    h = next(h for h in case.hyps if case.hyp_meta[h.id].get("mechanism"))
    assert case.hyp_meta[h.id]["mechanism"] == "payment_timed_out"
    assert "rose" in h.statement and h.posterior >= 0.8
    assert all("-> confirmed" in p for p in h.predictions)                      # in segment + not elsewhere
    samples = case.hyp_meta[h.id]["samples"]
    assert samples and samples[0]["attrs"].get("response") == "gateway_timeout"
    assert samples[0]["attrs"].get("payment_provider") == "Stripe"


# ====================================================================== agent-only tables, step 4, step 5
EYE_TABLES = ("metrics", "observations", "graph_nodes", "graph_edges", "incidents", "evidence", "anomalies")


def eye_counts(db):
    return {t: db.execute(f"select count(*) as n from eye.{t}").fetchone()["n"] for t in EYE_TABLES}


@pytest.fixture(scope="module")
def agent(data, graph, eye_db):
    from sentinel.adapters.eye_process import AgentMetricsEngine, add_process_metrics
    from sentinel.adapters.eye_store import AgentStore
    from sentinel.bus import EventBus
    from sentinel.investigator.incidents import IncidentManager
    from sentinel.investigator.react import Investigator
    from sentinel.meta.meta_agent import MetaAgent
    from sentinel.monitor.engine import MonitorEngine
    g, _, _ = graph
    _, live = eye_db
    before = eye_counts(data.db)
    bus = EventBus(None)
    store = AgentStore(data.db)
    engine = AgentMetricsEngine(data, store)
    mon = MonitorEngine(g, data, bus, live - timedelta(hours=24), live)
    meta = MetaAgent(g, data, mon, bus)
    process = add_process_metrics(g, data, store, engine, bus, live - timedelta(hours=24), live)
    mined = meta.mine_unmeasured_activities(engine, store, live, live - timedelta(hours=24))
    for w in (live, live + timedelta(hours=1)):                        # the two fault hours
        engine.update_window(w)
    inv = Investigator(g, data, mon, bus, meta=None)
    im = IncidentManager(g, mon, inv, bus)
    store.attach(bus, im)
    return {"store": store, "engine": engine, "bus": bus, "im": im, "inv": inv, "mon": mon,
            "process": process, "mined": mined, "before": before}


def test_agent_tables_are_separate_from_eye(agent, data):
    assert eye_counts(data.db) == agent["before"]                       # nothing written to eye.*
    tables = {r["table_name"] for r in data.db.execute(
        "select table_name from information_schema.tables where table_schema = 'sentinel'")}
    assert {"metrics", "observations", "incidents", "evidence", "bus_events"} <= tables
    cols = {r["column_name"] for r in data.db.execute(
        "select column_name from information_schema.columns where table_schema='sentinel' and table_name='incidents'")}
    eye_cols = {r["column_name"] for r in data.db.execute(
        "select column_name from information_schema.columns where table_schema='eye' and table_name='incidents'")}
    assert eye_cols - {"id"} <= cols and {"first_cause", "label", "hypotheses", "report_md"} <= cols


def test_step4_unmeasured_activities_become_metrics(agent, data, graph, eye_db):
    g, _, _ = graph
    _, live = eye_db
    mined = {m["metric"] for m in agent["mined"]}
    assert "product_page_load_failed_rate" in mined                     # failure / the request it follows
    assert "cart_add_completion_rate" in mined                          # completion of a request activity
    for m in agent["store"].metrics():                                 # a completion rate never counts failures
        if m["calculation"].get("pattern") == "completion":
            assert "failed" not in m["calculation"]["activity"], m["id"]
    stored = {r["id"]: r for r in agent["store"].metrics()}
    assert stored["product_page_load_failed_rate"]["provenance"] == "meta.process_mining"
    df = data.metric("product_page_load_failed_rate", live - timedelta(hours=24), live, group_by=["country"])
    rate = df.numerator.sum() / df.denominator.sum()
    assert 0.01 < rate < 0.06                                          # simulator: ~3% of page requests fail
    assert "product_page_load_failed_rate" in g.nodes
    assert any(e.type == "causal" and e.src == "product_page_load_failed_rate" and e.sign == "-"
               for e in g.edges.values())                              # acts on the product-view funnel step


def test_step5_conformance_and_performance(agent, data, eye_db):
    _, live = eye_db
    assert {"payment_process_fitness", "session_process_fitness", "payment_outcome_seconds"} <= set(agent["process"])
    fit = data.metric("payment_process_fitness", live - timedelta(hours=24), live)
    assert fit.numerator.sum() / fit.denominator.sum() > 0.9            # history conforms to its own model
    hist = data.metric("payment_outcome_seconds", live - timedelta(hours=24), live, filters=PAY)
    fault = data.metric("payment_outcome_seconds", live, live + timedelta(hours=2), filters=PAY)
    h = hist.numerator.sum() / hist.denominator.sum()
    f = fault.numerator.sum() / fault.denominator.sum()
    assert f > 1.5 * h, (h, f)                                         # timeouts make the payment step slower


def test_investigation_persisted_in_agent_tables(agent, data, eye_db):
    _, live = eye_db
    data.now = live + timedelta(hours=2)
    anomaly = {"metric": "payment_success_rate", "segment": "country=DE|payment_provider=Stripe|platform=mobile",
               "ts": str(live + timedelta(hours=1)), "actual": 0.35, "expected": 0.96, "z": -20.0,
               "detector": "spike", "impact_eur_per_h": -500.0}
    agent["im"].handle([anomaly], live + timedelta(hours=2))
    inc = data.db.execute("select * from sentinel.incidents").fetchone()
    assert inc and inc["first_cause"] and inc["report_md"].startswith("## INC-")
    assert inc["label"] in ("supported_cause", "hypothesis", "unexplained")
    n_ev = data.db.execute("select count(*) as n from sentinel.evidence").fetchone()["n"]
    n_bus = data.db.execute("select count(*) as n from sentinel.bus_events").fetchone()["n"]
    assert n_ev > 3 and n_bus > n_ev
    assert eye_counts(data.db) == agent["before"]                       # still nothing written to eye.*


# ====================================================================== last: progressive publication (A3)
def test_unpublished_hour_is_not_final(eye_db):
    """A3: Eye publishes a live hour progressively (published_through); until it is published through its
    end + GRACE the hour is not final: not in the agent's clock, completeness 0."""
    import_eye(EYE)
    from eye import simulator
    dsn, live = eye_db
    d = EyeData(dsn, EYE)
    final_end = d.last_window_end()
    t = final_end
    aware_t = t.tz_localize("UTC").to_pydatetime()
    simulator.emit(d.db, aware_t, staged=True)
    simulator.publish_through(d.db, aware_t, aware_t + timedelta(minutes=30))   # half the hour published
    d2 = EyeData(dsn, EYE)
    assert d2.last_window_end() == final_end
    d2.now = t + timedelta(hours=1)                         # even if a caller pushes the clock forward
    assert d2.completeness("payments", t) == 0.0
    simulator.publish_through(d.db, aware_t, aware_t + timedelta(hours=1, minutes=15))
    d3 = EyeData(dsn, EYE)
    assert d3.last_window_end() == t + timedelta(hours=1)
