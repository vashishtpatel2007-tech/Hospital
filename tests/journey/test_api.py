"""Stage 2: the Journey Engine HTTP API (what the dashboard and workers will call)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from journey.api import create_app
from tests.journey.conftest import RAMESH, dob


def test_api_end_to_end(eng, twin_admin):
    c = TestClient(create_app(eng))
    assert c.get("/health").json() == {"status": "ok", "contract": 3}
    assert "book_appointment" in c.get("/tools").json()

    s = c.post("/sessions", json={"phone": RAMESH["phone"], "channel": "web"}).json()
    sid, jid = s["session_id"], s["journey_id"]
    assert s["level"] == "SOFT"
    r = c.post(f"/sessions/{sid}/tools/verify_identity",
               json={"args": {"name": "Ramesh Sharma", "birth_date": dob(twin_admin, "P-1001")}}).json()
    assert r["ok"] and r["journey"]["state"] == "VERIFIED"
    r = c.post(f"/sessions/{sid}/tools/escalate", json={"args": {"urgency": "URGENT", "reason": "breathless"}}).json()
    assert r["journey"]["escalated"]

    # dashboard views
    assert c.get(f"/journeys/{jid}/verify").json()["ok"]
    assert any(e["type"] == "escalated" for e in c.get(f"/journeys/{jid}/events").json())
    assert "phone_hash" not in c.get(f"/journeys/{jid}").json()
    assert any(t["kind"] == "escalation" for t in c.get("/tasks").json())

    # humans must identify themselves; the ratchet holds over HTTP too
    assert c.post(f"/journeys/{jid}/urgency", json={"urgency": "ROUTINE", "staff_id": "", "reason": "x"}).status_code == 422
    ok = c.post(f"/journeys/{jid}/urgency", json={"urgency": "SOON", "staff_id": "N-07", "reason": "assessed"})
    assert ok.status_code == 200 and ok.json()["urgency"] == 1
    done = c.post(f"/journeys/{jid}/resolve-escalation", json={"staff_id": "N-07", "note": "handled"}).json()
    assert done["escalated"] is False

    assert c.get("/journeys/J-NOPE").status_code == 404
