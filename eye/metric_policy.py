"""Registration defaults and virtual-time cadence for metric checks."""

from datetime import timedelta
from typing import Literal

Cadence = Literal[5, 15, 30, 60, 360, 1440]
CADENCES = (5, 15, 30, 60, 360, 1440)


def default_cadence(metric):
    if metric["calculation"].get("method") == "budget":
        return 1440
    return 60 if metric.get("direction") == "none" else 5


def check_due(metric, start):
    cadence = metric["access"].get("cadence_minutes", default_cadence(metric))
    end = start + timedelta(hours=1)
    if cadence == 1440 and metric["calculation"].get("method") == "budget":
        from zoneinfo import ZoneInfo

        end = end.astimezone(ZoneInfo("Europe/Berlin"))
    return (end.hour * 60 + end.minute) % cadence == 0
