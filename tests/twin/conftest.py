"""Twin contract-test fixtures (the server itself is in tests/conftest.py)."""

from __future__ import annotations

import httpx
import pytest


@pytest.fixture
def twin(twin_server) -> httpx.Client:
    """Fresh data, chaos off, real clock - for every test."""
    with httpx.Client(base_url=twin_server, timeout=30) as client:
        r = client.post("/admin/reset")
        assert r.status_code == 200, r.text
        yield client


@pytest.fixture
def free_slot(twin):
    """A free slot at least a day ahead (so clock tricks never push it into the past)."""
    slots = twin.get("/slots", params={"date_from": _days_ahead(twin, 2), "limit": 50}).json()
    assert slots, "seed should always have free slots"
    return slots[0]


def _days_ahead(client: httpx.Client, n: int) -> str:
    from datetime import date, timedelta

    now = client.get("/health").json()["now"]
    return (date.fromisoformat(now[:10]) + timedelta(days=n)).isoformat()


def hold(client: httpx.Client, slot_id: str, key: str | None = None) -> httpx.Response:
    import uuid

    return client.post(f"/slots/{slot_id}/hold", json={"idempotency_key": key or uuid.uuid4().hex})


def days_ahead(client: httpx.Client, n: int) -> str:
    return _days_ahead(client, n)
