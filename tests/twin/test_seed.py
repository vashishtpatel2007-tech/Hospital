"""Stage 1 gate: the seed data is realistic, complete and deterministic."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from twin import db, seed
from twin.matching import name_score

NOW = datetime(2026, 11, 22, 9, 30)  # a Sunday; any date must work


@pytest.fixture(scope="module")
def rows():
    return seed.build(NOW.date(), NOW)


def test_counts(rows):
    assert len(rows["departments"]) == 12
    assert len(rows["practitioners"]) == 30
    assert len(rows["patients"]) == 300
    assert len({p["id"] for p in rows["patients"]}) == 300


def test_every_doctor_has_free_slots_in_next_7_days(rows):
    week_end = NOW + timedelta(days=7)
    free = Counter(s["practitioner_id"] for s in rows["slots"]
                   if s["status"] == "free" and NOW < s["start"] <= week_end)
    for d in rows["practitioners"]:
        assert free[d["id"]] >= 5, f"{d['id']} has only {free[d['id']]} free slots"


def test_about_a_third_of_slots_booked(rows):
    booked = sum(s["status"] == "booked" for s in rows["slots"]) / len(rows["slots"])
    assert 0.25 < booked < 0.45


def test_no_slot_booked_twice(rows):
    future = [a for a in rows["appointments"] if a["slot_id"]]
    assert len({a["slot_id"] for a in future}) == len(future)


def test_demo_personas(rows):
    by_id = {p["id"]: p for p in rows["patients"]}
    assert (by_id["P-1001"]["given"], by_id["P-1001"]["family"]) == ("Ramesh", "Sharma")
    assert by_id["P-1002"]["language"] == "kn"
    ramesh_future = [a for a in rows["appointments"] if a["patient_id"] == "P-1001" and a["start"] > NOW]
    assert ramesh_future and ramesh_future[0]["practitioner_id"] == "D-05"
    assert ramesh_future[0]["start"].weekday() == 3  # Thursday


def test_near_duplicates_exist_and_match(rows):
    for a, b in seed.NEAR_DUPLICATES:
        pa = next(p for p in rows["patients"] if (p["given"], p["family"]) == (a[0], a[1]))
        pb = next(p for p in rows["patients"] if (p["given"], p["family"]) == (b[0], b[1]))
        assert pa["birth_date"] == pb["birth_date"]
        assert name_score(f"{a[0]} {a[1]}", b[0], b[1]) >= 0.75, (a, b)


def test_distinct_people_do_not_match():
    assert name_score("Ramesh Sharma", "Suresh", "Sharma") < 0.9
    assert name_score("Ramesh", "Suresh", "Rao") < 0.75


def test_deterministic():
    a = seed.build(NOW.date(), NOW)
    b = seed.build(NOW.date(), NOW)
    assert a["patients"] == b["patients"] and a["slots"] == b["slots"]


def test_reset_writes_everything(tmp_path):
    engine = db.make_engine(f"sqlite:///{tmp_path}/t.db")
    counts = seed.reset(engine, NOW.date(), NOW)
    with engine.connect() as conn:
        assert len(conn.execute(select(db.slots.c.id)).all()) == counts["slots"]
    seed.ensure_seeded(engine, NOW.date(), NOW)  # no-op on a seeded DB
    with engine.connect() as conn:
        assert len(conn.execute(select(db.patients.c.id)).all()) == 300
