"""Semantic helpers over contract metadata: units, populations, dimensions.

These are the checks behind "formula equality on sample data alone is insufficient"
(docs/harness.md): before an arithmetic or subset relationship is accepted, units,
populations, time semantics and dimensions must be compatible.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .formula import Unit

# ---------------------------------------------------------------- units
_UNIT_SYNONYMS = [
    (r"\beur cents?\b", "EURc"), (r"\bcents?\b", "EURc"), (r"\beur\b", "EURc*100"),
    (r"\bseconds?\b", "s"), (r"\bclicks?\b", "count"), (r"\bunits\b", "stock_unit"),
]


def normalize_unit(s: str) -> Unit:
    """'EUR cents per click' -> EURc/count ; 'units' -> stock_unit ; 'ratio' -> dimensionless."""
    t = (s or "").strip().lower()
    t = re.sub(r"\s+per\s+", " / ", t)
    for pat, rep in _UNIT_SYNONYMS:
        t = re.sub(pat, rep, t)
    t = t.replace("*100", "")
    return Unit.parse(t)


# ---------------------------------------------------------------- populations
@dataclass(frozen=True)
class Population:
    raw: str
    entity: str          # e.g. "sessions", "finalised_payment_attempts"
    qualifier: str       # e.g. "checkout", "timeout" ("" = whole entity)
    basis: str           # time basis, e.g. "session_start", "finalisation_time", "snapshot"

    @property
    def key(self) -> tuple[str, str]:
        return self.entity, self.basis


def parse_population(raw: str) -> Population:
    """Parse '<entity>[_with_<qualifier>]_by_<basis>' or '<entity>_at_snapshot'. Unknown shapes stay opaque."""
    s = (raw or "").strip().lower()
    basis = ""
    m = re.match(r"^(.*)_(?:by|at)_(.+)$", s)
    if m:
        s, basis = m.group(1), m.group(2)
    entity, _, qualifier = s.partition("_with_")
    return Population(raw, entity, qualifier, basis)


def subset_relation(child: str, parent: str) -> bool:
    """True if child population is a qualified subset of parent (same entity and time basis)."""
    c, p = parse_population(child), parse_population(parent)
    return bool(c.entity) and c.key == p.key and c.qualifier != "" and p.qualifier == ""


# ---------------------------------------------------------------- time semantics
FLOW_SEMANTICS = {"event_time", "session_cohort"}


def time_compatible(a: str, b: str) -> bool:
    """Exact arithmetic needs identical time semantics. Session-cohort and event-time flows are both
    flows but are NOT bucket-aligned (a cohort's orders confirm in later event-time buckets)."""
    return a == b


ADDITIVE_POLICIES = {"sum"}
RATIO_POLICIES = {"ratio_of_sums"}
