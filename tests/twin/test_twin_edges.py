"""Stage 1 audit: retries, races and state rules the first pass did not cover."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

from tests.twin.conftest import days_ahead, hold

RAMESH = "P-1001"


def book(client, hold_id: str, patient_id: str = RAMESH):
    return client.post("/appointments", json={"hold_id": hold_id, "patient_id": patient_id})


def two_free_slots(twin):
    return twin.get("/slots", params={"date_from": days_ahead(twin, 2), "limit": 2}).json()


# ---------------------------------------------------------------- retries are safe


def test_booking_retry_with_the_same_hold_returns_the_same_appointment(twin, free_slot):
    h = hold(twin, free_slot["id"]).json()
    first = book(twin, h["id"])
    assert first.status_code == 201
    again = book(twin, h["id"])  # the caller timed out and tried again
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    other = book(twin, h["id"], "P-1002")  # but the hold never books for someone else
    assert other.status_code == 409 and other.json()["error"] == "hold_used"
    appts = [a for a in twin.get(f"/patients/{RAMESH}/appointments").json() if a["slot"] == [free_slot["id"]]]
    assert len(appts) == 1


def test_reschedule_retry_with_the_same_hold_returns_the_same_appointment(twin):
    a, b = two_free_slots(twin)
    appt = book(twin, hold(twin, a["id"]).json()["id"]).json()
    h = hold(twin, b["id"]).json()
    first = twin.post(f"/appointments/{appt['id']}/reschedule", json={"hold_id": h["id"]})
    again = twin.post(f"/appointments/{appt['id']}/reschedule", json={"hold_id": h["id"]})
    assert first.status_code == again.status_code == 200
    assert again.json()["id"] == appt["id"] and again.json()["start"] == b["start"]
    assert hold(twin, a["id"]).status_code == 201  # the old slot really is free


def test_ten_parallel_reschedules_onto_one_hold_only_one_wins(twin):
    slots = twin.get("/slots", params={"date_from": days_ahead(twin, 3), "limit": 11}).json()
    appts = [book(twin, hold(twin, s["id"]).json()["id"]).json()["id"] for s in slots[:10]]
    target = hold(twin, slots[10]["id"]).json()["id"]
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(
            lambda a: twin.post(f"/appointments/{a}/reschedule", json={"hold_id": target}).status_code, appts))
    assert codes.count(200) == 1 and codes.count(409) == 9
    on_target = [a for a in appts if twin.get(f"/appointments/{a}").json()["slot"] == [slots[10]["id"]]]
    assert len(on_target) == 1


def test_ten_parallel_registrations_get_distinct_ids(twin):
    def register(i):
        return twin.post("/patients", json={"given": f"Test{i}", "family": "Person", "gender": "other",
                                            "birth_date": "1990-01-01", "phone": f"+9170000000{i:02d}"})

    with ThreadPoolExecutor(10) as pool:
        results = list(pool.map(register, range(10)))
    assert all(r.status_code == 201 for r in results)
    assert len({r.json()["id"] for r in results}) == 10


# ---------------------------------------------------------------- state rules


def test_reschedule_under_steal_keeps_the_original(twin):
    a, b = two_free_slots(twin)
    appt = book(twin, hold(twin, a["id"]).json()["id"]).json()
    twin.post("/admin/chaos", json={"steal_held_slot": True})
    r = twin.post(f"/appointments/{appt['id']}/reschedule", json={"hold_id": hold(twin, b["id"]).json()["id"]})
    assert r.status_code == 409 and r.json()["error"] == "slot_taken"
    kept = twin.get(f"/appointments/{appt['id']}").json()
    assert kept["status"] == "booked" and kept["start"] == a["start"]


def test_cancelled_appointment_cannot_be_rescheduled_or_checked_in(twin):
    a, b = two_free_slots(twin)
    appt = book(twin, hold(twin, a["id"]).json()["id"]).json()
    twin.post(f"/appointments/{appt['id']}/cancel", json={"reason": "travelling"})
    r = twin.post(f"/appointments/{appt['id']}/reschedule", json={"hold_id": hold(twin, b["id"]).json()["id"]})
    assert r.status_code == 409 and r.json()["error"] == "invalid_state"
    assert twin.post(f"/appointments/{appt['id']}/arrive").json()["error"] == "invalid_state"


def test_check_in_only_on_the_day(twin, free_slot):
    appt = book(twin, hold(twin, free_slot["id"]).json()["id"]).json()
    r = twin.post(f"/appointments/{appt['id']}/arrive")
    assert r.status_code == 409 and "appointment day" in r.json()["message"]


def test_released_or_expired_hold_cannot_book(twin, free_slot):
    h = hold(twin, free_slot["id"]).json()
    twin.delete(f"/holds/{h['id']}")
    assert book(twin, h["id"]).json()["error"] == "hold_expired"
    assert twin.delete(f"/holds/{h['id']}").status_code == 200  # releasing twice is harmless


def test_unknown_hold_and_patient(twin, free_slot):
    assert book(twin, "H-NOPE").status_code == 404
    h = hold(twin, free_slot["id"]).json()
    assert book(twin, h["id"], "P-9999").status_code == 404


def test_phone_formats_all_find_the_same_patient(twin):
    for phone in ["+919845012345", "9845012345", "98450 12345", "098450 12345", "+91-98450-12345"]:
        ids = {c["patient"]["id"] for c in twin.get("/patients", params={"phone": phone}).json()["candidates"]}
        assert "P-1001" in ids, phone
    assert twin.get("/patients", params={"phone": "12345"}).status_code == 422


def test_slot_search_window_and_part_of_day(twin):
    d = days_ahead(twin, 2)
    morning = twin.get("/slots", params={"date_from": d, "date_to": d, "part_of_day": "morning", "limit": 200}).json()
    assert morning and all(s["start"].startswith(d) and int(s["start"][11:13]) < 12 for s in morning)
    assert twin.get("/slots", params={"date_from": d, "date_to": days_ahead(twin, 1)}).status_code == 422
    assert twin.get("/slots", params={"part_of_day": "night"}).status_code == 422
    assert twin.get("/slots", params={"practitioner_id": "D-99"}).status_code == 404


def test_chaos_never_blocks_admin_and_error_rate_one_fails_everything(twin):
    twin.post("/admin/chaos", json={"error_rate": 1.0})
    assert twin.get("/departments").status_code == 500
    assert twin.get("/admin/chaos").status_code == 200 and twin.get("/health").status_code == 200
    assert twin.post("/admin/chaos", json={"error_rate": 2}).status_code == 422
    twin.post("/admin/reset")
    assert twin.get("/departments").status_code == 200  # reset turns chaos off


def test_hold_idempotency_key_is_per_slot(twin):
    a, b = two_free_slots(twin)
    key = uuid.uuid4().hex
    assert hold(twin, a["id"], key).status_code == 201
    assert hold(twin, a["id"], key).json()["slot_id"] == a["id"]
    assert hold(twin, b["id"], key).status_code == 422
