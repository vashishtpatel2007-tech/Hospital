"""Stage 1 gate: contract tests against a running Hospital Twin."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from tests.twin.conftest import days_ahead, hold

RAMESH = "P-1001"


def book(client, hold_id: str, patient_id: str = RAMESH):
    return client.post("/appointments", json={"hold_id": hold_id, "patient_id": patient_id})


def minutes_until(client, iso: str) -> float:
    now = datetime.fromisoformat(client.get("/health").json()["now"])
    return (datetime.fromisoformat(iso) - now).total_seconds() / 60


# ---------------------------------------------------------------- directory


def test_health_and_docs(twin):
    assert twin.get("/health").json()["status"] == "ok"
    assert twin.get("/docs").status_code == 200


def test_directory_counts_and_shape(twin):
    depts = twin.get("/departments").json()
    docs = twin.get("/practitioners").json()
    assert len(depts) == 12 and len(docs) == 30
    rao = twin.get("/practitioners/D-05").json()
    assert rao["resourceType"] == "Practitioner"
    assert rao["name"][0]["text"] == "Dr. Suresh Rao" and rao["department"]["id"] == "CARD"
    assert "kn" in rao["communication"]


def test_practitioner_filters(twin):
    kannada_cardio = twin.get("/practitioners", params={"department_id": "CARD", "language": "kn"}).json()
    assert {d["id"] for d in kannada_cardio} == {"D-05", "D-07"}
    female_gyn = twin.get("/practitioners", params={"department_id": "OBGY", "gender": "female"}).json()
    assert len(female_gyn) == 3
    by_name = twin.get("/practitioners", params={"name": "Dr. Suresh Rao"}).json()
    assert by_name[0]["id"] == "D-05"


def test_unknown_ids_return_404(twin):
    assert twin.get("/practitioners/D-99").json()["error"] == "not_found"
    assert twin.get("/patients/P-9999").status_code == 404
    assert hold(twin, "S-999999").status_code == 404


# ---------------------------------------------------------------- slot search


def test_slot_search_filters(twin):
    slots = twin.get("/slots", params={"department_id": "CARD", "part_of_day": "evening", "limit": 30}).json()
    assert slots
    for s in slots:
        assert s["status"] == "free"
        assert s["schedule"]["department_id"] == "CARD"
        assert datetime.fromisoformat(s["start"]).hour >= 16
    starts = [s["start"] for s in slots]
    assert starts == sorted(starts)


def test_slot_search_only_future(twin):
    now = datetime.fromisoformat(twin.get("/health").json()["now"])
    for s in twin.get("/slots", params={"limit": 200}).json():
        assert datetime.fromisoformat(s["start"]) > now


# ---------------------------------------------------------------- hold & book


def test_hold_then_book_happy_path(twin, free_slot):
    h = hold(twin, free_slot["id"])
    assert h.status_code == 201
    assert h.json()["status"] == "active" and 100 < h.json()["seconds_left"] <= 120
    a = book(twin, h.json()["id"])
    assert a.status_code == 201, a.text
    appt = a.json()
    assert appt["status"] == "booked" and appt["slot"] == [free_slot["id"]]
    # the slot no longer appears in search, and can't be held again
    ids = {s["id"] for s in twin.get("/slots", params={
        "practitioner_id": free_slot["schedule"]["practitioner_id"], "limit": 200,
        "date_from": free_slot["start"][:10], "date_to": free_slot["start"][:10]}).json()}
    assert free_slot["id"] not in ids
    assert hold(twin, free_slot["id"]).json()["error"] == "slot_unavailable"
    assert appt["id"] in {x["id"] for x in twin.get(f"/patients/{RAMESH}/appointments").json()}


def test_same_slot_cannot_be_held_twice(twin, free_slot):
    assert hold(twin, free_slot["id"]).status_code == 201
    second = hold(twin, free_slot["id"])
    assert second.status_code == 409 and second.json()["error"] == "slot_unavailable"


def test_hold_is_idempotent(twin, free_slot):
    a = hold(twin, free_slot["id"], key="call-42-turn-7")
    b = hold(twin, free_slot["id"], key="call-42-turn-7")
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["id"] == b.json()["id"]


def test_ten_parallel_holds_on_one_slot_only_one_wins(twin, free_slot):
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: hold(twin, free_slot["id"]), range(10)))
    codes = sorted(r.status_code for r in results)
    assert codes == [201] + [409] * 9, codes


def test_ten_parallel_bookings_with_one_hold_only_one_wins(twin, free_slot):
    h = hold(twin, free_slot["id"]).json()
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda i: book(twin, h["id"], f"P-{1010 + i}"), range(10)))
    assert sorted(r.status_code for r in results) == [201] + [409] * 9
    appts = [r.json() for r in results if r.status_code == 201]
    assert len(appts) == 1


def test_ten_parallel_holds_on_different_slots_all_succeed(twin):
    slots = twin.get("/slots", params={"date_from": days_ahead(twin, 1), "limit": 10}).json()
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda s: hold(twin, s["id"]), slots))
    assert all(r.status_code == 201 for r in results), [r.text for r in results if r.status_code != 201]


def test_booking_unknown_patient_fails_and_keeps_hold(twin, free_slot):
    h = hold(twin, free_slot["id"]).json()
    r = book(twin, h["id"], "P-9999")
    assert r.status_code == 404
    assert book(twin, h["id"]).status_code == 201  # hold still usable


def test_hold_expires_after_ttl(twin, free_slot):
    h = hold(twin, free_slot["id"]).json()
    twin.post("/admin/clock", json={"advance_minutes": 2.1})  # 126 s later
    r = book(twin, h["id"])
    assert r.status_code == 409 and r.json()["error"] == "hold_expired"
    # the slot is free again for someone else
    assert hold(twin, free_slot["id"]).status_code == 201


def test_released_hold_frees_slot(twin, free_slot):
    h = hold(twin, free_slot["id"]).json()
    assert twin.delete(f"/holds/{h['id']}").json()["status"] == "released"
    assert book(twin, h["id"]).json()["error"] == "hold_expired"
    assert hold(twin, free_slot["id"]).status_code == 201


def test_used_hold_cannot_book_again(twin, free_slot):
    h = hold(twin, free_slot["id"]).json()
    assert book(twin, h["id"]).status_code == 201
    r = book(twin, h["id"], "P-1010")
    assert r.status_code == 409 and r.json()["error"] == "hold_used"


def test_past_slot_cannot_be_held(twin, free_slot):
    twin.post("/admin/clock", json={"advance_minutes": minutes_until(twin, free_slot["start"]) + 1})
    r = hold(twin, free_slot["id"])
    assert r.status_code == 409 and r.json()["error"] == "slot_unavailable"


# ---------------------------------------------------------------- reschedule & cancel


def test_reschedule_moves_appointment_and_frees_old_slot(twin):
    slots = twin.get("/slots", params={"practitioner_id": "D-08", "date_from": days_ahead(twin, 1), "limit": 2}).json()
    old, new = slots
    appt = book(twin, hold(twin, old["id"]).json()["id"]).json()
    r = twin.post(f"/appointments/{appt['id']}/reschedule", json={"hold_id": hold(twin, new["id"]).json()["id"]})
    assert r.status_code == 200, r.text
    moved = r.json()
    assert moved["id"] == appt["id"] and moved["start"] == new["start"]
    assert moved["rescheduledFrom"] == old["start"]
    assert hold(twin, old["id"]).status_code == 201  # old slot free again


def test_cancel_frees_slot_and_is_not_repeatable(twin, free_slot):
    appt = book(twin, hold(twin, free_slot["id"]).json()["id"]).json()
    r = twin.post(f"/appointments/{appt['id']}/cancel", json={"reason": "feeling better"})
    assert r.json()["status"] == "cancelled"
    again = twin.post(f"/appointments/{appt['id']}/cancel", json={"reason": "again"})
    assert again.status_code == 409 and again.json()["error"] == "invalid_state"
    assert hold(twin, free_slot["id"]).status_code == 201
    r = twin.post(f"/appointments/{appt['id']}/reschedule", json={"hold_id": "H-X"})
    assert r.status_code == 409


# ---------------------------------------------------------------- chaos switches

NON_ADMIN_GETS = ["/departments", "/practitioners", "/slots", "/patients/P-1001", "/patients?phone=9845012345",
                  "/patients/P-1001/reports", "/patients/P-1001/invoices", "/waitlist"]


def test_his_down_every_endpoint_returns_503(twin, free_slot):
    twin.post("/admin/chaos", json={"his_down": True})
    for path in NON_ADMIN_GETS:
        r = twin.get(path)
        assert r.status_code == 503 and r.json()["error"] == "his_unavailable", path
    assert hold(twin, free_slot["id"]).status_code == 503
    assert twin.post("/appointments", json={"hold_id": "x", "patient_id": "y"}).status_code == 503
    # health and admin stay up so the demo can recover
    assert twin.get("/health").status_code == 200
    twin.post("/admin/chaos", json={"his_down": False})
    assert twin.get("/departments").status_code == 200


def test_error_rate_and_latency(twin):
    twin.post("/admin/chaos", json={"error_rate": 1.0})
    r = twin.get("/departments")
    assert r.status_code == 500 and r.json()["error"] == "internal_error"
    twin.post("/admin/chaos", json={"error_rate": 0.0, "latency_ms": 400})
    t0 = time.perf_counter()
    assert twin.get("/departments").status_code == 200
    assert time.perf_counter() - t0 >= 0.4


def test_steal_held_slot_is_one_shot(twin):
    a, b = twin.get("/slots", params={"date_from": days_ahead(twin, 1), "limit": 2}).json()
    twin.post("/admin/chaos", json={"steal_held_slot": True})
    h = hold(twin, a["id"]).json()
    r = book(twin, h["id"])
    assert r.status_code == 409 and r.json()["error"] == "slot_taken"
    assert twin.get("/admin/chaos").json()["steal_held_slot"] is False
    assert hold(twin, a["id"]).status_code == 409  # someone else has it now
    assert book(twin, hold(twin, b["id"]).json()["id"]).status_code == 201  # next booking works


# ---------------------------------------------------------------- patients


def test_patient_search_by_phone_finds_family(twin):
    # the caregiver persona: daughter and father share a phone
    c = twin.get("/patients", params={"phone": "99000 34567"}).json()["candidates"]
    names = {x["patient"]["name"][0]["text"] for x in c}
    assert {"Kavya Gowda", "Manjunath Gowda"} <= names


def test_patient_search_fuzzy_name_finds_near_duplicates(twin):
    c = twin.get("/patients", params={"name": "Laxmi Devi"}).json()["candidates"]
    names = {x["patient"]["name"][0]["text"] for x in c}
    assert {"Laxmi Devi", "Lakshmi Devi"} <= names


def test_patient_search_needs_a_parameter(twin):
    assert twin.get("/patients").status_code == 422


def test_register_patient_then_find_by_phone(twin):
    body = {"given": "Asha", "family": "Kamath", "gender": "female", "birth_date": "1990-05-17",
            "phone": "98450 11111", "postal_code": "560034", "language": "kn"}
    r = twin.post("/patients", json=body)
    assert r.status_code == 201
    p = r.json()
    assert p["id"] == "P-1301" and p["telecom"][0]["value"] == "+919845011111"
    found = twin.get("/patients", params={"phone": "+919845011111"}).json()["candidates"]
    assert found[0]["patient"]["id"] == "P-1301"


def test_register_patient_validation(twin):
    bad = {"given": "X", "family": "Y", "gender": "male", "birth_date": "2999-01-01", "phone": "123"}
    r = twin.post("/patients", json=bad)
    assert r.status_code == 422 and r.json()["error"] == "validation_error"


def test_reports_are_status_only(twin):
    reps = twin.get(f"/patients/{RAMESH}/reports").json()
    assert {r["code"]["text"] for r in reps} >= {"Lipid profile", "ECG"}
    for r in reps:
        assert set(r) >= {"status", "code"} and "result" not in r


def test_invoices_include_cashless_status(twin):
    inv = twin.get("/patients/P-1002/invoices").json()
    assert any(i["payer"] == "PM-JAY" and i["insuranceStatus"] == "pre_auth_pending" for i in inv)


# ---------------------------------------------------------------- queue & waitlist


def test_queue_token_delay_and_check_in(twin):
    tomorrow = days_ahead(twin, 1)
    slots = twin.get("/slots", params={"practitioner_id": "D-01", "date_from": tomorrow, "date_to": days_ahead(twin, 7),
                                       "limit": 200}).json()
    # pick the 5th slot of the first OPD day that is fully in the future
    day = slots[0]["start"][:10]
    day_slots = [s for s in slots if s["start"][:10] == day]
    target = day_slots[min(4, len(day_slots) - 1)]
    appt = book(twin, hold(twin, target["id"]).json()["id"]).json()

    # before the appointment day: check-in refused
    assert twin.post(f"/appointments/{appt['id']}/arrive").status_code == 409

    # jump to 06:00 on the appointment day
    twin.post("/admin/clock", json={"advance_minutes": minutes_until(twin, f"{day}T06:00:00")})
    q = twin.get(f"/appointments/{appt['id']}/queue").json()
    assert q["now_serving"] == 0 and q["token"] >= 1
    assert q["ahead"] == q["token"] - 1
    wait_before = q["est_wait_minutes"]

    # doctor running 30 minutes late
    d = twin.post("/practitioners/D-01/delay", json={"minutes": 30, "date": day}).json()
    assert d["upcoming_appointments_affected"] >= 1
    q2 = twin.get(f"/appointments/{appt['id']}/queue").json()
    assert q2["est_wait_minutes"] == wait_before + 30 and q2["doctor_delay_minutes"] == 30

    # check in on the day
    arrived = twin.post(f"/appointments/{appt['id']}/arrive").json()
    assert arrived["status"] == "arrived" and arrived["token"] == q["token"]

    # OPD starts at 08:00 + 30 min delay; at 08:31 token 1 or 2 is being seen
    twin.post("/admin/clock", json={"advance_minutes": minutes_until(twin, f"{day}T08:31:00")})
    q3 = twin.get(f"/appointments/{appt['id']}/queue").json()
    assert q3["now_serving"] == 1 and q3["ahead"] == q["token"] - 1


def test_waitlist(twin):
    r = twin.post("/waitlist", json={"patient_id": RAMESH, "department_id": "card",
                                     "date_from": days_ahead(twin, 1), "date_to": days_ahead(twin, 5)})
    assert r.status_code == 201 and r.json()["department_id"] == "CARD"
    assert len(twin.get("/waitlist", params={"patient_id": RAMESH}).json()) == 1
    bad = twin.post("/waitlist", json={"patient_id": RAMESH, "date_from": days_ahead(twin, 1),
                                       "date_to": days_ahead(twin, 2)})
    assert bad.status_code == 422


# ---------------------------------------------------------------- admin


def test_clock_advance_and_reset(twin):
    before = datetime.fromisoformat(twin.get("/admin/clock").json()["now"])
    after = datetime.fromisoformat(twin.post("/admin/clock", json={"advance_minutes": 1440}).json()["now"])
    assert 1439 <= (after - before).total_seconds() / 60 <= 1441
    reset = twin.post("/admin/clock", json={"reset": True}).json()
    assert reset["offset_minutes"] == 0


def test_validation_errors_use_standard_shape(twin):
    r = twin.post(f"/slots/S-000001/hold", json={})
    assert r.status_code == 422 and r.json()["error"] == "validation_error"
    r = twin.post("/admin/chaos", json={"error_rate": 5})
    assert r.status_code == 422


def test_idempotency_key_reuse_for_other_slot_rejected(twin):
    a, b = twin.get("/slots", params={"date_from": days_ahead(twin, 1), "limit": 2}).json()
    key = uuid.uuid4().hex
    assert hold(twin, a["id"], key).status_code == 201
    assert hold(twin, b["id"], key).status_code == 422
