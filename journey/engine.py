"""Journey Engine: the rule-keeper between the AI and the hospital system.

The LLM proposes, this code disposes. Every tool call passes one gate:

  1. Is the tool known, and is the call (session) still live?
  2. Is the journey escalated? Then only escalate / transfer / callback / end are allowed.
  3. Is the tool allowed in the journey's current state?
  4. Has the caller reached the verification level the tool needs?

Then the tool runs against the hospital system, and every outcome (including refusals)
is appended to the hash-chained event log with personal data redacted.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import logging
import os
import secrets
import threading
import uuid
import types
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Union, get_args, get_origin, get_type_hints

from sqlalchemy import Engine, insert, select, update

from core.names import name_score
from core.timeutil import real_now_ist

from . import chain, store
from .redact import mask_phone, normalise_phone, phone_hash, redact_payload, redact_text
from .states import APPOINTMENT_STATES, OPEN_STATES, SLA_MINUTES, Level, State, Urgency, can_move
from .twin_client import TwinClient, TwinError, TwinUnavailable

log = logging.getLogger("sahay.journey")

LOCK_MINUTES = 30
MAX_VERIFY_FAILURES = 2
OTP_TTL = timedelta(minutes=5)
MAX_OTP_ATTEMPTS = 3
MAX_OTPS_PER_SESSION = 3
MAX_OFFERED_SLOTS = 5
NAME_MATCH = 0.8
RELATIONS = {"self", "parent", "child", "spouse", "sibling", "other_family", "friend", "other"}
S = State


# ------------------------------------------------------------------ errors & specs


class ToolRefused(Exception):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


def invalid(message: str) -> ToolRefused:
    return ToolRefused("invalid_input", message)


@dataclass(frozen=True)
class ToolSpec:
    description: str
    states: frozenset[State] = OPEN_STATES
    min_level: Level = Level.NONE
    allowed_when_escalated: bool = False


_PRE_PATIENT = frozenset({S.CONTACT, S.INFO_ONLY, S.IDENTIFYING, S.REGISTERING})  # no patient attached yet
_MANAGE = frozenset({S.VERIFIED, S.BOOKED, S.CONFIRMED, S.PREPARED})

# Identity tools work in every open state, DEDUPE_REVIEW included: verifying as an existing
# patient moves the call to that patient's own journey and leaves the review for staff.
TOOL_SPECS: dict[str, ToolSpec] = {
    # identity
    "lookup_caller": ToolSpec("Check whether the calling number belongs to known patients (first names only)."),
    "set_caller_relation": ToolSpec("Record that the caller is calling for someone else (needs their consent). "
                                    "Changing it after verification means verifying again."),
    "verify_identity": ToolSpec("Verify the patient with full name + date of birth (or birth year + PIN code)."),
    "send_otp": ToolSpec("Send a one-time code to the patient's registered phone (for reports and bills, or new registration)."),
    "check_otp": ToolSpec("Check the one-time code the caller reads out."),
    "register_patient": ToolSpec("Register a new patient after reading every detail back and getting a yes.",
                                 _PRE_PATIENT),
    # public information
    "list_departments": ToolSpec("List hospital departments and where they are."),
    "find_doctors": ToolSpec("Find doctors by department, language, gender or name."),
    # booking
    "start_booking": ToolSpec("Start a new booking with the reason for the visit (non-clinical intake).",
                              frozenset({S.VERIFIED, S.FOLLOW_UP}), Level.MEDIUM),
    "get_slots": ToolSpec("Find free appointment slots (max 5). Only these slots can be held.",
                          frozenset({S.INTAKE, S.RESCHEDULED, S.SLOT_SEARCH, S.WAITLISTED}), Level.MEDIUM),
    "hold_slot": ToolSpec("Hold one offered slot for 120 seconds and get the read-back text.",
                          frozenset({S.SLOT_SEARCH}), Level.MEDIUM),
    "release_hold": ToolSpec("Release the held slot (caller wants a different one).", frozenset({S.SLOT_HELD}), Level.MEDIUM),
    "book_appointment": ToolSpec("Book the held slot. Only after the read-back and an explicit yes.",
                                 frozenset({S.SLOT_HELD}), Level.MEDIUM),
    "join_waitlist": ToolSpec("Put the patient on the waitlist when no slot suits.", frozenset({S.SLOT_SEARCH}), Level.MEDIUM),
    "list_appointments": ToolSpec("List the verified patient's upcoming appointments.", OPEN_STATES, Level.MEDIUM),
    "start_reschedule": ToolSpec("Start moving one of the patient's appointments.", _MANAGE, Level.MEDIUM),
    "cancel_appointment": ToolSpec("Cancel an appointment after an explicit yes.", _MANAGE, Level.MEDIUM),
    "get_queue_status": ToolSpec("Token number, now serving, and estimated wait for an appointment.", OPEN_STATES, Level.MEDIUM),
    # strong verification
    "get_report_status": ToolSpec("Report status only (never results).", OPEN_STATES, Level.STRONG),
    "get_bill_status": ToolSpec("Bill amounts and cashless / PM-JAY status.", OPEN_STATES, Level.STRONG),
    # always available
    "create_callback": ToolSpec("Ask staff to call the patient back.", OPEN_STATES, allowed_when_escalated=True),
    "escalate": ToolSpec("Raise urgency and hand the call to a human. Urgency can only go up.", OPEN_STATES,
                         allowed_when_escalated=True),
    "transfer_to_human": ToolSpec("Transfer the caller to staff (caller asked, or the AI can't help).", OPEN_STATES,
                                  allowed_when_escalated=True),
    "end_call": ToolSpec("End this call. The journey stays open for the next call.", frozenset(State),
                         allowed_when_escalated=True),
}


@dataclass
class Ctx:
    session: dict
    journey: dict
    context: dict = field(default_factory=dict)
    context_json: str = "{}"

    @property
    def state(self) -> State:
        return State(self.journey["state"])

    @property
    def level(self) -> Level:
        return Level(self.session["level"])


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def _console_otp_sender(phone: str, code: str, purpose: str) -> None:
    # Until WhatsApp is wired (Stage 9), codes go to the developer console.
    print(f"[DEV OTP] {purpose} code for {mask_phone(phone)}: {code}", flush=True)


# ------------------------------------------------------------------ engine


class JourneyEngine:
    def __init__(self, db_url: str | None = None, twin: TwinClient | None = None,
                 now: Callable[[], datetime] = real_now_ist,
                 otp_sender: Callable[[str, str, str], None] | None = None):
        self.engine: Engine = store.open_store(db_url or os.getenv("JOURNEY_DB_URL", "sqlite:///./journey.db"))
        self.twin = twin or TwinClient()
        self.now = now
        self._otp_sender = otp_sender or _console_otp_sender
        self._salt = os.getenv("PHI_HASH_SALT", "sahay-dev-salt-change-me")
        # In-memory only (never written to the database): raw phones for callbacks,
        # names for redaction, registration drafts awaiting OTP / staff review.
        self._vault: dict[str, str] = {}
        self._names: dict[str, set[str]] = {}
        self._drafts: dict[str, dict] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._dept_names: dict[str, str] | None = None

    # ============================================================== system entry points

    def open_session(self, phone: str | None, channel: str = "voice") -> dict:
        """Called when a call / chat starts. Resumes the caller's open journey if there is one."""
        e164 = normalise_phone(phone) if phone else None
        ph = phone_hash(e164) if e164 else f"anon-{uuid.uuid4().hex[:12]}"
        if e164:
            self._vault[ph] = e164
        now = self.now()
        with self._journey_lock(f"phone:{ph}"):  # two channels at once must still share one journey
            jc, sc = store.journeys.c, store.sessions.c
            with self.engine.connect() as conn:
                # the journey this phone's last call ended on, else its most recently active one
                j = conn.execute(select(store.journeys).join(store.sessions, sc.journey_id == jc.id)
                                 .where(sc.phone_hash == ph, jc.phone_hash == ph, jc.state != S.CLOSED)
                                 .order_by(sc.started_at.desc())).mappings().first()
                if j is None:
                    j = conn.execute(select(store.journeys).where(jc.phone_hash == ph, jc.state != S.CLOSED)
                                     .order_by(jc.updated_at.desc())).mappings().first()
            resumed = j is not None
            if not resumed:
                j = self._create_journey(ph, actor="system", why="new contact")
        refs: list[str] = []
        names: list[str] = []
        twin_down = False
        if e164:
            try:
                for c in self.twin.search_patients(phone=e164):
                    if "phone" in c["matched_on"]:
                        refs.append(c["patient"]["id"])
                        names.append(c["patient"]["name"][0]["given"][0])
            except TwinUnavailable:
                twin_down = True
        sid = _new_id("C")
        with self.engine.begin() as conn:
            conn.execute(insert(store.sessions).values(
                id=sid, journey_id=j["id"], phone_hash=ph, channel=channel, relation="self", proxy_consent=False,
                level=int(Level.SOFT if refs else Level.NONE), patient_ref=None, soft_candidates=json.dumps(refs),
                verify_failures=0, otp_attempts=0, otps_sent=0, started_at=now))
        self._append(j["id"], "session_started", "system", {
            "session_id": sid, "channel": channel, "caller_known": bool(refs), "resumed": resumed,
            "hospital_system_down": twin_down})
        j = self.journey(j["id"])
        return {"session_id": sid, "journey_id": j["id"], "resumed": resumed, "state": j["state"],
                "urgency": Urgency(j["urgency"]).name, "level": (Level.SOFT if refs else Level.NONE).name,
                "caller_known": bool(refs), "greeting_names": names, "hospital_system_down": twin_down,
                "allowed_tools": self.allowed_tools(sid)}

    def allowed_tools(self, session_id: str) -> list[str]:
        ctx = self._load_ctx(session_id)
        if self._needs_reopen(ctx):
            ctx = self._as_reopened(ctx)
        return [name for name in TOOL_SPECS if self._precheck(ctx, name) is None]

    def tool_specs(self) -> dict[str, ToolSpec]:
        return dict(TOOL_SPECS)

    def call(self, session_id: str, tool: str, args: dict | None = None, actor: str = "ai") -> dict:
        """Run one tool for the AI (or the Sentinel). Never raises: every outcome is a result dict."""
        if not isinstance(args, (dict, type(None))):
            return {"ok": False, "tool": tool, "error": {"code": "invalid_input", "message": "args must be an object"}}
        args = dict(args or {})
        if "actor" in args:  # who is acting is decided by the caller of call(), never by the model
            return {"ok": False, "tool": tool, "error": {"code": "invalid_input", "message": "'actor' is not an argument"}}
        try:
            sess = self._session(session_id)
        except KeyError:
            return {"ok": False, "tool": tool, "error": {"code": "unknown_session", "message": "No such session"}}
        with self._journey_lock(sess["journey_id"]):
            ctx = self._load_ctx(session_id)
            if self._needs_reopen(ctx) and tool != "end_call" and self._precheck(self._as_reopened(ctx), tool) is None:
                # The call goes on after its journey closed (e.g. a cancellation): a new request is a new journey.
                self._reopen(ctx)
            refusal = self._precheck(ctx, tool)
            if refusal is None:
                handler = getattr(self, f"_t_{tool}")
                try:
                    args = _checked_args(handler, ctx, args)
                except ToolRefused as e:
                    refusal = e
            if refusal is not None:
                self._append(ctx.journey["id"], "tool_refused", actor, {
                    "tool": tool, "code": refusal.code, "state": ctx.state, "level": ctx.level.name,
                    "args": self._redact(ctx, args)})
                return self._result(ctx, tool, False, None, refusal)
            ok, data, err = True, None, None
            try:
                data = handler(ctx, **args) if tool != "escalate" else handler(ctx, actor=actor, **args)
            except ToolRefused as e:
                ok, err = False, e
            except TwinUnavailable:
                task_id = self._pending_sync_task(ctx, tool)
                ok, err = False, ToolRefused(
                    "system_unavailable",
                    "The hospital system is not responding. Tell the caller honestly, and that staff will call back.",
                    callback_task_id=task_id)
            except TwinError as e:
                code = {"validation_error": "invalid_input"}.get(e.code, e.code)
                ok, err = False, ToolRefused(code, e.message)
            except Exception:  # a bug must never take the call down; the caller gets a human instead
                log.exception("tool %s crashed", tool)
                ok, err = False, ToolRefused(
                    "internal_error", "Something went wrong on our side. Apologise and offer a transfer or callback.")
                ctx = self._load_ctx(ctx.session["id"])  # drop half-made changes to the working context
            self._save_context(ctx)
            self._append(ctx.journey["id"], "tool_called", actor, {
                "tool": tool, "ok": ok, "error": err.code if err else None, "args": self._redact(ctx, args),
                "session_id": ctx.session["id"]})
            ctx.journey = self.journey(ctx.journey["id"])
            return self._result(ctx, tool, ok, data, err)

    # ============================================================== human & system actions

    def set_urgency(self, journey_id: str, urgency: str | Urgency, actor: str, reason: str,
                    staff_id: str | None = None) -> dict:
        """Raise freely; lowering requires a named human."""
        new = urgency if isinstance(urgency, Urgency) else _urgency(urgency)
        with self._journey_lock(journey_id):
            j = self.journey(journey_id)
            old = Urgency(j["urgency"])
            if new < old and (actor != "human" or not staff_id):
                self._append(journey_id, "urgency_lower_refused", actor, {"from": old.name, "to": new.name})
                raise ToolRefused("urgency_ratchet", "Urgency can only be lowered by a named staff member")
            if new != old:
                self._append(journey_id, "urgency_changed", actor, {
                    "from": old.name, "to": new.name, "reason": redact_text(reason), "staff_id": staff_id},
                    {"urgency": int(new)})
            return self.journey(journey_id)

    def human_resolve_escalation(self, journey_id: str, staff_id: str, note: str,
                                 urgency: str | None = None) -> dict:
        if not staff_id:
            raise ToolRefused("staff_required", "staff_id is required")
        self.journey(journey_id)  # KeyError for an unknown journey
        with self._journey_lock(journey_id):
            if urgency:
                self.set_urgency(journey_id, urgency, "human", note, staff_id)
            self._resolve_open_tasks(journey_id, "escalation", staff_id, note)
            self._append(journey_id, "escalation_resolved", "human", {"staff_id": staff_id, "note": redact_text(note)},
                         {"escalated": False})
            return self.journey(journey_id)

    def human_resolve_dedupe(self, task_id: str, staff_id: str, decision: str, patient_ref: str | None = None) -> dict:
        """Staff decide whether a possible duplicate is an existing patient or a new one. Never automatic."""
        if not staff_id:
            raise ToolRefused("staff_required", "staff_id is required")
        task = self.task(task_id)
        if task["kind"] != "dedupe_review" or task["status"] != "open":
            raise ToolRefused("invalid_state", "Task is not an open dedupe review")
        jid = task["journey_id"]
        with self._journey_lock(jid):
            j = self.journey(jid)
            if j["state"] != S.DEDUPE_REVIEW:
                raise ToolRefused("invalid_state", f"Journey is {j['state']}")
            data = json.loads(task["data"])
            if decision == "existing":
                if patient_ref not in data["candidate_refs"]:
                    raise ToolRefused("invalid_input", "patient_ref must be one of the candidates")
                self._update_journey(jid, patient_ref=patient_ref)
                self._move_journey(jid, S.VERIFIED, "human", f"staff {staff_id}: existing patient")
                with self.engine.begin() as conn:
                    conn.execute(update(store.sessions)
                                 .where(store.sessions.c.journey_id == jid, store.sessions.c.ended_at.is_(None))
                                 .values(level=int(Level.MEDIUM), patient_ref=patient_ref))
                self._drafts.pop(jid, None)
            elif decision == "new":
                if jid not in self._drafts:
                    raise ToolRefused("draft_lost", "Registration details are no longer held; register again")
                self._move_journey(jid, S.REGISTERING, "human", f"staff {staff_id}: new patient")
            else:
                raise ToolRefused("invalid_input", "decision must be 'existing' or 'new'")
            self._resolve_task(task_id, staff_id, f"decision={decision}")
            return self.journey(jid)

    def system_advance(self, journey_id: str, to_state: str, reason: str) -> dict:
        """For workers (WhatsApp confirmations, check-in feed, no-show detector)."""
        try:
            dst = State(to_state)
        except ValueError:
            raise invalid(f"unknown state {to_state!r}")
        with self._journey_lock(journey_id):
            self._move_journey(journey_id, dst, "system", reason)
            return self.journey(journey_id)

    def reveal_phone(self, task_id: str, staff_id: str, reason: str) -> str | None:
        """Staff need the number to call back. Every reveal is logged with who and why."""
        if not staff_id or not reason:
            raise ToolRefused("staff_required", "staff_id and a reason are required to reveal a phone number")
        t = self.task(task_id)
        ph = json.loads(t["data"]).get("phone_hash")
        self._append(t["journey_id"], "phone_revealed", "human", {"task_id": task_id, "staff_id": staff_id,
                                                                  "reason": reason})
        return self._vault.get(ph)

    def resolve_task(self, task_id: str, staff_id: str, note: str) -> dict:
        if not staff_id:
            raise ToolRefused("staff_required", "staff_id is required")
        self._resolve_task(task_id, staff_id, note)
        return self.task(task_id)

    def session_snapshot(self, session_id: str) -> dict:
        """Where a live call stands now (same shape as a tool result, minus the tool part).

        The agent reads this at the start of every turn: staff may have changed things from the
        dashboard in between (resolved an escalation, lowered urgency, decided a dedupe review).
        """
        sess = self._session(session_id)
        j = self.journey(sess["journey_id"])
        return {"journey": {"id": j["id"], "state": j["state"], "urgency": Urgency(j["urgency"]).name,
                            "escalated": bool(j["escalated"])},
                "session": {"id": sess["id"], "level": Level(sess["level"]).name, "relation": sess["relation"],
                            "ended": sess["ended_at"] is not None},
                "allowed_tools": self.allowed_tools(session_id) if sess["ended_at"] is None else []}

    SAFETY_KINDS = frozenset({"red_flag", "human_requested", "recording_declined", "injection_attempt",
                              "reply_blocked", "llm_failure", "confirmation_blocked", "internal_text_dropped"})

    def record_safety(self, session_id: str, kind: str, data: dict | None = None, actor: str = "sentinel") -> dict:
        """Log a Safety Sentinel event (L1 hit, blocked reply, injection attempt...) into the audit chain."""
        if kind not in self.SAFETY_KINDS:
            raise invalid(f"kind must be one of {sorted(self.SAFETY_KINDS)}")
        if actor not in ("sentinel", "system", "ai"):
            raise invalid("actor must be sentinel, system or ai")
        sess = self._session(session_id)
        names = self._names.get(session_id, set())
        with self._journey_lock(sess["journey_id"]):
            j = self._append(sess["journey_id"], "safety_event", actor,
                             {"kind": kind, "session_id": session_id, **(data or {})}, names=names)
        return {"recorded": "safety_event", "journey_id": j["id"], "seq": j["last_seq"]}

    # ============================================================== reads

    def journey(self, journey_id: str) -> dict:
        with self.engine.connect() as conn:
            row = conn.execute(select(store.journeys).where(store.journeys.c.id == journey_id)).mappings().first()
        if row is None:
            raise KeyError(journey_id)
        return dict(row)

    def events(self, journey_id: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(store.journey_events).where(store.journey_events.c.journey_id == journey_id)
                                .order_by(store.journey_events.c.seq)).mappings().all()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    def verify_chain(self, journey_id: str) -> chain.ChainReport:
        with self.engine.connect() as conn:
            rows = conn.execute(select(store.journey_events).where(store.journey_events.c.journey_id == journey_id)
                                .order_by(store.journey_events.c.id)).mappings().all()
        return chain.verify(rows)

    def task(self, task_id: str) -> dict:
        with self.engine.connect() as conn:
            row = conn.execute(select(store.tasks).where(store.tasks.c.id == task_id)).mappings().first()
        if row is None:
            raise KeyError(task_id)
        return dict(row)

    def tasks(self, journey_id: str | None = None, status: str | None = "open") -> list[dict]:
        q = select(store.tasks).order_by(store.tasks.c.sla_due)
        if journey_id:
            q = q.where(store.tasks.c.journey_id == journey_id)
        if status:
            q = q.where(store.tasks.c.status == status)
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    def session(self, session_id: str) -> dict:
        return self._session(session_id)

    # ============================================================== tools: identity

    def _t_lookup_caller(self, ctx: Ctx) -> dict:
        refs = json.loads(ctx.session["soft_candidates"])
        names = []
        for ref in refs:
            p = self.twin.patient(ref)
            names.append(p["name"][0]["given"][0])
        if ctx.state in (S.CONTACT, S.INFO_ONLY):
            self._move(ctx, S.IDENTIFYING, "ai", "caller needs patient-specific help")
        return {"caller_known": bool(refs), "candidates": [{"patient_ref": r, "first_name": n} for r, n in zip(refs, names)],
                "note": "First names only. Ask for full name and date of birth before anything personal."}

    def _t_set_caller_relation(self, ctx: Ctx, relation: str, consent: bool = False) -> dict:
        relation = relation.strip().lower()
        if relation not in RELATIONS:
            raise invalid(f"relation must be one of {sorted(RELATIONS)}")
        if relation != "self" and consent is not True:
            raise ToolRefused("consent_required", "Ask the caller to confirm the patient has agreed to them calling.")
        reset = relation != ctx.session["relation"] and ctx.level >= Level.MEDIUM
        if reset:
            # Verification was for a caller in another role (e.g. a proxy now saying "it's me"): start again.
            base = Level.SOFT if json.loads(ctx.session["soft_candidates"]) else Level.NONE
            self._update_session(ctx, level=int(base), patient_ref=None, otp_hash=None, otp_expires_at=None,
                                 otp_phone_hash=None)
            self._append(ctx.journey["id"], "verification_reset", "system",
                         {"why": "caller relation changed after verification", "level": base.name})
        self._update_session(ctx, relation=relation, proxy_consent=consent is True)
        self._append(ctx.journey["id"], "caller_relation", "ai", {"relation": relation, "consent": consent is True})
        note = "Verify the PATIENT's name and date of birth next." if relation != "self" else None
        if reset:
            note = "Verification was reset because the caller's role changed. Verify again before anything personal."
        return {"relation": relation, "proxy": relation != "self", "verification_reset": reset, "note": note}

    def _t_verify_identity(self, ctx: Ctx, name: str, birth_date: str | None = None, birth_year: int | None = None,
                           postal_code: str | None = None) -> dict:
        lock = self._lock_until(f"verify:{ctx.session['phone_hash']}")
        if lock:
            raise ToolRefused("locked", "Too many failed attempts. Offer to transfer the caller to staff.",
                              locked_minutes=max(1, int((lock - self.now()).total_seconds() // 60)))
        dob: date | None = None
        if birth_date:
            try:
                dob = date.fromisoformat(birth_date)
            except ValueError:
                raise invalid("birth_date must be YYYY-MM-DD")
        elif not (birth_year and postal_code):
            raise invalid("Need date of birth, or birth year plus PIN code")
        if not name or len(name.strip()) < 2:
            raise invalid("Need the patient's full name")

        # Search results already carry the full record; only the caller-ID matches need fetching.
        people: dict[str, dict] = {c["patient"]["id"]: c["patient"] for c in self.twin.search_patients(
            name=name, birth_date=dob.isoformat() if dob else None)}
        for ref in json.loads(ctx.session["soft_candidates"]):
            if ref not in people:
                people[ref] = self.twin.patient(ref)
        matches = []
        for p in people.values():
            p_dob = date.fromisoformat(p["birthDate"])
            given, family = p["name"][0]["given"][0], p["name"][0]["family"]
            dob_ok = (p_dob == dob) if dob else (
                p_dob.year == int(birth_year) and (p["address"][0].get("postalCode") or "") == str(postal_code).strip())
            if dob_ok and name_score(name, given, family) >= NAME_MATCH:
                matches.append((p, given, family))
        soft = set(json.loads(ctx.session["soft_candidates"]))
        if len(matches) > 1:
            preferred = [m for m in matches if m[0]["id"] in soft]
            matches = preferred if len(preferred) == 1 else matches
        if len(matches) > 1:
            self._append(ctx.journey["id"], "verification_ambiguous", "ai", {"candidates": len(matches)})
            raise ToolRefused("ambiguous_identity", "More than one record matches. Transfer to staff to confirm identity.")
        if not matches:
            failures = ctx.session["verify_failures"] + 1
            self._update_session(ctx, verify_failures=failures)
            self._append(ctx.journey["id"], "verification_failed", "ai", {"attempt": failures})
            if failures >= MAX_VERIFY_FAILURES:
                until = self.now() + timedelta(minutes=LOCK_MINUTES)
                self._set_lock(f"verify:{ctx.session['phone_hash']}", until, "verification failed twice")
                self._append(ctx.journey["id"], "verification_locked", "system", {"minutes": LOCK_MINUTES})
                raise ToolRefused("locked", "Details did not match twice. Verification is locked; offer staff transfer.",
                                  locked_minutes=LOCK_MINUTES)
            raise ToolRefused("verification_failed", "Those details don't match our records. Ask once more, slowly.",
                              attempts_left=MAX_VERIFY_FAILURES - failures)

        p, given, family = matches[0]
        self._names.setdefault(ctx.session["id"], set()).update({given, family})
        ref = p["id"]
        self._attach_journey(ctx, ref)
        if ctx.state in (S.CONTACT, S.INFO_ONLY):
            self._move(ctx, S.IDENTIFYING, "ai", "verifying")
        if ctx.state in (S.IDENTIFYING, S.REGISTERING):
            self._move(ctx, S.VERIFIED, "ai", "name and date of birth matched")
        prev = ctx.session["patient_ref"]
        switched = prev is not None and prev != ref
        # Verification belongs to one patient: a code proven for someone else does not carry over.
        new_level = Level.MEDIUM if switched else max(ctx.level, Level.MEDIUM)
        extra = {"otp_hash": None, "otp_expires_at": None, "otp_phone_hash": None} if switched else {}
        self._update_session(ctx, level=int(new_level), patient_ref=ref, verify_failures=0, **extra)
        self._append(ctx.journey["id"], "verification_changed", "ai", {
            "level": new_level.name, "patient_ref": ref, "relation": ctx.session["relation"]})
        upcoming = [a for a in self.twin.appointments(ref) if a["status"] == "booked"]
        return {"verified": True, "patient_ref": ref, "first_name": given, "level": new_level.name,
                "calling_for": "self" if ctx.session["relation"] == "self" else f"patient (caller is {ctx.session['relation']})",
                "upcoming_appointments": len(upcoming), "journey_id": ctx.journey["id"], "state": ctx.journey["state"]}

    def _t_send_otp(self, ctx: Ctx) -> dict:
        if ctx.session["otps_sent"] >= MAX_OTPS_PER_SESSION:
            raise ToolRefused("otp_limit", "Too many codes sent on this call. Offer a callback.")
        draft = self._drafts.get(ctx.journey["id"]) if ctx.state == S.REGISTERING else None
        if draft:
            target, purpose = draft["phone"], "registration"
        elif ctx.level >= Level.MEDIUM and ctx.session["patient_ref"]:
            p = self.twin.patient(ctx.session["patient_ref"])
            target, purpose = p["telecom"][0]["value"], "strong_verification"  # registered number only
        else:
            raise ToolRefused("verification_required", "Verify name and date of birth before sending a code.",
                              needed_level=Level.MEDIUM.name)
        code = f"{secrets.randbelow(10 ** 6):06d}"
        self._update_session(ctx, otp_hash=self._otp_hash(ctx.session["id"], code), otp_phone_hash=phone_hash(target),
                             otp_expires_at=self.now() + OTP_TTL, otp_attempts=0, otps_sent=ctx.session["otps_sent"] + 1)
        self._otp_sender(target, code, purpose)
        self._append(ctx.journey["id"], "otp_sent", "system", {"purpose": purpose, "to": mask_phone(target)})
        return {"sent_to": mask_phone(target), "expires_in_seconds": int(OTP_TTL.total_seconds()), "purpose": purpose}

    def _t_check_otp(self, ctx: Ctx, code: str) -> dict:
        s = ctx.session
        if not s["otp_hash"]:
            raise ToolRefused("no_otp_pending", "No code has been sent on this call.")
        if s["otp_attempts"] >= MAX_OTP_ATTEMPTS:
            raise ToolRefused("otp_locked", "Too many wrong codes. Offer a callback or staff transfer.")
        if self.now() > s["otp_expires_at"]:
            raise ToolRefused("otp_expired", "The code expired. Offer to send a new one.")
        if ctx.state == S.REGISTERING and ctx.journey["id"] not in self._drafts:
            # The details awaiting this code lived only in memory and the server restarted. Don't burn an attempt.
            raise ToolRefused("registration_lost", "The registration details were lost (system restart). Read them back "
                              "again and call register_patient again; a new code will be sent.")
        attempts = s["otp_attempts"] + 1
        self._update_session(ctx, otp_attempts=attempts)
        given = "".join(ch for ch in str(code) if ch.isdigit())
        if not hmac.compare_digest(self._otp_hash(s["id"], given), s["otp_hash"]):
            self._append(ctx.journey["id"], "otp_failed", "ai", {"attempt": attempts})
            raise ToolRefused("otp_incorrect", "That code is not right.", attempts_left=MAX_OTP_ATTEMPTS - attempts)
        self._update_session(ctx, otp_hash=None, otp_expires_at=None)
        draft = self._drafts.get(ctx.journey["id"]) if ctx.state == S.REGISTERING else None
        if draft:
            p = self.twin.register(**{k: v for k, v in draft.items() if v is not None})
            ref = p["id"]
            self._drafts.pop(ctx.journey["id"], None)
            self._update_journey(ctx.journey["id"], patient_ref=ref)
            ctx.journey["patient_ref"] = ref
            self._move(ctx, S.VERIFIED, "ai", "new patient registered, phone confirmed by code")
            self._update_session(ctx, level=int(Level.STRONG), patient_ref=ref)
            self._append(ctx.journey["id"], "patient_registered", "system", {"patient_ref": ref})
            return {"verified": True, "level": Level.STRONG.name, "patient_ref": ref, "registered": True}
        # STRONG only if the code went to this patient's registered number (not a number the caller gave)
        ref = ctx.session["patient_ref"]
        registered = self.twin.patient(ref)["telecom"][0]["value"] if ref and ctx.level >= Level.MEDIUM else None
        if not registered or phone_hash(registered) != s["otp_phone_hash"]:
            raise ToolRefused("verification_required", "That code was not sent to this patient's registered number. "
                              "Verify name and date of birth, then send a new code.", needed_level=Level.MEDIUM.name)
        self._update_session(ctx, level=int(Level.STRONG))
        self._append(ctx.journey["id"], "verification_changed", "ai", {"level": Level.STRONG.name})
        return {"verified": True, "level": Level.STRONG.name}

    def _t_register_patient(self, ctx: Ctx, given: str, family: str, gender: str, birth_date: str, phone: str,
                            postal_code: str | None = None, language: str | None = None,
                            caller_confirmed: bool = False) -> dict:
        if not caller_confirmed:
            raise ToolRefused("confirmation_required",
                              "Read every detail back (spell the name) and register only after the caller says yes.")
        e164 = normalise_phone(phone)
        if not e164:
            raise invalid("phone must be a 10-digit Indian mobile number")
        try:
            dob = date.fromisoformat(birth_date)
        except ValueError:
            raise invalid("birth_date must be YYYY-MM-DD")
        if dob > self.now().date():
            raise invalid("birth_date is in the future")
        if gender not in ("male", "female", "other"):
            raise invalid("gender must be male, female or other")
        if postal_code and not (postal_code.isdigit() and len(postal_code) == 6):
            raise invalid("postal_code must be 6 digits")
        self._names.setdefault(ctx.session["id"], set()).update({given, family})
        if ctx.state in (S.CONTACT, S.INFO_ONLY):
            self._move(ctx, S.IDENTIFYING, "ai", "new caller")
        if ctx.state == S.IDENTIFYING:
            self._move(ctx, S.REGISTERING, "ai", "new patient registration")

        full = f"{given} {family}"
        exact, possible = [], []
        for c in self.twin.search_patients(phone=e164, name=full, birth_date=dob.isoformat()):
            p = c["patient"]
            same_dob = p["birthDate"] == dob.isoformat()
            same_phone = p["telecom"][0]["value"] == e164
            ns = name_score(full, p["name"][0]["given"][0], p["name"][0]["family"])
            if same_dob and ns >= 0.9 and same_phone:
                exact.append(p["id"])
            elif (same_dob and ns >= 0.75) or (same_phone and ns >= 0.9):  # spelling variant, or a misheard DOB
                possible.append((p["id"], round(ns, 2)))
        draft = {"given": given.strip(), "family": family.strip(), "gender": gender, "birth_date": dob.isoformat(),
                 "phone": e164, "postal_code": postal_code, "language": language}
        if exact:
            self._move(ctx, S.IDENTIFYING, "ai", "caller is already registered")
            return {"status": "already_registered",
                    "next": "This caller is already registered. Use verify_identity with their name and date of birth."}
        if possible:
            self._drafts[ctx.journey["id"]] = draft
            self._move(ctx, S.DEDUPE_REVIEW, "ai", "possible duplicate record")
            task_id = self._create_task(ctx, "dedupe_review", Urgency.ROUTINE,
                                        "Possible duplicate patient record: staff must decide (never auto-merge)",
                                        {"candidate_refs": [r for r, _ in possible],
                                         "similarity": {r: s for r, s in possible}})
            return {"status": "needs_staff_review", "task_id": task_id,
                    "say": "We may already have a record for you. Our front desk will confirm and call you back shortly."}
        self._drafts[ctx.journey["id"]] = draft
        sent = self._t_send_otp(ctx)
        return {"status": "otp_sent", **sent,
                "next": "Ask the caller to read out the 6-digit code, then call check_otp."}

    # ============================================================== tools: information

    def _t_list_departments(self, ctx: Ctx) -> dict:
        return {"departments": [{"id": d["id"], "name": d["name"], "location": d["location"]}
                                for d in self.twin.departments()]}

    def _t_find_doctors(self, ctx: Ctx, department_id: str | None = None, language: str | None = None,
                        gender: str | None = None, name: str | None = None) -> dict:
        docs = self.twin.practitioners(department_id=department_id, language=language, gender=gender, name=name)
        return {"doctors": [{"id": d["id"], "name": d["name"][0]["text"], "department": d["department"]["name"],
                             "languages": d["communication"], "gender": d["gender"],
                             "opd": d["opd"], "fee_inr": d["fee"]["value"]} for d in docs]}

    # ============================================================== tools: booking

    def _t_start_booking(self, ctx: Ctx, reason: str, department_id: str | None = None,
                         practitioner_id: str | None = None) -> dict:
        if practitioner_id:
            try:
                doc = self.twin.practitioner(practitioner_id)
            except TwinError as e:
                if e.code == "not_found":
                    raise ToolRefused("not_found", "No such doctor. Use find_doctors.")
                raise
            department_id = doc["department"]["id"]
        elif department_id and department_id.upper() not in self._departments():
            raise ToolRefused("not_found", "No such department. Use list_departments.")
        ctx.context.update(mode="new", appointment_id=None, hold=None, offered={},
                           intake={"reason": redact_text(reason, self._known_names(ctx)),
                                   "department_id": department_id.upper() if department_id else None,
                                   "practitioner_id": practitioner_id})
        self._move(ctx, S.INTAKE, "ai", "booking requested")
        return {"intake_recorded": True, "department_id": ctx.context["intake"]["department_id"],
                "practitioner_id": practitioner_id,
                "next": "Ask preferred day / time of day, then call get_slots."}

    def _t_get_slots(self, ctx: Ctx, department_id: str | None = None, practitioner_id: str | None = None,
                     date_from: str | None = None, date_to: str | None = None, part_of_day: str | None = None,
                     language: str | None = None, gender: str | None = None) -> dict:
        intake = ctx.context.get("intake") or {}
        dep = department_id or intake.get("department_id")
        pr = practitioner_id or intake.get("practitioner_id")
        if not (dep or pr):
            raise invalid("Ask which department or doctor the caller needs.")
        slots = self.twin.slots(department_id=dep, practitioner_id=pr, date_from=date_from, date_to=date_to,
                                part_of_day=part_of_day, language=language, gender=gender, limit=MAX_OFFERED_SLOTS)
        depts = self._departments()
        offered = {s["id"]: {"start": s["start"], "practitioner_id": s["schedule"]["practitioner_id"],
                             "practitioner_name": s["schedule"]["practitioner_name"],
                             "department": depts.get(s["schedule"]["department_id"], s["schedule"]["department_id"])}
                   for s in slots}
        ctx.context["offered"] = offered
        if ctx.state != S.SLOT_SEARCH:
            self._move(ctx, S.SLOT_SEARCH, "ai", "searching slots")
        out = [{"slot_id": k, "when": _say_when(v["start"]), "start": v["start"], "doctor": v["practitioner_name"],
                "department": v["department"]} for k, v in offered.items()]
        return {"slots": out, "note": None if out else "No free slots in that window. Offer other dates or the waitlist."}

    def _t_hold_slot(self, ctx: Ctx, slot_id: str) -> dict:
        offered = ctx.context.get("offered") or {}
        if slot_id not in offered:
            raise ToolRefused("not_offered", "Only slots returned by get_slots on this call can be held.")
        n = int(ctx.context.get("hold_seq", 0)) + 1
        try:
            h = self.twin.hold(slot_id, f"{ctx.journey['id']}:{slot_id}:{n}")
        except TwinError as e:
            if e.code == "slot_unavailable":
                offered.pop(slot_id, None)
                raise ToolRefused("slot_unavailable", "That slot was just taken. Offer the other options.")
            raise
        ctx.context.update(hold={"id": h["id"], "slot_id": slot_id, "expires_at": h["expires_at"]}, hold_seq=n)
        self._move(ctx, S.SLOT_HELD, "ai", "slot held")
        o = offered[slot_id]
        return {"hold_id": h["id"], "expires_in_seconds": h["seconds_left"],
                "read_back": f"{o['practitioner_name']}, {o['department']}, {_say_when(o['start'])}",
                "next": "Read this back exactly and book only after a clear yes."}

    def _t_release_hold(self, ctx: Ctx) -> dict:
        hold = ctx.context.get("hold")
        if hold:
            try:
                self.twin.release(hold["id"])
            except TwinError:
                pass  # already expired / gone: nothing to release
        ctx.context["hold"] = None
        self._move(ctx, S.SLOT_SEARCH, "ai", "caller wants another slot")
        return {"released": True}

    def _t_book_appointment(self, ctx: Ctx, patient_confirmed: bool = False) -> dict:
        if patient_confirmed is not True:
            raise ToolRefused("confirmation_required", "Read back doctor, department, day and time; book only after a clear yes.")
        hold = ctx.context.get("hold")
        if not hold:
            raise ToolRefused("no_hold", "No slot is held.")
        patient_ref = ctx.session["patient_ref"]
        if not patient_ref or (ctx.journey["patient_ref"] and patient_ref != ctx.journey["patient_ref"]):
            raise ToolRefused("verification_required", "Verify the patient first.", needed_level=Level.MEDIUM.name)
        mode = ctx.context.get("mode", "new")
        try:
            if mode == "reschedule":
                appt = self.twin.reschedule(ctx.context["appointment_id"], hold["id"])
            else:
                appt = self.twin.book(hold["id"], patient_ref, (ctx.context.get("intake") or {}).get("reason"))
        except TwinError as e:
            if e.code in ("hold_expired", "hold_used", "slot_taken"):
                (ctx.context.get("offered") or {}).pop(hold["slot_id"], None)
                ctx.context["hold"] = None
                self._move(ctx, S.SLOT_SEARCH, "ai", e.code)
                msg = {"hold_expired": "The hold expired before booking. Offer to search again.",
                       "slot_taken": "Someone else just took that slot. Apologise and offer the next options."}
                raise ToolRefused(e.code, msg.get(e.code, "That slot can't be booked now. Offer other options."))
            raise
        offered = ctx.context.get("offered") or {}
        o = offered.get(hold["slot_id"], {})
        ctx.context.update(appointment_id=appt["id"], hold=None, offered={})
        self._move(ctx, S.BOOKED, "ai", "rescheduled" if mode == "reschedule" else "booked")
        self._append(ctx.journey["id"], "appointment_booked", "ai", {
            "appointment_id": appt["id"], "start": appt["start"], "mode": mode,
            "practitioner_id": appt["participant"][1]["actor"]["id"]})
        return {"appointment_id": appt["id"], "mode": mode,
                "confirmed": f"{o.get('practitioner_name', '')}, {o.get('department', '')}, {_say_when(appt['start'])}"}

    def _t_join_waitlist(self, ctx: Ctx, date_from: str, date_to: str) -> dict:
        intake = ctx.context.get("intake") or {}
        entry = self.twin.join_waitlist(patient_id=ctx.session["patient_ref"], practitioner_id=intake.get("practitioner_id"),
                                        department_id=intake.get("department_id"), date_from=date_from, date_to=date_to)
        ctx.context["waitlist_id"] = entry["id"]
        self._move(ctx, S.WAITLISTED, "ai", "no suitable slot")
        return {"waitlist_id": entry["id"], "window": [date_from, date_to],
                "say": "You're on the waitlist; we'll message you when a slot opens."}

    def _t_list_appointments(self, ctx: Ctx) -> dict:
        appts = [a for a in self.twin.appointments(ctx.session["patient_ref"]) if a["status"] == "booked"]
        return {"appointments": [self._describe_appt(a) for a in appts]}

    def _t_start_reschedule(self, ctx: Ctx, appointment_id: str) -> dict:
        a = self._own_appointment(ctx, appointment_id)
        if a["status"] != "booked":
            raise ToolRefused("invalid_state", f"That appointment is {a['status']}.")
        doc_id = a["participant"][1]["actor"]["id"]
        doc = self.twin.practitioner(doc_id)
        ctx.context.update(mode="reschedule", appointment_id=appointment_id, hold=None, offered={},
                           intake={"reason": "reschedule", "department_id": doc["department"]["id"],
                                   "practitioner_id": doc_id})
        self._move(ctx, S.RESCHEDULED, "ai", "caller wants to move an appointment")
        return {"current": self._describe_appt(a), "next": "Ask for the new preferred day/time, then get_slots."}

    def _t_cancel_appointment(self, ctx: Ctx, appointment_id: str, reason: str, patient_confirmed: bool = False) -> dict:
        if patient_confirmed is not True:
            raise ToolRefused("confirmation_required", "Read back which appointment; cancel only after a clear yes.")
        a = self._own_appointment(ctx, appointment_id)
        self.twin.cancel(appointment_id, redact_text(reason, self._known_names(ctx)) or "cancelled by caller")
        self._append(ctx.journey["id"], "appointment_cancelled", "ai", {"appointment_id": appointment_id,
                                                                          "reason": reason},
                     names=self._known_names(ctx))
        if ctx.context.get("appointment_id") == appointment_id and ctx.state in APPOINTMENT_STATES:
            self._move(ctx, S.CLOSED, "ai", "appointment cancelled")
        return {"cancelled": True, "appointment": self._describe_appt(a)}

    def _t_get_queue_status(self, ctx: Ctx, appointment_id: str) -> dict:
        a = self._own_appointment(ctx, appointment_id)
        if a["status"] not in ("booked", "arrived"):
            raise ToolRefused("invalid_state", f"That appointment is {a['status']}; there is no queue for it.")
        q = self.twin.queue(appointment_id)
        return {k: q.get(k) for k in ("date", "token", "now_serving", "ahead", "est_wait_minutes",
                                      "doctor_delay_minutes", "note")}

    def _t_get_report_status(self, ctx: Ctx) -> dict:
        self._no_proxy(ctx, "report")
        return {"reports": [{"test": r["code"]["text"], "status": r["status"], "ready": r["status"] == "final"}
                            for r in self.twin.reports(ctx.session["patient_ref"])],
                "note": "Status only. Never read or interpret results."}

    def _t_get_bill_status(self, ctx: Ctx) -> dict:
        self._no_proxy(ctx, "bill")
        return {"bills": [{"description": i["description"], "amount_inr": i["totalGross"]["value"], "status": i["status"],
                           "payer": i["payer"], "insurance_status": i["insuranceStatus"]}
                          for i in self.twin.invoices(ctx.session["patient_ref"])]}

    # ============================================================== tools: always available

    def _t_create_callback(self, ctx: Ctx, reason: str, urgency: str = "ROUTINE") -> dict:
        u = _urgency(urgency)
        task_id = self._create_task(ctx, "callback", u, reason, {})
        return {"task_id": task_id, "within_minutes": SLA_MINUTES[u]}

    def _t_escalate(self, ctx: Ctx, urgency: str, reason: str, verbatim: str | None = None, actor: str = "ai") -> dict:
        u = _urgency(urgency)
        old = Urgency(ctx.journey["urgency"])
        new = max(old, u)  # the ratchet: never lower
        names = self._known_names(ctx)
        card = {
            "urgency": new.name, "reason": redact_text(reason, names),
            "verbatim_trigger": redact_text(verbatim, names) if verbatim else None,
            "patient_ref": ctx.session["patient_ref"] or ctx.journey["patient_ref"],
            "verified": ctx.level.name, "caller_relation": ctx.session["relation"],
            "journey_state": ctx.journey["state"], "appointment_id": ctx.context.get("appointment_id"),
            "raised_by": actor,
        }
        existing = [t for t in self.tasks(ctx.journey["id"]) if t["kind"] == "escalation"]
        if existing:
            task_id = existing[0]["id"]
            with self.engine.begin() as conn:
                conn.execute(update(store.tasks).where(store.tasks.c.id == task_id).values(
                    urgency=int(max(Urgency(existing[0]["urgency"]), new)), data=json.dumps(card),
                    sla_due=min(existing[0]["sla_due"], self.now() + timedelta(minutes=SLA_MINUTES[new]))))
        else:
            task_id = self._create_task(ctx, "escalation", new, reason, card)
        self._append(ctx.journey["id"], "escalated", actor, {"urgency": new.name, "requested": u.name,
                                                             "reason": reason, "task_id": task_id},
                     {"escalated": True, "urgency": int(new)}, names=names)
        ctx.journey = self.journey(ctx.journey["id"])
        return {"handoff_task_id": task_id, "urgency": new.name, "staff_within_minutes": SLA_MINUTES[new],
                "note": "Automated booking is now frozen until a human resolves this."}

    def _t_transfer_to_human(self, ctx: Ctx, reason: str, desk: str = "front_desk") -> dict:
        u = Urgency(ctx.journey["urgency"])
        task_id = self._create_task(ctx, "transfer", u, reason, {"desk": desk})
        return {"status": "transfer_requested", "task_id": task_id, "desk": desk}

    def _t_end_call(self, ctx: Ctx, summary: str | None = None) -> dict:
        self._update_session(ctx, ended_at=self.now())
        self._append(ctx.journey["id"], "session_ended", "ai", {"session_id": ctx.session["id"], "summary": summary or ""},
                     names=self._known_names(ctx))
        return {"ended": True, "journey_state": ctx.journey["state"]}

    # ============================================================== internals

    def _precheck(self, ctx: Ctx, tool: str) -> ToolRefused | None:
        spec = TOOL_SPECS.get(tool)
        if spec is None:
            return ToolRefused("unknown_tool", f"No tool called {tool}")
        if ctx.session["ended_at"] is not None:
            return ToolRefused("session_ended", "This call has ended.")
        if ctx.journey["escalated"] and not spec.allowed_when_escalated:
            return ToolRefused("escalated_frozen",
                               "A human is handling this journey. Only escalate, transfer_to_human, create_callback "
                               "and end_call are allowed.")
        if ctx.state not in spec.states:
            return ToolRefused("not_allowed_in_state", f"{tool} is not allowed while the journey is {ctx.state}.",
                               state=ctx.state.value)
        if ctx.level < spec.min_level:
            return ToolRefused("verification_required",
                               f"{tool} needs {spec.min_level.name} verification; caller is {ctx.level.name}.",
                               needed_level=spec.min_level.name)
        return None

    def _result(self, ctx: Ctx, tool: str, ok: bool, data: Any, err: ToolRefused | None) -> dict:
        sess = self._session(ctx.session["id"])
        j = self.journey(sess["journey_id"])
        return {
            "ok": ok, "tool": tool, "data": data, "error": err.as_dict() if err else None,
            "journey": {"id": j["id"], "state": j["state"], "urgency": Urgency(j["urgency"]).name,
                        "escalated": bool(j["escalated"])},
            "session": {"id": sess["id"], "level": Level(sess["level"]).name, "relation": sess["relation"],
                        "ended": sess["ended_at"] is not None},
            "allowed_tools": self.allowed_tools(sess["id"]) if sess["ended_at"] is None else [],
        }

    def _load_ctx(self, session_id: str) -> Ctx:
        sess = self._session(session_id)
        j = self.journey(sess["journey_id"])
        return Ctx(sess, j, json.loads(j["context"] or "{}"), j["context"] or "{}")

    def _session(self, session_id: str) -> dict:
        with self.engine.connect() as conn:
            row = conn.execute(select(store.sessions).where(store.sessions.c.id == session_id)).mappings().first()
        if row is None:
            raise KeyError(session_id)
        return dict(row)

    def _journey_lock(self, journey_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(journey_id, threading.RLock())

    def _create_journey(self, phone_hash_: str, actor: str, why: str, patient_ref: str | None = None) -> dict:
        jid, now = _new_id("J"), self.now()
        with self.engine.begin() as conn:
            conn.execute(insert(store.journeys).values(
                id=jid, phone_hash=phone_hash_, patient_ref=patient_ref, state=S.CONTACT.value, urgency=0,
                escalated=False, context="{}", last_seq=0, last_hash=chain.GENESIS, opened_at=now, updated_at=now))
        return self._append(jid, "journey_opened", actor, {"why": why, "patient_ref": patient_ref})

    def _append(self, journey_id: str, type_: str, actor: str, payload: dict, updates: dict | None = None,
                names: Any = ()) -> dict:
        """Append one event to the journey's chain and update the journey row, in one transaction."""
        clean = json.loads(chain.canonical(redact_payload(payload, names)))
        with self.engine.begin() as conn:
            j = conn.execute(select(store.journeys).where(store.journeys.c.id == journey_id).with_for_update()).mappings().one()
            seq, now = j["last_seq"] + 1, self.now()
            ts = now.isoformat()
            body = chain.event_body(journey_id, seq, ts, type_, actor, clean)
            h = chain.compute_hash(j["last_hash"], body)
            conn.execute(insert(store.journey_events).values(
                journey_id=journey_id, seq=seq, ts=ts, type=type_, actor=actor, payload=chain.canonical(clean),
                prev_hash=j["last_hash"], hash=h))
            values = {"last_seq": seq, "last_hash": h, "updated_at": now, **(updates or {})}
            conn.execute(update(store.journeys).where(store.journeys.c.id == journey_id).values(**values))
            row = conn.execute(select(store.journeys).where(store.journeys.c.id == journey_id)).mappings().one()
        return dict(row)

    def _move(self, ctx: Ctx, dst: State, actor: str, why: str) -> None:
        ctx.journey = self._move_journey(ctx.journey["id"], dst, actor, why)

    def _move_journey(self, journey_id: str, dst: State, actor: str, why: str) -> dict:
        j = self.journey(journey_id)
        src = State(j["state"])
        if src == dst:
            return j
        if not can_move(src, dst):
            raise ToolRefused("illegal_transition", f"Cannot move from {src} to {dst}.")
        updates: dict[str, Any] = {"state": dst.value}
        if dst == S.CLOSED:
            updates["closed_at"] = self.now()
        return self._append(journey_id, "state_changed", actor, {"from": src.value, "to": dst.value, "why": why}, updates)

    def _attach_journey(self, ctx: Ctx, ref: str) -> None:
        """Point the call at the verified patient's journey.

        Keep the current journey if it is theirs, or adopt it if nobody is attached yet. Otherwise
        (family phone, a pending staff review, a different phone) resume that patient's open journey,
        or start one.
        """
        j = ctx.journey
        if j["patient_ref"] == ref:
            return
        unclaimed = j["patient_ref"] is None and ctx.state in _PRE_PATIENT
        if unclaimed:
            self._drafts.pop(j["id"], None)  # an existing patient, so no registration
        jc = store.journeys.c
        with self.engine.connect() as conn:
            other = conn.execute(select(store.journeys).where(jc.patient_ref == ref, jc.state != S.CLOSED)
                                 .order_by((jc.phone_hash == ctx.session["phone_hash"]).desc(), jc.updated_at.desc())
                                 ).mappings().first()
        if other is None and unclaimed:
            self._update_journey(j["id"], patient_ref=ref)
            ctx.journey["patient_ref"] = ref
            return
        if other is None:
            why = "different patient on the same phone" if j["patient_ref"] else "verified while another request waits"
            other = self._create_journey(ctx.session["phone_hash"], "ai", why, ref)
        elif unclaimed:  # this call's empty journey folds into the patient's open one
            self._move(ctx, S.CLOSED, "system", "caller is a patient with an open journey; continuing that one")
        self._append(j["id"], "session_moved", "system", {"session_id": ctx.session["id"], "to_journey": other["id"]})
        self._update_session(ctx, journey_id=other["id"])
        ctx.journey, ctx.context_json = dict(other), other["context"] or "{}"
        ctx.context = json.loads(ctx.context_json)

    def _needs_reopen(self, ctx: Ctx) -> bool:
        return ctx.state == S.CLOSED and ctx.session["ended_at"] is None

    def _reopen_target(self, ctx: Ctx) -> tuple[str | None, State]:
        ref = ctx.session["patient_ref"] if ctx.level >= Level.MEDIUM else None
        return ref, (S.VERIFIED if ref else S.CONTACT)

    def _as_reopened(self, ctx: Ctx) -> Ctx:
        """What the call would look like on a fresh journey (to list tools without creating one)."""
        ref, state = self._reopen_target(ctx)
        j = dict(ctx.journey) | {"state": state.value, "escalated": False, "urgency": 0, "patient_ref": ref}
        return Ctx(ctx.session, j, {}, "{}")

    def _reopen(self, ctx: Ctx) -> None:
        ref, state = self._reopen_target(ctx)
        old = ctx.journey["id"]
        new = self._create_journey(ctx.session["phone_hash"], "system", "new request after the last journey closed", ref)
        self._append(old, "session_moved", "system", {"session_id": ctx.session["id"], "to_journey": new["id"]})
        self._update_session(ctx, journey_id=new["id"])
        ctx.journey, ctx.context, ctx.context_json = new, {}, "{}"
        if state == S.VERIFIED:
            self._move(ctx, S.IDENTIFYING, "system", "caller already verified on this call")
            self._move(ctx, S.VERIFIED, "system", "caller already verified on this call")

    def _update_journey(self, journey_id: str, **values) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(store.journeys).where(store.journeys.c.id == journey_id).values(**values))

    def _update_session(self, ctx: Ctx, **values) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(store.sessions).where(store.sessions.c.id == ctx.session["id"]).values(**values))
        ctx.session.update(values)

    def _save_context(self, ctx: Ctx) -> None:
        new = json.dumps(ctx.context, sort_keys=True, default=str)
        if new != ctx.context_json:
            self._update_journey(ctx.journey["id"], context=new)
            ctx.context_json = new

    def _create_task(self, ctx: Ctx, kind: str, urgency: Urgency, reason: str, data: dict) -> str:
        task_id, now = _new_id("T"), self.now()
        names = self._known_names(ctx)
        data = dict(data) | {"phone_hash": ctx.session["phone_hash"], "session_id": ctx.session["id"],
                             "patient_ref": ctx.session["patient_ref"] or ctx.journey["patient_ref"]}
        with self.engine.begin() as conn:
            conn.execute(insert(store.tasks).values(
                id=task_id, journey_id=ctx.journey["id"], kind=kind, urgency=int(urgency), status="open",
                reason=redact_text(reason, names), data=json.dumps(redact_payload(data, names), default=str),
                sla_due=now + timedelta(minutes=SLA_MINUTES[urgency]), created_at=now))
        self._append(ctx.journey["id"], "task_created", "system", {"task_id": task_id, "kind": kind,
                                                                   "urgency": urgency.name})
        return task_id

    def _pending_sync_task(self, ctx: Ctx, tool: str) -> str:
        for t in self.tasks(ctx.journey["id"]):
            if t["kind"] == "pending_sync":
                return t["id"]
        return self._create_task(ctx, "pending_sync", max(Urgency(ctx.journey["urgency"]), Urgency.ROUTINE),
                                 f"Hospital system unavailable during {tool}; call the patient back", {"tool": tool})

    def _resolve_task(self, task_id: str, staff_id: str, note: str) -> None:
        t = self.task(task_id)
        with self.engine.begin() as conn:
            conn.execute(update(store.tasks).where(store.tasks.c.id == task_id).values(
                status="done", resolved_at=self.now(), resolved_by=staff_id, resolution=redact_text(note)))
        self._append(t["journey_id"], "task_resolved", "human", {"task_id": task_id, "staff_id": staff_id,
                                                                 "note": note})

    def _resolve_open_tasks(self, journey_id: str, kind: str, staff_id: str, note: str) -> None:
        for t in self.tasks(journey_id):
            if t["kind"] == kind:
                self._resolve_task(t["id"], staff_id, note)

    def _lock_until(self, key: str) -> datetime | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(store.locks).where(store.locks.c.key == key)).mappings().first()
        if row and row["until"] > self.now():
            return row["until"]
        return None

    def _set_lock(self, key: str, until: datetime, reason: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(store.locks.delete().where(store.locks.c.key == key))
            conn.execute(insert(store.locks).values(key=key, until=until, reason=reason))

    def _otp_hash(self, session_id: str, code: str) -> str:
        return hashlib.sha256(f"{self._salt}:{session_id}:{code}".encode()).hexdigest()

    def _known_names(self, ctx: Ctx) -> set[str]:
        return self._names.get(ctx.session["id"], set())

    def _redact(self, ctx: Ctx, args: dict) -> dict:
        return redact_payload(args, self._known_names(ctx))

    def _no_proxy(self, ctx: Ctx, what: str) -> None:
        if ctx.session["relation"] != "self":
            raise ToolRefused("proxy_not_authorised",
                              f"{what.capitalize()} details go only to the patient. Offer to send them to the patient's "
                              "registered WhatsApp instead.")

    def _own_appointment(self, ctx: Ctx, appointment_id: str) -> dict:
        try:
            a = self.twin.appointment(appointment_id)
        except TwinError as e:
            if e.code == "not_found":
                raise ToolRefused("not_found", "No such appointment for this patient.")
            raise
        if a["participant"][0]["actor"]["id"] != ctx.session["patient_ref"]:
            raise ToolRefused("not_found", "No such appointment for this patient.")  # never confirm others' data
        return a

    def _describe_appt(self, a: dict) -> dict:
        doc_id = a["participant"][1]["actor"]["id"]
        doc = self.twin.practitioner(doc_id)
        return {"appointment_id": a["id"], "doctor": doc["name"][0]["text"], "department": doc["department"]["name"],
                "when": _say_when(a["start"]), "start": a["start"], "status": a["status"]}

    def _departments(self) -> dict[str, str]:
        if self._dept_names is None:
            self._dept_names = {d["id"]: d["name"] for d in self.twin.departments()}
        return self._dept_names


_HINTS: dict[str, dict[str, Any]] = {}


def _checked_args(handler: Callable, ctx: Ctx, args: dict) -> dict:
    """Check the model's arguments against the tool's signature before anything runs.

    Unknown or missing arguments and wrong types are refused as invalid_input. Numbers are
    accepted for text fields (a code or PIN read out as 482913) and digit strings for whole
    numbers ("1968"). Yes/no fields pass through: the tools themselves insist on a real True.
    """
    try:
        inspect.signature(handler).bind(ctx, **args)
    except TypeError as e:
        raise invalid(f"bad arguments: {e}")
    hints = _HINTS.get(handler.__name__)
    if hints is None:
        hints = _HINTS[handler.__name__] = get_type_hints(handler)
    out = {}
    for k, v in args.items():
        want = hints.get(k)
        types_ = set(get_args(want)) if get_origin(want) in (Union, types.UnionType) else {want}
        if v is None and type(None) in types_:
            out[k] = v
        elif str in types_:
            if isinstance(v, bool) or not isinstance(v, (str, int, float)):
                raise invalid(f"{k} must be text")
            out[k] = v if isinstance(v, str) else str(v)
        elif int in types_:
            if isinstance(v, int) and not isinstance(v, bool):
                out[k] = v
            elif isinstance(v, str) and v.strip().isdigit():
                out[k] = int(v.strip())
            else:
                raise invalid(f"{k} must be a whole number")
        else:
            out[k] = v
    return out


def _urgency(value: str) -> Urgency:
    try:
        return Urgency[str(value).upper()]
    except KeyError:
        raise invalid("urgency must be ROUTINE, SOON, URGENT or EMERGENCY")


def _say_when(iso: str) -> str:
    """'2026-11-26T10:30:00' -> 'Thursday 26 November, 10:30 AM' (what the agent reads back)."""
    t = datetime.fromisoformat(iso)
    return f"{t:%A} {t.day} {t:%B}, {t:%I:%M %p}".replace(" 0", " ")
