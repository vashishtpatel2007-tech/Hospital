"""Journey Engine over HTTP (for the dashboard, WhatsApp worker and anything not in-process).

Run:  make journey   (http://localhost:8002/docs; needs the Twin on :8001)
"""

from __future__ import annotations

from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .engine import TOOL_SPECS, JourneyEngine, ToolRefused

load_dotenv()

CONTRACT_VERSION = 3  # 3 = Stage 3: actor on tool calls, /sessions/{id}/safety, GET /sessions/{id}


class SessionIn(BaseModel):
    phone: str | None = Field(default=None, description="Caller ID; omit if withheld")
    channel: str = "voice"


class ToolIn(BaseModel):
    args: dict[str, Any] = Field(default_factory=dict)
    # Set by the agent process, never by the model: "sentinel" for deterministic safety actions.
    actor: Literal["ai", "sentinel", "system"] = "ai"


class SafetyIn(BaseModel):
    kind: str
    data: dict[str, Any] = Field(default_factory=dict)
    actor: Literal["sentinel", "system", "ai"] = "sentinel"


class UrgencyIn(BaseModel):
    urgency: str
    staff_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ResolveEscalationIn(BaseModel):
    staff_id: str = Field(min_length=1)
    note: str = Field(min_length=1)
    urgency: str | None = None


class DedupeIn(BaseModel):
    staff_id: str = Field(min_length=1)
    decision: str
    patient_ref: str | None = None


class NoteIn(BaseModel):
    staff_id: str = Field(min_length=1)
    note: str = Field(min_length=1)


def create_app(engine: JourneyEngine | None = None) -> FastAPI:
    eng = engine or JourneyEngine()
    app = FastAPI(title="Sahay Journey Engine", version="1.0",
                  description="State machine + verification + guarded tools + hash-chained audit log.")
    app.state.engine = eng

    @app.exception_handler(ToolRefused)
    async def _refused(_req, exc: ToolRefused):
        status = 422 if exc.code in ("invalid_input", "staff_required") else 409
        return JSONResponse(status_code=status, content={"error": exc.code, "message": exc.message, **exc.extra})

    @app.exception_handler(KeyError)
    async def _missing(_req, exc: KeyError):
        return JSONResponse(status_code=404, content={"error": "not_found", "message": f"{exc.args[0]} not found"})

    @app.get("/health")
    def health():
        # contract: bumped when the agent needs a new route; the agent refuses an older server
        return {"status": "ok", "contract": CONTRACT_VERSION}

    @app.get("/tools")
    def tools():
        return {name: {"description": s.description, "states": sorted(s.states), "min_level": s.min_level.name,
                       "allowed_when_escalated": s.allowed_when_escalated} for name, s in TOOL_SPECS.items()}

    @app.post("/sessions", status_code=201)
    def open_session(body: SessionIn):
        return eng.open_session(body.phone, body.channel)

    @app.get("/sessions/{session_id}")
    def session(session_id: str):
        return eng.session_snapshot(session_id)

    @app.get("/sessions/{session_id}/tools")
    def allowed(session_id: str):
        return {"allowed_tools": eng.allowed_tools(session_id)}

    @app.post("/sessions/{session_id}/tools/{tool}")
    def call_tool(session_id: str, tool: str, body: ToolIn):
        return eng.call(session_id, tool, body.args, actor=body.actor)

    @app.post("/sessions/{session_id}/safety", status_code=201)
    def safety(session_id: str, body: SafetyIn):
        eng.session(session_id)  # unknown session -> 404
        return eng.record_safety(session_id, body.kind, body.data, body.actor)

    @app.get("/journeys/{journey_id}")
    def journey(journey_id: str):
        j = eng.journey(journey_id)
        return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in j.items() if k != "phone_hash"}

    @app.get("/journeys/{journey_id}/events")
    def events(journey_id: str):
        eng.journey(journey_id)  # 404 for an unknown journey, not an empty list
        return eng.events(journey_id)

    @app.get("/journeys/{journey_id}/verify")
    def verify(journey_id: str):
        eng.journey(journey_id)
        return eng.verify_chain(journey_id).as_dict()

    @app.get("/tasks")
    def tasks(status: str | None = "open", journey_id: str | None = None):
        return eng.tasks(journey_id, status)

    # -- human actions (the dashboard calls these; every one is logged with the staff id)
    @app.post("/journeys/{journey_id}/urgency")
    def set_urgency(journey_id: str, body: UrgencyIn):
        return eng.set_urgency(journey_id, body.urgency, "human", body.reason, body.staff_id)

    @app.post("/journeys/{journey_id}/resolve-escalation")
    def resolve_escalation(journey_id: str, body: ResolveEscalationIn):
        return eng.human_resolve_escalation(journey_id, body.staff_id, body.note, body.urgency)

    @app.post("/tasks/{task_id}/dedupe")
    def dedupe(task_id: str, body: DedupeIn):
        return eng.human_resolve_dedupe(task_id, body.staff_id, body.decision, body.patient_ref)

    @app.post("/tasks/{task_id}/resolve")
    def resolve(task_id: str, body: NoteIn):
        return eng.resolve_task(task_id, body.staff_id, body.note)

    return app

# Run with:  uvicorn journey.api:create_app --factory --port 8002
