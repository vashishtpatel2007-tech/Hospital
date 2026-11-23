# Interface contracts (frozen in Stage 0)

These are the seams between parts of Sahay. Lanes can build in parallel against them. Change them only by editing this file first and telling the team.

All times are **India Standard Time (IST), naive ISO-8601** (`2026-11-24T10:30:00`). All money is **INR**.

---

## 1. Hospital Twin API (the fake hospital system) — `http://localhost:8001`

FastAPI; live docs at `/docs`. Resources are **FHIR-shaped** (same field names as FHIR R4 where one exists) so a real hospital system can be swapped in later behind the same adapter.

Errors always look like:

```json
{"error": "slot_unavailable", "message": "Slot S-123 is already held or booked"}
```

| Code | Status | Meaning |
|---|---|---|
| `not_found` | 404 | Unknown id |
| `validation_error` | 422 | Bad input |
| `slot_unavailable` | 409 | Slot already held/booked, or in the past |
| `hold_expired` | 409 | Hold's 120 s are up |
| `hold_used` | 409 | Hold already turned into an appointment |
| `slot_taken` | 409 | Someone else booked the held slot (race / chaos) |
| `invalid_state` | 409 | E.g. cancelling a cancelled appointment |
| `his_unavailable` | 503 | Hospital system down (chaos switch) |
| `internal_error` | 500 | Random failure (chaos switch) |

### Read

| Method & path | Returns |
|---|---|
| `GET /health` | `{status, now}` (never affected by chaos) |
| `GET /departments` | `HealthcareService[]` |
| `GET /practitioners?department_id&language&gender&name` | `Practitioner[]` |
| `GET /practitioners/{id}` | `Practitioner` |
| `GET /slots?practitioner_id&department_id&date_from&date_to&part_of_day&language&gender&limit` | `Slot[]`, free and future only, earliest first. `part_of_day` = `morning` (<12:00) / `afternoon` (12–16) / `evening` (≥16:00) |
| `GET /patients?phone&name&birth_date` | `{candidates: [{patient, score, matched_on[]}]}` — fuzzy name match (Laxmi ≈ Lakshmi), exact phone/DOB |
| `GET /patients/{id}` | `Patient` |
| `GET /patients/{id}/appointments` | `Appointment[]` |
| `GET /patients/{id}/reports` | `DiagnosticReport[]` (status only, never results) |
| `GET /patients/{id}/invoices` | `Invoice[]` |
| `GET /appointments/{id}` | `Appointment` |
| `GET /appointments/{id}/queue` | `{token, status, now_serving, ahead, est_wait_minutes, doctor_delay_minutes}` |
| `GET /waitlist?patient_id` | `WaitlistEntry[]` |

### Write

| Method & path | Body | Result |
|---|---|---|
| `POST /patients` | `{given, family, gender, birth_date, phone, postal_code?, language?, abha?}` | 201 `Patient` (the twin does **not** dedupe — the Journey Engine does) |
| `POST /slots/{slot_id}/hold` | `{idempotency_key, ttl_seconds?=120}` | 201 `Hold {id, slot_id, expires_at, status}`. Same key → same hold. |
| `DELETE /holds/{hold_id}` | — | 200, slot free again |
| `POST /appointments` | `{hold_id, patient_id, reason?}` | 201 `Appointment`. Same hold + same patient again → 200, the same appointment (a retry after a timeout never double-books) |
| `POST /appointments/{id}/reschedule` | `{hold_id}` | 200 `Appointment` (same id, new time; old slot freed). Same hold again → the same result |
| `POST /appointments/{id}/cancel` | `{reason}` | 200 `Appointment` (slot freed) |
| `POST /appointments/{id}/arrive` | — | 200 `{token, …queue}` (only on the appointment day) |
| `POST /practitioners/{id}/delay` | `{minutes, date?}` | 200 — "doctor running late" |
| `POST /waitlist` | `{patient_id, practitioner_id? or department_id?, date_from, date_to}` | 201 `WaitlistEntry` |

### Admin (never affected by chaos)

| Method & path | Body | Purpose |
|---|---|---|
| `GET/POST /admin/chaos` | `{latency_ms?, error_rate?, his_down?, steal_held_slot?}` | Failure switches for demos and tests |
| `GET/POST /admin/clock` | `{advance_minutes?}` or `{reset: true}` | Demo clock (fast-forward reminders, hold expiry, queue) |
| `POST /admin/reset` | — | Wipe and reseed; chaos off; clock reset |

### Seed data (deterministic)

- 12 departments, 30 doctors (OPD days/hours, languages, fee), slots for the next 14 days (~35% already booked)
- 300 patients `P-1001…P-1300`, including near-duplicate pairs (Laxmi/Lakshmi, Mohammed/Mohammad…) and families sharing one phone (caregiver calls)
- Demo personas: `P-1001` Ramesh Sharma (58, upcoming Cardiology visit with Dr. Suresh Rao), `P-1002` Lakshmi Narayan (72, Kannada, elderly mode)
- Reports and invoices with mixed statuses, including cashless pre-authorisation states

