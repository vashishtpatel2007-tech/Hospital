"""Claude tool definitions, built from the Journey Engine's own tool functions.

The schema for each tool comes from the engine handler's signature (names, types, defaults),
so the agent and the engine can never drift apart. Descriptions come from TOOL_SPECS, plus a
short note per argument below.

Every tool is sent on every request (a stable tool list keeps the prompt cache warm); the
per-turn state note tells the model which ones are allowed right now, and the engine refuses
anything else.
"""

from __future__ import annotations

import inspect
import types
from typing import Any, Union, get_args, get_origin, get_type_hints

from journey.engine import RELATIONS, TOOL_SPECS, JourneyEngine

CALLER_ID = "CALLER_ID"  # the model writes this; the agent swaps in the real number (the model never sees it)

URGENCIES = ["ROUTINE", "SOON", "URGENT", "EMERGENCY"]
LANG_CODES = ["en", "hi", "kn", "ta", "te", "ml", "mr", "ur", "bn"]

ARG_DOCS: dict[tuple[str, str], dict[str, Any]] = {
    ("set_caller_relation", "relation"): {"enum": sorted(RELATIONS), "description": "Who the patient is to the caller."},
    ("set_caller_relation", "consent"): {"description": "true only if the caller says the patient agreed to them calling."},
    ("verify_identity", "name"): {"description": "The patient's full name as the caller says it."},
    ("verify_identity", "birth_date"): {"description": "Date of birth, YYYY-MM-DD."},
    ("verify_identity", "birth_year"): {"description": "Four-digit birth year, with postal_code, when the caller doesn't know the full date."},
    ("verify_identity", "postal_code"): {"description": "6-digit PIN code of the registered address."},
    ("check_otp", "code"): {"description": "The digits the caller reads out."},
    ("register_patient", "given"): {"description": "First name, spelled as the caller confirmed."},
    ("register_patient", "family"): {"description": "Surname, spelled as the caller confirmed."},
    ("register_patient", "gender"): {"enum": ["male", "female", "other"]},
    ("register_patient", "birth_date"): {"description": "YYYY-MM-DD."},
    ("register_patient", "phone"): {"description": f"10-digit mobile number, or exactly {CALLER_ID} if the caller says to use the number they are calling from."},
    ("register_patient", "postal_code"): {"description": "6-digit PIN code."},
    ("register_patient", "language"): {"enum": LANG_CODES, "description": "Preferred language."},
    ("register_patient", "caller_confirmed"): {"description": "true only after reading every detail back (spell the name) and the caller saying yes."},
    ("find_doctors", "department_id"): {"description": "Department id from list_departments, e.g. CARD."},
    ("find_doctors", "language"): {"enum": LANG_CODES},
    ("find_doctors", "gender"): {"enum": ["male", "female"]},
    ("find_doctors", "name"): {"description": "Part of the doctor's name."},
    ("start_booking", "reason"): {"description": "Why they want to come, in the caller's words (no clinical judgement)."},
    ("start_booking", "department_id"): {"description": "Department id, e.g. GENM, CARD."},
    ("start_booking", "practitioner_id"): {"description": "Doctor id from find_doctors, e.g. D-05."},
    ("get_slots", "date_from"): {"description": "YYYY-MM-DD"},
    ("get_slots", "date_to"): {"description": "YYYY-MM-DD"},
    ("get_slots", "part_of_day"): {"enum": ["morning", "afternoon", "evening"]},
    ("get_slots", "language"): {"enum": LANG_CODES, "description": "Doctor must speak this language."},
    ("get_slots", "gender"): {"enum": ["male", "female"], "description": "Doctor's gender, if the caller asked."},
    ("hold_slot", "slot_id"): {"description": "A slot_id returned by get_slots on this call."},
    ("book_appointment", "patient_confirmed"): {"description": "true only after reading the read_back text and hearing a clear yes in the caller's latest message."},
    ("join_waitlist", "date_from"): {"description": "YYYY-MM-DD"},
    ("join_waitlist", "date_to"): {"description": "YYYY-MM-DD"},
    ("start_reschedule", "appointment_id"): {"description": "From list_appointments."},
    ("cancel_appointment", "appointment_id"): {"description": "From list_appointments."},
    ("cancel_appointment", "reason"): {"description": "The caller's reason."},
    ("cancel_appointment", "patient_confirmed"): {"description": "true only after reading back which appointment and hearing a clear yes."},
    ("get_queue_status", "appointment_id"): {"description": "From list_appointments."},
    ("create_callback", "reason"): {"description": "What staff should call back about."},
    ("create_callback", "urgency"): {"enum": URGENCIES, "description": "SOON for symptom or medicine questions."},
    ("escalate", "urgency"): {"enum": URGENCIES, "description": "URGENT or EMERGENCY. This freezes booking until a human takes over."},
    ("escalate", "reason"): {"description": "One line for the nurse: what the caller reported."},
    ("escalate", "verbatim"): {"description": "The caller's exact words."},
    ("transfer_to_human", "reason"): {"description": "Why a person is needed."},
    ("transfer_to_human", "desk"): {"enum": ["front_desk", "nurse", "emergency", "billing"]},
    ("end_call", "summary"): {"description": "One line: what was done and what is still pending."},
}

# Extra guidance appended to the engine's own one-line description.
TOOL_NOTES: dict[str, str] = {
    "escalate": " Use for URGENT or EMERGENCY only; for mild or unclear symptoms use create_callback with urgency SOON and keep helping.",
    "create_callback": " Also use when the caller asks about medicines or whether a symptom is serious (urgency SOON).",
    "end_call": " Say goodbye first, then call this.",
    "get_slots": " Offer the caller at most 3 of them.",
}

_JSON = {str: "string", int: "integer", bool: "boolean", float: "number"}


def _json_type(hint: Any) -> tuple[str, bool]:
    """(json type, optional?) for a handler parameter's annotation."""
    origin = get_origin(hint)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(hint) if a is not type(None)]
        return _JSON[args[0]], True
    return _JSON[hint], False


def build_tools() -> list[dict]:
    tools = []
    for name, spec in TOOL_SPECS.items():
        handler = getattr(JourneyEngine, f"_t_{name}")
        hints = get_type_hints(handler)
        props: dict[str, dict] = {}
        required: list[str] = []
        for pname, param in inspect.signature(handler).parameters.items():
            if pname in ("self", "ctx", "actor"):
                continue
            jtype, optional = _json_type(hints[pname])
            props[pname] = {"type": jtype, **ARG_DOCS.get((name, pname), {})}
            if param.default is inspect.Parameter.empty and not optional:
                required.append(pname)
        tools.append({
            "name": name,
            "description": spec.description + TOOL_NOTES.get(name, ""),
            "input_schema": {"type": "object", "properties": props, "required": required, "additionalProperties": False},
        })
    return tools


# Arguments that need a clear yes in the caller's latest message before the agent lets the call through.
CONFIRM_FLAGS: dict[str, str] = {
    "book_appointment": "patient_confirmed",
    "cancel_appointment": "patient_confirmed",
    "register_patient": "caller_confirmed",
}
