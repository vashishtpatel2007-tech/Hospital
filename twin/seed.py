"""Deterministic seed data for the Hospital Twin: a fictional 250-bed hospital in Bengaluru.

Same seed + same 'today' -> same data. Slots are generated relative to 'today' so the
demo always has availability.

    python -m twin.seed --reset     # wipe and reseed the DB in TWIN_DB_URL
"""

from __future__ import annotations

import argparse
import os
import random
from datetime import date, datetime, time, timedelta

from sqlalchemy import Engine, insert

from . import db

SEED = 42
DAYS_AHEAD = 14
BOOKED_FRACTION = 0.35

DEPARTMENTS = [
    # id, name, floor, extension
    ("GENM", "General Medicine", "Ground floor, Block A", "101"),
    ("CARD", "Cardiology", "2nd floor, Block B", "201"),
    ("ORTH", "Orthopaedics", "1st floor, Block A", "111"),
    ("OBGY", "Obstetrics & Gynaecology", "3rd floor, Block B", "301"),
    ("PAED", "Paediatrics", "1st floor, Block B", "121"),
    ("ENT", "ENT", "2nd floor, Block A", "211"),
    ("DERM", "Dermatology", "2nd floor, Block A", "221"),
    ("OPHT", "Ophthalmology", "Ground floor, Block C", "131"),
    ("NEUR", "Neurology", "3rd floor, Block A", "311"),
    ("GAST", "Gastroenterology", "3rd floor, Block C", "321"),
    ("LAB", "Laboratory (sample collection)", "Ground floor, Block B", "141"),
    ("RADI", "Radiology & Imaging", "Basement, Block B", "151"),
]

