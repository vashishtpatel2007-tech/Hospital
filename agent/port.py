"""How the agent reaches the Journey Engine: in-process (tests, eval) or over HTTP (live calls).

Live calls should go over HTTP to the one `make journey` process: the engine keeps a small
in-memory vault (raw phone numbers for callbacks, registration drafts) that the dashboard
must share. Both ports return exactly the same result dicts.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx

from core.timeutil import real_now_ist

CONTRACT_VERSION = 3  # must match journey.api.CONTRACT_VERSION


class JourneyPort(Protocol):
    def open_session(self, phone: str | None, channel: str) -> dict: ...
    def call(self, session_id: str, tool: str, args: dict, actor: str = "ai") -> dict: ...
    def record_safety(self, session_id: str, kind: str, data: dict | None = None, actor: str = "sentinel") -> None: ...
    def journey(self, journey_id: str) -> dict: ...
    def snapshot(self, session_id: str) -> dict: ...
    def tool_descriptions(self) -> dict[str, str]: ...
    def now(self) -> datetime: ...


def _parse_now(iso: str | None) -> datetime:
    if not iso:
        return real_now_ist()
    return datetime.fromisoformat(iso).replace(tzinfo=None, microsecond=0)


class _TwinClock:
    """The hospital system's (demo) clock, kept as an offset from real time and re-read every few seconds
    (so a demo-clock jump shows up almost at once, without a request on every model call)."""

    def __init__(self, fetch) -> None:
        self.fetch = fetch
        self.offset: timedelta | None = None
        self.at = 0.0

    def now(self) -> datetime:
        if self.offset is None or time.monotonic() - self.at > 5:
            try:
                self.offset = _parse_now(self.fetch()) - real_now_ist()
            except Exception:  # the twin being down must not stop the call
                self.offset = self.offset or timedelta(0)
            self.at = time.monotonic()
        return real_now_ist() + self.offset


class LocalJourney:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.clock = _TwinClock(engine.twin.now)

    def open_session(self, phone: str | None, channel: str) -> dict:
        return self.engine.open_session(phone, channel)

    def call(self, session_id: str, tool: str, args: dict, actor: str = "ai") -> dict:
        return self.engine.call(session_id, tool, args, actor=actor)

    def record_safety(self, session_id: str, kind: str, data: dict | None = None, actor: str = "sentinel") -> None:
        self.engine.record_safety(session_id, kind, data, actor)

    def journey(self, journey_id: str) -> dict:
        return self.engine.journey(journey_id)

    def snapshot(self, session_id: str) -> dict:
        return self.engine.session_snapshot(session_id)

    def tool_descriptions(self) -> dict[str, str]:
        return {name: spec.description for name, spec in self.engine.tool_specs().items()}

    def now(self) -> datetime:
        return self.clock.now()


class HttpJourney:
    def __init__(self, base_url: str, twin_url: str | None = None, timeout: float = 10.0) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=timeout)
        self.twin_url = twin_url
        self.clock = _TwinClock(lambda: httpx.get(f"{twin_url}/health", timeout=1).json().get("now") if twin_url else None)

    def close(self) -> None:
        self.http.close()

    def _ok(self, r: httpx.Response) -> Any:
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = {"error": "http_error", "message": r.text[:200]}
            raise RuntimeError(f"journey API {r.status_code}: {body}")
        return r.json()

    def open_session(self, phone: str | None, channel: str) -> dict:
        return self._ok(self.http.post("/sessions", json={"phone": phone, "channel": channel}))

    def call(self, session_id: str, tool: str, args: dict, actor: str = "ai") -> dict:
        try:
            r = self.http.post(f"/sessions/{session_id}/tools/{tool}", json={"args": args, "actor": actor})
        except httpx.HTTPError as e:
            return {"ok": False, "tool": tool, "data": None,
                    "error": {"code": "system_unavailable", "message": f"journey engine unreachable ({type(e).__name__})"}}
        if r.status_code >= 400:  # a refused body, an unknown session, a server bug: a tool error, never a crash
            try:
                body = r.json()
            except ValueError:
                body = {}
            code = {422: "invalid_input", 404: "unknown_session"}.get(r.status_code, body.get("error") or "internal_error")
            return {"ok": False, "tool": tool, "data": None,
                    "error": {"code": code, "message": str(body.get("message") or r.text)[:200]}}
        return r.json()

    def record_safety(self, session_id: str, kind: str, data: dict | None = None, actor: str = "sentinel") -> None:
        self._ok(self.http.post(f"/sessions/{session_id}/safety", json={"kind": kind, "data": data or {}, "actor": actor}))

    def journey(self, journey_id: str) -> dict:
        return self._ok(self.http.get(f"/journeys/{journey_id}"))

    def snapshot(self, session_id: str) -> dict:
        return self._ok(self.http.get(f"/sessions/{session_id}"))

    def check_contract(self) -> None:
        """Refuse a journey server running older code (it would silently drop safety events)."""
        got = self._ok(self.http.get("/health")).get("contract", 0)
        if got < CONTRACT_VERSION:
            raise RuntimeError(f"The journey server at {self.http.base_url} runs older code (contract {got}, need "
                               f"{CONTRACT_VERSION}). Restart it: make journey")

    def tool_descriptions(self) -> dict[str, str]:
        return {name: spec["description"] for name, spec in self._ok(self.http.get("/tools")).items()}

    def now(self) -> datetime:
        return self.clock.now()
