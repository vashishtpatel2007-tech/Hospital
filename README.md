# Sahay — Autonomous Hospital Receptionist

CODE RED '26 · Track 6 · Cassette Labs PS-01. A voice-first AI front desk for Indian hospitals.

Build plan: 12 stages over 3 days, each with a test gate. The plan documents (BLUEPRINT.md, BUILD_PLAN.md,
Sahay_Final_Build_Plan.pdf) are kept outside this repo, in `~/Desktop/L1R6/hospital-receptionist/`; the interfaces
between all parts are in `docs/contracts.md`.

## Quick start

```bash
make setup        # Python 3.12 env + all packages (pinned in requirements.lock); creates .env
make check-keys   # one tiny call per service: ✅ / ❌ / ⚪ (not set)
make up           # Postgres + pgvector in Docker (needs Docker Desktop)
make twin         # Hospital Twin on http://localhost:8001/docs
make journey      # Journey Engine on http://localhost:8002/docs (needs the twin)
make chat         # type to Sahay (PHONE=+919845012345 to call as Ramesh); needs ANTHROPIC_API_KEY
make scenarios    # the text scenario suite, 3 runs each; needs ANTHROPIC_API_KEY
make test         # all tests (no keys needed)
```

Fill `.env` yourself from `.env.example`. Never commit it, never paste keys into chat. `make check-keys` also checks the
`AGENT_*` settings (a typo there fails at start, not mid-call). The tests never read `.env` and never call a paid API.

Dev SMS: until WhatsApp (Stage 9), one-time codes print as `[DEV OTP] …` on the journey server's console (or in the
chat window when `make chat` runs the engine itself). After changing engine or API code, restart `make journey`:
the agent refuses a journey server running older code.

## What's built

| Stage | Status | Proof |
|---|---|---|
| 0 · Setup | Done, except Postgres (Docker not installed; not needed while the store is SQLite) and keys (not added yet) | `make check-keys`; `tests/test_stage0_setup.py` → 16 tests |
| 1 · Hospital Twin | Done | `make test-twin` → 56 tests |
| 2 · Journey Engine | Done (SQLite store; switch `JOURNEY_DB_URL` to Postgres when Docker is in) | `make test-journey` → 114 tests; `python scripts/demo_journey.py` |
| 0–3 · Pipelines | Done | `tests/test_pipeline_http.py` → the 3 required journeys + hospital system outage over real HTTP; `tests/test_pipeline_live.py` → twin, journey server and agent as three separate processes (the Stage 2 demo, a new patient registering with the console OTP and booking, emergency, human exit) |
| 3 · Text agent + safety core | Built and tested offline with a stand-in model; live runs wait for the Anthropic key | `make test-safety` → 248 tests (46 emergency + 7 urgent phrases fire, 40 normal sentences don't, 29 unsafe replies blocked, 24 normal replies pass); `make test-agent` → 75 tests, including the agent against the real anthropic SDK with a fake API server; `make scenarios` → 10 scenarios × 3 runs (needs the key) |

`make test` runs all 517 (about 26 s), with no keys and without touching your running servers.

### Stage 3 in one paragraph

Every caller message first goes through deterministic checks: an emergency phrase (English, Hinglish, Hindi, Kannada)
escalates and transfers with a fixed 108/112 script (Tele-MANAS 14416 for self-harm) without asking the model;
"staff" / "insaan se baat karao" transfers in any state; "don't record" turns the transcript off. Everything else goes
to Claude (`AGENT_MODEL`, default `claude-haiku-5-5`) with a state note listing what it may do right now. The Journey
Engine checks every tool call, the agent refuses a booking without a clear yes in the caller's latest message, and every
sentence passes the reply guard (no diagnosis, medicines, results or promises) before the caller hears it. A slow model
gets a filler line at 1.5 s, one retry, then an honest "staff will call you back" with a real callback task. Details:
`docs/contracts.md` §3.

## Layout

```
twin/        fake hospital system (FastAPI, FHIR-shaped, chaos switches, demo clock)
core/        shared: database setup, IST time, Indian-name fuzzy matching
journey/     state machine, verification ladder, guarded tools, hash-chained event log, redaction
agent/       Claude conversation loop (brain.py), tools from the engine, prompt + state note, trace, textchat; voice in Stage 4
safety/      L1 emergency / human / consent phrases, L3 reply guard, fixed scripts in 4 languages; L2 classifier in Stage 5
knowledge/   Stage 6: approved-only knowledge base with citations
dashboard/   Stage 7+: Mission Control (Next.js)
eval/        scenario runner (run.py) and scenarios/*.yaml: 10 now, 60 by Stage 10
docs/        contracts.md (interfaces between all parts), submission pack later
scripts/     check_keys.py
tests/       pytest suites per stage
```

## Hospital Twin cheat-sheet

- Demo personas: `P-1001` Ramesh Sharma (Cardiology with Dr. Suresh Rao next Thursday), `P-1002` Lakshmi Narayan (72, Kannada, PM-JAY pre-auth pending), `P-1003`/`P-1004` daughter and father sharing one phone.
- Failure switches: `POST /admin/chaos {"his_down": true}` · `{"latency_ms": 3000}` · `{"error_rate": 0.3}` · `{"steal_held_slot": true}`
- Demo clock: `POST /admin/clock {"advance_minutes": 1440}` · `{"reset": true}`
- Fresh data: `POST /admin/reset` or `make twin-reset`
# Hospital