# id, given, family, gender, dept, qualification, languages, opd_days, start, end, slot_min, fee
PRACTITIONERS = [
    ("D-01", "Anil", "Mehta", "male", "GENM", "MBBS, MD (Medicine)", "en,hi", "0,1,2,3,4,5", "08:00", "13:00", 15, 500),
    ("D-02", "Kavitha", "Reddy", "female", "GENM", "MBBS, MD (Medicine)", "en,kn,te", "0,1,2,3,4,5", "14:00", "19:00", 15, 500),
    ("D-03", "Farhan", "Siddiqui", "male", "GENM", "MBBS, DNB (Medicine)", "en,hi,ur", "0,2,4,5", "09:00", "13:00", 15, 500),
    ("D-04", "Priya", "Nair", "female", "GENM", "MBBS, MD (Medicine)", "en,ml,kn", "1,3,5", "16:00", "20:00", 15, 500),
    ("D-05", "Suresh", "Rao", "male", "CARD", "MBBS, MD, DM (Cardiology)", "en,kn,hi", "0,1,2,3,4,5", "10:00", "13:00", 15, 1000),
    ("D-06", "Meenakshi", "Iyer", "female", "CARD", "MBBS, MD, DM (Cardiology)", "en,ta", "0,2,4", "16:00", "19:00", 15, 1000),
    ("D-07", "Vikram", "Shetty", "male", "CARD", "MBBS, DNB (Cardiology)", "en,kn", "1,3,5", "09:00", "12:00", 15, 900),
    ("D-08", "Rajesh", "Gowda", "male", "ORTH", "MBBS, MS (Ortho)", "en,kn", "0,1,2,3,4", "09:00", "13:00", 15, 800),
    ("D-09", "Arjun", "Malhotra", "male", "ORTH", "MBBS, MS (Ortho)", "en,hi", "1,3,5", "16:00", "19:00", 15, 900),
    ("D-10", "Sneha", "Kulkarni", "female", "ORTH", "MBBS, DNB (Ortho)", "en,mr,kn", "0,2,4", "14:00", "17:00", 15, 800),
    ("D-11", "Anjali", "Deshpande", "female", "OBGY", "MBBS, MS (OBG)", "en,mr,hi", "0,1,2,3,4,5", "10:00", "13:00", 15, 900),
    ("D-12", "Fatima", "Khan", "female", "OBGY", "MBBS, DGO", "en,hi,ur", "1,3,5", "15:00", "18:00", 15, 800),
    ("D-13", "Shalini", "Murthy", "female", "OBGY", "MBBS, MS (OBG)", "en,kn", "0,2,4", "16:00", "19:00", 15, 900),
    ("D-14", "Ravi", "Shankar", "male", "PAED", "MBBS, MD (Paediatrics)", "en,kn,ta", "0,1,2,3,4,5", "09:00", "13:00", 15, 700),
    ("D-15", "Neha", "Agarwal", "female", "PAED", "MBBS, DCH", "en,hi", "1,3,5", "16:00", "19:00", 15, 700),
    ("D-16", "Deepa", "Hegde", "female", "PAED", "MBBS, MD (Paediatrics)", "en,kn", "0,2,4", "14:00", "17:00", 15, 700),
    ("D-17", "Manoj", "Pillai", "male", "ENT", "MBBS, MS (ENT)", "en,ml", "0,2,4", "09:00", "12:00", 15, 700),
    ("D-18", "Asha", "Patil", "female", "ENT", "MBBS, DLO", "en,kn,mr", "1,3,5", "15:00", "18:00", 15, 700),
    ("D-19", "Nikhil", "Bhat", "male", "DERM", "MBBS, MD (Dermatology)", "en,kn,hi", "0,1,3,4", "11:00", "14:00", 15, 800),
    ("D-20", "Ritu", "Sharma", "female", "DERM", "MBBS, DVD", "en,hi", "2,5", "16:00", "19:00", 15, 800),
    ("D-21", "Sunil", "Joshi", "male", "OPHT", "MBBS, MS (Ophthalmology)", "en,hi,mr", "0,1,2,3,4", "09:00", "12:00", 15, 700),
    ("D-22", "Lavanya", "Krishnan", "female", "OPHT", "MBBS, DNB (Ophthalmology)", "en,ta,kn", "1,3,5", "14:00", "17:00", 15, 700),
    ("D-23", "Harish", "Chandra", "male", "NEUR", "MBBS, MD, DM (Neurology)", "en,hi,kn", "0,2,4", "10:00", "13:00", 20, 1200),
    ("D-24", "Smitha", "Rao", "female", "NEUR", "MBBS, DNB (Neurology)", "en,kn", "1,3,5", "15:00", "18:00", 20, 1100),
    ("D-25", "Imran", "Qureshi", "male", "GAST", "MBBS, MD, DM (Gastro)", "en,hi,ur", "0,2,4", "10:00", "13:00", 15, 1100),
    ("D-26", "Pooja", "Venkatesh", "female", "GAST", "MBBS, DNB (Gastro)", "en,kn,te", "1,3,5", "15:00", "18:00", 15, 1000),
    ("D-27", "Ganesh", "Babu", "male", "LAB", "MBBS, MD (Pathology)", "en,kn,ta", "0,1,2,3,4,5,6", "07:00", "11:00", 10, 100),
    ("D-28", "Rekha", "Menon", "female", "LAB", "MBBS, MD (Biochemistry)", "en,ml", "0,1,2,3,4,5", "11:00", "15:00", 10, 100),
    ("D-29", "Sanjay", "Kamath", "male", "RADI", "MBBS, MD (Radiology)", "en,kn,hi", "0,1,2,3,4,5", "09:00", "13:00", 20, 600),
    ("D-30", "Divya", "Prasad", "female", "RADI", "MBBS, DMRD", "en,te,kn", "0,2,4", "14:00", "18:00", 20, 600),
]

MALE_GIVEN = [
    "Ramesh", "Suresh", "Mahesh", "Ganesh", "Prakash", "Venkatesh", "Manjunath", "Naveen", "Kiran", "Arun",
    "Rahul", "Amit", "Sandeep", "Vinod", "Rohit", "Harsha", "Siddharth", "Abdul", "Imran", "Joseph",
    "Thomas", "Basavaraj", "Chandrashekar", "Gopal", "Raghavendra", "Shivakumar", "Anand", "Deepak", "Yusuf", "Aditya",
]
FEMALE_GIVEN = [
    "Lakshmi", "Savitha", "Geetha", "Shobha", "Kavya", "Divya", "Anitha", "Rekha", "Pooja", "Sunita",
    "Meena", "Asha", "Bhavya", "Ayesha", "Nazia", "Mary", "Shanthi", "Pavithra", "Roopa", "Sowmya",
    "Nandini", "Harini", "Usha", "Vani", "Sneha", "Priyanka", "Shruti", "Farzana", "Deepika", "Radha",
]
FAMILY = [
    "Sharma", "Rao", "Gowda", "Reddy", "Shetty", "Nair", "Iyer", "Kumar", "Patil", "Hegde", "Naik", "Bhat",
    "Kulkarni", "Joshi", "Khan", "Ahmed", "Fernandes", "D'Souza", "Murthy", "Prasad", "Singh", "Verma",
    "Gupta", "Pillai", "Menon", "Shenoy", "Kamath", "Desai", "Pai", "Naidu",
]
AREAS = [
    ("560001", "Bengaluru"), ("560004", "Bengaluru"), ("560011", "Bengaluru"), ("560034", "Bengaluru"),
    ("560038", "Bengaluru"), ("560054", "Bengaluru"), ("560064", "Bengaluru"), ("560076", "Bengaluru"),
    ("560097", "Bengaluru"), ("562125", "Anekal"), ("562157", "Devanahalli"), ("571401", "Mandya"),
]
LANGS = ["kn"] * 4 + ["en"] * 3 + ["hi"] * 3 + ["ta", "te", "ml"]