---

## 2. Journey Engine (built in Stage 2) — `http://localhost:8002`

In-process: `JourneyEngine.call(session_id, tool, args, actor="ai")`. Over HTTP: `POST /sessions/{id}/tools/{tool}` with `{"args": {...}, "actor": "ai"}`. `actor` is set by the agent process, never by the model: `sentinel` for the deterministic safety actions (red-flag escalation, the human exit), `system` for hang-ups and failure callbacks.

Every call returns:

```json
{"ok": true, "tool": "hold_slot", "data": {...}, "error": null,
 "journey": {"id": "J-…", "state": "SLOT_HELD", "urgency": "ROUTINE", "escalated": false},
 "session": {"id": "C-…", "level": "MEDIUM", "relation": "self", "ended": false},
 "allowed_tools": ["book_appointment", "release_hold", "…"]}
```

Refusals come back as `ok: false` with `error.code`: `not_allowed_in_state`, `verification_required` (+ `needed_level`), `escalated_frozen`, `confirmation_required`, `not_offered`, `locked`, `verification_failed`, `ambiguous_identity`, `otp_incorrect` / `otp_expired` / `otp_locked` / `otp_limit` / `no_otp_pending`, `proxy_not_authorised`, `consent_required`, `hold_expired`, `slot_taken`, `slot_unavailable`, `invalid_state`, `system_unavailable` (+ `callback_task_id`), `invalid_input`, `not_found`, `unknown_tool`, `unknown_session`, `session_ended`, `registration_lost` (the server restarted between register and code: register again), `internal_error` (a bug: apologise, offer transfer). Added by the agent before the engine: `confirmation_required` (no clear yes), `use_callback` (escalate below URGENT), `interrupted` (the caller spoke before the tool ran).

`call()` never raises. Arguments are checked against each tool's signature first: unknown, missing or wrongly typed arguments are `invalid_input` (numbers are accepted for text fields, digit strings for whole numbers; yes/no fields must be a real `true`). Ids that go into hospital-system URLs must look like ids (`[A-Za-z0-9_-]`, at most 64), so nothing like `../admin/reset` gets through.

