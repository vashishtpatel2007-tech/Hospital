"""Turn DB rows into FHIR-shaped JSON (field names follow FHIR R4 where one exists)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

SLOT_STATUS = {"free": "free", "held": "busy-tentative", "booked": "busy"}


def _iso(v: date | datetime | None) -> str | None:
    return v.isoformat() if v is not None else None


def department(r: Mapping[str, Any]) -> dict:
    return {
        "resourceType": "HealthcareService",
        "id": r["id"],
        "name": r["name"],
        "location": r["floor"],
        "telecom": [{"system": "phone", "value": f"ext {r['phone_ext']}"}],
    }


def practitioner(r: Mapping[str, Any], dept_name: str | None = None) -> dict:
    return {
        "resourceType": "Practitioner",
        "id": r["id"],
        "name": [{"text": f"Dr. {r['given']} {r['family']}", "given": [r["given"]], "family": r["family"],
                  "prefix": ["Dr."]}],
        "gender": r["gender"],
        "qualification": [{"code": {"text": r["qualification"]}}],
        "communication": r["languages"].split(","),
        "department": {"id": r["department_id"], "name": dept_name},
        "opd": {
            "days": [int(x) for x in r["opd_days"].split(",")],  # Mon=0
            "start": r["opd_start"],
            "end": r["opd_end"],
            "slot_minutes": r["slot_minutes"],
        },
        "fee": {"value": r["fee_inr"], "currency": "INR"},
    }


def patient(r: Mapping[str, Any]) -> dict:
    out = {
        "resourceType": "Patient",
        "id": r["id"],
        "name": [{"text": f"{r['given']} {r['family']}", "given": [r["given"]], "family": r["family"]}],
        "gender": r["gender"],
        "birthDate": _iso(r["birth_date"]),
        "telecom": [{"system": "phone", "value": r["phone"]}],
        "address": [{"postalCode": r["postal_code"], "city": r["city"]}],
        "communication": [{"language": r["language"]}] if r["language"] else [],
        "identifier": [{"system": "abha-address", "value": r["abha"]}] if r["abha"] else [],
    }
    return out


def slot(r: Mapping[str, Any], now: datetime, practitioner_name: str | None = None,
         department_id: str | None = None) -> dict:
    status = r["status"]
    if status == "held" and r["hold_expires_at"] is not None and r["hold_expires_at"] < now:
        status = "free"  # an expired hold no longer blocks the slot
    return {
        "resourceType": "Slot",
        "id": r["id"],
        "schedule": {"practitioner_id": r["practitioner_id"], "practitioner_name": practitioner_name,
                     "department_id": department_id},
        "start": _iso(r["start"]),
        "end": _iso(r["end"]),
        "status": SLOT_STATUS[status],
    }


def hold(r: Mapping[str, Any], now: datetime) -> dict:
    status = r["status"]
    if status == "active" and r["expires_at"] < now:
        status = "expired"
    return {
        "id": r["id"],
        "slot_id": r["slot_id"],
        "expires_at": _iso(r["expires_at"]),
        "seconds_left": max(0, int((r["expires_at"] - now).total_seconds())) if status == "active" else 0,
        "status": status,
    }


def appointment(r: Mapping[str, Any]) -> dict:
    return {
        "resourceType": "Appointment",
        "id": r["id"],
        "status": r["status"],
        "start": _iso(r["start"]),
        "end": _iso(r["end"]),
        "slot": [r["slot_id"]] if r["slot_id"] else [],
        "participant": [
            {"actor": {"type": "Patient", "id": r["patient_id"]}},
            {"actor": {"type": "Practitioner", "id": r["practitioner_id"]}},
        ],
        "reasonText": r["reason"],
        "cancelationReason": r["cancel_reason"],
        "rescheduledFrom": _iso(r["rescheduled_from"]),
        "created": _iso(r["created_at"]),
    }


def report(r: Mapping[str, Any]) -> dict:
    return {
        "resourceType": "DiagnosticReport",
        "id": r["id"],
        "subject": {"id": r["patient_id"]},
        "code": {"text": r["test_name"]},
        "status": r["status"],  # registered | preliminary | final
        "ordered": _iso(r["ordered_at"]),
        "issued": _iso(r["issued_at"]),
        "note": "Status only. Results are released by the doctor, never by phone.",
    }


def invoice(r: Mapping[str, Any]) -> dict:
    return {
        "resourceType": "Invoice",
        "id": r["id"],
        "subject": {"id": r["patient_id"]},
        "status": r["status"],
        "description": r["description"],
        "totalGross": {"value": r["amount_inr"], "currency": "INR"},
        "payer": r["payer"],
        "insuranceStatus": r["insurance_status"],
        "date": _iso(r["created_at"]),
    }


def waitlist_entry(r: Mapping[str, Any]) -> dict:
    return {
        "id": r["id"],
        "patient_id": r["patient_id"],
        "practitioner_id": r["practitioner_id"],
        "department_id": r["department_id"],
        "date_from": _iso(r["date_from"]),
        "date_to": _iso(r["date_to"]),
        "status": r["status"],
        "created": _iso(r["created_at"]),
    }
