"""Hospital Twin storage: SQLAlchemy Core tables.

SQLite by default (zero setup); any SQLAlchemy URL works. Booking safety does not
depend on the database: every claim on a slot or hold is a single conditional
UPDATE ("only if still free"), so two callers can never both win.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

metadata = MetaData()

departments = Table(
    "departments",
    metadata,
    Column("id", String, primary_key=True),  # e.g. CARD
    Column("name", String, nullable=False),
    Column("floor", String, nullable=False),
    Column("phone_ext", String, nullable=False),
)

practitioners = Table(
    "practitioners",
    metadata,
    Column("id", String, primary_key=True),  # D-01
    Column("given", String, nullable=False),
    Column("family", String, nullable=False),
    Column("gender", String, nullable=False),
    Column("department_id", String, ForeignKey("departments.id"), nullable=False),
    Column("qualification", String, nullable=False),
    Column("languages", String, nullable=False),  # comma-separated BCP-47 codes
    Column("opd_days", String, nullable=False),  # comma-separated weekday numbers, Mon=0
    Column("opd_start", String, nullable=False),  # "09:00"
    Column("opd_end", String, nullable=False),
    Column("slot_minutes", Integer, nullable=False),
    Column("fee_inr", Integer, nullable=False),
)

patients = Table(
    "patients",
    metadata,
    Column("id", String, primary_key=True),  # P-1001
    Column("given", String, nullable=False),
    Column("family", String, nullable=False),
    Column("gender", String, nullable=False),
    Column("birth_date", Date, nullable=False),
    Column("phone", String, nullable=False, index=True),
    Column("postal_code", String),
    Column("city", String),
    Column("language", String),
    Column("abha", String),
    Column("created_at", DateTime, nullable=False),
)

slots = Table(
    "slots",
    metadata,
    Column("id", String, primary_key=True),  # S-000123
    Column("practitioner_id", String, ForeignKey("practitioners.id"), nullable=False, index=True),
    Column("start", DateTime, nullable=False, index=True),
    Column("end", DateTime, nullable=False),
    Column("status", String, nullable=False),  # free | held | booked
    Column("hold_id", String),
    Column("hold_expires_at", DateTime),
)

holds = Table(
    "holds",
    metadata,
    Column("id", String, primary_key=True),  # H-xxxxxxxx
    Column("slot_id", String, ForeignKey("slots.id"), nullable=False),
    Column("idempotency_key", String, nullable=False, unique=True),
    Column("created_at", DateTime, nullable=False),
    Column("expires_at", DateTime, nullable=False),
    Column("status", String, nullable=False),  # active | used | released | lost
)

appointments = Table(
    "appointments",
    metadata,
    Column("id", String, primary_key=True),  # A-000001
    Column("patient_id", String, ForeignKey("patients.id"), nullable=False, index=True),
    Column("practitioner_id", String, ForeignKey("practitioners.id"), nullable=False),
    Column("slot_id", String, ForeignKey("slots.id")),
    Column("start", DateTime, nullable=False),
    Column("end", DateTime, nullable=False),
    Column("status", String, nullable=False),  # booked | cancelled | arrived | fulfilled | noshow
    Column("reason", Text),
    Column("cancel_reason", Text),
    Column("rescheduled_from", DateTime),
    Column("token", Integer),
    Column("arrived_at", DateTime),
    Column("created_at", DateTime, nullable=False),
)

doctor_delays = Table(
    "doctor_delays",
    metadata,
    Column("practitioner_id", String, ForeignKey("practitioners.id"), primary_key=True),
    Column("date", Date, primary_key=True),
    Column("minutes", Integer, nullable=False),
)

reports = Table(
    "reports",
    metadata,
    Column("id", String, primary_key=True),  # R-0001
    Column("patient_id", String, ForeignKey("patients.id"), nullable=False, index=True),
    Column("test_name", String, nullable=False),
    Column("status", String, nullable=False),  # registered | preliminary | final
    Column("ordered_at", DateTime, nullable=False),
    Column("issued_at", DateTime),
)

invoices = Table(
    "invoices",
    metadata,
    Column("id", String, primary_key=True),  # INV-0001
    Column("patient_id", String, ForeignKey("patients.id"), nullable=False, index=True),
    Column("description", String, nullable=False),
    Column("amount_inr", Float, nullable=False),
    Column("status", String, nullable=False),  # issued | balanced | cancelled
    Column("payer", String, nullable=False),  # self | PM-JAY | TPA:<name>
    Column("insurance_status", String, nullable=False),  # not_applicable | pre_auth_pending | approved | rejected
    Column("created_at", DateTime, nullable=False),
)

waitlist = Table(
    "waitlist",
    metadata,
    Column("id", String, primary_key=True),  # W-xxxxxxxx
    Column("patient_id", String, ForeignKey("patients.id"), nullable=False),
    Column("practitioner_id", String),
    Column("department_id", String),
    Column("date_from", Date, nullable=False),
    Column("date_to", Date, nullable=False),
    Column("status", String, nullable=False),  # waiting | offered | filled | expired
    Column("created_at", DateTime, nullable=False),
)


from core.db import make_engine  # noqa: E402,F401  (re-exported for twin users)