# Pairs that a careless system would merge or confuse (same birth date, similar name).
NEAR_DUPLICATES = [
    (("Laxmi", "Devi", "female"), ("Lakshmi", "Devi", "female")),
    (("Mohammed", "Irfan", "male"), ("Mohammad", "Irfan", "male")),
    (("Srinivas", "Murthy", "male"), ("Shrinivas", "Murthy", "male")),
    (("Shweta", "Kulkarni", "female"), ("Sweta", "Kulkarni", "female")),
    (("Raghavendra", "Rao", "male"), ("Ragavendra", "Rao", "male")),
    (("Jyothi", "Shetty", "female"), ("Jyoti", "Shetty", "female")),
    (("Basavaraj", "Patil", "male"), ("Basavaraju", "Patil", "male")),
    (("Vaishnavi", "Iyer", "female"), ("Vaishnavi", "Iyengar", "female")),
]

TESTS = [
    "Complete Blood Count (CBC)", "Lipid profile", "HbA1c", "Thyroid profile (TSH)", "Liver function test",
    "Kidney function test", "Urine routine", "X-ray chest", "Ultrasound abdomen", "ECG", "2D Echo", "MRI brain",
]


def _phone(rng: random.Random) -> str:
    return "+91" + rng.choice("6789") + "".join(rng.choice("0123456789") for _ in range(9))


def _dob(rng: random.Random, today: date, min_age: int, max_age: int) -> date:
    age_days = rng.randint(min_age * 365, max_age * 365)
    return today - timedelta(days=age_days)


def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def next_weekday(today: date, weekday: int, min_days_ahead: int = 1) -> date:
    d = today + timedelta(days=min_days_ahead)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


