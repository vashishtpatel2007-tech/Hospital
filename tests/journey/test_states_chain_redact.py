"""Stage 2 unit tests: the state table, the hash chain and redaction (no hospital system needed)."""

from __future__ import annotations

import json
from collections import deque

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from journey import chain, store
from journey.redact import normalise_phone, redact_payload, redact_text
from journey.states import TRANSITIONS, Level, State, Urgency, can_move

# ---------------------------------------------------------------- state table


def test_every_state_reachable_from_contact():
    seen, todo = {State.CONTACT}, deque([State.CONTACT])
    while todo:
        for nxt in TRANSITIONS[todo.popleft()]:
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    assert seen == set(State)


def test_only_closed_is_terminal():
    for s in State:
        assert (len(TRANSITIONS[s]) == 0) == (s is State.CLOSED), s


@pytest.mark.parametrize("src,dst,allowed", [
    (State.CONTACT, State.BOOKED, False),
    (State.IDENTIFYING, State.SLOT_HELD, False),
    (State.SLOT_SEARCH, State.BOOKED, False),  # must hold first
    (State.SLOT_HELD, State.BOOKED, True),
    (State.REGISTERING, State.DEDUPE_REVIEW, True),
    (State.CLOSED, State.CONTACT, False),
])
def test_specific_moves(src, dst, allowed):
    assert can_move(src, dst) is allowed


def test_orderings():
    assert Urgency.ROUTINE < Urgency.SOON < Urgency.URGENT < Urgency.EMERGENCY
    assert Level.NONE < Level.SOFT < Level.MEDIUM < Level.STRONG


# ---------------------------------------------------------------- hash chain


def _events(n: int) -> list[dict]:
    rows, prev = [], chain.GENESIS
    for seq in range(1, n + 1):
        payload = {"i": seq, "note": f"event {seq}"}
        body = chain.event_body("J-1", seq, f"2026-11-22T10:00:{seq:02d}", "test", "system", payload)
        h = chain.compute_hash(prev, body)
        rows.append({**body, "payload": json.dumps(payload), "prev_hash": prev, "hash": h})
        prev = h
    return rows


def test_chain_verifies():
    assert chain.verify(_events(5)).ok


def test_chain_detects_edited_payload():
    rows = _events(5)
    rows[1]["payload"] = json.dumps({"i": 2, "note": "edited"})
    r = chain.verify(rows)
    assert not r.ok and r.first_bad_seq == 2


def test_chain_detects_deleted_event():
    rows = _events(5)
    del rows[2]
    r = chain.verify(rows)
    assert not r.ok and r.first_bad_seq == 4


def test_chain_detects_reorder_and_rehash_attempt():
    rows = _events(4)
    rows[1], rows[2] = rows[2], rows[1]
    assert not chain.verify(rows).ok
    rows = _events(4)
    rows[0]["actor"] = "human"  # change who did it
    assert not chain.verify(rows).ok


def test_event_table_rejects_update_and_delete(tmp_path):
    eng = store.open_store(f"sqlite:///{tmp_path}/j.db")
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO journey_events (journey_id, seq, ts, type, actor, payload, prev_hash, hash) "
                          "VALUES ('J-1', 1, 't', 'x', 'system', '{}', 'p', 'h')"))
    with pytest.raises(DatabaseError, match="append-only"):
        with eng.begin() as conn:
            conn.execute(text("UPDATE journey_events SET payload='tampered' WHERE seq=1"))
    with pytest.raises(DatabaseError, match="append-only"):
        with eng.begin() as conn:
            conn.execute(text("DELETE FROM journey_events"))


# ---------------------------------------------------------------- redaction


@pytest.mark.parametrize("raw,leak", [
    ("mera number 98450 12345 hai", "98450"),
    ("call +91-9845012345 please", "9845012345"),
    ("aadhaar 1234 5678 9012", "5678"),
    ("born 14/03/1968", "1968"),
    ("DOB 14th March 1968", "March"),
    ("my date of birth is 1968-03-14", "1968"),
    ("OTP is 482913", "482913"),
    ("email ramesh@gmail.com", "ramesh@"),
    ("ABHA ramesh.sharma@abdm", "abdm"),
])
def test_redact_text_removes_personal_data(raw, leak):
    assert leak not in redact_text(raw)


def test_redact_known_names_and_keeps_meaning():
    out = redact_text("Ramesh Sharma says seene mein dard hai", ["Ramesh", "Sharma"])
    assert out == "[NAME] [NAME] says seene mein dard hai"


def test_redact_payload_keys_and_free_text():
    out = redact_payload({"name": "Ramesh", "birth_date": "1968-03-14", "slot_start": "2026-11-26T10:30:00",
                          "reason": "call 9845012345", "nested": [{"phone": "+919845012345"}]})
    assert out["name"] == out["birth_date"] == "[REDACTED]"
    assert out["slot_start"] == "2026-11-26T10:30:00"  # appointment times are not personal data
    assert out["reason"] == "call [PHONE]"
    assert out["nested"][0]["phone"] == "[REDACTED]"


def test_normalise_phone():
    assert normalise_phone("98450 12345") == "+919845012345"
    assert normalise_phone("09845012345") == "+919845012345"
    assert normalise_phone("+91 98450-12345") == "+919845012345"
    assert normalise_phone("12345") is None
    assert normalise_phone("5845012345") is None  # Indian mobiles start 6-9
