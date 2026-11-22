"""Problem framing: from a paragraph of text to a problem on the metrics that exist.

An operator report, an alert digest or a ticket is a mix of symptoms, guesses about the cause, scope hints
and noise. This module turns it into a structured ProblemFrame and then hands it to the deterministic agent:

  1. understand   Devin (Jacob's v3 client, role `problem_frame`) maps the text onto the agent's catalog:
                  every symptom to an existing metric id, every scope hint to an existing dimension value,
                  every mechanism hint to an existing event activity; claimed causes are kept as claims.
                  Without Devin, a lexicon parser does the same for the names it can match literally.
  2. ground       local validation: unknown metric ids, dimensions, values and activities are dropped
                  (and listed), so nothing the text says can enter the analysis unless it exists in the data.
  3. verify       each mapped symptom is checked on the data over the stated window (deviation, z): the
                  text frames the question, the data answers it. Confirmed symptoms are evidence records.
  4. investigate  the strongest confirmed symptom becomes the entry of the usual investigation (graph walk,
                  localisation, controls, mechanism, events). Claimed causes are tested as `premise`
                  hypotheses: a claim the telemetry cannot identify stays unresolved, never confirmed.

The text never counts as evidence: conclusions come from the data, as in Jacob's benchmark rubric
("Frankfurt is an operator premise ... leave hosting location unresolved unless telemetry identifies it").
"""
from __future__ import annotations

import json
import os
import re
from datetime import timedelta

import numpy as np
import pandas as pd

from sentinel.contracts import seg_key

TEXT = {"type": "string"}


def _obj(fields: dict) -> dict:
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


FRAME_SCHEMA = _obj({
    "summary": TEXT,
    "symptoms": {"type": "array", "items": _obj({
        "phrase": TEXT, "metric_id": TEXT,
        "direction": {"enum": ["up", "down", "unknown"]},
        "role": {"enum": ["first_cause_candidate", "symptom", "unclear"]}})},
    "segment": {"type": "array", "items": _obj({"dimension": TEXT, "value": TEXT, "phrase": TEXT})},
    "onset": TEXT,                                   # ISO time or "" when the text gives none
    "claimed_causes": {"type": "array", "items": _obj({
        "phrase": TEXT, "kind": {"enum": ["release", "infrastructure", "provider", "traffic", "data", "other"]},
        "dimension": TEXT, "value": TEXT})},
    "activities": {"type": "array", "items": TEXT},
    "noise": {"type": "array", "items": TEXT},
    "questions": {"type": "array", "items": TEXT},
})

FRAME_PROMPT = """You frame an operational problem for a monitoring agent. The supplied JSON has the operator's
text and the agent's catalog: metrics (id, name, definition, unit, dimensions), the values each dimension takes
in the data, and the event activities of the process map. Map the text onto that catalog only:
- symptoms: every observable effect the text mentions, each with the single best matching metric id from the
  catalog (empty string if none fits), the direction if stated, and whether it reads as a possible first cause
  or as a downstream symptom;
- segment: scope hints as dimension/value pairs that exist in the catalog's dimension values;
- onset: the start time if the text states one (ISO 8601 UTC), else an empty string;
- claimed_causes: explanations the text asserts or suspects (releases, infrastructure, providers, traffic);
  give a dimension/value only if the catalog has one that identifies it, else empty strings;
- activities: event activities from the catalog that the text describes (e.g. timeouts);
- noise: parts of the text that are irrelevant to the problem;
- questions: what the data must answer to confirm or reject the framing.
Never invent metric ids, dimensions, values or activities. Claims are hypotheses, not facts."""


# ---------------------------------------------------------------- catalog context
def catalog_context(graph, data) -> dict:
    metrics = [{"id": n.id, "name": n.name, "definition": n.description[:240], "unit": n.unit,
                "dimensions": list(n.dimensions)} for n in graph.nodes.values()]
    values: dict[str, set] = {}
    if hasattr(data, "db"):                                     # Eye: dimension values from the observed scopes
        for r in data.db.execute("""select distinct jsonb_object_keys(scope) as k from observations""").fetchall():
            k = r["k"]
            values[k] = {x["v"] for x in data.db.execute(
                "select distinct scope->>%s as v from observations where scope ? %s limit 200", (k, k)).fetchall()
                if x["v"] is not None}
    else:                                                       # tabular sources: group-by on one hour of data
        for n in graph.nodes.values():
            for d in n.dimensions:
                if d in values:
                    continue
                try:
                    df = data.metric(n.id, data.now - timedelta(days=1), data.now, group_by=[d])
                    values[d] = set(map(str, df[d].unique()))
                except Exception:                               # noqa: BLE001 - best effort
                    pass
    acts = [a["activity"] for a in data.process_activities()] if hasattr(data, "process_activities") else []
    return {"metrics": metrics, "dimension_values": {k: sorted(v)[:60] for k, v in values.items()},
            "activities": acts}


