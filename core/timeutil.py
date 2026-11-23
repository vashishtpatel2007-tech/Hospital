"""Time helpers. Everything in Sahay is India Standard Time, stored as naive datetimes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def real_now_ist() -> datetime:
    """Current IST wall time as a naive datetime, second precision."""
    return datetime.now(IST).replace(tzinfo=None, microsecond=0)
