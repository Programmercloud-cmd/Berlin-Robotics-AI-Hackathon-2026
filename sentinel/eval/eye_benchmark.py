"""Jacob's Test Lab benchmark (jacob/v1, eye/benchmark_cases.py), run for Eye and for Sentinel side by side.

The ten cases, their seeded history and their controls are Jacob's own (seed_template, CASES, CASE_HOURS):
one scored hour with the case's control, then three hours of ordinary recovery traffic, after three days
of matching history. Every case gets its own copy of the seeded database.

Hour by hour, Eye's simulator publishes the hour, Eye's own runtime processes it (five-minute rolling
windows, its investigation branches), then Sentinel takes its hourly tick on the same database. Both
are online: neither sees data that has not been published yet.

Jacob's runner scores reports with a Devin reviewer against each case's rubric (needs DEVIN_API_KEY).
Without it, this harness scores both systems deterministically with expectations taken from the same
rubrics: was the break detected, in the right metric family, with the right scope, without false
incidents on the benign cases and without the claims the rubric warns against.

  .venv-eye/Scripts/python -m sentinel.eval.eye_benchmark              all ten cases
  .venv-eye/Scripts/python -m sentinel.eval.eye_benchmark --cases timeouts,cart
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
import warnings
from datetime import timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
EYE = ROOT / ".eye-jacob"
OUT = ROOT / "state" / "eval" / "eye_benchmark"

# metric families (Eye catalog ids plus metrics the agent mines from activities)
FAMILY = {
    "payment": lambda m: "payment" in m or m in ("successful_payments",),
    "cart": lambda m: "cart" in m,
    "product_view": lambda m: "product_view" in m or "product_page" in m,
    "search": lambda m: any(k in m for k in ("search", "feed", "recommend", "assisted")),
    "checkout_stock": lambda m: any(k in m for k in ("checkout", "stock", "available_units", "unavailable")),
    "data": lambda m: m in ("data_completeness_ratio", "event_ingestion_lag_seconds") or "completeness" in m,
    "volume": lambda m: m in ("sessions", "orders", "product_view_sessions", "session_conversion_rate"),
}
DEBUG = False
FRAME_TEXT = False
FRAME_CACHE = False
# operator-style paragraphs per case: symptoms mixed with noise and an unverifiable premise
CASE_TEXTS = {
    "timeouts": ("Since about 09:00 UTC customers in Germany report that card payments fail in the mobile app. "
                 "Support gets many complaints and several say the payment spinner times out. The CDN dashboard "
                 "looked normal and marketing sent a newsletter this morning. Ops suspects the Frankfurt data "
                 "center."),
    "frankfurt": ("Since 09:00 UTC product pages fail to load for many shoppers. Ops believes the Frankfurt "
                  "hosting region is degraded. Sales also look lower than usual."),
    "cart": ("From 09:00 UTC mobile shoppers in Germany cannot add items to the cart; payments seem lower too. "
             "A new checkout release went out yesterday."),
}
DEBUG_SERIES = [("payment_success_rate", {}), ("payment_success_rate", {"country": "DE"}),
                ("payment_success_rate", {"country": "DE", "platform": "mobile"}),
                ("payment_success_rate", {"payment_provider": "Stripe"})]
DE_MOBILE = {"country": "DE", "platform": "mobile"}
PAY_SCOPE = {**DE_MOBILE, "payment_provider": "Stripe"}

# expectations derived from Jacob's rubrics (eye/benchmark_cases.py)
EXPECT = {
    "normal": {"benign": True},
    "surge": {"benign": True},
    "timeouts": {"breaks": [("payment", PAY_SCOPE)]},
    "receipts": {"breaks": [("payment", PAY_SCOPE)], "forbid": ["payment_timeout"]},
    "frankfurt": {"breaks": [("product_view", {})]},
    "cart": {"breaks": [("cart", DE_MOBILE)], "forbid": ["payment_success_rate", "payment_timeout"]},
    "concurrent": {"breaks": [("cart", DE_MOBILE), ("payment", PAY_SCOPE)]},
    "search": {"breaks": [("search", {})]},
    "inventory": {"breaks": [("checkout_stock", {})], "forbid": ["payment_success_rate", "payment_timeout"]},
    "storefront": {"breaks": [("data", {})], "data_issue": True},
}


def seg(s) -> dict:
    if isinstance(s, dict):
        return {k: str(v) for k, v in s.items()}
    return dict(p.split("=", 1) for p in s.split("|")) if s else {}


def family_of(metric: str) -> set[str]:
    return {f for f, test in FAMILY.items() if test(metric or "")}


def scope_ok(found: dict, want: dict) -> str:
    """exact: the found scope names every expected key with the right value (extra keys allowed only if
    the expectation is marketplace-wide); partial: right values for some keys, none wrong; wrong otherwise."""
    if not want:
        return "exact" if not found or set(found) <= {"category"} else "partial"
    wrong = [k for k, v in found.items() if k in want and want[k] != v]
    if wrong:
        return "wrong"
    hit = [k for k in want if found.get(k) == want[k]]
    return "exact" if len(hit) == len(want) else ("partial" if hit else "wrong")


def score_case(case_id: str, findings: list[dict], early: list[dict] | None = None) -> dict:
    """findings: [{metric, scope (dict), label, minutes, text}] of the incidents a system raised."""
    exp = EXPECT[case_id]
    out = {"incidents": len(findings)}
    if exp.get("benign"):
        out.update(detected=None, false_incidents=len(findings), passed=len(findings) == 0)
        return out
    per_break, used = [], set()
    for fam, want in exp["breaks"]:
        cands = [(i, f) for i, f in enumerate(findings) if fam in family_of(f["metric"]) and i not in used]
        best = max(cands, key=lambda x: ("wrong", "partial", "exact").index(scope_ok(x[1]["scope"], want)),
                   default=None)
        if best is None:
            per_break.append({"family": fam, "found": False})
            continue
        used.add(best[0])
        f = best[1]
        first = min((g["minutes"] for _, g in cands if g["minutes"] is not None), default=None)
        warn = [w["minutes"] for w in (early or []) if fam in family_of(w["metric"])
                and scope_ok(seg(w["segment"]), want) != "wrong"]
        if warn:                                     # an early warning in the right family counts as detection
            first = min([first, *warn]) if first is not None else min(warn)
        per_break.append({"family": fam, "found": True, "metric": f["metric"], "scope": f["scope"],
                          "scope_match": scope_ok(f["scope"], want), "minutes": first})
    forbidden = [f["metric"] for f in findings for x in exp.get("forbid", []) if x in f["metric"]
                 and not any(fam in family_of(f["metric"]) for fam, _ in exp["breaks"] if fam != "payment")]
    if exp.get("data_issue"):
        claims_business = [f for f in findings if f["label"] in ("supported_cause",) and "data" not in family_of(f["metric"])]
        forbidden += [f"business cause claimed on {f['metric']}" for f in claims_business]
    found = [b for b in per_break if b["found"]]
    out.update(
        breaks=per_break,
        detected=len(found) / len(per_break),
        scope=(sum(b["scope_match"] == "exact" for b in found) + 0.5 * sum(b["scope_match"] == "partial" for b in found))
        / len(per_break),
        minutes=min((b["minutes"] for b in found if b["minutes"] is not None), default=None),
        unrelated_incidents=len(findings) - len(used),
        forbidden_claims=forbidden,
        passed=len(found) == len(per_break) and all(b["scope_match"] != "wrong" for b in found) and not forbidden,
    )
    return out


# ------------------------------------------------------------------ one case, both systems
def run_case(dsn: str, case, seed: int, features: dict | None, log) -> dict:
    from eye import investigation, runtime, simulator
    from eye.benchmark_cases import CASE_HOURS
    from eye.db import connect
    from eye.intervals import HOUR

    from sentinel.run_eye import EyeRuntime
    identifier, name, control, benign, _ = case
    start = simulator.DEMO_START
    t0 = time.time()
    rt = EyeRuntime(dsn, EYE, events_path=str(OUT / f"events_{identifier}.jsonl"), features=features)
    log(f"  sentinel ready in {time.time() - t0:.0f}s (history through {rt.live_start})")
    from threading import Event
    never = Event()                                          # Jacob's runner passes a stop flag
    eye_s, sen_s = 0.0, 0.0
    early = []
    for hour in range(CASE_HOURS):
        clock = start + hour * HOUR
        t = time.time()
        with connect(dsn) as db:
            simulator.emit(db, clock, seed=seed, patterned=True, **(control if hour == 0 else {}))
        while runtime.step(dsn, None):                       # Eye: five-minute rolling ticks + investigation
            pass
        while investigation.finish_next_branch(dsn, never):
            pass
        eye_s += time.time() - t
        t = time.time()
        naive = pd.Timestamp(clock).tz_convert("UTC").tz_localize(None)
        if hasattr(rt.monitor, "early_tick") and rt.monitor.features.get("calibrate", True):
            rt.data.now = naive + timedelta(hours=1, minutes=15)   # what Eye has published for this hour
            for k in range(11, -1, -1):                        # the rolling windows that end inside this hour
                ws = naive - timedelta(minutes=5 * k)
                for w in rt.monitor.early_tick(ws):
                    early.append({**w, "minutes": (ws + timedelta(hours=1, minutes=15) - pd.Timestamp(start)
                                                   .tz_convert("UTC").tz_localize(None)) / pd.Timedelta(minutes=1)})
        rt.engine.update_window(naive)                       # Sentinel: hourly tick on the same database
        now = naive + timedelta(hours=1)
        rt.data.now = now
        rt.bus.sim_ts = str(now)
        anomalies = rt.monitor.tick(now)
        rt.incidents.handle(anomalies, now)
        rt.incidents.follow_up(now)
        sen_s += time.time() - t
        log(f"  hour {hour}: sentinel anomalies={len(anomalies)} incidents={len(rt.incidents.incidents)}")
        if DEBUG:
            for m, sg in DEBUG_SERIES:
                try:
                    d = rt.data.metric(m, naive, now, filters=sg)
                    if d.empty:
                        log(f"    {m} {sg}: no hour-aligned observation"); continue
                    r = d.iloc[0]
                    b = rt.monitor.baseline(m, sg)
                    sc = b.score(naive, r.value, r.denominator)
                    log(f"    {m} {sg}: actual {r.value:.4f} (n={r.denominator:.0f}) expected {sc['expected']:.4f} "
                        f"z={sc['z']:+.1f} complete={r.complete} key_in_history={b.med.get(__import__('sentinel.monitor.baseline', fromlist=['profile_key']).profile_key(naive)) is not None}")
                except Exception as ex:                          # noqa: BLE001
                    log(f"    {m} {sg}: {ex}")

    live0 = pd.Timestamp(start).tz_convert("UTC").tz_localize(None)
    framed = None
    if FRAME_TEXT and identifier in CASE_TEXTS:              # an operator paragraph about the same hours
        from sentinel.frame import investigate_text
        t = time.time()
        cache = OUT / "frame_cache.json"
        raw = json.loads(cache.read_text(encoding="utf-8")).get(identifier) if FRAME_CACHE and cache.exists() else None
        framed = investigate_text(rt, CASE_TEXTS[identifier], live0 + timedelta(hours=CASE_HOURS), raw_frame=raw)
        framed["seconds"] = round(time.time() - t, 1)
        log(f"  text framing ({framed['provider'].get('reasoning_provider')}, {framed['seconds']}s): "
            f"{framed.get('first_cause')} [{framed.get('segment')}] {framed.get('label')}")
    # Sentinel's findings: what it paged (FINAL_REPORT alerts) plus every investigated incident's conclusion
    alerts = [e for e in rt.bus.history if e.type == "FINAL_REPORT" and e.payload.get("kind") == "alert"]
    sen = [{"metric": e.payload["first_cause"], "scope": seg(e.payload["segment"]), "label": e.payload["label"],
            "minutes": (pd.Timestamp(e.sim_ts) - live0) / pd.Timedelta(minutes=1), "text": e.payload["report_md"][:600]}
           for e in alerts]
    sen_reports = []
    for e in alerts:
        case_ = rt.incidents.cases.get(e.incident_id)
        sen_reports.append({
            "incident_id": e.incident_id, "phase": "initial", "observed_through": e.sim_ts,
            "report": {"markdown": e.payload["report_md"], "label": e.payload["label"],
                       "first_cause": e.payload["first_cause"], "segment": e.payload["segment"]},
            "telemetry": [ev.model_dump() for ev in case_.evidence] if case_ is not None else []})
    # Eye's findings: its incidents (Eye opens an incident for what it reports)
    with connect(dsn) as db:
        incs = db.execute("""select i.id, i.metric_id, i.scope, i.opened_at, report->'break'->>'label' as step,
                                    report->>'summary' as summary from incidents i order by id""").fetchall()
        first = db.execute("select min(observed_at) as t from anomalies").fetchone()["t"]
        eye_reports = db.execute("select * from report_snapshots order by incident_id, phase desc").fetchall()
    eye = [{"metric": r["metric_id"] or "", "scope": seg(r["scope"] or {}), "label": r["step"] or "",
            "minutes": (pd.Timestamp(first) - pd.Timestamp(start)) / pd.Timedelta(minutes=1) if first else None,
            "text": (r["summary"] or "")[:600]} for r in incs]
    return {"id": identifier, "name": name, "benign": benign,
            "eye": {"findings": eye, "score": score_case(identifier, eye), "seconds": round(eye_s, 1),
                    "reports": eye_reports},
            "sentinel": {"findings": sen, "score": score_case(identifier, sen, early), "early_warnings": early,
                         "seconds": round(sen_s, 1),
                         "investigations": len(rt.incidents.incidents), "reports": sen_reports},
            "framed": None if framed is None else {k: v for k, v in framed.items() if k != "frame"}}


REPORT_CHARS, TELEMETRY_CHARS, PROMPT_BUDGET = 3000, 4000, 26000


def _fit_item(it: dict) -> dict:
    """Shorten one review item to the v1 prompt budget: report text and telemetry are cut, not dropped."""
    it = dict(it)
    rep_ = json.dumps(it.get("report"), default=str)
    if len(rep_) > REPORT_CHARS:
        it["report"] = rep_[:REPORT_CHARS] + " …[shortened]"
    tel = json.dumps(it.get("telemetry"), default=str)
    if len(tel) > TELEMETRY_CHARS:
        it["telemetry"] = tel[:TELEMETRY_CHARS] + " …[shortened]"
    return it


def pack_batches(cases: list[dict]) -> list[list[dict]]:
    """Greedy packing of review items into prompts below PROMPT_BUDGET characters (rubric repeated per batch)."""
    batches, cur, size = [], {}, 0
    for c in cases:
        for it in (_fit_item(x) for x in c["reports"]):
            n = len(json.dumps(it, default=str)) + (len(c["rubric"]) if c["id"] not in cur else 0)
            if size + n > PROMPT_BUDGET and cur:
                batches.append(list(cur.values()))
                cur, size = {}, 0
                n = len(json.dumps(it, default=str)) + len(c["rubric"])
            cur.setdefault(c["id"], {"id": c["id"], "rubric": c["rubric"], "reports": []})["reports"].append(it)
            size += n
    if cur:
        batches.append(list(cur.values()))
    return batches


class DevinV1:
    """Devin through the v1 sessions API (for keys without v3 organisation access). Same role schemas,
    prompt wording and structured output as Jacob's eye.devin.DevinReasoner; only the transport differs."""

    def __init__(self, key: str, timeout_s: int = 900, poll_s: int = 15):
        self.key, self.timeout_s, self.poll_s = key, timeout_s, poll_s

    def reason(self, role, system, context, log=print):
        import httpx
        from eye.devin import ROLES
        label, schema = ROLES[role]
        nl = chr(10)
        prompt = (f"{system}{nl}Complete this one task from the supplied JSON. Use only the supplied evidence. "
                  "Do not access repositories, browse, modify files, or ask for input. Submit the result as "
                  f"structured output that validates against this JSON schema and mark it final:{nl}"
                  f"{json.dumps(schema)}{nl}{json.dumps(context, default=str)}")
        with httpx.Client(base_url="https://api.devin.ai", headers={"Authorization": f"Bearer {self.key}"},
                          timeout=30) as c:
            r = c.post("/v1/sessions", json={"prompt": prompt, "title": f"Sentinel benchmark: {label}"})
            if r.status_code >= 400:
                raise RuntimeError(f"Devin v1 create session: HTTP {r.status_code} {r.text[:400]}")
            sid = r.json()["session_id"]
            log(f"Devin v1 session started ({sid[:12]}…)")
            deadline = time.monotonic() + self.timeout_s
            out, status = {}, None
            while time.monotonic() < deadline:
                time.sleep(self.poll_s)
                g = c.get(f"/v1/session/{sid}")
                g.raise_for_status()
                ses = g.json()
                status = ses.get("status_enum") or ses.get("status")
                so = ses.get("structured_output")
                if isinstance(so, dict) and so.get("reviews"):
                    out = so
                    if status in ("finished", "blocked", "stopped", "expired"):
                        break
                    continue           # keep polling until the session marks it final
                if status in ("finished", "blocked", "stopped", "expired"):
                    break
            if not out:            # fall back to the last JSON object in Devin's messages
                for m in reversed(ses.get("messages") or []):
                    txt = m.get("message") or ""
                    i, j = txt.find("{"), txt.rfind("}")
                    if i >= 0 and j > i:
                        try:
                            cand = json.loads(txt[i:j + 1])
                        except ValueError:
                            continue
                        if isinstance(cand, dict) and cand.get("reviews"):
                            out = cand
                            break
            if status not in ("finished", "stopped", "expired"):
                try:
                    c.delete(f"/v1/sessions/{sid}")
                except httpx.HTTPError:
                    pass
            log(f"Devin v1 session ended: status={status}, reviews={len(out.get('reviews', []))}")
            return out, {"reasoning_provider": "devin-v1", "reasoning_session_id": sid}