# ---------------------------------------------------------------- understanding
class DevinFramer:
    """Text understanding through Jacob's Devin v3 client (one session per text)."""

    def __init__(self):
        from eye import devin as eye_devin
        from eye.model import configured_devin
        eye_devin.ROLES.setdefault("problem_frame", ("problem framing", FRAME_SCHEMA))
        self.client = configured_devin()
        if self.client is None:
            raise RuntimeError("DEVIN_API_KEY and DEVIN_ORG_ID are required for the Devin framer")

    def frame(self, text: str, context: dict) -> tuple[dict, dict]:
        out, prov = self.client.reason("problem_frame", FRAME_PROMPT, {"text": text, "catalog": context})
        return out or {}, prov or {}


COUNTRY_WORDS = {"germany": "DE", "german": "DE", "france": "FR", "french": "FR", "spain": "ES", "spanish": "ES",
                 "netherlands": "NL", "dutch": "NL", "poland": "PL", "polish": "PL", "italy": "IT"}


class LexiconFramer:
    """Offline fallback: literal matches of metric names/ids, dimension values and activities."""

    def frame(self, text: str, context: dict) -> tuple[dict, dict]:
        t = text.lower()
        syms = []
        for m in context["metrics"]:
            names = {m["id"].replace("_", " "), m["name"].lower()}
            if any(n and n in t for n in names):
                syms.append({"phrase": m["name"], "metric_id": m["id"], "direction": "unknown", "role": "unclear"})
        seg = []
        for d, vals in context["dimension_values"].items():
            for v in vals:
                if re.search(rf"\b{re.escape(str(v).lower())}\b", t):
                    seg.append({"dimension": d, "value": v, "phrase": v})
        for w, code in COUNTRY_WORDS.items():
            if w in t:
                for d, vals in context["dimension_values"].items():
                    if code in vals or code.lower() in vals:
                        seg.append({"dimension": d, "value": code if code in vals else code.lower(), "phrase": w})
        acts = [a for a in context["activities"] if a.replace("_", " ") in t or a in t]
        if "timeout" in t:
            acts += [a for a in context["activities"] if "timed_out" in a or "timeout" in a]
        return ({"summary": text[:200], "symptoms": syms, "segment": seg, "onset": "", "claimed_causes": [],
                 "activities": sorted(set(acts)), "noise": [], "questions": []}, {"reasoning_provider": "lexicon"})


def make_framer(prefer: str = "devin"):
    if prefer == "devin" and os.getenv("DEVIN_API_KEY"):
        try:
            return DevinFramer()
        except Exception:                                       # noqa: BLE001 - fall back to the lexicon
            pass
    return LexiconFramer()


# ---------------------------------------------------------------- grounding
def ground(frame: dict, graph, context: dict) -> tuple[dict, list[str]]:
    """Keep only what exists in the catalog and the data; return the cleaned frame and what was dropped."""
    dropped, vals = [], {d: {str(v).lower(): str(v) for v in vs} for d, vs in context["dimension_values"].items()}
    syms = []
    for s in frame.get("symptoms", []):
        if s.get("metric_id") in graph.nodes:
            syms.append(s)
        else:
            dropped.append(f"symptom '{s.get('phrase')}': no metric '{s.get('metric_id')}' in the catalog")
    seg = {}
    for x in frame.get("segment", []):
        d, v = x.get("dimension"), str(x.get("value", "")).lower()
        if d in vals and v in vals[d]:
            seg[d] = vals[d][v]
        else:
            dropped.append(f"scope '{x.get('phrase')}': {d}={x.get('value')} does not exist in the data")
    acts = [a for a in frame.get("activities", []) if a in context["activities"]]
    dropped += [f"activity '{a}' is not in the process map" for a in frame.get("activities", [])
                if a not in context["activities"]]
    claims = []
    for c in frame.get("claimed_causes", []):
        d, v = c.get("dimension") or "", str(c.get("value") or "").lower()
        claims.append({**c, "dimension": d if d in vals and v in vals.get(d, {}) else "",
                       "value": vals[d][v] if d in vals and v in vals.get(d, {}) else ""})
    onset = None
    if frame.get("onset"):
        try:
            onset = pd.Timestamp(frame["onset"]).tz_convert("UTC").tz_localize(None) \
                if pd.Timestamp(frame["onset"]).tzinfo else pd.Timestamp(frame["onset"])
        except (ValueError, TypeError):
            dropped.append(f"onset '{frame['onset']}' is not a time")
    return ({**frame, "symptoms": syms, "segment_map": seg, "activities": acts, "claimed_causes": claims,
             "onset_ts": onset}, dropped)


