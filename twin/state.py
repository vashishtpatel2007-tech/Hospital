"""Runtime switches for the Twin: the demo clock and the chaos panel."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from core.timeutil import IST, real_now_ist  # noqa: F401  (re-exported)


@dataclass
class DemoClock:
    """Real IST time plus an offset that demos and tests can fast-forward."""

    offset: timedelta = field(default_factory=timedelta)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def now(self) -> datetime:
        with self._lock:
            return real_now_ist() + self.offset

    def advance(self, minutes: float) -> datetime:
        with self._lock:
            self.offset += timedelta(minutes=minutes)
        return self.now()

    def reset(self) -> datetime:
        with self._lock:
            self.offset = timedelta()
        return self.now()

    def snapshot(self) -> dict:
        return {"now": self.now().isoformat(), "offset_minutes": self.offset.total_seconds() / 60}


@dataclass
class Chaos:
    latency_ms: int = 0  # added to every non-admin request
    error_rate: float = 0.0  # 0..1 chance of HTTP 500
    his_down: bool = False  # every non-admin request -> 503
    steal_held_slot: bool = False  # next booking finds its held slot taken (one-shot)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, **changes) -> dict:
        with self._lock:
            for k, v in changes.items():
                if v is not None:
                    setattr(self, k, v)
        return self.snapshot()

    def reset(self) -> dict:
        return self.update(latency_ms=0, error_rate=0.0, his_down=False, steal_held_slot=False)

    def take_steal(self) -> bool:
        """Consume the one-shot 'steal held slot' switch."""
        with self._lock:
            hit, self.steal_held_slot = self.steal_held_slot, False
            return hit

    def snapshot(self) -> dict:
        # (dataclasses.asdict would try to deep-copy the lock)
        return {
            "latency_ms": self.latency_ms,
            "error_rate": self.error_rate,
            "his_down": self.his_down,
            "steal_held_slot": self.steal_held_slot,
        }