def review(results: list[dict], log) -> dict:
    """Jacob's benchmark review: one Devin session scores every report against its case rubric
    (observable_break / scope / evidence: supported, partial, unsupported, insufficient)."""
    import os
    from eye.benchmark_cases import CASES
    from eye.benchmark_runner import REVIEW_PROMPT, Review, review_item
    from eye.model import configured_devin
    devin = configured_devin()
    if devin is None and not os.getenv("DEVIN_API_KEY"):
        raise RuntimeError("set DEVIN_API_KEY (and DEVIN_ORG_ID for the v3 API) to run the Devin review")
    rubrics = {c[0]: c[-1] for c in CASES}
    cases = []
    for r in results:
        if "error" in r:
            continue
        items = []
        eye_initial = {x["incident_id"] for x in r["eye"]["reports"] if x["phase"] == "initial"}
        first_of_case = r is next(q for q in results if q["id"] == r["id"] and "error" not in q)
        for x in (r["eye"]["reports"] if first_of_case else []):
            it = review_item({"id": r["id"]}, x, x["incident_id"] in eye_initial)
            it["id"] = it["id"].replace(f"{r['id']}/", f"{r['id']}/eye-", 1)
            if "initial_report_id" in it:
                it["initial_report_id"] = it["initial_report_id"].replace(f"{r['id']}/", f"{r['id']}/eye-", 1)
            items.append(it)
        for x in r["sentinel"]["reports"]:
            tag = f"sentinel_{r.get('config', 'v4')}".replace("+", "_")
            items.append({"id": f"{r['id']}/{tag}-{x['incident_id']}/initial", "phase": "initial",
                          "report": x["report"], "telemetry": [
                              {k: e.get(k) for k in ("id", "signal", "entity", "interpretation", "verdict")}
                              for e in x["telemetry"]],
                          "observed_through": x["observed_through"]})
        if items:
            known = next((c for c in cases if c["id"] == r["id"]), None)
            if known is None:
                cases.append({"id": r["id"], "rubric": rubrics[r["id"]], "reports": items})
            else:
                known["reports"] += items
    expected = {it["id"] for c in cases for it in c["reports"]}
    log(f"Devin review: {len(expected)} reports in {len(cases)} cases (one session; can take several minutes)")
    answer, provenance = devin.reason("benchmark_review", REVIEW_PROMPT, {"cases": cases}) if devin else ({}, {})
    reviews = [Review.model_validate(row).model_dump() for row in answer.get("reviews", ())]
    if not reviews:          # v3 organisation API refused (403) or empty: v1 sessions API, in batches that fit
        log("v3 organisation API gave no result; using the v1 sessions API (prompts < 30,000 characters)")
        v1, sessions = DevinV1(os.environ["DEVIN_API_KEY"]), []
        for batch in pack_batches(cases):
            log(f"  batch: {sum(len(c['reports']) for c in batch)} report(s) of {', '.join(c['id'] for c in batch)}")
            ans, prov = v1.reason("benchmark_review", REVIEW_PROMPT, {"cases": batch}, log=log)
            sessions.append(prov.get("reasoning_session_id"))
            for row in ans.get("reviews", ()):
                try:
                    reviews.append(Review.model_validate(row).model_dump())
                except Exception:                              # noqa: BLE001 - malformed row: count as missing
                    pass
        provenance = {"reasoning_provider": "devin-v1", "sessions": len(sessions)}
    missing = expected - {rv["id"] for rv in reviews}
    return {"reviews": reviews, "missing": sorted(missing), "provenance": {k: v for k, v in provenance.items()
                                                                          if "key" not in k.lower()},
            "env_org": bool(os.getenv("DEVIN_ORG_ID"))}