# ---------------------------------------------------------------- verification + investigation
def verify(rt, frame: dict, now) -> list[dict]:
    """Each mapped symptom on the data: deviation over [onset or now-3h, now) in the text's scope."""
    now = pd.Timestamp(now)
    start = frame.get("onset_ts") or now - timedelta(hours=3)
    start = min(pd.Timestamp(start).floor("h"), now - timedelta(hours=1))
    out = []
    for s in frame["symptoms"]:
        m = s["metric_id"]
        node = rt.graph.nodes[m]
        seg = {k: v for k, v in frame["segment_map"].items() if k in node.dimensions}
        try:
            w = rt.investigator.tools.window_dev(m, seg, start, now)
        except Exception as ex:                                  # noqa: BLE001 - unsliceable metric
            out.append({**s, "segment": seg, "confirmed": False, "why": f"cannot be read: {str(ex)[:80]}"})
            continue
        z, rel = float(w.get("z", 0.0)), w.get("rel", np.nan)
        dir_ok = s["direction"] == "unknown" or (s["direction"] == "down") == (z < 0)
        confirmed = bool(abs(z) >= 3 and dir_ok and np.isfinite(rel))
        out.append({**s, "segment": seg, "start": str(start), "z": round(z, 2),
                    "rel": None if not np.isfinite(rel) else round(float(rel), 4),
                    "actual": w.get("actual"), "expected": w.get("expected"), "confirmed": confirmed,
                    "why": (f"{rel:+.1%} (z={z:+.1f}) in {seg_key(seg) or 'total'}"
                            + ("" if dir_ok else " - opposite to the direction the text states"))})
    return out


def investigate_text(rt, text: str, now, framer=None, raw_frame: dict | None = None) -> dict:
    """Frame the text, verify it on the data and run the agent's investigation from the confirmed symptom.
    `raw_frame` reuses an earlier understanding of the same text (no second Devin session)."""
    now = pd.Timestamp(now)
    ctx = catalog_context(rt.graph, rt.data)
    if raw_frame is not None:
        raw, prov = raw_frame, {"reasoning_provider": "cached"}
    else:
        framer = framer or make_framer()
        raw, prov = framer.frame(text, {**ctx, "now_utc": str(now)})
    frame, dropped = ground(raw, rt.graph, ctx)
    checks = verify(rt, frame, now)
    rt.bus.publish("PROBLEM_FRAME", "framing", {
        "text": text, "provider": prov.get("reasoning_provider"), "summary": frame.get("summary"), "raw": raw,
        "symptoms": checks, "segment": frame["segment_map"], "claims": frame["claimed_causes"],
        "activities": frame["activities"], "noise": frame.get("noise", []), "dropped": dropped})
    conf = [c for c in checks if c["confirmed"]]

    def upstream_of(c):            # how many other confirmed symptoms lie downstream of this metric in the graph
        down = rt.graph.downstream(c["metric_id"])
        return sum(1 for o in conf if o is not c and o["metric_id"] in down)
    # the most upstream confirmed symptom is the entry; the text's own role label and |z| break ties
    confirmed = sorted(conf, key=lambda c: (-upstream_of(c), c["role"] != "first_cause_candidate", -abs(c["z"])))
    result = {"frame": frame, "checks": checks, "dropped": dropped, "provider": prov, "incident": None}
    if not confirmed:
        result["conclusion"] = "no symptom of the text is visible in the data over the stated window"
        return result
    entry = confirmed[0]
    hourly = rt.investigator.tools.hourly(entry["metric_id"], entry["segment"], pd.Timestamp(entry["start"]), now)
    peak = hourly.loc[hourly.z.abs().idxmax()] if not hourly.empty else None
    ts = pd.Timestamp(peak.ts) if peak is not None else pd.Timestamp(entry["start"])
    anomaly = {"metric": entry["metric_id"], "segment": seg_key(entry["segment"]), "ts": str(ts),
               "actual": float(peak.actual) if peak is not None else entry["actual"],
               "expected": float(peak.expected) if peak is not None else entry["expected"],
               "z": float(peak.z) if peak is not None else entry["z"], "detector": "text",
               "direction": "up" if entry["z"] > 0 else "down",
               "good_direction": rt.graph.nodes[entry["metric_id"]].good_direction, "planned_adjustment": [],
               "impact_eur_per_h": rt.monitor.impact_eur(entry["metric_id"], entry["segment"], ts,
                                                         float(peak.actual) if peak is not None else 0.0,
                                                         float(peak.expected) if peak is not None else 0.0),
               "frame": {"text": text, "checks": checks, "dropped": dropped, "summary": frame.get("summary"),
                         "noise": frame.get("noise", []), "activities": frame["activities"]},
               "claims": frame["claimed_causes"]}
    inc = rt.incidents._open(anomaly, now)
    case = rt.investigator.investigate(inc, anomaly, now)
    rt.incidents.cases[inc.id] = case
    rt.incidents.related[inc.id] = rt.incidents._related_set(case)
    rt.incidents._after_conclusion(inc, now)
    result.update(incident=inc.id, label=inc.label, first_cause=inc.first_cause, segment=inc.segment,
                  report_md=inc.report_md)
    return result


__all__ = ["FRAME_SCHEMA", "DevinFramer", "LexiconFramer", "make_framer", "catalog_context", "ground", "verify",
           "investigate_text", "json"]
