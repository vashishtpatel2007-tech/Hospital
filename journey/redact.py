"""Strip personal data before anything is written to the event log or task queue.

Structured data: sensitive keys are dropped to "[REDACTED]".
Free text (what callers say, reasons, notes): phone numbers, Aadhaar numbers, dates,
emails/ABHA addresses, long numbers (OTPs, PIN codes) and known names are masked.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Iterable, Mapping

SENSITIVE_KEYS = frozenset({
    "phone", "birth_date", "birth_year", "dob", "postal_code", "pin", "aadhaar", "abha", "code", "otp",
    "name", "given", "family", "patient_name", "email", "address",
})
FREE_TEXT_KEYS = frozenset({"reason", "verbatim", "note", "summary", "message", "utterance", "text", "resolution",
                            "matched", "detail"})

_MONTHS = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"

_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*", re.I), "[EMAIL]"),
    # Aadhaar: 12 digits, often 4-4-4 (check before phone numbers)
    (re.compile(r"(?<!\d)\d{4}[\s-]?\d{4}[\s-]?\d{4}(?!\d)"), "[AADHAAR]"),
    # Indian mobile, with or without +91 / 0, spaces or dashes inside
    (re.compile(r"(?<!\d)(?:\+?91[\s-]?|0)?[6-9]\d{2}[\s-]?\d{2}[\s-]?\d{5}(?!\d)"), "[PHONE]"),
    (re.compile(r"(?<!\d)[6-9]\d{4}[\s-]\d{5}(?!\d)"), "[PHONE]"),
    # dates: 2026-03-14, 14/03/1968, 14-3-68, 14 March 1968, 14th of March, March 14 1968
    (re.compile(r"(?<!\d)\d{4}-\d{1,2}-\d{1,2}(?:T[\d:.]+)?(?!\d)"), "[DATE]"),
    (re.compile(r"(?<!\d)\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}(?!\d)"), "[DATE]"),
    (re.compile(rf"(?<!\d)\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTHS}\b,?(?:\s+\d{{2,4}})?", re.I), "[DATE]"),
    (re.compile(rf"\b{_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?,?(?:\s+\d{{2,4}})?", re.I), "[DATE]"),
    # any remaining long number (OTP, PIN code, ID numbers)
    (re.compile(r"(?<!\d)\d{5,}(?!\d)"), "[NUMBER]"),
    # four-digit years on their own ("born in 1968")
    (re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)"), "[YEAR]"),
]


def redact_text(text: str, known_names: Iterable[str] = ()) -> str:
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    for name in sorted({n for n in known_names if n and len(n) >= 3}, key=len, reverse=True):
        out = re.sub(rf"\b{re.escape(name)}\b", "[NAME]", out, flags=re.I)
    return out


def redact_payload(payload: Any, known_names: Iterable[str] = (), _key: str | None = None) -> Any:
    names = tuple(known_names)
    if isinstance(payload, Mapping):
        out = {}
        for k, v in payload.items():
            if k in SENSITIVE_KEYS:
                out[k] = "[REDACTED]" if v not in (None, "") else v
            else:
                out[k] = redact_payload(v, names, k)
        return out
    if isinstance(payload, (list, tuple)):
        return [redact_payload(v, names, _key) for v in payload]
    if isinstance(payload, str):
        if _key in FREE_TEXT_KEYS:
            return redact_text(payload, names)
        # structured strings (ids, states, slot times) stay, but never a phone/Aadhaar number
        return _PATTERNS[2][0].sub("[PHONE]", _PATTERNS[1][0].sub("[AADHAAR]", payload))
    return payload


def phone_hash(phone_e164: str) -> str:
    """Stable pseudonym for a phone number (stored instead of the number)."""
    salt = os.getenv("PHI_HASH_SALT", "sahay-dev-salt-change-me")
    return hashlib.sha256(f"{salt}:{phone_e164}".encode()).hexdigest()[:32]


def normalise_phone(raw: str) -> str | None:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        digits = "91" + digits
    if len(digits) != 12 or not digits.startswith("91") or digits[2] not in "6789":
        return None
    return "+" + digits


def mask_phone(phone_e164: str) -> str:
    return "******" + phone_e164[-4:]