def review_table(results: list[dict], rv: dict) -> str:
    pts = {"supported": 1.0, "partial": 0.5, "unsupported": 0.0, "insufficient": 0.0}
    lines = ["| case | system | reports | observable break | scope | evidence | unsupported claims |",
             "|---|---|---|---|---|---|---|"]
    systems = []
    for r in results:
        if "error" in r:
            continue
        for sysname in ("eye", f"sentinel_{r.get('config', 'v4')}".replace("+", "_")):
            if (r["id"], sysname) in systems:
                continue
            systems.append((r["id"], sysname))
            got = [x for x in rv["reviews"] if x["id"].startswith(f"{r['id']}/{sysname}-")]
            if not got:
                lines.append(f"| {r['id']} | {sysname} | 0 | - | - | - | - |")
                continue
            avg = {k: sum(pts[x[k]] for x in got) / len(got) for k in ("observable_break", "scope", "evidence")}
            lines.append(f"| {r['id']} | {sysname} | {len(got)} | {avg['observable_break']:.0%} | {avg['scope']:.0%} | "
                         f"{avg['evidence']:.0%} | {sum(len(x['unsupported_claims']) for x in got)} |")
    return "\n".join(lines)


def main(argv=None):
    global DEBUG, OUT
    warnings.filterwarnings("ignore")
    logging.disable(logging.INFO)          # keep warnings (e.g. a failed Devin request) visible
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    p = argparse.ArgumentParser(prog="sentinel.eval.eye_benchmark")
    p.add_argument("--cases", default="")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--config", default="v4", help="Sentinel feature set (see sentinel.eval.evaluate.CONFIGS)")
    p.add_argument("--configs", default="", help="several feature sets, e.g. v1,v2,v3,v4 (overrides --config)")
    p.add_argument("--debug", action="store_true", help="log Sentinel's expectation for the payment series")
    p.add_argument("--review", action="store_true", help="score the reports with Jacob's Devin reviewer")
    p.add_argument("--frame-text", action="store_true", help="also frame an operator paragraph per case (Devin)")
    p.add_argument("--frame-cache", action="store_true", help="reuse <out>/frame_cache.json instead of Devin")
    p.add_argument("--review-only", action="store_true", help="review the reports of the last run")
    p.add_argument("--out", default=str(OUT), help="output folder (results, reports, review)")
    a = p.parse_args(argv)
    DEBUG = a.debug
    global FRAME_TEXT, FRAME_CACHE
    FRAME_TEXT, FRAME_CACHE = a.frame_text, a.frame_cache
    OUT = Path(a.out)
    from sentinel.adapters.eye import import_eye
    if "fcntl" not in sys.modules:       # eye.benchmark locks its scoreboard with fcntl (POSIX only); this
        import types                     # harness never writes Jacob's scoreboard, so a stand-in is enough
        sys.modules["fcntl"] = types.ModuleType("fcntl")
    import_eye(EYE)
    from pgserver import get_server
    from psycopg import sql
    from eye.benchmark_cases import CASES
    from eye.benchmark_runner import seed_template
    from eye.db import connect

    from sentinel.eval.evaluate import CONFIGS
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    log = lambda m: print(f"[{time.time() - t0:6.0f}s] {m}", flush=True)  # noqa: E731
    cases = [c for c in CASES if not a.cases or c[0] in a.cases.split(",")]
    configs = a.configs.split(",") if a.configs else [a.config]
    results = []
    if a.review_only:
        results = json.loads((OUT / "results.json").read_text(encoding="utf-8"))
        cases = []
    with tempfile.TemporaryDirectory(prefix="sentinel-eye-bench-") as directory:
        server = get_server(Path(directory) / "data")
        try:
            with connect(server.get_uri()) as db:
                db.commit()
                db.autocommit = True
                db.execute("create database benchmark_baseline")
            log("seeding Jacob's benchmark template (3 days of matching hours) …")
            if cases:
                seed_template(server.get_uri(database="benchmark_baseline"), a.seed)
            for case in cases:
              for cfg in configs:
                database = f"case_{case[0]}_{cfg}".replace("+", "_")
                with connect(server.get_uri()) as db:
                    db.commit()
                    db.autocommit = True
                    db.execute(sql.SQL("create database {} template benchmark_baseline").format(sql.Identifier(database)))
                log(f"case {case[0]} [{cfg}] — {case[1]}")
                try:
                    r = run_case(server.get_uri(database=database), case, a.seed, CONFIGS.get(cfg), log)
                    r["config"] = cfg
                except Exception as ex:                                  # noqa: BLE001 - keep the suite going
                    import traceback
                    traceback.print_exc()
                    r = {"id": case[0], "name": case[1], "config": cfg, "error": str(ex)[:300]}
                with connect(server.get_uri()) as db:        # free the disk: the case database is not needed
                    db.commit()
                    db.autocommit = True
                    db.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(database)))
                results.append(r)
                (OUT / "results.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
                if "error" not in r:
                    log(f"  EYE      {json.dumps(r['eye']['score'], default=str)[:300]}")
                    log(f"  SENTINEL {json.dumps(r['sentinel']['score'], default=str)[:300]}")
        finally:
            server.cleanup()
    lines = ["| case | system | passed | detected | scope | first alert (min) | incidents | unrelated | forbidden |",
             "|---|---|---|---|---|---|---|---|---|"]
    eye_done = set()
    for r in results:
        if "error" in r:
            lines.append("| " + r["id"] + " | - | error | | | | | | |")
            continue
        for sysname in ("eye", "sentinel"):
            if sysname == "eye" and r["id"] in eye_done:
                continue
            eye_done.add(r["id"])
            label = "eye" if sysname == "eye" else f"sentinel {r.get('config', '')}".strip()
            s = r[sysname]["score"]
            det = "-" if s.get("detected") is None else format(s["detected"], ".0%")
            scp = "-" if s.get("scope") is None else format(s["scope"], ".0%")
            mins = "-" if s.get("minutes") is None else format(s["minutes"], ".0f")
            other = s.get("unrelated_incidents", s.get("false_incidents", 0))
            passed = "yes" if s["passed"] else "no"
            lines.append(f"| {r['id']} | {label} | {passed} | {det} | {scp} | {mins} | {s['incidents']} | "
                         f"{other} | {len(s.get('forbidden_claims', []))} |")
    table = "\n".join(lines)
    if a.review or a.review_only:
        rv = review(results, log)
        (OUT / "review.json").write_text(json.dumps(rv, indent=1, default=str), encoding="utf-8")
        table += "\n\nDevin review (Jacob's rubric v2 prompt):\n\n" + review_table(results, rv)
        if rv["missing"]:
            table += f"\n\nreports without a verdict: {len(rv['missing'])}"
    (OUT / "summary.md").write_text(table + "\n", encoding="utf-8")
    print(table)


if __name__ == "__main__":
    main()
