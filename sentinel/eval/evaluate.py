"""Fault-injection evaluation of the Monitor and the Investigator.

Each run injects known faults, innocent changes and data issues into the simulated marketplace
(dev/fake_data.py), lets the agent run unattended, and scores its alerts and reports against the
ground truth derived from the injected scenarios.

Monitor
  precision        alerts that match a real fault / all alerts
  recall           faults with at least one matching alert / faults
  FAR              hours without an active fault that got a false alert / such hours
  innocent FAR     same, restricted to hours of the planned campaign (innocent change)
  MTTD             first matching alert - fault onset (simulated hours)
  trigger rate     investigations opened per simulated hour
Investigator
  top-1 root cause the alerted incident names an accepted first-cause metric AND exactly the faulted segment
  root-cause recall in runs with concurrent faults: share of them found with a correct first cause
  label accuracy   supported cause only when the cause is recorded (release); planned change explained
  path recall      share of the true propagation path (metrics) the investigation visited
  steps            tool calls (ACTION events) per investigation
  MTTR             wall-clock seconds from opening an incident to its conclusion
  dead-end rate    investigations that called the meta-agent
  tool validity    tool calls that returned an observation
  tokens           0: the investigator's policy is deterministic (no LLM calls)

Graph walk and evidence (v3)
  nodes visited / pruned   per investigation: nodes decomposed by the BFS / neighbours pruned as normal
  secondary branches       anomalous branches off the first-cause chain (compound-incident candidates)
  hypotheses               hypotheses formed per investigation (v3 forms them only on anomalous branches)
  evidence anchoring       share of hypothesis judgements that cite existing evidence records
  structured records       share of evidence records with tool, signal, window and entity filled

  python -m sentinel.eval.evaluate                 all suite runs: v1 (monitor features off), v2 (on, greedy
                                                   investigator walk), v3 (v2 + pruned BFS investigator)
  python -m sentinel.eval.evaluate --quick         one run per configuration
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "state" / "eval"
LIVE = pd.Timestamp("2026-10-26")
CONFIGS = {"v1": {"scan": False, "riskloc": False, "process": False, "learning": False, "bfs": False,
                  "calibrate": False},
           "v2": {"scan": True, "riskloc": True, "process": True, "learning": True, "bfs": False, "calibrate": False},
           "v3": {"scan": True, "riskloc": True, "process": True, "learning": True, "bfs": True, "calibrate": False},
           # v3's investigator changes (pruned BFS) on the v1 monitor: isolates the BFS effect without v2
           "v1+bfs": {"scan": False, "riskloc": False, "process": False, "learning": False, "bfs": True,
                      "calibrate": False},
           # v4: v3 + data profile and calibration (dispersion, planned-effect end, target harm, slow harm)
           "v4": {"scan": True, "riskloc": True, "process": True, "learning": True, "bfs": True, "calibrate": True}}
DEFAULT_SCENARIOS = ["autumn_campaign", "late_partition", "android_payment_de", "ios_release", "for_you_model_shift"]


def payment(country, platform, start, end, version):
    return {"android_payment_de": {
        "start": f"{start}Z", "end": f"{end}Z", "scope": {"country": country, "platform": platform},
        "event": {"type": "release", "service": f"payments-{platform}", "description": f"{version} rollout"},
        "end_event": {"type": "release", "service": f"payments-{platform}", "description": "rollback"}}}


SUITE = [
    {"name": "de-android", "seed": 42, "overrides": {}, "scenarios": DEFAULT_SCENARIOS},
    {"name": "fr-ios", "seed": 7, "scenarios": DEFAULT_SCENARIOS,
     "overrides": payment("fr", "ios", "2026-10-26T11:00", "2026-10-26T16:00", "6.1")},
    {"name": "es-web+supplier", "seed": 11, "scenarios": DEFAULT_SCENARIOS + ["supplier_delay_electronics"],
     "overrides": payment("es", "web", "2026-10-26T18:00", "2026-10-26T23:00", "2026.10.26")},
]

# The same faults on Chaimaa's Shoply simulator (branch chaimaa): all 7 spec scenarios, incl. the two silent
# faults without any logged event (search_relevance_de, pacing_bug) and ~2 harmless background releases a
# day as red herrings. Runs until 10-28 08:00 so that pacing_bug (10-28 00:00) is inside the window.
SHOPLY_SCENARIOS = DEFAULT_SCENARIOS + ["search_relevance_de", "pacing_bug"]
SHOPLY_UNTIL = "2026-10-28T08:00"
SHOPLY_SUITE = [
    {"name": "shoply-de-android", "data": "shoply", "seed": 42, "overrides": {}, "scenarios": SHOPLY_SCENARIOS},
    {"name": "shoply-fr-ios", "data": "shoply", "seed": 7, "scenarios": SHOPLY_SCENARIOS,
     "overrides": payment("fr", "ios", "2026-10-26T11:00", "2026-10-26T16:00", "6.1")},
    {"name": "shoply-es-web", "data": "shoply", "seed": 11, "scenarios": SHOPLY_SCENARIOS,
     "overrides": payment("es", "web", "2026-10-26T18:00", "2026-10-26T23:00", "2026.10.26")},
]
SUITES = {"fake": SUITE, "shoply": SHOPLY_SUITE}

# accepted first causes and the true propagation path per fault scenario
TRUTH = {
    "android_payment_de": {"metrics": {"payment_success_rate"}, "path": ["orders", "conversion_rate", "payment_success_rate"],
                           "label": "supported_cause"},
    "for_you_model_shift": {"metrics": {"feed_ctr", "feed_purchase_rate", "add_to_cart_rate", "feed_oos_share"},
                            "path": ["conversion_rate", "add_to_cart_rate"], "label": "supported_cause",
                            "scope": {"channel": "for_you"}},
    "supplier_delay_electronics": {"metrics": {"in_stock_rate", "add_to_cart_rate", "feed_purchase_rate"},
                                   "path": ["add_to_cart_rate", "in_stock_rate"], "label": None,
                                   "scope": {"category": "electronics"}},
    # silent faults (Shoply): no event is logged, so no label is prescribed; finding the metric and scope counts
    "search_relevance_de": {"metrics": {"search_relevance", "search_ctr"}, "path": ["search_ctr", "search_relevance"],
                            "label": None, "scope": {"country": "de"}},
    "pacing_bug": {"metrics": {"pacing_ratio", "ad_revenue", "sponsored_clicks", "avg_cpc"},
                   "path": ["ad_revenue", "pacing_ratio"], "label": None, "scope": {"advertiser": "adv_017"}},
}


def _sd(s) -> dict:
    """A scenario as a dict: dev/fake_data keeps dicts, Shoply keeps dataclasses."""
    if isinstance(s, dict):
        return s
    return {"start": s.start, "end": s.end, "scope": dict(s.scope or {})}


def ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_convert("UTC").tz_localize(None) if t.tzinfo else t


def seg(s: str) -> dict:
    return dict(p.split("=", 1) for p in s.split("|")) if s else {}


def overlaps(a: dict, b: dict) -> bool:
    return all(b[k] == v for k, v in a.items() if k in b)


def ground_truth(rt, until) -> dict:
    sc = rt.data.scenarios
    faults = []
    for sid, t in TRUTH.items():
        if sid not in rt.data.active:
            continue
        s = _sd(sc[sid])
        start = max(ts(s["start"]), LIVE)
        end = ts(s["end"]) if s.get("end") else until
        if start >= until:
            continue
        scope = t.get("scope") or {k: str(v) for k, v in (s.get("scope") or {}).items()}
        faults.append({"id": sid, "start": start, "end": end, "scope": scope, **t})
    camp = _sd(sc["autumn_campaign"])
    return {"faults": faults, "innocent": (ts(camp["start"]), ts(camp["end"])), "hours": int((until - LIVE) / pd.Timedelta(hours=1))}


def score(rt, gt: dict, wall: dict) -> dict:
    bus = rt.bus.history
    alerts = [e for e in bus if e.type == "FINAL_REPORT" and e.payload.get("kind") == "alert"]
    incs = rt.incidents.incidents
    matched, alert_ok = {}, []
    for a in alerts:
        at = ts(a.sim_ts)
        hit = None
        for f in gt["faults"]:
            s_alert = seg(a.payload["segment"])
            shares = any(f["scope"].get(k) == v for k, v in s_alert.items())
            if f["start"] <= at <= f["end"] + pd.Timedelta(hours=6) and overlaps(s_alert, f["scope"]) and \
                    (shares or (not s_alert and a.payload["first_cause"] in f["metrics"])):
                hit = f
                break
        alert_ok.append(hit is not None)
        if hit and hit["id"] not in matched:
            matched[hit["id"]] = a
    n_f = len(gt["faults"])
    fault_hours = set()
    for f in gt["faults"]:
        fault_hours |= {f["start"] + pd.Timedelta(hours=i) for i in range(int((f["end"] - f["start"]) / pd.Timedelta(hours=1)) + 7)}
    all_hours = [LIVE + pd.Timedelta(hours=i + 1) for i in range(gt["hours"])]
    false_hours = {ts(a.sim_ts).floor("h") for a, ok in zip(alerts, alert_ok) if not ok}
    clean = [h for h in all_hours if h not in fault_hours]
    i0, i1 = gt["innocent"]
    innocent = [h for h in clean if i0 <= h <= i1 + pd.Timedelta(hours=1)]
    res = {
        "alerts": len(alerts), "faults": n_f,
        "precision": (sum(alert_ok) / len(alerts)) if alerts else None,
        "recall": len(matched) / n_f if n_f else None,
        "far": len(false_hours & set(clean)) / len(clean) if clean else None,
        "innocent_far": len(false_hours & set(innocent)) / len(innocent) if innocent else None,
        "mttd_h": (sum((ts(a.sim_ts) - next(f for f in gt["faults"] if f["id"] == fid)["start"]) / pd.Timedelta(hours=1)
                       for fid, a in matched.items()) / len(matched)) if matched else None,
        "trigger_rate": sum(e.type == "INCIDENT_OPENED" for e in bus) / gt["hours"],
    }
    # investigator
    top1, label_ok, path_rec, found = [], [], [], 0
    for f in gt["faults"]:
        a = matched.get(f["id"])
        if a is None:
            continue
        inc = incs[a.incident_id]
        root_ok = inc.first_cause in f["metrics"]
        seg_ok = seg(inc.segment) == f["scope"]
        top1.append(root_ok and seg_ok)
        found += root_ok
        if f["label"]:
            label_ok.append(inc.label == f["label"])
        visited = {e.payload.get("focus") for e in bus if e.incident_id == inc.id and e.type == "ACTION"}
        path_rec.append(len(visited & set(f["path"])) / len(f["path"]))
    camp_incs = [i for i in incs.values() if "channel=paid" in i.segment and i.first_cause]
    label_ok += [i.label == "explained" and i.id not in rt.incidents.alerted for i in camp_incs]
    opened = [e for e in bus if e.type == "INCIDENT_OPENED"]
    investigated = [i for i in incs.values() if any(e.incident_id == i.id and e.type == "ACTION" for e in bus)]
    actions = [e for e in bus if e.type == "ACTION"]
    obs = [e for e in bus if e.type == "OBSERVATION" and not e.payload.get("derived")]   # tool-call results only
    mttr = []
    for e in opened:
        end = next((x for x in bus if x.incident_id == e.incident_id and x.type == "THOUGHT"
                    and x.payload.get("stage") == "conclude"), None)
        if end:
            mttr.append((datetime.fromisoformat(end.wall_ts) - datetime.fromisoformat(e.wall_ts)).total_seconds())
    res.update({
        "top1_root_cause": sum(top1) / len(top1) if top1 else None,
        "root_cause_recall": found / n_f if n_f else None,
        "label_accuracy": sum(label_ok) / len(label_ok) if label_ok else None,
        "path_recall": sum(path_rec) / len(path_rec) if path_rec else None,
        "steps_per_investigation": len(actions) / len(investigated) if investigated else None,
        "mttr_s": sum(mttr) / len(mttr) if mttr else None,
        "dead_end_rate": sum(e.type == "DEAD_END" for e in bus) / len(investigated) if investigated else None,
        "tool_validity": len(obs) / len(actions) if actions else None,
        "tokens": 0, "investigations": len(investigated), "wall_s": wall["run"],
        **_v3_metrics(rt, bus, investigated),
        "matched": {k: {"alert_at": v.sim_ts, "incident": v.incident_id, "first_cause": incs[v.incident_id].first_cause,
                        "segment": incs[v.incident_id].segment, "label": incs[v.incident_id].label} for k, v in matched.items()},
        "false_alerts": [{"at": a.sim_ts, "first_cause": a.payload["first_cause"], "segment": a.payload["segment"]}
                         for a, ok in zip(alerts, alert_ok) if not ok],
    })
    return res


def _v3_metrics(rt, bus, investigated) -> dict:
    """Graph-walk and evidence-anchoring metrics (v3)."""
    cases = [rt.incidents.cases[i.id] for i in investigated if i.id in rt.incidents.cases]
    walks = [c.notes["walk"] for c in cases if c.notes.get("walk", {}).get("visited")]
    links = [l for c in cases for h in c.hyps for l in h.evidence]
    hyps = [h for c in cases for h in c.hyps]
    ev_ids = {e.id for c in cases for e in c.evidence}
    structured = [e for c in cases for e in c.evidence if e.tool and e.signal and e.window and e.entity]
    n_ev = sum(len(c.evidence) for c in cases)
    return {
        "nodes_visited": sum(len(w["visited"]) for w in walks) / len(walks) if walks else None,
        "nodes_pruned": sum(len(w["pruned"]) for w in walks) / len(walks) if walks else None,
        "secondary_branches": sum(len(w["secondary"]) for w in walks) / len(walks) if walks else None,
        "hypotheses_per_investigation": len(hyps) / len(cases) if cases else None,
        "evidence_anchoring": (sum(bool(l.evidence_ids) and all(i in ev_ids for i in l.evidence_ids) for l in links)
                               / len(links)) if links else None,
        "structured_records": len(structured) / n_ev if n_ev else None,
    }


def run_one(item: dict, config: str, until: pd.Timestamp) -> dict:
    from sentinel.run import Runtime
    out = Path(item.get("out", OUT))
    args = SimpleNamespace(no_meta=False, seed=item["seed"], overrides=item["overrides"], scenarios=item["scenarios"],
                           features=CONFIGS[config], data=item.get("data", "fake"),
                           events_path=str(out / f"events_{item['name']}_{config}.jsonl"))
    t0 = time.time()
    rt = Runtime(args)
    t1 = time.time()
    rt.run(until, 0.0, None)
    wall = {"setup": t1 - t0, "run": time.time() - t1}
    res = score(rt, ground_truth(rt, until), wall)
    res.update({"run": item["name"], "config": config, "setup_s": wall["setup"]})
    try:
        import psutil
        res["peak_rss_gb"] = round(psutil.Process().memory_info().peak_wset / 1e9, 2)             if hasattr(psutil.Process().memory_info(), "peak_wset") else None
    except Exception:            # noqa: BLE001 - memory figure is informational
        res["peak_rss_gb"] = None
    return res


def _child(item, config, until, q):
    warnings.filterwarnings("ignore")
    try:
        q.put(("ok", run_one(item, config, until)))
    except Exception as e:       # noqa: BLE001 - report the failure to the parent
        import traceback
        q.put(("error", f"{e}\n{traceback.format_exc()}"))


def run_isolated(item: dict, config: str, until: pd.Timestamp) -> dict:
    """One run per fresh process: all its memory is returned to the OS when it ends, so the peak
    stays at one run's footprint instead of growing across the suite."""
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    proc = ctx.Process(target=_child, args=(item, config, until, q))
    proc.start()
    status, payload = q.get()
    proc.join()
    if status != "ok":
        raise RuntimeError(f"run {item['name']} [{config}] failed:\n{payload}")
    return payload


