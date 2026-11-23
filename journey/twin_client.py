"""Thin client for the hospital system (the Twin today, a real HIS adapter later)."""

from __future__ import annotations

import os
import re
from typing import Any

import httpx


class TwinUnavailable(Exception):
    """Hospital system down, too slow, or erroring. The caller must get an honest message + callback."""


class TwinError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _seg(ident: Any) -> str:
    """An id that goes into a URL path. Ids come from the model, so nothing like '../admin' gets through."""
    if not isinstance(ident, str) or not _ID.match(ident):
        raise TwinError(404, "not_found", "Unknown id")
    return ident


class TwinClient:
    def __init__(self, base_url: str | None = None, timeout: float = 4.0, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url or os.getenv("TWIN_URL", "http://localhost:8001")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kw) -> Any:
        last_exc: Exception | None = None
        for attempt in range(2):  # one retry on a plain 500
            try:
                r = self._http.request(method, path, **kw)
            except httpx.HTTPError as e:  # connection refused, timeout, ...
                raise TwinUnavailable(f"{type(e).__name__} calling {path}") from e
            if r.status_code == 503:
                raise TwinUnavailable("hospital system reports it is down")
            if r.status_code >= 500:
                last_exc = TwinUnavailable(f"hospital system error {r.status_code}")
                continue
            if r.is_success:
                return r.json()
            try:
                body = r.json()
            except ValueError:
                body = {}
            raise TwinError(r.status_code, body.get("error", "error"), body.get("message", r.text[:200]))
        raise last_exc  # type: ignore[misc]

    def now(self) -> str:
        """The hospital system's clock (ISO, IST). Follows the demo clock; works even when chaos is on."""
        return self._request("GET", "/health")["now"]

    # -- directory
    def departments(self) -> list[dict]:
        return self._request("GET", "/departments")

    def practitioners(self, **filters) -> list[dict]:
        return self._request("GET", "/practitioners", params={k: v for k, v in filters.items() if v})

    def practitioner(self, pid: str) -> dict:
        return self._request("GET", f"/practitioners/{_seg(pid)}")

    # -- scheduling
    def slots(self, **filters) -> list[dict]:
        return self._request("GET", "/slots", params={k: v for k, v in filters.items() if v})

    def hold(self, slot_id: str, idempotency_key: str) -> dict:
        return self._request("POST", f"/slots/{_seg(slot_id)}/hold", json={"idempotency_key": idempotency_key})

    def release(self, hold_id: str) -> dict:
        return self._request("DELETE", f"/holds/{_seg(hold_id)}")

    def book(self, hold_id: str, patient_id: str, reason: str | None) -> dict:
        return self._request("POST", "/appointments", json={"hold_id": hold_id, "patient_id": patient_id, "reason": reason})

    def reschedule(self, appointment_id: str, hold_id: str) -> dict:
        return self._request("POST", f"/appointments/{_seg(appointment_id)}/reschedule", json={"hold_id": hold_id})

    def cancel(self, appointment_id: str, reason: str) -> dict:
        return self._request("POST", f"/appointments/{_seg(appointment_id)}/cancel", json={"reason": reason})

    def appointment(self, appointment_id: str) -> dict:
        return self._request("GET", f"/appointments/{_seg(appointment_id)}")

    def queue(self, appointment_id: str) -> dict:
        return self._request("GET", f"/appointments/{_seg(appointment_id)}/queue")

    def join_waitlist(self, **body) -> dict:
        return self._request("POST", "/waitlist", json=body)

    # -- patients
    def search_patients(self, **params) -> list[dict]:
        return self._request("GET", "/patients", params={k: v for k, v in params.items() if v})["candidates"]

    def patient(self, pid: str) -> dict:
        return self._request("GET", f"/patients/{_seg(pid)}")

    def register(self, **body) -> dict:
        return self._request("POST", "/patients", json=body)

    def appointments(self, pid: str) -> list[dict]:
        return self._request("GET", f"/patients/{_seg(pid)}/appointments")

    def reports(self, pid: str) -> list[dict]:
        return self._request("GET", f"/patients/{_seg(pid)}/reports")

    def invoices(self, pid: str) -> list[dict]:
        return self._request("GET", f"/patients/{_seg(pid)}/invoices")
