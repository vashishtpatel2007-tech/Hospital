"""Hospital Twin: a FHIR-shaped mock hospital information system (HIS).

Run:  make twin   (http://localhost:8001/docs)

Every claim on a slot is a single conditional UPDATE ("only if still free"), so the
same slot can never be held or booked twice, however many callers race for it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import random
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Engine, and_, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from . import db, fhir, seed
from .matching import name_score
from .state import Chaos, DemoClock

load_dotenv()

HOLD_TTL_SECONDS = 120
CHAOS_EXEMPT = ("/health", "/admin", "/docs", "/openapi.json", "/redoc")


class TwinError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def not_found(what: str, ident: str) -> TwinError:
    return TwinError(404, "not_found", f"{what} {ident} not found")


# ------------------------------------------------------------------ request bodies


class HoldIn(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=200)
    ttl_seconds: int = Field(default=HOLD_TTL_SECONDS, ge=10, le=900)


class BookIn(BaseModel):
    hold_id: str
    patient_id: str
    reason: str | None = Field(default=None, max_length=500)


class RescheduleIn(BaseModel):
    hold_id: str


class CancelIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class PatientIn(BaseModel):
    given: str = Field(min_length=1, max_length=80)
    family: str = Field(min_length=1, max_length=80)
    gender: Literal["male", "female", "other"]
    birth_date: date
    phone: str = Field(min_length=10, max_length=16)
    postal_code: str | None = Field(default=None, pattern=r"^\d{6}$")
    language: str | None = Field(default=None, max_length=8)
    abha: str | None = Field(default=None, max_length=80)


class DelayIn(BaseModel):
    minutes: int = Field(ge=0, le=600)
    date: Optional[dt.date] = None  # dt.date: the field name shadows the bare `date` type


class WaitlistIn(BaseModel):
    patient_id: str
    practitioner_id: str | None = None
    department_id: str | None = None
    date_from: date
    date_to: date


class ChaosIn(BaseModel):
    latency_ms: int | None = Field(default=None, ge=0, le=30000)
    error_rate: float | None = Field(default=None, ge=0, le=1)
    his_down: bool | None = None
    steal_held_slot: bool | None = None


class ClockIn(BaseModel):
    advance_minutes: float | None = Field(default=None, ge=0, le=60 * 24 * 30)
    reset: bool = False


# ------------------------------------------------------------------ helpers


def normalise_phone(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 11 and digits.startswith("0"):  # 098450 12345
        digits = digits[1:]
    if len(digits) == 10:
        digits = "91" + digits
    if len(digits) != 12 or not digits.startswith("91"):
        raise TwinError(422, "validation_error", "phone must be a 10-digit Indian mobile number")
    return "+" + digits


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def free_slot_clause(now: datetime):
    """A slot is available if free, or held by a hold that has expired."""
    s = db.slots.c
    return or_(s.status == "free", and_(s.status == "held", s.hold_expires_at < now))


def part_of_day(t: time) -> str:
    return "morning" if t.hour < 12 else "afternoon" if t.hour < 16 else "evening"


# ------------------------------------------------------------------ app factory


def create_app(db_url: str | None = None, seed_on_start: bool = True) -> FastAPI:
    url = db_url or os.getenv("TWIN_DB_URL", "sqlite:///./twin.db")
    engine: Engine = db.make_engine(url)
    clock = DemoClock()
    chaos = Chaos()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if seed_on_start:
            now = clock.now()
            seed.ensure_seeded(engine, now.date(), now)
        yield
        engine.dispose()

    app = FastAPI(
        title="Sahay Hospital Twin",
        version="1.0",
        description="Mock hospital system (FHIR-shaped). Times are IST. Admin routes control the demo clock "
                    "and failure switches.",
        lifespan=lifespan,
    )
    app.state.engine, app.state.clock, app.state.chaos = engine, clock, chaos

    # ---------------------------------------------------------------- errors & chaos

    @app.exception_handler(TwinError)
    async def _twin_error(_req: Request, exc: TwinError):
        return JSONResponse(status_code=exc.status, content={"error": exc.code, "message": exc.message})

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_req: Request, exc: RequestValidationError):
        msgs = "; ".join(f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}" for e in exc.errors())
        return JSONResponse(status_code=422, content={"error": "validation_error", "message": msgs})

    @app.middleware("http")
    async def _chaos(request: Request, call_next):
        if not request.url.path.startswith(CHAOS_EXEMPT):
            c = chaos.snapshot()
            if c["his_down"]:
                return JSONResponse(status_code=503, content={
                    "error": "his_unavailable", "message": "Hospital system is down (chaos switch)"})
            if c["latency_ms"]:
                await asyncio.sleep(c["latency_ms"] / 1000)
            if c["error_rate"] and random.random() < c["error_rate"]:
                return JSONResponse(status_code=500, content={
                    "error": "internal_error", "message": "Random failure (chaos switch)"})
        return await call_next(request)

    # ---------------------------------------------------------------- lookups

    def get_row(conn, table, ident: str, what: str):
        row = conn.execute(select(table).where(table.c.id == ident)).mappings().first()
        if row is None:
            raise not_found(what, ident)
        return row

    def dept_names(conn) -> dict[str, str]:
        return dict(conn.execute(select(db.departments.c.id, db.departments.c.name)).all())

    def doctor_index(conn) -> dict[str, dict]:
        return {r["id"]: dict(r) for r in conn.execute(select(db.practitioners)).mappings()}

    def check_hold_usable(conn, hold_id: str, now: datetime):
        h = get_row(conn, db.holds, hold_id, "Hold")
        if h["status"] == "used":
            raise TwinError(409, "hold_used", f"Hold {hold_id} was already used")
        if h["status"] == "lost":
            raise TwinError(409, "slot_taken", "The held slot was taken by someone else")
        if h["status"] == "released" or h["expires_at"] < now:
            raise TwinError(409, "hold_expired", f"Hold {hold_id} has expired; hold the slot again")
        return h

    def claim_hold(conn, h, now: datetime) -> None:
        """Turn an active hold into a booked slot, atomically, or raise."""
        res = conn.execute(update(db.holds)
                           .where(db.holds.c.id == h["id"], db.holds.c.status == "active",
                                  db.holds.c.expires_at >= now)
                           .values(status="used"))
        if res.rowcount != 1:
            raise TwinError(409, "hold_used", f"Hold {h['id']} was already used")
        res = conn.execute(update(db.slots)
                           .where(db.slots.c.id == h["slot_id"], db.slots.c.hold_id == h["id"],
                                  db.slots.c.status == "held")
                           .values(status="booked", hold_expires_at=None))
        if res.rowcount != 1:
            raise TwinError(409, "slot_taken", "The held slot was taken by someone else")

    def replayed(conn, hold_id: str, patient_id: str | None = None, appointment_id: str | None = None):
        """Same hold sent again (a retry after a timeout): return what it already produced.

        The booking side may have given up waiting while the first request still went through.
        Answering the retry with the same appointment means a retry can never double-book.
        """
        h = conn.execute(select(db.holds).where(db.holds.c.id == hold_id)).mappings().first()
        if h is None or h["status"] != "used":
            return None
        a = db.appointments.c
        q = select(db.appointments).where(a.slot_id == h["slot_id"], a.status == "booked")
        q = q.where(a.id == appointment_id) if appointment_id else q.where(a.patient_id == patient_id)
        return conn.execute(q).mappings().first()

    def maybe_steal(h) -> None:
        """Chaos: someone else grabs the slot between hold and booking."""
        if not chaos.take_steal():
            return
        with engine.begin() as conn:
            conn.execute(update(db.slots).where(db.slots.c.id == h["slot_id"])
                         .values(status="booked", hold_id=None, hold_expires_at=None))
            conn.execute(update(db.holds).where(db.holds.c.id == h["id"]).values(status="lost"))
        raise TwinError(409, "slot_taken", "The held slot was taken by someone else")

    # ---------------------------------------------------------------- read routes

    @app.get("/health", tags=["system"])
    def health():
        return {"status": "ok", "now": clock.now().isoformat()}

    @app.get("/departments", tags=["directory"])
    def list_departments():
        with engine.connect() as conn:
            return [fhir.department(r) for r in conn.execute(select(db.departments).order_by(db.departments.c.name)).mappings()]

    @app.get("/practitioners", tags=["directory"])
    def list_practitioners(department_id: str | None = None, language: str | None = None,
                           gender: Literal["male", "female"] | None = None, name: str | None = None):
        with engine.connect() as conn:
            names = dept_names(conn)
            out = []
            for d in doctor_index(conn).values():
                if department_id and d["department_id"] != department_id.upper():
                    continue
                if language and language not in d["languages"].split(","):
                    continue
                if gender and d["gender"] != gender:
                    continue
                if name and name_score(name.replace("Dr.", "").replace("Dr ", ""), d["given"], d["family"]) < 0.7:
                    continue
                out.append(fhir.practitioner(d, names.get(d["department_id"])))
            return out

    @app.get("/practitioners/{practitioner_id}", tags=["directory"])
    def get_practitioner(practitioner_id: str):
        with engine.connect() as conn:
            d = get_row(conn, db.practitioners, practitioner_id, "Practitioner")
            return fhir.practitioner(d, dept_names(conn).get(d["department_id"]))

    @app.get("/slots", tags=["scheduling"])
    def search_slots(practitioner_id: str | None = None, department_id: str | None = None,
                     date_from: date | None = None, date_to: date | None = None,
                     part_of_day_: Literal["morning", "afternoon", "evening"] | None = Query(None, alias="part_of_day"),
                     language: str | None = None, gender: Literal["male", "female"] | None = None,
                     limit: int = Query(20, ge=1, le=200)):
        now = clock.now()
        start_day = max(date_from or now.date(), now.date())
        end_day = date_to or (start_day + timedelta(days=7))
        if end_day < start_day:
            raise TwinError(422, "validation_error", "date_to is before date_from")
        with engine.connect() as conn:
            doctors = doctor_index(conn)
            ids = [i for i, d in doctors.items()
                   if (not practitioner_id or i == practitioner_id)
                   and (not department_id or d["department_id"] == department_id.upper())
                   and (not language or language in d["languages"].split(","))
                   and (not gender or d["gender"] == gender)]
            if practitioner_id and practitioner_id not in doctors:
                raise not_found("Practitioner", practitioner_id)
            s = db.slots.c
            rows = conn.execute(
                select(db.slots)
                .where(s.practitioner_id.in_(ids), s.start > now,
                       s.start >= datetime.combine(start_day, time.min),
                       s.start < datetime.combine(end_day + timedelta(days=1), time.min),
                       free_slot_clause(now))
                .order_by(s.start, s.practitioner_id)).mappings()
            out = []
            for r in rows:
                if part_of_day_ and part_of_day(r["start"].time()) != part_of_day_:
                    continue
                d = doctors[r["practitioner_id"]]
                out.append(fhir.slot(r, now, f"Dr. {d['given']} {d['family']}", d["department_id"]))
                if len(out) >= limit:
                    break
            return out

    @app.get("/patients", tags=["patients"])
    def search_patients(phone: str | None = None, name: str | None = None, birth_date: date | None = None):
        if not (phone or name or birth_date):
            raise TwinError(422, "validation_error", "give at least one of phone, name, birth_date")
        want_phone = normalise_phone(phone) if phone else None
        with engine.connect() as conn:
            cands = []
            for p in conn.execute(select(db.patients)).mappings():
                matched, score = [], 0.0
                if want_phone and p["phone"] == want_phone:
                    matched.append("phone")
                if birth_date and p["birth_date"] == birth_date:
                    matched.append("birth_date")
                ns = name_score(name, p["given"], p["family"]) if name else 0.0
                if name and ns >= 0.75:
                    matched.append("name")
                if not matched:
                    continue
                # weight: phone .4, dob .3, name .3 (scaled by similarity)
                score = (0.4 if "phone" in matched else 0) + (0.3 if "birth_date" in matched else 0) + (0.3 * ns if name else 0)
                cands.append({"patient": fhir.patient(p), "score": round(score, 3), "name_similarity": round(ns, 3),
                              "matched_on": matched})
            cands.sort(key=lambda c: (-c["score"], c["patient"]["id"]))
            return {"candidates": cands[:10]}

    @app.get("/patients/{patient_id}", tags=["patients"])
    def get_patient(patient_id: str):
        with engine.connect() as conn:
            return fhir.patient(get_row(conn, db.patients, patient_id, "Patient"))

    @app.get("/patients/{patient_id}/appointments", tags=["patients"])
    def patient_appointments(patient_id: str):
        with engine.connect() as conn:
            get_row(conn, db.patients, patient_id, "Patient")
            rows = conn.execute(select(db.appointments).where(db.appointments.c.patient_id == patient_id)
                                .order_by(db.appointments.c.start)).mappings()
            return [fhir.appointment(r) for r in rows]

    @app.get("/patients/{patient_id}/reports", tags=["patients"])
    def patient_reports(patient_id: str):
        with engine.connect() as conn:
            get_row(conn, db.patients, patient_id, "Patient")
            rows = conn.execute(select(db.reports).where(db.reports.c.patient_id == patient_id)
                                .order_by(db.reports.c.ordered_at.desc())).mappings()
            return [fhir.report(r) for r in rows]

    @app.get("/patients/{patient_id}/invoices", tags=["patients"])
    def patient_invoices(patient_id: str):
        with engine.connect() as conn:
            get_row(conn, db.patients, patient_id, "Patient")
            rows = conn.execute(select(db.invoices).where(db.invoices.c.patient_id == patient_id)
                                .order_by(db.invoices.c.created_at.desc())).mappings()
            return [fhir.invoice(r) for r in rows]

    @app.get("/appointments/{appointment_id}", tags=["scheduling"])
    def get_appointment(appointment_id: str):
        with engine.connect() as conn:
            return fhir.appointment(get_row(conn, db.appointments, appointment_id, "Appointment"))

    def queue_info(conn, a, now: datetime) -> dict:
        d = get_row(conn, db.practitioners, a["practitioner_id"], "Practitioner")
        day = a["start"].date()
        delay = conn.execute(select(db.doctor_delays.c.minutes).where(
            db.doctor_delays.c.practitioner_id == d["id"], db.doctor_delays.c.date == day)).scalar() or 0
        day_slots = conn.execute(select(db.slots.c.start).where(
            db.slots.c.practitioner_id == d["id"],
            db.slots.c.start >= datetime.combine(day, time.min),
            db.slots.c.start < datetime.combine(day + timedelta(days=1), time.min)).order_by(db.slots.c.start)).scalars().all()
        token = (day_slots.index(a["start"]) + 1) if a["start"] in day_slots else None
        opd_start = datetime.combine(day, time.fromisoformat(d["opd_start"])) + timedelta(minutes=delay)
        mins = d["slot_minutes"]
        if now < opd_start:
            now_serving = 0
        else:
            now_serving = min(len(day_slots), int((now - opd_start).total_seconds() // (mins * 60)) + 1)
        info = {"appointment_id": a["id"], "practitioner_id": d["id"], "date": day.isoformat(), "token": token,
                "status": a["status"], "doctor_delay_minutes": delay, "now_serving": now_serving,
                "ahead": None, "est_wait_minutes": None}
        if token is not None:
            est_start = opd_start + timedelta(minutes=(token - 1) * mins)
            info["ahead"] = token - 1 if now_serving == 0 else max(0, token - now_serving)
            info["est_wait_minutes"] = max(0, int((est_start - now).total_seconds() // 60))
            if now_serving > token:
                info["note"] = "Turn has passed; please check with the OPD desk"
        return info

    @app.get("/appointments/{appointment_id}/queue", tags=["queue"])
    def appointment_queue(appointment_id: str):
        now = clock.now()
        with engine.connect() as conn:
            return queue_info(conn, get_row(conn, db.appointments, appointment_id, "Appointment"), now)

    @app.get("/waitlist", tags=["scheduling"])
    def list_waitlist(patient_id: str | None = None):
        with engine.connect() as conn:
            q = select(db.waitlist).order_by(db.waitlist.c.created_at)
            if patient_id:
                q = q.where(db.waitlist.c.patient_id == patient_id)
            return [fhir.waitlist_entry(r) for r in conn.execute(q).mappings()]

    # ---------------------------------------------------------------- write routes

    @app.post("/patients", status_code=201, tags=["patients"])
    def register_patient(body: PatientIn):
        now = clock.now()
        if body.birth_date > now.date():
            raise TwinError(422, "validation_error", "birth_date is in the future")
        phone = normalise_phone(body.phone)
        with engine.begin() as conn:
            ids = conn.execute(select(db.patients.c.id)).scalars().all()
            pid = f"P-{max((int(i.split('-')[1]) for i in ids), default=1000) + 1}"
            row = dict(id=pid, given=body.given.strip(), family=body.family.strip(), gender=body.gender,
                       birth_date=body.birth_date, phone=phone, postal_code=body.postal_code, city=None,
                       language=body.language, abha=body.abha, created_at=now)
            conn.execute(insert(db.patients), row)
        return fhir.patient(row)

    @app.post("/slots/{slot_id}/hold", status_code=201, tags=["scheduling"])
    def hold_slot(slot_id: str, body: HoldIn):
        now = clock.now()
        try:
            with engine.begin() as conn:
                existing = conn.execute(select(db.holds).where(db.holds.c.idempotency_key == body.idempotency_key)).mappings().first()
                if existing:
                    if existing["slot_id"] != slot_id:
                        raise TwinError(422, "validation_error", "idempotency_key was already used for a different slot")
                    return fhir.hold(existing, now)
                s = get_row(conn, db.slots, slot_id, "Slot")
                if s["start"] <= now:
                    raise TwinError(409, "slot_unavailable", f"Slot {slot_id} is in the past")
                hold_id = new_id("H")
                expires = now + timedelta(seconds=body.ttl_seconds)
                res = conn.execute(update(db.slots)
                                   .where(db.slots.c.id == slot_id, free_slot_clause(now))
                                   .values(status="held", hold_id=hold_id, hold_expires_at=expires))
                if res.rowcount != 1:
                    raise TwinError(409, "slot_unavailable", f"Slot {slot_id} is already held or booked")
                row = dict(id=hold_id, slot_id=slot_id, idempotency_key=body.idempotency_key, created_at=now,
                           expires_at=expires, status="active")
                conn.execute(insert(db.holds), row)
                return fhir.hold(row, now)
        except IntegrityError:  # same idempotency key raced in on another connection
            with engine.connect() as conn:
                existing = conn.execute(select(db.holds).where(db.holds.c.idempotency_key == body.idempotency_key)).mappings().first()
            if existing and existing["slot_id"] == slot_id:
                return fhir.hold(existing, now)
            raise TwinError(409, "slot_unavailable", f"Slot {slot_id} is already held or booked")

    @app.delete("/holds/{hold_id}", tags=["scheduling"])
    def release_hold(hold_id: str):
        now = clock.now()
        with engine.begin() as conn:
            h = get_row(conn, db.holds, hold_id, "Hold")
            if h["status"] == "active":
                conn.execute(update(db.holds).where(db.holds.c.id == hold_id).values(status="released"))
                conn.execute(update(db.slots).where(db.slots.c.id == h["slot_id"], db.slots.c.hold_id == hold_id,
                                                    db.slots.c.status == "held")
                             .values(status="free", hold_id=None, hold_expires_at=None))
            return fhir.hold({**h, "status": "released" if h["status"] == "active" else h["status"]}, now)

    @app.post("/appointments", status_code=201, tags=["scheduling"])
    def book_appointment(body: BookIn, response: Response):
        now = clock.now()
        with engine.connect() as conn:
            if (done := replayed(conn, body.hold_id, patient_id=body.patient_id)) is not None:
                response.status_code = 200
                return fhir.appointment(done)
            h = check_hold_usable(conn, body.hold_id, now)
            get_row(conn, db.patients, body.patient_id, "Patient")
        maybe_steal(h)
        with engine.begin() as conn:
            h = check_hold_usable(conn, body.hold_id, now)
            claim_hold(conn, h, now)
            s = get_row(conn, db.slots, h["slot_id"], "Slot")
            row = dict(id=new_id("A"), patient_id=body.patient_id, practitioner_id=s["practitioner_id"],
                       slot_id=s["id"], start=s["start"], end=s["end"], status="booked", reason=body.reason,
                       cancel_reason=None, rescheduled_from=None, token=None, arrived_at=None, created_at=now)
            conn.execute(insert(db.appointments), row)
        return fhir.appointment(row)

    @app.post("/appointments/{appointment_id}/reschedule", tags=["scheduling"])
    def reschedule_appointment(appointment_id: str, body: RescheduleIn):
        now = clock.now()
        with engine.connect() as conn:
            if (done := replayed(conn, body.hold_id, appointment_id=appointment_id)) is not None:
                return fhir.appointment(done)
            a = get_row(conn, db.appointments, appointment_id, "Appointment")
            if a["status"] != "booked":
                raise TwinError(409, "invalid_state", f"Appointment {appointment_id} is {a['status']}")
            h = check_hold_usable(conn, body.hold_id, now)
        maybe_steal(h)
        with engine.begin() as conn:
            a = get_row(conn, db.appointments, appointment_id, "Appointment")
            if a["status"] != "booked":
                raise TwinError(409, "invalid_state", f"Appointment {appointment_id} is {a['status']}")
            h = check_hold_usable(conn, body.hold_id, now)
            claim_hold(conn, h, now)
            s = get_row(conn, db.slots, h["slot_id"], "Slot")
            if a["slot_id"]:
                conn.execute(update(db.slots).where(db.slots.c.id == a["slot_id"], db.slots.c.status == "booked")
                             .values(status="free", hold_id=None, hold_expires_at=None))
            changes = dict(slot_id=s["id"], practitioner_id=s["practitioner_id"], start=s["start"], end=s["end"],
                           rescheduled_from=a["start"])
            conn.execute(update(db.appointments).where(db.appointments.c.id == appointment_id).values(**changes))
        return fhir.appointment({**a, **changes})

    @app.post("/appointments/{appointment_id}/cancel", tags=["scheduling"])
    def cancel_appointment(appointment_id: str, body: CancelIn):
        with engine.begin() as conn:
            a = get_row(conn, db.appointments, appointment_id, "Appointment")
            res = conn.execute(update(db.appointments)
                               .where(db.appointments.c.id == appointment_id, db.appointments.c.status == "booked")
                               .values(status="cancelled", cancel_reason=body.reason))
            if res.rowcount != 1:
                raise TwinError(409, "invalid_state", f"Appointment {appointment_id} is {a['status']}")
            if a["slot_id"]:
                conn.execute(update(db.slots).where(db.slots.c.id == a["slot_id"])
                             .values(status="free", hold_id=None, hold_expires_at=None))
        return fhir.appointment({**a, "status": "cancelled", "cancel_reason": body.reason})

    @app.post("/appointments/{appointment_id}/arrive", tags=["queue"])
    def arrive(appointment_id: str):
        now = clock.now()
        with engine.begin() as conn:
            a = get_row(conn, db.appointments, appointment_id, "Appointment")
            if a["status"] != "booked":
                raise TwinError(409, "invalid_state", f"Appointment {appointment_id} is {a['status']}")
            if a["start"].date() != now.date():
                raise TwinError(409, "invalid_state", "Check-in is only possible on the appointment day")
            info = queue_info(conn, a, now)
            conn.execute(update(db.appointments).where(db.appointments.c.id == appointment_id)
                         .values(status="arrived", arrived_at=now, token=info["token"]))
            return {**info, "status": "arrived"}

    @app.post("/practitioners/{practitioner_id}/delay", tags=["queue"])
    def set_delay(practitioner_id: str, body: DelayIn):
        now = clock.now()
        day = body.date or now.date()
        with engine.begin() as conn:
            get_row(conn, db.practitioners, practitioner_id, "Practitioner")
            conn.execute(db.doctor_delays.delete().where(db.doctor_delays.c.practitioner_id == practitioner_id,
                                                         db.doctor_delays.c.date == day))
            conn.execute(insert(db.doctor_delays), dict(practitioner_id=practitioner_id, date=day, minutes=body.minutes))
            affected = conn.execute(select(func.count()).select_from(db.appointments).where(
                db.appointments.c.practitioner_id == practitioner_id,
                db.appointments.c.status.in_(["booked", "arrived"]),
                db.appointments.c.start >= max(now, datetime.combine(day, time.min)),
                db.appointments.c.start < datetime.combine(day + timedelta(days=1), time.min))).scalar()
        return {"practitioner_id": practitioner_id, "date": day.isoformat(), "delay_minutes": body.minutes,
                "upcoming_appointments_affected": affected}

    @app.post("/waitlist", status_code=201, tags=["scheduling"])
    def join_waitlist(body: WaitlistIn):
        if not (body.practitioner_id or body.department_id):
            raise TwinError(422, "validation_error", "give practitioner_id or department_id")
        if body.date_to < body.date_from:
            raise TwinError(422, "validation_error", "date_to is before date_from")
        now = clock.now()
        with engine.begin() as conn:
            get_row(conn, db.patients, body.patient_id, "Patient")
            if body.practitioner_id:
                get_row(conn, db.practitioners, body.practitioner_id, "Practitioner")
            if body.department_id:
                get_row(conn, db.departments, body.department_id.upper(), "Department")
            row = dict(id=new_id("W"), patient_id=body.patient_id, practitioner_id=body.practitioner_id,
                       department_id=body.department_id.upper() if body.department_id else None,
                       date_from=body.date_from, date_to=body.date_to, status="waiting", created_at=now)
            conn.execute(insert(db.waitlist), row)
        return fhir.waitlist_entry(row)

    # ---------------------------------------------------------------- admin

    @app.get("/admin/chaos", tags=["admin"])
    def get_chaos():
        return chaos.snapshot()

    @app.post("/admin/chaos", tags=["admin"])
    def set_chaos(body: ChaosIn):
        return chaos.update(**body.model_dump())

    @app.get("/admin/clock", tags=["admin"])
    def get_clock():
        return clock.snapshot()

    @app.post("/admin/clock", tags=["admin"])
    def set_clock(body: ClockIn):
        if body.reset:
            clock.reset()
        elif body.advance_minutes:
            clock.advance(body.advance_minutes)
        return clock.snapshot()

    @app.post("/admin/reset", tags=["admin"])
    def admin_reset():
        chaos.reset()
        clock.reset()
        now = clock.now()
        counts = seed.reset(engine, now.date(), now)
        return {"status": "reseeded", "counts": counts, "now": now.isoformat()}

    return app


app = create_app()