KEYS = ["precision", "recall", "far", "innocent_far", "mttd_h", "trigger_rate", "top1_root_cause", "root_cause_recall",
        "label_accuracy", "path_recall", "steps_per_investigation", "mttr_s", "dead_end_rate", "tool_validity", "tokens",
        "nodes_visited", "nodes_pruned", "secondary_branches", "hypotheses_per_investigation", "evidence_anchoring",
        "structured_records"]


def aggregate(rows: list[dict]) -> dict:
    out = {}
    for cfg in CONFIGS:
        rs = [r for r in rows if r["config"] == cfg]
        out[cfg] = {k: (sum(r[k] for r in rs if r.get(k) is not None) / max(1, sum(r.get(k) is not None for r in rs))
                        if any(r.get(k) is not None for r in rs) else None) for k in KEYS}
    return out


def fmt(k, v):
    if v is None:
        return "–"
    if k in ("mttd_h",):
        return f"{v:.1f} h"
    if k in ("mttr_s",):
        return f"{v:.1f} s"
    if k in ("steps_per_investigation", "trigger_rate", "nodes_visited", "nodes_pruned", "secondary_branches",
             "hypotheses_per_investigation"):
        return f"{v:.2f}"
    if k == "tokens":
        return "0"
    return f"{v:.0%}"