| Tool | Allowed in | Needs |
|---|---|---|
| `lookup_caller`, `set_caller_relation`, `verify_identity`, `send_otp`, `check_otp` | any open state (in DEDUPE_REVIEW, verifying moves the call to that patient's own journey; the review stays for staff) | — |
| `register_patient` | CONTACT, INFO_ONLY, IDENTIFYING, REGISTERING | `caller_confirmed: true` |
| `list_departments`, `find_doctors` | any open state | — |
| `start_booking` | VERIFIED, FOLLOW_UP | MEDIUM |
| `get_slots` | INTAKE, RESCHEDULED, SLOT_SEARCH, WAITLISTED | MEDIUM |
| `hold_slot` (offered slots only) | SLOT_SEARCH | MEDIUM |
| `release_hold` | SLOT_HELD | MEDIUM |
| `book_appointment` | SLOT_HELD | MEDIUM + `patient_confirmed: true` |
| `join_waitlist` | SLOT_SEARCH | MEDIUM |
| `list_appointments`, `get_queue_status` | any open state | MEDIUM |
| `start_reschedule`, `cancel_appointment` (+ `patient_confirmed`) | VERIFIED, BOOKED, CONFIRMED, PREPARED | MEDIUM |
| `get_report_status`, `get_bill_status` | any open state | STRONG, and not a proxy caller |
| `create_callback`, `escalate`, `transfer_to_human`, `end_call` | any (also while escalated) | — |

While a journey is **escalated**, only the last row works until a named human resolves it. Urgency only goes up; lowering needs `actor="human"` + `staff_id`.

Rules about who the journey belongs to:

- **Resume:** a new contact resumes the open journey this phone's last call ended on.
- **Verify picks the journey:** `verify_identity` keeps the call's journey if it is that patient's; otherwise it continues that patient's open journey (same phone first, then any phone), adopts an empty one, or starts one. Each move is logged as `session_moved`.
- **Verification is per patient:** switching patient drops the call to MEDIUM and voids any pending code. STRONG needs a code sent to *that patient's registered number*.
- **Changing the caller's role resets verification:** `set_caller_relation` after verification (e.g. a proxy saying "actually it's me") means verifying again.
- **A call outlives its journey:** if the journey closes mid-call (a cancellation), the next tool, `escalate` included, runs on a fresh journey (already VERIFIED if the caller was). `allowed_tools` shows what that fresh journey would allow.
- **Registration review:** same DOB + similar name, *or* same phone + same name with a different DOB (a misheard date), goes to staff review instead of creating a record.

Every route (`{id}` is a session `C-…`, journey `J-…` or task `T-…` id):

| Route | What |
|---|---|
| `GET /health` | `{"status": "ok", "contract": 3}`; the agent refuses a server with an older contract (restart `make journey`) |
| `GET /tools` | every tool: description, states, needed level, allowed while escalated |
| `POST /sessions` | `{"phone": "+91…" or null, "channel": "voice"}` → session id, journey id, resumed?, caller known?, first names, allowed tools |
| `GET /sessions/{id}` | where the call stands now: `journey`, `session`, `allowed_tools` (the agent reads this every turn, so staff changes show up) |
| `GET /sessions/{id}/tools` | `{"allowed_tools": [...]}` |
| `POST /sessions/{id}/tools/{tool}` | run a tool: `{"args": {...}, "actor": "ai" \| "sentinel" \| "system"}` |
| `POST /sessions/{id}/safety` | log a Safety Sentinel event (below) |
| `GET /journeys/{id}` | the journey row (no phone hash) |
| `GET /journeys/{id}/events` | the hash-chained event log |
| `GET /journeys/{id}/verify` | re-check the chain: `{"ok": true, ...}` |
| `GET /tasks` | open tasks (`?status=`, `?journey_id=`): callbacks, transfers, escalations, dedupe reviews, pending syncs |
| `POST /journeys/{id}/urgency` | staff lower or raise urgency (`staff_id` + `reason` required) |
| `POST /journeys/{id}/resolve-escalation` | a named human takes the call back out of ESCALATED |
| `POST /tasks/{id}/dedupe` | staff decide a possible duplicate registration |
| `POST /tasks/{id}/resolve` | staff close a task |

Safety events: `POST /sessions/{id}/safety` with `{"kind": "red_flag", "data": {...}}` appends a `safety_event` (actor
`sentinel`) to the chain. Kinds: `red_flag`, `human_requested`, `recording_declined`, `injection_attempt`,
`reply_blocked` (category only, never the blocked words), `confirmation_blocked`, `llm_failure`,
`internal_text_dropped`. In-process: `JourneyEngine.record_safety()`.

## 3. The agent (built in Stage 3)

`agent.brain.Agent(port, phone=…, channel="voice"|"text", language="hi-en"|"en"|"hi"|"kn")`: one per call.
`start()` returns the opening line; `respond(text)` yields events as they happen (`Say`, `ToolUse`, `Safety`), so a
voice front end can speak the first sentence while the model is still writing; `turn(text)` collects them; `hangup()`.
The port is `LocalJourney(engine)` (tests, eval) or `HttpJourney(JOURNEY_URL)` (live calls: one engine process shared
with the dashboard).

Each caller turn, in order:

| Step | Decided by | What happens |
|---|---|---|
| L1 red flag (emergency/urgent phrase, English, Hinglish, Hindi, Kannada) | code (`safety/lexicon.py`) | `escalate` + `transfer_to_human` (actor `sentinel`), fixed script: 108/112, or Tele-MANAS 14416 + a person for self-harm. No model call. Later turns of an emergency repeat the 108 line. If the model itself escalates as EMERGENCY (a symptom the phrase list missed), the same fixed script and emergency transfer follow. |
| Human exit ("staff", "insaan se baat karao", "real person") | code | `transfer_to_human` in any state, fixed reply. |
| Recording declined | code | no more words in the trace; acknowledged once. |
| Prompt-injection phrases | code logs, model declines | `safety_event` `injection_attempt`. |
| Everything else | Claude (`AGENT_MODEL`), tools gated by the engine | every sentence through the L3 reply guard (`safety/guard.py`) before the caller hears it. |

Before each turn the agent re-reads `GET /sessions/{id}` (staff may have resolved an escalation or ended the call) and
repairs its history if the previous turn was cut short (caller talked over it): unanswered tool calls get their real
result, or `interrupted` if they never ran. A "don't record" is honoured first, even inside an emergency message.
Asking for staff again doesn't queue a second transfer.

The agent's own checks before a model tool call reaches the engine: `escalate` below URGENT → `use_callback` (it
would freeze booking for a mild symptom; `create_callback` SOON is the right tool); `book_appointment`, `cancel_appointment` and
`register_patient` with the confirmed flag need a clear yes in the caller's latest message (and a booking can't happen in
the same turn as the hold) → `confirmation_required`; `register_patient` with `phone: "CALLER_ID"` gets the real number
(the model never sees it); unknown tools → `unknown_tool`.

Model timing: no words or tool call after 1.5 s → one filler line ("Ek second…"); none after `AGENT_LLM_TIMEOUT` (6 s)
→ one retry; a second failure → `create_callback` and an honest line. A refusal is not retried. More than 8 tool rounds
in one turn → transfer to the front desk.

Trace: `logs/<session>.jsonl`, phone numbers, dates and every name learned on the call masked.

## 4. Event format (append-only log, hash-chained)

```json
{
  "journey_id": "J-…", "seq": 17, "ts": "2026-11-24T10:31:02",
  "type": "tool_called | state_changed | safety_flag | escalated | message | …",
  "actor": "ai | sentinel | human | system | patient",
  "payload": { "…": "personal data redacted before storage" },
  "prev_hash": "…", "hash": "sha256(prev_hash + canonical_json(event without hash))"
}
```

The dashboard and WhatsApp/reminder workers subscribe to this stream; nothing else writes to it.
