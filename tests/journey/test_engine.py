"""Stage 2 gate: Journey Engine rules, against a real Hospital Twin."""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import select, text

from journey import store
from journey.engine import ToolRefused
from tests.journey.conftest import (
    KAVYA, LAKSHMI, MANJUNATH, NEW_CALLER_PHONE, RAMESH, dob, held_slot, ok, refused, verified_session,
)

BOOKING_TOOLS = {"start_booking", "get_slots", "hold_slot", "book_appointment", "start_reschedule",
                 "cancel_appointment", "join_waitlist"}


# ---------------------------------------------------------------- illegal moves


def test_booking_refused_while_identifying(eng):
    sid = eng.open_session(RAMESH["phone"])["session_id"]
    r = ok(eng.call(sid, "lookup_caller"))
    assert r["journey"]["state"] == "IDENTIFYING"
    assert r["data"]["candidates"] == [{"patient_ref": "P-1001", "first_name": "Ramesh"}]
    assert not BOOKING_TOOLS & set(r["allowed_tools"])
    refused(eng.call(sid, "book_appointment", {"patient_confirmed": True}), "not_allowed_in_state")
    refused(eng.call(sid, "hold_slot", {"slot_id": "S-000001"}), "not_allowed_in_state")
    refused(eng.call(sid, "start_booking", {"reason": "x", "department_id": "CARD"}), "not_allowed_in_state")
    # the refusals are on the audit trail
    events = eng.events(r["journey"]["id"])
    assert [e["payload"]["tool"] for e in events if e["type"] == "tool_refused"] == [
        "book_appointment", "hold_slot", "start_booking"]


def test_caller_id_alone_is_not_enough_to_book(eng):
    s = eng.open_session(RAMESH["phone"])
    assert s["level"] == "SOFT" and s["greeting_names"] == ["Ramesh"]
    refused(eng.call(s["session_id"], "list_appointments"), "verification_required")


def test_report_status_needs_strong_verification(eng, twin_admin, outbox):
    sid = verified_session(eng, twin_admin, RAMESH)
    r = refused(eng.call(sid, "get_report_status"), "verification_required")
    assert r["error"]["needed_level"] == "STRONG"
    refused(eng.call(sid, "get_bill_status"), "verification_required")
    sent = ok(eng.call(sid, "send_otp"))["data"]
    assert sent["sent_to"] == "******2345"
    assert outbox[-1]["phone"] == RAMESH["phone"]  # only ever the registered number
    code = outbox[-1]["code"]
    wrong = "000000" if code != "000000" else "111111"
    assert refused(eng.call(sid, "check_otp", {"code": wrong}), "otp_incorrect")["error"]["attempts_left"] == 2
    assert ok(eng.call(sid, "check_otp", {"code": code}))["session"]["level"] == "STRONG"
    reports = ok(eng.call(sid, "get_report_status"))["data"]["reports"]
    assert {"test": "ECG", "status": "final", "ready": True} in reports
    assert all(set(x) == {"test", "status", "ready"} for x in reports)  # status only, never results
    assert ok(eng.call(sid, "get_bill_status"))["data"]["bills"]


def test_otp_expires_and_locks(eng, twin_admin, clock, outbox):
    sid = verified_session(eng, twin_admin, RAMESH)
    ok(eng.call(sid, "send_otp"))
    clock.advance(6)
    refused(eng.call(sid, "check_otp", {"code": outbox[-1]["code"]}), "otp_expired")
    ok(eng.call(sid, "send_otp"))
    for _ in range(3):
        refused(eng.call(sid, "check_otp", {"code": "999999" if outbox[-1]["code"] != "999999" else "888888"}),
                "otp_incorrect")
    refused(eng.call(sid, "check_otp", {"code": outbox[-1]["code"]}), "otp_locked")


# ---------------------------------------------------------------- booking rules