def build(today: date, now: datetime) -> dict[str, list[dict]]:
    """Build every table's rows in memory (deterministic)."""
    rng = random.Random(SEED)
    rows: dict[str, list[dict]] = {k: [] for k in (
        "departments", "practitioners", "patients", "slots", "appointments", "reports", "invoices")}

    rows["departments"] = [dict(id=i, name=n, floor=f, phone_ext=e) for i, n, f, e in DEPARTMENTS]
    rows["practitioners"] = [
        dict(id=i, given=g, family=fa, gender=ge, department_id=d, qualification=q, languages=l,
             opd_days=days, opd_start=s, opd_end=e, slot_minutes=sm, fee_inr=fee)
        for i, g, fa, ge, d, q, l, days, s, e, sm, fee in PRACTITIONERS
    ]

    # ---- patients
    patients: list[dict] = []

    def add_patient(given, family, gender, dob, phone, language=None, area=None, abha=None) -> dict:
        pin, city = area or rng.choice(AREAS)
        p = dict(
            id=f"P-{1001 + len(patients)}", given=given, family=family, gender=gender, birth_date=dob,
            phone=phone, postal_code=pin, city=city, language=language or rng.choice(LANGS), abha=abha,
            created_at=now - timedelta(days=rng.randint(30, 2000)),
        )
        patients.append(p)
        return p

    # Demo personas (fixed ids, used by the demo script and scenarios)
    ramesh = add_patient("Ramesh", "Sharma", "male", date(today.year - 58, 3, 14), "+919845012345",
                         "hi", ("560038", "Bengaluru"), "ramesh.sharma@abdm")
    lakshmi = add_patient("Lakshmi", "Narayan", "female", date(today.year - 72, 7, 2), "+919880023456",
                          "kn", ("560004", "Bengaluru"))
    # Caregiver persona: daughter calls for her father from her own phone
    add_patient("Kavya", "Gowda", "female", date(today.year - 34, 11, 21), "+919900034567", "kn",
                ("560076", "Bengaluru"))
    add_patient("Manjunath", "Gowda", "male", date(today.year - 67, 1, 9), "+919900034567", "kn",
                ("560076", "Bengaluru"))

    for (a, b) in NEAR_DUPLICATES:
        dob = _dob(rng, today, 25, 75)
        same_phone = rng.random() < 0.5
        p1 = add_patient(a[0], a[1], a[2], dob, _phone(rng))
        add_patient(b[0], b[1], b[2], dob, p1["phone"] if same_phone else _phone(rng))

    # Families sharing one phone (child + parent)
    for _ in range(6):
        fam = rng.choice(FAMILY)
        phone = _phone(rng)
        g = rng.choice(["male", "female"])
        add_patient(rng.choice(MALE_GIVEN if g == "male" else FEMALE_GIVEN), fam, g, _dob(rng, today, 30, 50), phone)
        cg = rng.choice(["male", "female"])
        add_patient(rng.choice(MALE_GIVEN if cg == "male" else FEMALE_GIVEN), fam, cg, _dob(rng, today, 1, 14), phone)

    while len(patients) < 300:
        g = rng.choice(["male", "female"])
        given = rng.choice(MALE_GIVEN if g == "male" else FEMALE_GIVEN)
        add_patient(given, rng.choice(FAMILY), g, _dob(rng, today, 0, 90), _phone(rng),
                    abha=f"{given.lower()}{rng.randint(10, 9999)}@abdm" if rng.random() < 0.3 else None)
    rows["patients"] = patients

    # ---- slots for the next DAYS_AHEAD days
    slots: list[dict] = []
    for d in rows["practitioners"]:
        days = {int(x) for x in d["opd_days"].split(",")}
        for offset in range(DAYS_AHEAD):
            day = today + timedelta(days=offset)
            if day.weekday() not in days:
                continue
            t = datetime.combine(day, _hhmm(d["opd_start"]))
            end_of_day = datetime.combine(day, _hhmm(d["opd_end"]))
            while t + timedelta(minutes=d["slot_minutes"]) <= end_of_day:
                slots.append(dict(id=f"S-{len(slots) + 1:06d}", practitioner_id=d["id"], start=t,
                                  end=t + timedelta(minutes=d["slot_minutes"]), status="free",
                                  hold_id=None, hold_expires_at=None))
                t += timedelta(minutes=d["slot_minutes"])
    rows["slots"] = slots

    # ---- appointments
    appts: list[dict] = []

    def book(slot: dict, patient: dict, reason: str | None = None, status: str = "booked") -> dict:
        slot["status"] = "booked"
        a = dict(id=f"A-{len(appts) + 1:06d}", patient_id=patient["id"], practitioner_id=slot["practitioner_id"],
                 slot_id=slot["id"], start=slot["start"], end=slot["end"], status=status, reason=reason,
                 cancel_reason=None, rescheduled_from=None, token=None, arrived_at=None,
                 created_at=now - timedelta(days=rng.randint(1, 10)))
        appts.append(a)
        return a

    def first_free(doctor_id: str, day: date, at: str | None = None) -> dict | None:
        for s in slots:
            if s["practitioner_id"] == doctor_id and s["start"].date() == day and s["status"] == "free":
                if at is None or s["start"].strftime("%H:%M") == at:
                    return s
        return None

    # Ramesh: Cardiology follow-up with Dr. Suresh Rao next Thursday 10:30 (journey 2 / EMG-03)
    thursday = next_weekday(today, 3)
    s = first_free("D-05", thursday, "10:30") or first_free("D-05", thursday)
    if s:
        book(s, ramesh, "Cardiology follow-up")
    # Lakshmi: Cardiology with Dr. Rao in 2-3 days (elderly reschedule demo) + lipid profile at the lab
    lk_day = today + timedelta(days=2)
    while lk_day.weekday() == 6:
        lk_day += timedelta(days=1)
    s = first_free("D-05", lk_day, "11:00") or first_free("D-05", lk_day)
    if s:
        book(s, lakshmi, "BP review")

    # Random future bookings (~35% of slots)
    for s in slots:
        if s["status"] == "free" and rng.random() < BOOKED_FRACTION:
            book(s, rng.choice(patients[20:]), rng.choice([None, "Follow-up", "New complaint", "Review reports"]))

    # Past visits (history for returning patients; no slot rows needed)
    past_doctors = [d["id"] for d in rows["practitioners"]]
    for p in [ramesh, lakshmi] + rng.sample(patients[4:], 120):
        visits = [("D-05", 30)] if p is ramesh else [("D-05", 45)] if p is lakshmi else [
            (rng.choice(past_doctors), rng.randint(5, 180)) for _ in range(rng.randint(1, 3))]
        for doc, days_ago in visits:
            start = datetime.combine(today - timedelta(days=days_ago), time(10, 0))
            appts.append(dict(
                id=f"A-{len(appts) + 1:06d}", patient_id=p["id"], practitioner_id=doc, slot_id=None,
                start=start, end=start + timedelta(minutes=15),
                status=rng.choice(["fulfilled"] * 9 + ["noshow"]) if p not in (ramesh, lakshmi) else "fulfilled",
                reason="Consultation", cancel_reason=None, rescheduled_from=None, token=None, arrived_at=None,
                created_at=start - timedelta(days=3)))
    rows["appointments"] = appts

    # ---- reports (status only)
    reports: list[dict] = []

    def add_report(p: dict, test: str, status: str, days_ago: int) -> None:
        ordered = now - timedelta(days=days_ago)
        reports.append(dict(id=f"R-{len(reports) + 1:04d}", patient_id=p["id"], test_name=test, status=status,
                            ordered_at=ordered,
                            issued_at=ordered + timedelta(hours=rng.randint(6, 48)) if status == "final" else None))

    add_report(ramesh, "Lipid profile", "registered", 0)  # ordered, sample not given yet (needs fasting)
    add_report(ramesh, "ECG", "final", 30)
    add_report(lakshmi, "Lipid profile", "final", 2)
    add_report(lakshmi, "HbA1c", "preliminary", 1)
    for p in rng.sample(patients[4:], 80):
        add_report(p, rng.choice(TESTS), rng.choice(["registered", "preliminary", "final", "final"]),
                   rng.randint(0, 30))
    rows["reports"] = reports

    # ---- invoices
    invoices: list[dict] = []

    def add_invoice(p: dict, desc: str, amount: float, payer: str, ins: str, status: str, days_ago: int) -> None:
        invoices.append(dict(id=f"INV-{len(invoices) + 1:04d}", patient_id=p["id"], description=desc,
                             amount_inr=amount, status=status, payer=payer, insurance_status=ins,
                             created_at=now - timedelta(days=days_ago)))

    add_invoice(ramesh, "Cardiology consultation", 1000, "self", "not_applicable", "balanced", 30)
    add_invoice(lakshmi, "Angiography (planned)", 38000, "PM-JAY", "pre_auth_pending", "issued", 3)
    for p in rng.sample(patients[4:], 80):
        payer = rng.choice(["self"] * 7 + ["PM-JAY", "TPA:Star Health", "TPA:Medi Assist"])
        ins = "not_applicable" if payer == "self" else rng.choice(["pre_auth_pending", "approved", "approved", "rejected"])
        add_invoice(p, rng.choice(["Consultation", "Lab tests", "Imaging", "Pharmacy", "Day-care procedure"]),
                    float(rng.choice([500, 800, 1200, 2400, 4500, 12000, 26000])), payer, ins,
                    rng.choice(["issued", "balanced", "balanced"]), rng.randint(0, 60))
    rows["invoices"] = invoices
    return rows


