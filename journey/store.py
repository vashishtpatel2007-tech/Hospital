"""Journey Engine storage.

journeys        one row per patient journey (a projection, updated with each event)
journey_events  append-only, hash-chained audit log (the source of truth)
sessions        one row per call / WhatsApp conversation; verification lives here (resets per call)
tasks           work for humans: callbacks, escalations, dedupe reviews, transfers
locks           verification lockouts (wrong DOB twice)

Raw phone numbers, birth dates and OTPs are never stored: phones are hashed, OTPs are
hashed, free text is redacted.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)

from core.db import make_engine

metadata = MetaData()

journeys = Table(
    "journeys",
    metadata,
    Column("id", String, primary_key=True),  # J-xxxxxxxx
    Column("phone_hash", String, index=True),
    Column("patient_ref", String, index=True),
    Column("state", String, nullable=False),
    Column("urgency", Integer, nullable=False, default=0),
    Column("escalated", Boolean, nullable=False, default=False),
    Column("context", Text, nullable=False, default="{}"),  # JSON, no raw personal data
    Column("last_seq", Integer, nullable=False, default=0),
    Column("last_hash", String, nullable=False),
    Column("opened_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("closed_at", DateTime),
)

journey_events = Table(
    "journey_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("journey_id", String, nullable=False, index=True),
    Column("seq", Integer, nullable=False),
    Column("ts", String, nullable=False),
    Column("type", String, nullable=False),
    Column("actor", String, nullable=False),  # ai | sentinel | human | system | patient
    Column("payload", Text, nullable=False),  # canonical JSON, redacted
    Column("prev_hash", String, nullable=False),
    Column("hash", String, nullable=False),
    UniqueConstraint("journey_id", "seq"),
)

sessions = Table(
    "sessions",
    metadata,
    Column("id", String, primary_key=True),  # C-xxxxxxxx
    Column("journey_id", String, nullable=False, index=True),
    Column("phone_hash", String, nullable=False),
    Column("channel", String, nullable=False),  # voice | web | whatsapp | text
    Column("relation", String, nullable=False, default="self"),
    Column("proxy_consent", Boolean, nullable=False, default=False),
    Column("level", Integer, nullable=False, default=0),
    Column("patient_ref", String),
    Column("soft_candidates", Text, nullable=False, default="[]"),
    Column("verify_failures", Integer, nullable=False, default=0),
    Column("otp_hash", String),
    Column("otp_phone_hash", String),
    Column("otp_expires_at", DateTime),
    Column("otp_attempts", Integer, nullable=False, default=0),
    Column("otps_sent", Integer, nullable=False, default=0),
    Column("started_at", DateTime, nullable=False),
    Column("ended_at", DateTime),
)

tasks = Table(
    "tasks",
    metadata,
    Column("id", String, primary_key=True),  # T-xxxxxxxx
    Column("journey_id", String, nullable=False, index=True),
    Column("kind", String, nullable=False),  # callback | escalation | dedupe_review | transfer | pending_sync
    Column("urgency", Integer, nullable=False),
    Column("status", String, nullable=False),  # open | done
    Column("reason", Text, nullable=False),
    Column("data", Text, nullable=False, default="{}"),
    Column("sla_due", DateTime, nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("resolved_at", DateTime),
    Column("resolved_by", String),
    Column("resolution", Text),
)

locks = Table(
    "locks",
    metadata,
    Column("key", String, primary_key=True),
    Column("until", DateTime, nullable=False),
    Column("reason", String, nullable=False),
)

# The event log is append-only at the database level too: UPDATE and DELETE are rejected.
_APPEND_ONLY_SQLITE = [
    """CREATE TRIGGER IF NOT EXISTS journey_events_no_update BEFORE UPDATE ON journey_events
       BEGIN SELECT RAISE(ABORT, 'journey_events is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS journey_events_no_delete BEFORE DELETE ON journey_events
       BEGIN SELECT RAISE(ABORT, 'journey_events is append-only'); END""",
]
_APPEND_ONLY_POSTGRES = [
    """CREATE OR REPLACE FUNCTION journey_events_append_only() RETURNS trigger AS $$
       BEGIN RAISE EXCEPTION 'journey_events is append-only'; END; $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS journey_events_append_only ON journey_events",
    """CREATE TRIGGER journey_events_append_only BEFORE UPDATE OR DELETE ON journey_events
       FOR EACH ROW EXECUTE FUNCTION journey_events_append_only()""",
]


def open_store(url: str) -> Engine:
    engine = make_engine(url)
    metadata.create_all(engine)
    ddl = _APPEND_ONLY_SQLITE if engine.dialect.name == "sqlite" else (
        _APPEND_ONLY_POSTGRES if engine.dialect.name == "postgresql" else [])
    with engine.begin() as conn:
        for stmt in ddl:
            conn.execute(text(stmt))
    return engine