def test_full_booking_needs_verified_identity_held_slot_and_explicit_yes(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    hold = held_slot(eng, sid)
    assert hold["journey"]["state"] == "SLOT_HELD"
    assert "Cardiology" in hold["data"]["read_back"]
    r = refused(eng.call(sid, "book_appointment", {}), "confirmation_required")
    assert r["journey"]["state"] == "SLOT_HELD"
    refused(eng.call(sid, "book_appointment", {"patient_confirmed": "yes"}), "confirmation_required")
    booked = ok(eng.call(sid, "book_appointment", {"patient_confirmed": True}))
    assert booked["journey"]["state"] == "BOOKED"
    appt_id = booked["data"]["appointment_id"]
    twin_appt = twin_admin.get(f"/appointments/{appt_id}").json()
    assert twin_appt["status"] == "booked" and twin_appt["participant"][0]["actor"]["id"] == "P-1001"
    assert eng.verify_chain(booked["journey"]["id"]).ok


def test_only_offered_slots_can_be_held(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    ok(eng.call(sid, "start_booking", {"reason": "knee pain", "department_id": "ORTH"}))
    ok(eng.call(sid, "get_slots", {}))
    other = twin_admin.get("/slots", params={"department_id": "DERM", "limit": 1}).json()[0]["id"]
    refused(eng.call(sid, "hold_slot", {"slot_id": other}), "not_offered")


def test_hold_expiry_returns_to_slot_search(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    held_slot(eng, sid)
    twin_admin.post("/admin/clock", json={"advance_minutes": 3})
    r = refused(eng.call(sid, "book_appointment", {"patient_confirmed": True}), "hold_expired")
    assert r["journey"]["state"] == "SLOT_SEARCH"


def test_slot_stolen_between_hold_and_book(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    held_slot(eng, sid)
    twin_admin.post("/admin/chaos", json={"steal_held_slot": True})
    r = refused(eng.call(sid, "book_appointment", {"patient_confirmed": True}), "slot_taken")
    assert r["journey"]["state"] == "SLOT_SEARCH"


def test_reschedule_existing_appointment(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    appts = ok(eng.call(sid, "list_appointments"))["data"]["appointments"]
    thursday = next(a for a in appts if a["doctor"] == "Dr. Suresh Rao")
    r = ok(eng.call(sid, "start_reschedule", {"appointment_id": thursday["appointment_id"]}))
    assert r["journey"]["state"] == "RESCHEDULED"
    slots = ok(eng.call(sid, "get_slots", {}))["data"]["slots"]
    assert {s["doctor"] for s in slots} == {"Dr. Suresh Rao"}  # same doctor by default
    ok(eng.call(sid, "hold_slot", {"slot_id": slots[-1]["slot_id"]}))
    done = ok(eng.call(sid, "book_appointment", {"patient_confirmed": True}))
    assert done["data"]["mode"] == "reschedule" and done["data"]["appointment_id"] == thursday["appointment_id"]
    moved = twin_admin.get(f"/appointments/{thursday['appointment_id']}").json()
    assert moved["rescheduledFrom"] == thursday["start"] and moved["start"] == slots[-1]["start"]


def test_cannot_touch_someone_elses_appointment(eng, twin_admin):
    lakshmi_appt = next(a for a in twin_admin.get("/patients/P-1002/appointments").json() if a["status"] == "booked")
    sid = verified_session(eng, twin_admin, RAMESH)
    refused(eng.call(sid, "cancel_appointment", {"appointment_id": lakshmi_appt["id"], "reason": "x",
                                                 "patient_confirmed": True}), "not_found")
    refused(eng.call(sid, "get_queue_status", {"appointment_id": lakshmi_appt["id"]}), "not_found")
    assert twin_admin.get(f"/appointments/{lakshmi_appt['id']}").json()["status"] == "booked"


def test_cancel_needs_explicit_yes(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    appt = ok(eng.call(sid, "list_appointments"))["data"]["appointments"][0]
    refused(eng.call(sid, "cancel_appointment", {"appointment_id": appt["appointment_id"], "reason": "travelling"}),
            "confirmation_required")
    ok(eng.call(sid, "cancel_appointment", {"appointment_id": appt["appointment_id"], "reason": "travelling",
                                            "patient_confirmed": True}))
    assert twin_admin.get(f"/appointments/{appt['appointment_id']}").json()["status"] == "cancelled"


# ---------------------------------------------------------------- urgency ratchet & escalation


def test_urgency_only_goes_up_unless_a_human_lowers_it(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    r = ok(eng.call(sid, "escalate", {"urgency": "URGENT", "reason": "chest pressure on stairs"}))
    jid = r["journey"]["id"]
    assert r["journey"]["urgency"] == "URGENT" and r["journey"]["escalated"]
    # asking the AI-facing tool for a lower tier never lowers it
    assert ok(eng.call(sid, "escalate", {"urgency": "ROUTINE", "reason": "caller says it's fine now"}))[
        "journey"]["urgency"] == "URGENT"
    for actor in ("ai", "sentinel", "system"):
        with pytest.raises(ToolRefused, match="named staff"):
            eng.set_urgency(jid, "ROUTINE", actor, "looks fine")
    with pytest.raises(ToolRefused):
        eng.set_urgency(jid, "ROUTINE", "human", "looks fine", staff_id=None)
    assert eng.journey(jid)["urgency"] == 2
    eng.set_urgency(jid, "SOON", "human", "nurse assessed: stable", staff_id="N-07")
    assert eng.journey(jid)["urgency"] == 1
    lowered = [e for e in eng.events(jid) if e["type"] == "urgency_changed"][-1]
    assert lowered["actor"] == "human" and lowered["payload"]["staff_id"] == "N-07"
    # ai + sentinel + system + a "human" with no staff id: all four refused and logged
    assert [e["type"] for e in eng.events(jid)].count("urgency_lower_refused") == 4


def test_escalation_freezes_automation_until_a_human_resolves(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    r = ok(eng.call(sid, "escalate", {"urgency": "EMERGENCY", "reason": "chest pain to left arm",
                                      "verbatim": "seene mein dard, left haath tak"}))
    assert set(r["allowed_tools"]) == {"create_callback", "escalate", "transfer_to_human", "end_call"}
    refused(eng.call(sid, "start_booking", {"reason": "x", "department_id": "CARD"}), "escalated_frozen")
    ok(eng.call(sid, "transfer_to_human", {"reason": "emergency"}))
    card = json.loads([t for t in eng.tasks(r["journey"]["id"]) if t["kind"] == "escalation"][0]["data"])
    assert card["urgency"] == "EMERGENCY" and card["patient_ref"] == "P-1001" and card["verified"] == "MEDIUM"
    eng.human_resolve_escalation(r["journey"]["id"], "N-07", "patient in ER, family informed")
    assert "start_booking" in eng.allowed_tools(sid)
    assert eng.journey(r["journey"]["id"])["urgency"] == 3  # resolving does not silently lower urgency


# ---------------------------------------------------------------- verification lockout


def test_wrong_dob_twice_locks_and_never_leaks_the_stored_dob(eng, twin_admin, clock):
    real = dob(twin_admin, "P-1001")
    sid = eng.open_session(RAMESH["phone"])["session_id"]
    ok(eng.call(sid, "lookup_caller"))
    r1 = refused(eng.call(sid, "verify_identity", {"name": "Ramesh Sharma", "birth_date": "1970-01-01"}),
                 "verification_failed")
    assert r1["error"]["attempts_left"] == 1
    r2 = refused(eng.call(sid, "verify_identity", {"name": "Ramesh Sharma", "birth_date": "1971-02-02"}), "locked")
    r3 = refused(eng.call(sid, "verify_identity", {"name": "Ramesh Sharma", "birth_date": real}), "locked")
    for r in (r1, r2, r3):
        blob = json.dumps(r)
        assert real not in blob and real.replace("-", "/") not in blob
    # calling again doesn't reset the lock
    sid2 = eng.open_session(RAMESH["phone"])["session_id"]
    refused(eng.call(sid2, "verify_identity", {"name": "Ramesh Sharma", "birth_date": real}), "locked")
    clock.advance(31)
    ok(eng.call(sid2, "verify_identity", {"name": "Ramesh Sharma", "birth_date": real}))


def test_wrong_name_with_right_dob_fails(eng, twin_admin):
    sid = eng.open_session(RAMESH["phone"])["session_id"]
    refused(eng.call(sid, "verify_identity", {"name": "Suresh Sharma", "birth_date": dob(twin_admin, "P-1001")}),
            "verification_failed")


def test_birth_year_plus_pin_also_verifies(eng, twin_admin):
    sid = eng.open_session(RAMESH["phone"])["session_id"]
    year = int(dob(twin_admin, "P-1001")[:4])
    ok(eng.call(sid, "verify_identity", {"name": "Ramesh Sharma", "birth_year": year, "postal_code": "560038"}))


# ---------------------------------------------------------------- registration & duplicates


def test_near_duplicate_goes_to_staff_review_and_is_never_merged(eng, twin_admin):
    laxmi = twin_admin.get("/patients", params={"name": "Laxmi Devi"}).json()["candidates"]
    laxmi_dob = next(c for c in laxmi if c["patient"]["name"][0]["text"] == "Laxmi Devi")["patient"]["birthDate"]
    sid = eng.open_session(NEW_CALLER_PHONE)["session_id"]
    r = ok(eng.call(sid, "register_patient", {
        "given": "Laksmi", "family": "Devi", "gender": "female", "birth_date": laxmi_dob,
        "phone": NEW_CALLER_PHONE, "caller_confirmed": True}))
    assert r["data"]["status"] == "needs_staff_review" and r["journey"]["state"] == "DEDUPE_REVIEW"
    assert twin_admin.get("/patients/P-1301").status_code == 404  # nothing created
    task = eng.task(r["data"]["task_id"])
    cands = json.loads(task["data"])["candidate_refs"]
    assert len(cands) >= 2 and task["kind"] == "dedupe_review"
    # registering and booking are frozen while staff decide; proving identity still works
    allowed = set(r["allowed_tools"])
    assert not allowed & {"register_patient", "start_booking", "get_slots", "hold_slot", "book_appointment"}
    assert "verify_identity" in allowed
    refused(eng.call(sid, "register_patient", {
        "given": "Laksmi", "family": "Devi", "gender": "female", "birth_date": laxmi_dob,
        "phone": NEW_CALLER_PHONE, "caller_confirmed": True}), "not_allowed_in_state")
    refused(eng.call(sid, "send_otp"), "verification_required")  # no code to an unverified caller
    with pytest.raises(ToolRefused):
        eng.human_resolve_dedupe(task["id"], "S-01", "existing", "P-1001")  # not a candidate
    eng.human_resolve_dedupe(task["id"], "S-01", "existing", cands[0])
    j = eng.journey(r["journey"]["id"])
    assert j["state"] == "VERIFIED" and j["patient_ref"] == cands[0]
    assert eng.session(sid)["level"] == 2
    assert twin_admin.get("/patients/P-1301").status_code == 404  # still nothing merged or created


def test_new_patient_registration_with_otp(eng, twin_admin, outbox):
    sid = eng.open_session(NEW_CALLER_PHONE)["session_id"]
    body = {"given": "Asha", "family": "Kamath", "gender": "female", "birth_date": "1991-06-05",
            "phone": "77000 11122", "postal_code": "560034", "language": "kn"}
    refused(eng.call(sid, "register_patient", body), "confirmation_required")
    r = ok(eng.call(sid, "register_patient", body | {"caller_confirmed": True}))
    assert r["data"]["status"] == "otp_sent" and r["journey"]["state"] == "REGISTERING"
    assert outbox[-1]["phone"] == NEW_CALLER_PHONE and outbox[-1]["purpose"] == "registration"
    assert twin_admin.get("/patients/P-1301").status_code == 404  # not created before the code is confirmed
    done = ok(eng.call(sid, "check_otp", {"code": outbox[-1]["code"]}))
    assert done["data"]["registered"] and done["journey"]["state"] == "VERIFIED"
    assert done["session"]["level"] == "STRONG"
    assert twin_admin.get("/patients/P-1301").json()["name"][0]["text"] == "Asha Kamath"
    ok(eng.call(sid, "start_booking", {"reason": "skin rash", "department_id": "DERM"}))


def test_existing_patient_trying_to_register_is_sent_to_verification(eng, twin_admin):
    sid = eng.open_session(RAMESH["phone"])["session_id"]
    r = ok(eng.call(sid, "register_patient", {
        "given": "Ramesh", "family": "Sharma", "gender": "male", "birth_date": dob(twin_admin, "P-1001"),
        "phone": RAMESH["phone"], "caller_confirmed": True}))
    assert r["data"]["status"] == "already_registered" and r["journey"]["state"] == "IDENTIFYING"


# ---------------------------------------------------------------- caregiver / proxy


def test_caregiver_can_book_but_not_hear_reports(eng, twin_admin, outbox):
    sid = eng.open_session(KAVYA["phone"])["session_id"]
    assert set(ok(eng.call(sid, "lookup_caller"))["data"]["candidates"][i]["first_name"] for i in range(2)) == {
        "Kavya", "Manjunath"}
    refused(eng.call(sid, "set_caller_relation", {"relation": "child"}), "consent_required")
    ok(eng.call(sid, "set_caller_relation", {"relation": "child", "consent": True}))
    v = ok(eng.call(sid, "verify_identity", {"name": "Manjunath Gowda", "birth_date": dob(twin_admin, "P-1004")}))
    assert v["data"]["patient_ref"] == "P-1004" and "child" in v["data"]["calling_for"]
    ok(eng.call(sid, "start_booking", {"reason": "knee pain", "department_id": "ORTH"}))
    ok(eng.call(sid, "send_otp"))
    ok(eng.call(sid, "check_otp", {"code": outbox[-1]["code"]}))
    refused(eng.call(sid, "get_report_status"), "proxy_not_authorised")
    refused(eng.call(sid, "get_bill_status"), "proxy_not_authorised")


def test_different_patient_on_same_phone_gets_own_journey(eng, twin_admin):
    sid = eng.open_session(KAVYA["phone"])["session_id"]
    first = ok(eng.call(sid, "verify_identity", {"name": "Kavya Gowda", "birth_date": dob(twin_admin, "P-1003")}))
    ok(eng.call(sid, "set_caller_relation", {"relation": "child", "consent": True}))
    second = ok(eng.call(sid, "verify_identity", {"name": "Manjunath Gowda", "birth_date": dob(twin_admin, "P-1004")}))
    assert first["journey"]["id"] != second["journey"]["id"]
    assert eng.journey(first["journey"]["id"])["patient_ref"] == "P-1003"
    assert eng.journey(second["journey"]["id"])["patient_ref"] == "P-1004"


# ---------------------------------------------------------------- resume across calls


def test_second_call_resumes_same_journey_but_must_reverify(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    ok(eng.call(sid, "start_booking", {"reason": "follow-up", "department_id": "CARD"}))
    slots = ok(eng.call(sid, "get_slots", {}))["data"]["slots"]
    jid = ok(eng.call(sid, "end_call", {"summary": "will call back after checking dates"}))["journey"]["id"]
    refused(eng.call(sid, "get_slots", {}), "session_ended")

    s2 = eng.open_session("98450 12345")  # same number, typed differently
    assert s2["resumed"] and s2["journey_id"] == jid and s2["state"] == "SLOT_SEARCH"
    assert s2["level"] == "SOFT"  # verification never carries over between calls
    refused(eng.call(s2["session_id"], "hold_slot", {"slot_id": slots[0]["slot_id"]}), "verification_required")
    ok(eng.call(s2["session_id"], "verify_identity", {"name": "Ramesh Sharma", "birth_date": dob(twin_admin, "P-1001")}))
    assert ok(eng.call(s2["session_id"], "hold_slot", {"slot_id": slots[-1]["slot_id"]}))["journey"]["state"] == "SLOT_HELD"


def test_closed_journey_is_not_resumed(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    hold = held_slot(eng, sid)
    booked = ok(eng.call(sid, "book_appointment", {"patient_confirmed": True}))
    ok(eng.call(sid, "cancel_appointment", {"appointment_id": booked["data"]["appointment_id"], "reason": "changed plans",
                                            "patient_confirmed": True}))
    assert eng.journey(hold["journey"]["id"])["state"] == "CLOSED"
    s2 = eng.open_session(RAMESH["phone"])
    assert not s2["resumed"] and s2["journey_id"] != hold["journey"]["id"]


# ---------------------------------------------------------------- hospital system down


def test_hospital_system_down_gives_clean_error_and_callback(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    ok(eng.call(sid, "start_booking", {"reason": "follow-up", "department_id": "CARD"}))
    twin_admin.post("/admin/chaos", json={"his_down": True})
    r = refused(eng.call(sid, "get_slots", {}), "system_unavailable")
    assert r["journey"]["state"] == "INTAKE"  # nothing moved
    task_id = r["error"]["callback_task_id"]
    assert eng.task(task_id)["kind"] == "pending_sync"
    again = refused(eng.call(sid, "get_slots", {}), "system_unavailable")
    assert again["error"]["callback_task_id"] == task_id  # no duplicate tickets
    # a new call while it's down still works
    s2 = eng.open_session(LAKSHMI["phone"])
    assert s2["hospital_system_down"] and s2["level"] == "NONE"
    refused(eng.call(s2["session_id"], "verify_identity", {"name": "Lakshmi Narayan", "birth_date": "1954-07-02"}),
            "system_unavailable")
    twin_admin.post("/admin/chaos", json={"his_down": False})
    assert ok(eng.call(sid, "get_slots", {}))["data"]["slots"]


def test_unreachable_hospital_system(tmp_path, clock):
    from journey.engine import JourneyEngine
    from journey.twin_client import TwinClient
    e = JourneyEngine(f"sqlite:///{tmp_path}/j.db", TwinClient("http://127.0.0.1:9", timeout=0.5), now=clock)
    s = e.open_session(RAMESH["phone"])
    assert s["hospital_system_down"]
    refused(e.call(s["session_id"], "list_departments"), "system_unavailable")


# ---------------------------------------------------------------- privacy


def test_no_raw_phone_dob_aadhaar_or_otp_stored_anywhere(eng, twin_admin, outbox):
    ramesh_dob = dob(twin_admin, "P-1001")
    sid = eng.open_session(RAMESH["phone"])["session_id"]
    ok(eng.call(sid, "lookup_caller"))
    refused(eng.call(sid, "verify_identity", {"name": "Ramesh Sharma", "birth_date": "1970-01-01"}), "verification_failed")
    ok(eng.call(sid, "verify_identity", {"name": "Ramesh Sharma", "birth_date": ramesh_dob}))
    ok(eng.call(sid, "start_booking", {
        "reason": "Ramesh Sharma here, call me on 98450 12345, aadhaar 1234 5678 9012, born 14/03/1968",
        "department_id": "CARD"}))
    ok(eng.call(sid, "send_otp"))
    ok(eng.call(sid, "check_otp", {"code": outbox[-1]["code"]}))
    ok(eng.call(sid, "create_callback", {"reason": "please call 9845012345 after 6pm"}))
    ok(eng.call(sid, "escalate", {"urgency": "SOON", "reason": "Ramesh says BP was 160",
                                  "verbatim": "mera number 9845012345 hai"}))
    s2 = eng.open_session(NEW_CALLER_PHONE)["session_id"]
    ok(eng.call(s2, "register_patient", {"given": "Asha", "family": "Kamath", "gender": "female",
                                         "birth_date": "1991-06-05", "phone": NEW_CALLER_PHONE, "caller_confirmed": True}))
    ok(eng.call(s2, "check_otp", {"code": outbox[-1]["code"]}))

    dump, no_hashes = [], []
    with eng.engine.connect() as conn:
        for table in store.metadata.sorted_tables:
            for row in conn.execute(select(table)).mappings():
                values = {k: str(v) for k, v in row.items()}
                dump.append(json.dumps(values))
                # SHA-256 hex columns can contain digit runs by chance; they are pseudonyms, not data
                no_hashes.append(json.dumps({k: v for k, v in values.items() if not k.endswith("hash")}))
    blob = "\n".join(dump)
    forbidden = ["9845012345", "98450 12345", "7700011122", "1234 5678 9012", "123456789012",
                 ramesh_dob, "14/03/1968", "1991-06-05", "Sharma", "Kamath"] + [o["code"] for o in outbox]
    leaks = [f for f in forbidden if f in blob]
    assert not leaks, f"personal data stored: {leaks}"
    m = re.search(r"(?<![0-9A-Za-z])[6-9]\d{9}(?![0-9A-Za-z])", "\n".join(no_hashes))
    assert not m, f"a 10-digit mobile number is stored: ...{m.string[max(0, m.start() - 60):m.end() + 20]}..."


def test_phone_reveal_for_callback_is_audited(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    task_id = ok(eng.call(sid, "create_callback", {"reason": "wants evening slot"}))["data"]["task_id"]
    with pytest.raises(ToolRefused):
        eng.reveal_phone(task_id, "S-01", "")
    assert eng.reveal_phone(task_id, "S-01", "calling back") == RAMESH["phone"]
    jid = eng.task(task_id)["journey_id"]
    reveal = [e for e in eng.events(jid) if e["type"] == "phone_revealed"][0]
    assert reveal["actor"] == "human" and reveal["payload"]["staff_id"] == "S-01"


# ---------------------------------------------------------------- audit chain on a real journey


def test_tampering_with_a_real_journey_is_detected(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    hold = held_slot(eng, sid)
    ok(eng.call(sid, "book_appointment", {"patient_confirmed": True}))
    jid = hold["journey"]["id"]
    report = eng.verify_chain(jid)
    assert report.ok and report.events >= 10
    # an attacker with direct database access removes the guard and edits an old event
    with eng.engine.begin() as conn:
        conn.execute(text("DROP TRIGGER journey_events_no_update"))
        conn.execute(text("UPDATE journey_events SET actor='human' WHERE journey_id=:j AND seq=3"), {"j": jid})
    bad = eng.verify_chain(jid)
    assert not bad.ok and bad.first_bad_seq == 3


# ---------------------------------------------------------------- robustness


def test_bad_calls_never_crash(eng, twin_admin):
    sid = verified_session(eng, twin_admin, RAMESH)
    refused(eng.call(sid, "launch_rockets"), "unknown_tool")
    refused(eng.call(sid, "start_booking", {"department_id": "CARD"}), "invalid_input")  # missing reason
    refused(eng.call(sid, "start_booking", {"reason": "x", "bogus": 1}), "invalid_input")
    refused(eng.call(sid, "escalate", {"urgency": "EMERGENCY", "reason": "x", "actor": "human"}), "invalid_input")
    refused(eng.call(sid, "escalate", {"urgency": "PANIC", "reason": "x"}), "invalid_input")
    refused(eng.call(sid, "verify_identity", {"name": "Ramesh Sharma", "birth_date": "14-03-1968"}), "invalid_input")
    assert eng.call("C-NOPE", "lookup_caller")["error"]["code"] == "unknown_session"