def reset(engine: Engine, today: date, now: datetime) -> dict[str, int]:
    """Drop everything, recreate tables, insert fresh seed data. Returns row counts."""
    db.metadata.drop_all(engine)
    db.metadata.create_all(engine)
    rows = build(today, now)
    order = ["departments", "practitioners", "patients", "slots", "appointments", "reports", "invoices"]
    with engine.begin() as conn:
        for name in order:
            if rows[name]:
                conn.execute(insert(db.metadata.tables[name]), rows[name])
    return {name: len(rows[name]) for name in order}


def ensure_seeded(engine: Engine, today: date, now: datetime) -> None:
    from sqlalchemy import inspect, select, func

    if inspect(engine).has_table("patients"):
        with engine.connect() as conn:
            if conn.execute(select(func.count()).select_from(db.patients)).scalar():
                return
    reset(engine, today, now)


if __name__ == "__main__":
    from dotenv import load_dotenv

    from .state import real_now_ist

    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="wipe and reseed")
    args = ap.parse_args()
    url = os.getenv("TWIN_DB_URL", "sqlite:///./twin.db")
    eng = db.make_engine(url)
    now = real_now_ist()
    if args.reset:
        print(reset(eng, now.date(), now))
    else:
        ensure_seeded(eng, now.date(), now)
        print("seeded (or already seeded)")