def main(argv=None):
    warnings.filterwarnings("ignore")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    p = argparse.ArgumentParser(prog="sentinel.eval.evaluate")
    p.add_argument("--until", default="2026-10-27T14:00")
    p.add_argument("--quick", action="store_true")
    p.add_argument("--configs", default="v1,v2,v3")
    p.add_argument("--in-process", action="store_true", help="run everything in this process (more memory)")
    p.add_argument("--resume", action="store_true", help="keep finished runs from results.json")
    p.add_argument("--suite", choices=sorted(SUITES), default="fake",
                   help="fake: dev/fake_data.py; shoply: Chaimaa's Shoply simulator (needs .shoply-chaimaa)")
    a = p.parse_args(argv)
    out = OUT if a.suite == "fake" else OUT / a.suite
    out.mkdir(parents=True, exist_ok=True)
    until = a.until if a.suite == "fake" or "--until" in (argv or sys.argv) else SHOPLY_UNTIL
    suite = [{**item, "out": str(out)} for item in SUITES[a.suite]]
    suite = suite[:1] if a.quick else suite
    rows = []
    if a.resume and (out / "results.json").exists():
        rows = json.loads((out / "results.json").read_text(encoding="utf-8"))
    done = {(r["run"], r["config"]) for r in rows}
    runner = run_one if a.in_process else run_isolated
    for item in suite:
        for cfg in a.configs.split(","):
            if (item["name"], cfg) in done:
                print(f"run {item['name']} [{cfg}]: kept from results.json", flush=True)
                continue
            print(f"run {item['name']} [{cfg}] …", flush=True)
            r = runner(item, cfg, pd.Timestamp(until))
            rows.append(r)
            print("   " + ", ".join(f"{k}={fmt(k, r.get(k))}" for k in KEYS[:9]), flush=True)
            (out / "results.json").write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
    agg = aggregate(rows)
    cfgs = [c for c in CONFIGS if any(r["config"] == c for r in rows)]
    lines = ["| metric | " + " | ".join(cfgs) + " |", "|---|" + "---|" * len(cfgs)]
    for k in KEYS:
        lines.append(f"| {k} | " + " | ".join(fmt(k, agg[c][k]) for c in cfgs) + " |")
    table = "\n".join(lines)
    (out / "summary.md").write_text(table + "\n", encoding="utf-8")
    print(table)


if __name__ == "__main__":
    main()
