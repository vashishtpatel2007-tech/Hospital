"""Stage 2 fixtures: a Journey Engine on a throwaway SQLite DB, talking to a real Twin server."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from core.timeutil import real_now_ist
from journey.engine import JourneyEngine
from journey.twin_client import TwinClient

RAMESH = {"ref": "P-1001", "phone": "+919845012345", "name": "Ramesh Sharma"}
LAKSHMI = {"ref": "P-1002", "phone": "+919880023456", "name": "Lakshmi Narayan"}
KAVYA = {"ref": "P-1003", "phone": "+919900034567", "name": "Kavya Gowda"}
MANJUNATH = {"ref": "P-1004", "phone": "+919900034567", "name": "Manjunath Gowda"}
NEW_CALLER_PHONE = "+917700011122"  # not in the seed


class FakeClock:
    def __init__(self):
        self.t = real_now_ist()

    def __call__(self):
        return self.t

    def advance(self, minutes: float) -> None:
        self.t += timedelta(minutes=minutes)


@pytest.fixture
def twin_admin(twin_server):
    with httpx.Client(base_url=twin_server, timeout=30) as c:
        assert c.post("/admin/reset").status_code == 200
        yield c


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def outbox():
    return []


@pytest.fixture
def eng(twin_admin, twin_server, tmp_path, clock, outbox):
    e = JourneyEngine(f"sqlite:///{tmp_path}/journey.db", TwinClient(twin_server), now=clock,
                      otp_sender=lambda phone, code, purpose: outbox.append(
                          {"phone": phone, "code": code, "purpose": purpose}))
    yield e
    e.twin.close()
    e.engine.dispose()


def dob(twin_admin, ref: str) -> str:
    return twin_admin.get(f"/patients/{ref}").json()["birthDate"]


def ok(result: dict) -> dict:
    assert result["ok"], result["error"]
    return result


def refused(result: dict, code: str) -> dict:
    assert not result["ok"], f"expected {code}, got ok: {result['data']}"
    assert result["error"]["code"] == code, result["error"]
    return result


def verified_session(eng, twin_admin, who: dict) -> str:
    sid = eng.open_session(who["phone"])["session_id"]
    ok(eng.call(sid, "lookup_caller"))
    ok(eng.call(sid, "verify_identity", {"name": who["name"], "birth_date": dob(twin_admin, who["ref"])}))
    return sid


def held_slot(eng, sid: str, department: str = "CARD") -> dict:
    ok(eng.call(sid, "start_booking", {"reason": "check-up", "department_id": department}))
    slots = ok(eng.call(sid, "get_slots", {}))["data"]["slots"]
    # a slot at least a day away, so twin clock tricks never push it into the past
    slot = slots[-1]
    return ok(eng.call(sid, "hold_slot", {"slot_id": slot["slot_id"]}))
