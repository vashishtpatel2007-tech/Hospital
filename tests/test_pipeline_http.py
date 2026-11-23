"""Stages 1+2 end to end, over real HTTP: Twin server <- Journey Engine server <- client.

The three required journeys (new patient, returning patient reschedule, emergency), plus the
hospital system going down mid-call, all driven the way the agent and dashboard will drive them.
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn

from journey.api import create_app
from journey.engine import JourneyEngine
from journey.twin_client import TwinClient

RAMESH_PHONE = "+919845012345"
NEW_PHONE = "+917700099988"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def pipeline(twin_server, tmp_path):
    with httpx.Client(base_url=twin_server, timeout=30) as twin:
        assert twin.post("/admin/reset").status_code == 200
        outbox: list[dict] = []
        eng = JourneyEngine(f"sqlite:///{tmp_path}/journey.db", TwinClient(twin_server),
                            otp_sender=lambda phone, code, purpose: outbox.append({"code": code, "purpose": purpose}))
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(create_app(eng), host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 15
        while not server.started:
            assert time.time() < deadline, "journey server did not start"
            time.sleep(0.05)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as journey:
            yield twin, journey, outbox
        server.should_exit = True
        thread.join(timeout=5)
        eng.twin.close()
        eng.engine.dispose()


def tool(j: httpx.Client, sid: str, tool_name: str, **args) -> dict:
    r = j.post(f"/sessions/{sid}/tools/{tool_name}", json={"args": args})
    assert r.status_code == 200, r.text
    return r.json()


def ok(result: dict) -> dict:
    assert result["ok"], result["error"]
    return result


def test_journey_1_new_patient_registers_and_books(pipeline):
    twin, j, outbox = pipeline
    s = j.post("/sessions", json={"phone": NEW_PHONE, "channel": "voice"}).json()
    assert not s["caller_known"] and s["level"] == "NONE"
    sid = s["session_id"]
    ok(tool(j, sid, "list_departments"))
    r = ok(tool(j, sid, "register_patient", given="Asha", family="Kamath", gender="female", birth_date="1991-06-05",
                phone=NEW_PHONE, postal_code="560034", language="kn", caller_confirmed=True))
    assert r["data"]["status"] == "otp_sent" and r["data"]["sent_to"] == "******9988"
    r = ok(tool(j, sid, "check_otp", code=outbox[-1]["code"]))
    ref = r["data"]["patient_ref"]
    assert r["data"]["registered"] and r["session"]["level"] == "STRONG"
    assert twin.get(f"/patients/{ref}").json()["name"][0]["text"] == "Asha Kamath"
    ok(tool(j, sid, "start_booking", reason="fever for three days", department_id="GENM"))
    slots = ok(tool(j, sid, "get_slots", part_of_day="morning"))["data"]["slots"]
    held = ok(tool(j, sid, "hold_slot", slot_id=slots[0]["slot_id"]))
    assert "General Medicine" in held["data"]["read_back"]
    booked = ok(tool(j, sid, "book_appointment", patient_confirmed=True))
    assert booked["journey"]["state"] == "BOOKED"
    appt = twin.get(f"/appointments/{booked['data']['appointment_id']}").json()
    assert appt["participant"][0]["actor"]["id"] == ref and appt["status"] == "booked"
    ok(tool(j, sid, "end_call", summary="new patient booked"))
    jid = booked["journey"]["id"]
    assert j.get(f"/journeys/{jid}/verify").json()["ok"]


def test_journey_2_returning_patient_reschedules_and_checks_queue(pipeline):
    twin, j, _ = pipeline
    dob = twin.get("/patients/P-1001").json()["birthDate"]
    s = j.post("/sessions", json={"phone": "98450 12345"}).json()
    assert s["caller_known"] and s["greeting_names"] == ["Ramesh"] and s["level"] == "SOFT"
    sid = s["session_id"]
    assert tool(j, sid, "list_appointments")["error"]["code"] == "verification_required"
    ok(tool(j, sid, "verify_identity", name="Ramesh Sharma", birth_date=dob))
    appt = ok(tool(j, sid, "list_appointments"))["data"]["appointments"][0]
    assert appt["doctor"] == "Dr. Suresh Rao"
    q = ok(tool(j, sid, "get_queue_status", appointment_id=appt["appointment_id"]))["data"]
    assert q["token"] >= 1
    ok(tool(j, sid, "start_reschedule", appointment_id=appt["appointment_id"]))
    slots = ok(tool(j, sid, "get_slots", part_of_day="morning"))["data"]["slots"]
    choice = next(x for x in slots if x["start"] != appt["start"])
    ok(tool(j, sid, "hold_slot", slot_id=choice["slot_id"]))
    moved = ok(tool(j, sid, "book_appointment", patient_confirmed=True))
    assert moved["data"]["appointment_id"] == appt["appointment_id"] and moved["data"]["mode"] == "reschedule"
    now = twin.get(f"/appointments/{appt['appointment_id']}").json()
    assert now["start"] == choice["start"] and now["rescheduledFrom"] == appt["start"]
    ok(tool(j, sid, "end_call"))
    # next call resumes the same journey, at BOOKED, but must verify again
    s2 = j.post("/sessions", json={"phone": RAMESH_PHONE}).json()
    assert s2["resumed"] and s2["state"] == "BOOKED" and s2["level"] == "SOFT"


def test_journey_3_emergency_escalates_and_a_nurse_takes_over(pipeline):
    twin, j, _ = pipeline
    s = j.post("/sessions", json={"phone": RAMESH_PHONE}).json()
    sid, jid = s["session_id"], s["journey_id"]
    # an emergency needs no verification at all
    r = ok(tool(j, sid, "escalate", urgency="EMERGENCY", reason="chest pain spreading to left arm",
                verbatim="seene mein dard hai, left haath tak"))
    assert r["journey"]["escalated"] and r["data"]["staff_within_minutes"] == 0
    assert set(r["allowed_tools"]) == {"create_callback", "escalate", "transfer_to_human", "end_call"}
    assert tool(j, sid, "start_booking", reason="x")["error"]["code"] == "escalated_frozen"
    ok(tool(j, sid, "transfer_to_human", reason="emergency", desk="nurse"))

    tasks = [t for t in j.get("/tasks").json() if t["journey_id"] == jid]
    assert {t["kind"] for t in tasks} >= {"escalation", "transfer"}
    esc = next(t for t in tasks if t["kind"] == "escalation")
    assert esc["urgency"] == 3 and "seene mein dard" in esc["data"]  # the nurse hears the caller's own words
    assert j.post(f"/journeys/{jid}/urgency", json={"urgency": "ROUTINE", "staff_id": "", "reason": "x"}).status_code == 422
    assert j.post(f"/journeys/{jid}/resolve-escalation", json={"staff_id": "N-07", "note": "patient in ER"}).json()[
        "escalated"] is False
    events = j.get(f"/journeys/{jid}/events").json()
    kinds = [e["type"] for e in events]
    assert "escalated" in kinds and "escalation_resolved" in kinds
    assert all(e["actor"] in {"ai", "system", "human", "sentinel", "patient"} for e in events)
    assert j.get(f"/journeys/{jid}/verify").json()["ok"]
    assert "phone_hash" not in j.get(f"/journeys/{jid}").json()


def test_hospital_system_down_mid_call_then_recovers(pipeline):
    twin, j, _ = pipeline
    dob = twin.get("/patients/P-1001").json()["birthDate"]
    sid = j.post("/sessions", json={"phone": RAMESH_PHONE}).json()["session_id"]
    ok(tool(j, sid, "verify_identity", name="Ramesh Sharma", birth_date=dob))
    ok(tool(j, sid, "start_booking", reason="BP review", department_id="CARD"))
    twin.post("/admin/chaos", json={"his_down": True})
    r = tool(j, sid, "get_slots")
    assert r["error"]["code"] == "system_unavailable" and r["journey"]["state"] == "INTAKE"
    assert any(t["kind"] == "pending_sync" for t in j.get("/tasks").json())
    twin.post("/admin/chaos", json={"his_down": False, "error_rate": 0.3})  # flaky, not down: one retry absorbs most
    results = [tool(j, sid, "list_departments") for _ in range(10)]
    assert sum(r["ok"] for r in results) >= 6  # P(fail) per call is 0.3 squared
    twin.post("/admin/chaos", json={"error_rate": 0})
    slots = ok(tool(j, sid, "get_slots"))["data"]["slots"]
    ok(tool(j, sid, "hold_slot", slot_id=slots[-1]["slot_id"]))
    assert ok(tool(j, sid, "book_appointment", patient_confirmed=True))["journey"]["state"] == "BOOKED"
