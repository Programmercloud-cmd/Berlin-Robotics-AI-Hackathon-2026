"""Virtual event time used by the publisher and the monitor."""

from datetime import timedelta

HOUR = timedelta(hours=1)
CADENCE = timedelta(minutes=5)
GRACE = timedelta(minutes=15)
DAY = timedelta(days=1)


def aligned_history(start, baseline):
    """Most recent baseline days at the same UTC hour and minute."""
    first = baseline.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if first > baseline:
        first -= DAY
    return [first - day * DAY for day in range(4)]
