"""Journey states, allowed moves, urgency and verification levels (BLUEPRINT §4.3–4.4)."""

from __future__ import annotations

from enum import IntEnum, StrEnum


class State(StrEnum):
    CONTACT = "CONTACT"
    INFO_ONLY = "INFO_ONLY"
    IDENTIFYING = "IDENTIFYING"
    REGISTERING = "REGISTERING"
    DEDUPE_REVIEW = "DEDUPE_REVIEW"
    VERIFIED = "VERIFIED"
    INTAKE = "INTAKE"
    SLOT_SEARCH = "SLOT_SEARCH"
    SLOT_HELD = "SLOT_HELD"
    WAITLISTED = "WAITLISTED"
    BOOKED = "BOOKED"
    CONFIRMED = "CONFIRMED"
    PREPARED = "PREPARED"
    RESCHEDULED = "RESCHEDULED"
    ARRIVED = "ARRIVED"
    IN_QUEUE = "IN_QUEUE"
    CONSULTED = "CONSULTED"
    NO_SHOW = "NO_SHOW"
    FOLLOW_UP = "FOLLOW_UP"
    CLOSED = "CLOSED"


S = State
# ESCALATED is not a state: it is an overlay flag on any journey (see Journey.escalated).
TRANSITIONS: dict[State, frozenset[State]] = {
    S.CONTACT: frozenset({S.IDENTIFYING, S.INFO_ONLY, S.CLOSED}),
    S.INFO_ONLY: frozenset({S.IDENTIFYING, S.CLOSED}),
    S.IDENTIFYING: frozenset({S.VERIFIED, S.REGISTERING, S.CLOSED}),
    S.REGISTERING: frozenset({S.VERIFIED, S.DEDUPE_REVIEW, S.IDENTIFYING, S.CLOSED}),
    S.DEDUPE_REVIEW: frozenset({S.VERIFIED, S.REGISTERING, S.CLOSED}),  # staff decides
    S.VERIFIED: frozenset({S.INTAKE, S.RESCHEDULED, S.FOLLOW_UP, S.CLOSED}),
    S.INTAKE: frozenset({S.SLOT_SEARCH, S.CLOSED}),
    S.SLOT_SEARCH: frozenset({S.SLOT_HELD, S.WAITLISTED, S.CLOSED}),
    S.SLOT_HELD: frozenset({S.BOOKED, S.SLOT_SEARCH, S.CLOSED}),
    S.WAITLISTED: frozenset({S.SLOT_SEARCH, S.CLOSED}),
    S.BOOKED: frozenset({S.CONFIRMED, S.PREPARED, S.RESCHEDULED, S.ARRIVED, S.CLOSED}),
    S.CONFIRMED: frozenset({S.PREPARED, S.RESCHEDULED, S.ARRIVED, S.NO_SHOW, S.CLOSED}),
    S.PREPARED: frozenset({S.RESCHEDULED, S.ARRIVED, S.NO_SHOW, S.CLOSED}),
    S.RESCHEDULED: frozenset({S.SLOT_SEARCH, S.CLOSED}),
    S.ARRIVED: frozenset({S.IN_QUEUE}),
    S.IN_QUEUE: frozenset({S.CONSULTED, S.CLOSED}),
    S.CONSULTED: frozenset({S.FOLLOW_UP, S.CLOSED}),
    S.NO_SHOW: frozenset({S.SLOT_SEARCH, S.CLOSED}),
    S.FOLLOW_UP: frozenset({S.INTAKE, S.CLOSED}),
    S.CLOSED: frozenset(),
}

OPEN_STATES = frozenset(s for s in State if s is not State.CLOSED)
# States where the caller is known to be a specific patient.
PATIENT_STATES = frozenset(OPEN_STATES - {S.CONTACT, S.INFO_ONLY, S.IDENTIFYING, S.REGISTERING, S.DEDUPE_REVIEW})
# States that hang off one specific appointment.
APPOINTMENT_STATES = frozenset({S.BOOKED, S.CONFIRMED, S.PREPARED})


def can_move(src: State, dst: State) -> bool:
    return dst in TRANSITIONS[src]


class Urgency(IntEnum):
    """Ordered: the AI may only move up. Only a human may move down."""

    ROUTINE = 0
    SOON = 1
    URGENT = 2
    EMERGENCY = 3


# How fast a human must act on a task of each urgency (minutes).
SLA_MINUTES = {Urgency.EMERGENCY: 0, Urgency.URGENT: 15, Urgency.SOON: 120, Urgency.ROUTINE: 24 * 60}


class Level(IntEnum):
    """Verification ladder for the current call (resets every call)."""

    NONE = 0  # hours, directions, departments, doctor list
    SOFT = 1  # caller ID matches a known patient: greet by first name, nothing else
    MEDIUM = 2  # name + DOB (or birth year + PIN): book, reschedule, cancel, queue
    STRONG = 3  # + OTP to the registered phone: report status, bills
