"""Stage 2 audit: the cases the first pass did not cover.

Robustness (whatever the model sends, the engine answers), family phones, proxy tricks,
calls that go on after a journey closes, retries and races, and the HTTP surface.
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from journey import store
from journey.api import create_app
from journey.engine import JourneyEngine
from journey.twin_client import TwinClient
from tests.journey.conftest import (
    KAVYA,
    LAKSHMI,
    MANJUNATH,
    NEW_CALLER_PHONE,
    RAMESH,
    dob,
    held_slot,
    ok,
    refused,
    verified_session,
)

NEW = {"given": "Asha", "family": "Kamath", "gender": "female", "birth_date": "1991-06-05",
       "phone": NEW_CALLER_PHONE, "caller_confirmed": True}


def wrong(code: str) -> str:
    return "000000" if code != "000000" else "111111"


# ---------------------------------------------------------------- whatever the model sends


GARBAGE = [
    ("verified", "verify_identity", {"name": 123, "birth_date": "1968-03-14"}),
    ("verified", "verify_identity", {"name": "Ramesh Sharma", "birth_year": "nineteen", "postal_code": "560038"}),
    ("verified", "verify_identity", {"name": None}),
    ("verified", "set_caller_relation", {"relation": None}),
    ("verified", "set_caller_relation", {"relation": ["child"], "consent": True}),
    ("verified", "check_otp", {"code": None}),
    ("verified", "check_otp", {"code": {"x": 1}}),
    ("verified", "create_callback", {"reason": None}),
    ("verified", "create_callback", {"reason": "x", "urgency": None}),
    ("verified", "escalate", {"urgency": ["EMERGENCY"], "reason": "x"}),
    ("verified", "transfer_to_human", {"reason": {"a": 1}}),
    ("verified", "find_doctors", {"department_id": ["CARD"]}),
    ("verified", "start_booking", {"reason": True}),
    ("verified", "get_queue_status", {"appointment_id": None}),
    ("verified", "cancel_appointment", {"appointment_id": 7, "reason": "x", "patient_confirmed": True}),
    ("verified", "start_reschedule", {"appointment_id": ""}),
    ("new", "register_patient", NEW | {"given": None}),
    ("new", "register_patient", NEW | {"birth_date": ["1991-06-05"]}),
    ("new", "register_patient", NEW | {"gender": "f"}),
    ("new", "register_patient", NEW | {"postal_code": "5600"}),
    ("new", "register_patient", NEW | {"phone": "12345"}),
    ("new", "register_patient", NEW | {"birth_date": "2099-01-01"}),
]


@pytest.mark.parametrize("who,tool,args", GARBAGE, ids=[f"{t}-{i}" for i, (_, t, _) in enumerate(GARBAGE)])
def test_garbage_arguments_are_refused_never_crash(eng, twin_admin, who, tool, args):
    sid = verified_session(eng, twin_admin, RAMESH) if who == "verified" else eng.open_session(NEW_CALLER_PHONE)["session_id"]
    r = eng.call(sid, tool, args)
    assert r["ok"] is False and r["error"]["code"] != "internal_error", r["error"]
    assert r["journey"]["escalated"] is False  # garbage never escalates anything by accident
    jid = r["journey"]["id"]
    assert eng.verify_chain(jid).ok
    assert any(e["payload"].get("tool") == tool for e in eng.events(jid))  # refusals are audited too
    ok(eng.call(sid, "list_departments"))  # the call carries on


def test_args_that_are_not_an_object_are_refused(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    assert eng.call(sid, "list_departments", ["CARD"])["error"]["code"] == "invalid_input"  # type: ignore[arg-type]


def test_numbers_read_out_for_text_fields_are_accepted(eng, twin_admin, outbox):
    sid = eng.open_session(RAMESH["phone"])["session_id"]
    year = int(dob(twin_admin, "P-1001")[:4])
    ok(eng.call(sid, "verify_identity", {"name": "Ramesh Sharma", "birth_year": str(year), "postal_code": 560038}))
    ok(eng.call(sid, "send_otp"))
    code = outbox[-1]["code"]
    as_number = int(code) if not code.startswith("0") else code  # an int would drop a leading zero
    assert ok(eng.call(sid, "check_otp", {"code": as_number}))["session"]["level"] == "STRONG"


def test_a_crashing_tool_gives_a_clean_error(eng, twin_admin, monkeypatch):
    sid = verified_session(eng, twin_admin, RAMESH)
    monkeypatch.setattr(eng.twin, "departments", lambda: [{"unexpected": "shape"}])
    r = refused(eng.call(sid, "list_departments"), "internal_error")
    assert r["journey"]["state"] == "VERIFIED"
    assert eng.verify_chain(r["journey"]["id"]).ok
    monkeypatch.undo()
    ok(eng.call(sid, "list_departments"))


# ---------------------------------------------------------------- ids that try to escape


TRICKS = ["../admin/reset", "../../admin/chaos", "A-1/../../admin/clock", "..", "%2e%2e", "D-05/../../admin/reset",
          "A-1?x=1", "A 1", "x" * 80]


def test_ids_can_never_reach_admin_routes(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    held_slot(eng, sid)
    appt = ok(eng.call(sid, "book_appointment", {"patient_confirmed": True}))["data"]["appointment_id"]
    twin_admin.post("/admin/clock", json={"advance_minutes": 1})  # an /admin/reset would put this back to 0
    for bad in TRICKS:
        refused(eng.call(sid, "get_queue_status", {"appointment_id": bad}), "not_found")
        refused(eng.call(sid, "start_reschedule", {"appointment_id": bad}), "not_found")
        refused(eng.call(sid, "cancel_appointment", {"appointment_id": bad, "reason": "x", "patient_confirmed": True}),
                "not_found")
    assert twin_admin.get("/admin/clock").json()["offset_minutes"] == 1  # no reset happened
    assert twin_admin.get(f"/appointments/{appt}").json()["status"] == "booked"
    s2 = verified_session(eng, twin_admin, LAKSHMI)
    for bad in TRICKS:
        refused(eng.call(s2, "start_booking", {"reason": "x", "practitioner_id": bad}), "not_found")


# ---------------------------------------------------------------- family phones, proxies, reviews


def test_strong_verification_does_not_carry_to_another_patient(eng, twin_admin, outbox):
    sid = verified_session(eng, twin_admin, KAVYA)
    ok(eng.call(sid, "send_otp"))
    ok(eng.call(sid, "check_otp", {"code": outbox[-1]["code"]}))
    assert ok(eng.call(sid, "get_report_status"))["session"]["level"] == "STRONG"
    # the model forgets to record that she is calling for her father and verifies him directly
    r = ok(eng.call(sid, "verify_identity", {"name": "Manjunath Gowda", "birth_date": dob(twin_admin, "P-1004")}))
    assert r["session"]["level"] == "MEDIUM"
    refused(eng.call(sid, "get_report_status"), "verification_required")


def test_pending_code_does_not_carry_to_another_patient(eng, twin_admin, outbox):
    sid = verified_session(eng, twin_admin, KAVYA)
    ok(eng.call(sid, "send_otp"))
    ok(eng.call(sid, "verify_identity", {"name": "Manjunath Gowda", "birth_date": dob(twin_admin, "P-1004")}))
    refused(eng.call(sid, "check_otp", {"code": outbox[-1]["code"]}), "no_otp_pending")


def test_proxy_cannot_switch_back_to_self_to_hear_reports(eng, twin_admin, outbox):
    sid = eng.open_session(KAVYA["phone"])["session_id"]
    ok(eng.call(sid, "set_caller_relation", {"relation": "child", "consent": True}))
    ok(eng.call(sid, "verify_identity", {"name": "Manjunath Gowda", "birth_date": dob(twin_admin, "P-1004")}))
    ok(eng.call(sid, "send_otp"))
    ok(eng.call(sid, "check_otp", {"code": outbox[-1]["code"]}))
    refused(eng.call(sid, "get_report_status"), "proxy_not_authorised")
    r = ok(eng.call(sid, "set_caller_relation", {"relation": "self"}))
    assert r["data"]["verification_reset"] and r["session"]["level"] == "SOFT"
    refused(eng.call(sid, "get_report_status"), "verification_required")
    assert any(e["type"] == "verification_reset" for e in eng.events(r["journey"]["id"]))


def test_consent_must_be_a_real_yes(eng, twin_admin):
    sid = eng.open_session(KAVYA["phone"])["session_id"]
    for maybe in ("no", "yes", 1, "true"):
        refused(eng.call(sid, "set_caller_relation", {"relation": "child", "consent": maybe}), "consent_required")
    assert ok(eng.call(sid, "set_caller_relation", {"relation": "child", "consent": True}))["data"]["proxy"]


def test_family_phone_returns_each_patient_to_their_own_journey(eng, twin_admin, clock):
    s1 = verified_session(eng, twin_admin, KAVYA)
    ok(eng.call(s1, "start_booking", {"reason": "skin rash", "department_id": "DERM"}))
    kavya_j = eng.session(s1)["journey_id"]
    ok(eng.call(s1, "end_call"))

    clock.advance(10)
    s2 = eng.open_session(MANJUNATH["phone"])["session_id"]  # resumes Kavya's journey: caller unknown yet
    r = ok(eng.call(s2, "verify_identity", {"name": "Manjunath Gowda", "birth_date": dob(twin_admin, "P-1004")}))
    father_j = r["journey"]["id"]
    assert father_j != kavya_j
    ok(eng.call(s2, "end_call"))

    clock.advance(10)
    s3 = eng.open_session(KAVYA["phone"])
    assert s3["journey_id"] == father_j  # most recent
    r = ok(eng.call(s3["session_id"], "verify_identity", {"name": "Kavya Gowda", "birth_date": dob(twin_admin, "P-1003")}))
    assert r["journey"]["id"] == kavya_j and r["journey"]["state"] == "INTAKE"  # back where she left off
    with eng.engine.connect() as conn:
        open_ = conn.execute(select(store.journeys).where(store.journeys.c.state != "CLOSED")).mappings().all()
    assert len(open_) == 2


def test_patient_calling_from_another_phone_resumes_their_journey(eng, twin_admin):
    s1 = verified_session(eng, twin_admin, RAMESH)
    ok(eng.call(s1, "start_booking", {"reason": "BP review", "department_id": "CARD"}))
    jid = eng.session(s1)["journey_id"]
    ok(eng.call(s1, "end_call"))
    s2 = eng.open_session("+919000000001")["session_id"]  # a relative's phone, unknown number
    r = ok(eng.call(s2, "verify_identity", {"name": "Ramesh Sharma", "birth_date": dob(twin_admin, "P-1001")}))
    assert r["journey"]["id"] == jid and r["journey"]["state"] == "INTAKE"


def test_family_member_can_verify_while_another_registration_awaits_review(eng, twin_admin):
    s1 = eng.open_session(KAVYA["phone"])["session_id"]
    review = ok(eng.call(s1, "register_patient", {
        "given": "Kaavya", "family": "Gowda", "gender": "female", "birth_date": dob(twin_admin, "P-1003"),
        "phone": "+919000000002", "caller_confirmed": True}))
    assert review["journey"]["state"] == "DEDUPE_REVIEW"
    ok(eng.call(s1, "end_call"))

    s2 = eng.open_session(MANJUNATH["phone"])  # resumes the journey under review
    assert s2["state"] == "DEDUPE_REVIEW"
    r = ok(eng.call(s2["session_id"], "verify_identity",
                    {"name": "Manjunath Gowda", "birth_date": dob(twin_admin, "P-1004")}))
    assert r["journey"]["state"] == "VERIFIED" and r["journey"]["id"] != review["journey"]["id"]
    ok(eng.call(s2["session_id"], "start_booking", {"reason": "knee pain", "department_id": "ORTH"}))
    assert eng.journey(review["journey"]["id"])["state"] == "DEDUPE_REVIEW"  # still waiting for staff
    assert [t for t in eng.tasks() if t["kind"] == "dedupe_review"]


def test_same_phone_same_name_wrong_dob_goes_to_review(eng, twin_admin):
    real = dob(twin_admin, "P-1003")
    misheard = f"{int(real[:4]) - 1}{real[4:]}" if real[5:] != "02-29" else f"{int(real[:4]) - 4}{real[4:]}"
    sid = eng.open_session(KAVYA["phone"])["session_id"]
    r = ok(eng.call(sid, "register_patient", {"given": "Kavya", "family": "Gowda", "gender": "female",
                                              "birth_date": misheard, "phone": KAVYA["phone"], "caller_confirmed": True}))
    assert r["data"]["status"] == "needs_staff_review"
    assert "P-1003" in json.loads(eng.task(r["data"]["task_id"])["data"])["candidate_refs"]


def test_registration_code_never_grants_strong_on_an_existing_record(eng, twin_admin, outbox):
    sid = eng.open_session(NEW_CALLER_PHONE)["session_id"]
    ok(eng.call(sid, "register_patient", NEW))  # code goes to the number the caller gave
    ok(eng.call(sid, "verify_identity", {"name": "Ramesh Sharma", "birth_date": dob(twin_admin, "P-1001")}))
    r = refused(eng.call(sid, "check_otp", {"code": outbox[-1]["code"]}), "verification_required")
    assert r["session"]["level"] == "MEDIUM"
    refused(eng.call(sid, "get_report_status"), "verification_required")


# ---------------------------------------------------------------- the call goes on after a journey closes


def _book_then_cancel(eng, twin_admin) -> tuple[str, str, str]:
    sid = verified_session(eng, twin_admin, RAMESH)
    held_slot(eng, sid)
    appt = ok(eng.call(sid, "book_appointment", {"patient_confirmed": True}))["data"]["appointment_id"]
    r = ok(eng.call(sid, "cancel_appointment", {"appointment_id": appt, "reason": "travelling",
                                                "patient_confirmed": True}))
    assert r["journey"]["state"] == "CLOSED"
    return sid, appt, r["journey"]["id"]


def test_emergency_after_cancelling_still_escalates(eng, twin_admin):
    sid, _, closed = _book_then_cancel(eng, twin_admin)
    assert "escalate" in eng.allowed_tools(sid)
    r = ok(eng.call(sid, "escalate", {"urgency": "EMERGENCY", "reason": "chest pain started while talking"}))
    assert r["journey"]["escalated"] and r["journey"]["urgency"] == "EMERGENCY"
    assert r["journey"]["id"] != closed and eng.journey(closed)["state"] == "CLOSED"
    assert any(t["kind"] == "escalation" and t["urgency"] == 3 for t in eng.tasks(r["journey"]["id"]))
    assert eng.verify_chain(closed).ok and eng.verify_chain(r["journey"]["id"]).ok


def test_can_book_again_after_cancelling_in_the_same_call(eng, twin_admin):
    sid, _, closed = _book_then_cancel(eng, twin_admin)
    assert "start_booking" in eng.allowed_tools(sid)
    r = ok(eng.call(sid, "start_booking", {"reason": "new date", "department_id": "CARD"}))
    assert r["journey"]["state"] == "INTAKE" and r["journey"]["id"] != closed
    assert eng.journey(r["journey"]["id"])["patient_ref"] == "P-1001"


def test_queue_status_for_a_cancelled_appointment_is_refused(eng, twin_admin):
    sid, appt, _ = _book_then_cancel(eng, twin_admin)
    refused(eng.call(sid, "get_queue_status", {"appointment_id": appt}), "invalid_state")


def test_closed_journey_and_ended_call_take_no_tools(eng, twin_admin):
    sid, _, _ = _book_then_cancel(eng, twin_admin)
    ok(eng.call(sid, "end_call"))
    assert eng.allowed_tools(sid) == []
    refused(eng.call(sid, "escalate", {"urgency": "EMERGENCY", "reason": "x"}), "session_ended")


# ---------------------------------------------------------------- booking edge cases


def test_unknown_department_or_doctor_is_refused(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    refused(eng.call(sid, "start_booking", {"reason": "x", "department_id": "XYZ"}), "not_found")
    refused(eng.call(sid, "start_booking", {"reason": "x", "practitioner_id": "D-99"}), "not_found")
    assert ok(eng.call(sid, "start_booking", {"reason": "x", "department_id": "card"}))["data"]["department_id"] == "CARD"


def test_start_booking_by_doctor_and_release_hold(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    ok(eng.call(sid, "start_booking", {"reason": "follow-up", "practitioner_id": "D-05"}))
    slots = ok(eng.call(sid, "get_slots", {}))["data"]["slots"]
    assert slots and {s["doctor"] for s in slots} == {"Dr. Suresh Rao"} and len(slots) <= 5
    held = ok(eng.call(sid, "hold_slot", {"slot_id": slots[0]["slot_id"]}))
    hold_id = held["data"]["hold_id"]
    r = ok(eng.call(sid, "release_hold"))
    assert r["journey"]["state"] == "SLOT_SEARCH"
    assert twin_admin.delete(f"/holds/{hold_id}").json()["status"] == "released"
    assert twin_admin.post(f"/slots/{slots[0]['slot_id']}/hold", json={"idempotency_key": "other"}).status_code == 201


def test_waitlist_then_book_later(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    ok(eng.call(sid, "start_booking", {"reason": "follow-up", "department_id": "CARD"}))
    ok(eng.call(sid, "get_slots", {"part_of_day": "evening"}))
    w = ok(eng.call(sid, "join_waitlist", {"date_from": "2026-12-01", "date_to": "2026-12-05"}))
    assert w["journey"]["state"] == "WAITLISTED"
    assert twin_admin.get("/waitlist", params={"patient_id": "P-1001"}).json()
    slots = ok(eng.call(sid, "get_slots", {}))["data"]["slots"]
    ok(eng.call(sid, "hold_slot", {"slot_id": slots[0]["slot_id"]}))
    assert ok(eng.call(sid, "book_appointment", {"patient_confirmed": True}))["journey"]["state"] == "BOOKED"


def test_waitlist_with_bad_window_is_refused(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    ok(eng.call(sid, "start_booking", {"reason": "follow-up", "department_id": "CARD"}))
    ok(eng.call(sid, "get_slots", {}))
    refused(eng.call(sid, "join_waitlist", {"date_from": "2026-12-05", "date_to": "2026-12-01"}), "invalid_input")
    refused(eng.call(sid, "join_waitlist", {"date_from": "soon", "date_to": "later"}), "invalid_input")


def test_reschedule_when_the_new_slot_is_stolen_keeps_the_old_one(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    appt = ok(eng.call(sid, "list_appointments"))["data"]["appointments"][0]
    ok(eng.call(sid, "start_reschedule", {"appointment_id": appt["appointment_id"]}))
    slots = ok(eng.call(sid, "get_slots", {}))["data"]["slots"]
    ok(eng.call(sid, "hold_slot", {"slot_id": slots[-1]["slot_id"]}))
    twin_admin.post("/admin/chaos", json={"steal_held_slot": True})
    r = refused(eng.call(sid, "book_appointment", {"patient_confirmed": True}), "slot_taken")
    assert r["journey"]["state"] == "SLOT_SEARCH"
    still = twin_admin.get(f"/appointments/{appt['appointment_id']}").json()
    assert still["status"] == "booked" and still["start"] == appt["start"]  # nothing lost
    slots = ok(eng.call(sid, "get_slots", {}))["data"]["slots"]
    ok(eng.call(sid, "hold_slot", {"slot_id": slots[0]["slot_id"]}))
    moved = ok(eng.call(sid, "book_appointment", {"patient_confirmed": True}))
    assert moved["data"]["appointment_id"] == appt["appointment_id"] and moved["data"]["mode"] == "reschedule"


def test_bill_status_shows_cashless_state_for_lakshmi(eng, twin_admin, outbox):
    sid = verified_session(eng, twin_admin, LAKSHMI)
    ok(eng.call(sid, "send_otp"))
    ok(eng.call(sid, "check_otp", {"code": outbox[-1]["code"]}))
    bills = ok(eng.call(sid, "get_bill_status"))["data"]["bills"]
    assert any(b["insurance_status"] == "pre_auth_pending" for b in bills)


def test_otp_send_limit_per_call(eng, twin_admin, outbox):
    sid = verified_session(eng, twin_admin, RAMESH)
    for _ in range(3):
        ok(eng.call(sid, "send_otp"))
    refused(eng.call(sid, "send_otp"), "otp_limit")
    refused(eng.call(sid, "check_otp", {"code": outbox[0]["code"]}), "otp_incorrect")  # only the latest code works
    ok(eng.call(sid, "check_otp", {"code": outbox[-1]["code"]}))
    refused(eng.call(sid, "check_otp", {"code": outbox[-1]["code"]}), "no_otp_pending")  # a code works once


# ---------------------------------------------------------------- lockout


def test_lockout_holds_across_calls_then_lifts_after_30_minutes(eng, twin_admin, clock):
    real = dob(twin_admin, "P-1001")
    sid = eng.open_session(RAMESH["phone"])["session_id"]
    for _ in range(2):
        eng.call(sid, "verify_identity", {"name": "Ramesh Sharma", "birth_date": "1970-01-01"})
    s2 = eng.open_session(RAMESH["phone"])["session_id"]  # hang up and call again
    refused(eng.call(s2, "verify_identity", {"name": "Ramesh Sharma", "birth_date": real}), "locked")
    clock.advance(31)
    ok(eng.call(s2, "verify_identity", {"name": "Ramesh Sharma", "birth_date": real}))


# ---------------------------------------------------------------- escalation


def test_repeated_escalation_keeps_one_ticket_and_only_raises(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    a = ok(eng.call(sid, "escalate", {"urgency": "URGENT", "reason": "breathless on stairs"}))
    b = ok(eng.call(sid, "escalate", {"urgency": "EMERGENCY", "reason": "now chest pain"}))
    c = ok(eng.call(sid, "escalate", {"urgency": "ROUTINE", "reason": "feels better"}))
    assert a["data"]["handoff_task_id"] == b["data"]["handoff_task_id"] == c["data"]["handoff_task_id"]
    assert c["journey"]["urgency"] == "EMERGENCY" and c["data"]["staff_within_minutes"] == 0
    tickets = [t for t in eng.tasks(a["journey"]["id"]) if t["kind"] == "escalation"]
    assert len(tickets) == 1 and tickets[0]["urgency"] == 3


def test_staff_can_lower_urgency_only_with_an_id(eng, twin_admin):
    from journey.engine import ToolRefused
    sid = verified_session(eng, twin_admin, RAMESH)
    jid = ok(eng.call(sid, "escalate", {"urgency": "URGENT", "reason": "x"}))["journey"]["id"]
    with pytest.raises(ToolRefused):
        eng.set_urgency(jid, "ROUTINE", "ai", "model thinks it's fine")
    with pytest.raises(ToolRefused):
        eng.set_urgency(jid, "PANIC", "human", "x", "N-01")
    assert eng.set_urgency(jid, "ROUTINE", "human", "nurse assessed", "N-01")["urgency"] == 0
    with pytest.raises(ToolRefused):
        eng.system_advance(jid, "NOT_A_STATE", "x")


# ---------------------------------------------------------------- retries and races


def test_booking_retry_after_a_slow_hospital_system_never_double_books(twin_server, twin_admin, tmp_path, clock, outbox):
    e = JourneyEngine(f"sqlite:///{tmp_path}/late.db", TwinClient(twin_server, timeout=0.5), now=clock,
                      otp_sender=lambda *a: outbox.append(a))
    try:
        sid = verified_session(e, twin_admin, RAMESH)
        slot_start = held_slot(e, sid)["data"]["read_back"]
        twin_admin.post("/admin/chaos", json={"latency_ms": 1200})
        r = refused(e.call(sid, "book_appointment", {"patient_confirmed": True}), "system_unavailable")
        assert r["journey"]["state"] == "SLOT_HELD"
        time.sleep(1.5)  # the hospital system finishes the first request after we stopped waiting
        twin_admin.post("/admin/chaos", json={"latency_ms": 0})
        again = ok(e.call(sid, "book_appointment", {"patient_confirmed": True}))
        assert again["journey"]["state"] == "BOOKED" and slot_start
        appts = [a for a in twin_admin.get("/patients/P-1001/appointments").json() if a["status"] == "booked"]
        starts = [a["start"] for a in appts]
        assert len(starts) == len(set(starts))  # one appointment per time: no double booking
    finally:
        e.twin.close()
        e.engine.dispose()


def test_parallel_tool_calls_on_one_call_book_once(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    held_slot(eng, sid)
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: eng.call(sid, "book_appointment", {"patient_confirmed": True}), range(8)))
    assert sum(r["ok"] for r in results) == 1
    assert {r["error"]["code"] for r in results if not r["ok"]} == {"not_allowed_in_state"}
    assert eng.verify_chain(results[0]["journey"]["id"]).ok


def test_parallel_contacts_from_one_phone_share_one_journey(eng, twin_admin):
    barrier = threading.Barrier(6)

    def contact(_):
        barrier.wait()
        return eng.open_session(RAMESH["phone"], "whatsapp")["journey_id"]

    with ThreadPoolExecutor(6) as pool:
        assert len(set(pool.map(contact, range(6)))) == 1


def test_parallel_calls_for_different_patients_stay_separate(eng, twin_admin):
    people = [(RAMESH, "CARD"), (LAKSHMI, "GENM"), (KAVYA, "DERM")]

    def journey(who_dept):
        who, dept = who_dept
        sid = verified_session(eng, twin_admin, who)
        held_slot(eng, sid, dept)
        return ok(eng.call(sid, "book_appointment", {"patient_confirmed": True}))

    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(journey, people))
    assert len({r["journey"]["id"] for r in results}) == 3
    assert len({r["data"]["appointment_id"] for r in results}) == 3
    assert all(eng.verify_chain(r["journey"]["id"]).ok for r in results)


# ---------------------------------------------------------------- privacy across all of the above


def test_privacy_scan_after_the_edge_case_flows(eng, twin_admin, outbox, clock):
    test_family_phone_returns_each_patient_to_their_own_journey(eng, twin_admin, clock)
    test_proxy_cannot_switch_back_to_self_to_hear_reports(eng, twin_admin, outbox)
    sid = eng.open_session(NEW_CALLER_PHONE)["session_id"]
    eng.call(sid, "escalate", {"urgency": "SOON", "reason": "my number is 77000 11122, DOB 5 June 1991"})
    blob = []
    with eng.engine.connect() as conn:
        for table in store.metadata.sorted_tables:
            for row in conn.execute(select(table)).mappings():
                blob.append(json.dumps({k: str(v) for k, v in row.items() if not k.endswith("hash")}))
    text = "\n".join(blob)
    for secret in [KAVYA["phone"][3:], "77000 11122", dob(twin_admin, "P-1003"), dob(twin_admin, "P-1004"),
                   "Gowda", "1991"] + [o["code"] for o in outbox]:
        assert secret not in text, f"stored: {secret}"
    assert not re.search(r"(?<![0-9A-Za-z])[6-9]\d{9}(?![0-9A-Za-z])", text)


# ---------------------------------------------------------------- HTTP surface


def test_http_errors_are_clean(eng, twin_admin):
    c = TestClient(create_app(eng))
    sid = c.post("/sessions", json={"phone": RAMESH["phone"]}).json()["session_id"]
    assert c.get(f"/sessions/{sid}/tools").json()["allowed_tools"]
    jid = eng.session(sid)["journey_id"]
    assert c.post(f"/journeys/{jid}/urgency", json={"urgency": "PANIC", "staff_id": "N-1", "reason": "x"}).status_code == 422
    assert c.get("/journeys/J-NOPE/events").status_code == 404
    assert c.get("/journeys/J-NOPE/verify").status_code == 404
    assert c.post("/tasks/T-NOPE/resolve", json={"staff_id": "S-1", "note": "x"}).status_code == 404
    assert c.post("/journeys/J-NOPE/resolve-escalation", json={"staff_id": "S-1", "note": "x"}).status_code == 404
    r = c.post(f"/sessions/{sid}/tools/launch_rockets", json={"args": {}})
    assert r.status_code == 200 and r.json()["error"]["code"] == "unknown_tool"
    assert c.post("/sessions/C-NOPE/tools/lookup_caller", json={"args": {}}).json()["error"]["code"] == "unknown_session"
    assert c.post(f"/sessions/{sid}/tools/lookup_caller", json={"args": "x"}).status_code == 422
    assert c.post("/sessions", json={"phone": "not a number"}).json()["level"] == "NONE"


def test_http_dedupe_review_by_staff(eng, twin_admin):
    c = TestClient(create_app(eng))
    laxmi = twin_admin.get("/patients", params={"name": "Laxmi Devi"}).json()["candidates"]
    laxmi_dob = next(x for x in laxmi if x["patient"]["name"][0]["text"] == "Laxmi Devi")["patient"]["birthDate"]
    sid = c.post("/sessions", json={"phone": NEW_CALLER_PHONE}).json()["session_id"]
    r = c.post(f"/sessions/{sid}/tools/register_patient", json={"args": {
        "given": "Laksmi", "family": "Devi", "gender": "female", "birth_date": laxmi_dob, "phone": NEW_CALLER_PHONE,
        "caller_confirmed": True}}).json()
    task = r["data"]["task_id"]
    assert c.post(f"/tasks/{task}/dedupe", json={"staff_id": "S-1", "decision": "maybe"}).status_code == 422
    assert c.post(f"/tasks/{task}/dedupe", json={"staff_id": "", "decision": "new"}).status_code == 422
    done = c.post(f"/tasks/{task}/dedupe", json={"staff_id": "S-1", "decision": "new"})
    assert done.status_code == 200 and done.json()["state"] == "REGISTERING"
    assert c.post(f"/tasks/{task}/dedupe", json={"staff_id": "S-1", "decision": "new"}).status_code == 409  # done once
    assert c.get("/tasks", params={"status": "done"}).json()


# ---------------------------------------------------------------- audit: restarts and paths


def test_journey_server_restart_mid_registration_says_so_and_registers_again(eng, twin_server, tmp_path, clock, outbox):
    from journey.engine import JourneyEngine
    from journey.twin_client import TwinClient

    sid = eng.open_session(NEW_CALLER_PHONE)["session_id"]
    details = dict(given="Asha", family="Kamath", gender="female", birth_date="1991-06-05", phone=NEW_CALLER_PHONE,
                   postal_code="560034", caller_confirmed=True)
    assert eng.call(sid, "register_patient", details)["data"]["status"] == "otp_sent"
    code = outbox[-1]["code"]
    # the server restarts: same database, empty memory
    again = JourneyEngine(str(eng.engine.url), TwinClient(twin_server), now=clock,
                          otp_sender=lambda phone, c, purpose: outbox.append({"phone": phone, "code": c, "purpose": purpose}))
    lost = again.call(sid, "check_otp", {"code": code})
    assert lost["error"]["code"] == "registration_lost"
    assert again.session(sid)["otp_attempts"] == 0  # no attempt burnt
    assert again.call(sid, "register_patient", details)["data"]["status"] == "otp_sent"
    done = again.call(sid, "check_otp", {"code": outbox[-1]["code"]})
    assert done["ok"] and done["data"]["registered"]
    again.twin.close()
    again.engine.dispose()


def test_relative_sqlite_paths_mean_the_repo_folder(tmp_path, monkeypatch):
    from core.db import ROOT, resolve_sqlite_url

    monkeypatch.chdir(tmp_path)  # started from somewhere else
    assert resolve_sqlite_url("sqlite:///./journey.db") == f"sqlite:///{ROOT / 'journey.db'}"
    for url in ("sqlite://", "sqlite:///:memory:", f"sqlite:///{tmp_path}/x.db", "postgresql://u@h/db"):
        assert resolve_sqlite_url(url) == url
