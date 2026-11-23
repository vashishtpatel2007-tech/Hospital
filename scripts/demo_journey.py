"""Hand check for Stage 2: drive a whole journey over HTTP, the way the agent will.

    make twin      (terminal 1)
    make journey   (terminal 2)
    python scripts/demo_journey.py

It RESETS the twin's data first. To leave a running twin alone, point it at other servers:
    TWIN_URL=http://localhost:9001 JOURNEY_URL=http://localhost:9002 python scripts/demo_journey.py
"""

from __future__ import annotations

import os
import sys

import httpx

J = httpx.Client(base_url=os.getenv("JOURNEY_URL", "http://localhost:8002"), timeout=10)
T = httpx.Client(base_url=os.getenv("TWIN_URL", "http://localhost:8001"), timeout=10)


def tool(sid: str, tool_name: str, **args) -> dict:
    r = J.post(f"/sessions/{sid}/tools/{tool_name}", json={"args": args}).json()
    status = "OK " if r["ok"] else f"NO ({r['error']['code']})"
    print(f"  {tool_name:<20} {status:<28} state={r['journey']['state']:<12} level={r['session']['level']}")
    return r


def main() -> int:
    T.post("/admin/reset")
    ramesh_dob = T.get("/patients/P-1001").json()["birthDate"]

    print("\nCall 1 - Ramesh books a cardiology visit")
    s = J.post("/sessions", json={"phone": "98450 12345", "channel": "web"}).json()
    sid, jid = s["session_id"], s["journey_id"]
    print(f"  caller known={s['caller_known']} greet as {s['greeting_names']} level={s['level']}")
    tool(sid, "book_appointment", patient_confirmed=True)  # too early: refused
    tool(sid, "lookup_caller")
    tool(sid, "verify_identity", name="Ramesh Sharma", birth_date=ramesh_dob)
    tool(sid, "start_booking", reason="BP follow-up", department_id="CARD")
    slots = tool(sid, "get_slots", part_of_day="morning")["data"]["slots"]
    for x in slots[:3]:
        print(f"     offered: {x['doctor']} - {x['when']}")
    held = tool(sid, "hold_slot", slot_id=slots[1]["slot_id"])
    print(f"     read-back: \"{held['data']['read_back']}\"")
    tool(sid, "book_appointment")  # no explicit yes yet: refused
    booked = tool(sid, "book_appointment", patient_confirmed=True)
    print(f"     booked {booked['data']['appointment_id']}: {booked['data']['confirmed']}")
    tool(sid, "end_call", summary="booked")

    print("\nCall 2 - same number, mentions chest pain")
    s2 = J.post("/sessions", json={"phone": "+919845012345"}).json()
    print(f"  resumed={s2['resumed']} same journey={s2['journey_id'] == jid} state={s2['state']} level={s2['level']}")
    tool(s2["session_id"], "escalate", urgency="EMERGENCY", reason="chest pain radiating to left arm",
         verbatim="seene mein dard hai, left haath tak")
    tool(s2["session_id"], "start_reschedule", appointment_id=booked["data"]["appointment_id"])  # frozen
    tool(s2["session_id"], "transfer_to_human", reason="emergency")
    lowered = J.post(f"/journeys/{jid}/urgency", json={"urgency": "ROUTINE", "staff_id": "", "reason": "x"})
    print(f"  lower urgency without staff id -> HTTP {lowered.status_code}")

    tasks = [t for t in J.get("/tasks").json() if t["journey_id"] == jid]
    print("\nOpen tasks for staff:")
    for t in tasks:
        print(f"  {t['kind']:<12} urgency={t['urgency']} reason={t['reason']}")

    events = J.get(f"/journeys/{jid}/events").json()
    chain = J.get(f"/journeys/{jid}/verify").json()
    print(f"\nAudit log: {len(events)} events, chain ok={chain['ok']}")
    for e in events[-6:]:
        print(f"  #{e['seq']:<3} {e['actor']:<7} {e['type']}")
    return 0 if chain["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
