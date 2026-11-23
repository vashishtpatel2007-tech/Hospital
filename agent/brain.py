"""The conversation loop: one Agent per call (BLUEPRINT §4.2, BUILD_PLAN Stage 3).

Every caller turn goes through, in order:

  1. L1 red flags (deterministic): emergency or urgent phrase -> escalate + transfer + fixed
     script. No model involved, nothing to get wrong.
  2. A call already escalated as an emergency: repeat the 108 / Tele-MANAS line.
  3. The human exit: "staff", "insaan se baat karao" -> transfer_to_human, in every state.
  4. Recording declined -> recording off (nothing more is written to the trace).
  5. Prompt-injection phrases are logged (the model is told to decline and carry on).
  6. Otherwise Claude: state-scoped guidance, tools checked by the Journey Engine, every
     sentence through the L3 reply guard before the caller hears it.

The agent adds two checks of its own before a tool reaches the engine: a booking, cancellation or
registration needs a clear yes in the caller's latest message (and a booking can't happen in the
same turn as the hold), and CALLER_ID is swapped for the real number the model never sees.

respond() yields events as they happen (Say, ToolUse, Safety), so the voice stage can speak
the first sentence while the model is still writing the second.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from journey.engine import TOOL_SPECS
from safety import lexicon, scripts
from safety.guard import SentenceSplitter, check_sentence

from .llm import LLMConfig, LLMFailure, default_client, stream_reply
from .port import JourneyPort
from .prompt import SYSTEM_PROMPT, state_note
from .tools import CALLER_ID, CONFIRM_FLAGS, build_tools
from .trace import Trace

log = logging.getLogger("sahay.agent")

MAX_ROUNDS = 8  # model calls per caller turn (each tool round is one)
_TOOLS = build_tools()


@dataclass
class Say:
    text: str
    kind: str = "reply"  # reply | greeting | script | filler | replacement


@dataclass
class ToolUse:
    name: str
    args: dict
    ok: bool
    error: str | None
    actor: str  # ai | sentinel | system
    data: Any = None  # the engine's result data (for the eval runner and debugging)


@dataclass
class Safety:
    kind: str
    detail: dict = field(default_factory=dict)


Event = Say | ToolUse | Safety


@dataclass
class TurnResult:
    says: list[Say]
    tools: list[ToolUse]
    safety: list[Safety]
    first_say_ms: int | None

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.says)


def _urgency_name(u: Any) -> str:
    names = ["ROUTINE", "SOON", "URGENT", "EMERGENCY"]
    return names[u] if isinstance(u, int) else str(u)


class Agent:
    def __init__(self, port: JourneyPort, *, phone: str | None, channel: str = "voice", language: str = "hi-en",
                 client: Any = None, config: LLMConfig | None = None, trace_dir: str | None = "logs") -> None:
        self.port = port
        self.phone = phone
        self.channel = channel
        self.lang = language
        self.client = client
        self.cfg = config or LLMConfig()
        self.trace_dir = trace_dir
        self.trace: Trace | None = None

        self.session_id: str | None = None
        self.journey: dict = {}
        self.session: dict = {}
        self.allowed: list[str] = []
        self.caller_known = False
        self.greeting_names: list[str] = []
        self.greeting_text: str | None = None
        self.resumed = False

        self.messages: list[dict] = []
        self.model_events: list[str] = []
        self.turn_index = 0
        self.hold_turn: int | None = None
        self.last_caller_text = ""
        self.recording = True
        self.med_callback_done = False
        self.desks_requested: set[str] = set()
        self.emergency_route: str | None = None
        self.known_names: set[str] = set()
        self._greeting_noted = False
        self._results_by_id: dict[str, dict] = {}

        self.transcript: list[dict] = []  # in memory only, for the eval runner
        self.tool_log: list[ToolUse] = []
        self.safety_log: list[Safety] = []
        self.llm_stats: list[dict] = []

    # ------------------------------------------------------------------ lifecycle

    @property
    def ended(self) -> bool:
        return bool(self.session.get("ended"))

    @property
    def journey_id(self) -> str | None:
        return self.journey.get("id")

    def start(self) -> list[Say]:
        """Open the session and say the opening line (AI disclosure + recording + the human exit)."""
        s = self.port.open_session(self.phone, self.channel)
        self.session_id = s["session_id"]
        self.session = {"id": s["session_id"], "level": s["level"], "relation": "self", "ended": False}
        self.caller_known = s["caller_known"]
        self.resumed = bool(s.get("resumed"))
        self.greeting_names = s.get("greeting_names") or []
        self.allowed = s["allowed_tools"]
        j = self.port.journey(s["journey_id"])
        self.journey = {"id": j["id"], "state": j["state"], "urgency": _urgency_name(j["urgency"]),
                        "escalated": bool(j["escalated"])}
        self.known_names.update(self.greeting_names)
        if self.trace_dir:
            self.trace = Trace(self.trace_dir, self.session_id)
            self.trace.add_names(self.known_names)
            self.trace.write("call_started", channel=self.channel, resumed=s["resumed"], state=self.journey["state"],
                             caller_known=self.caller_known, hospital_system_down=s.get("hospital_system_down"))
        if s.get("hospital_system_down"):
            self.model_events.append("The hospital system is not responding right now: you can't look anything up or book. "
                                     "Say so honestly and offer a callback (create_callback) or a transfer.")
        if s.get("resumed"):
            self.model_events.append(f"This caller has an open journey from an earlier call (state {self.journey['state']}). "
                                     "They must verify again on this call before you discuss it.")
        self.greeting_text = scripts.greeting(self.lang, self.channel)
        say = Say(self.greeting_text, "greeting")
        self._said(say)
        return [say]

    def hangup(self) -> None:
        """The caller hung up (or the line dropped)."""
        if self.session_id and not self.ended:
            r = self.port.call(self.session_id, "end_call", {"summary": "caller hung up"}, actor="system")
            self._update_state(r)
        if self.trace:
            self.trace.write("call_ended", state=self.journey.get("state"))

    # ------------------------------------------------------------------ one caller turn

    def turn(self, caller_text: str) -> TurnResult:
        t0 = time.monotonic()
        says, tools, safety, first = [], [], [], None
        for ev in self.respond(caller_text):
            if isinstance(ev, Say):
                if first is None:
                    first = int((time.monotonic() - t0) * 1000)
                says.append(ev)
            elif isinstance(ev, ToolUse):
                tools.append(ev)
            else:
                safety.append(ev)
        return TurnResult(says, tools, safety, first)

    def respond(self, caller_text: str) -> Iterator[Event]:
        if self.session_id is None:
            raise RuntimeError("call start() first")
        if self.ended:
            return
        t0 = time.monotonic()
        self._refresh()
        if self.ended:  # ended from outside (staff, or a hang-up on another channel)
            return
        self._repair_history()
        self.turn_index += 1
        text = (caller_text or "").strip()
        self.last_caller_text = text
        guess = lexicon.guess_language(text)
        if guess != "en" or len(text.split()) >= 3:
            self.lang = guess
        self.transcript.append({"role": "caller", "text": text})
        if self.trace:
            self.trace.write("caller", text=text, lang=self.lang)

        first_say: int | None = None
        for ev in self._route(text):
            if isinstance(ev, Say) and first_say is None:
                first_say = int((time.monotonic() - t0) * 1000)
            yield ev
        if self.trace:
            self.trace.write("turn", n=self.turn_index, first_say_ms=first_say, total_ms=int((time.monotonic() - t0) * 1000),
                             state=self.journey.get("state"), level=self.session.get("level"),
                             escalated=self.journey.get("escalated"))

    def _route(self, text: str) -> Iterator[Event]:
        # 0. a "don't record" counts even inside an emergency message, and injections are always logged
        declined = self.recording and lexicon.declines_recording(text)
        if declined:
            self.recording = False
            if self.trace:
                self.trace.recording = False
            yield self._safety("recording_declined", {})
        if lexicon.injection_attempt(text):
            yield self._safety("injection_attempt", {})
        # 1. red flags
        flag = lexicon.top_flag(text)
        if flag:
            yield self._safety("red_flag", {"flag": flag.id, "tier": flag.tier, "route": flag.route, "matched": flag.matched})
            if declined:
                self.model_events.append("The caller also declined recording; it is off.")
            yield from self._sentinel_escalation(flag, text)
            return
        # 2. an emergency call stays on the emergency script until a human takes over
        if self.journey.get("escalated") and self.journey.get("urgency") == "EMERGENCY" and self.emergency_route:
            line = scripts.pick(scripts.SELF_HARM_REPEAT if self.emergency_route == "TELE_MANAS_AND_HUMAN"
                                else scripts.EMERGENCY_REPEAT, self.lang)
            yield from self._scripted(text, line)
            return
        # 3. the human exit
        if lexicon.wants_human(text):
            yield self._safety("human_requested", {})
            yield from self._transfer(text, "Caller asked for a person", "front_desk")
            return
        # 4. recording consent (already switched off above)
        extra = ""
        if declined:
            ack = scripts.pick(scripts.RECORDING_OFF, self.lang)
            if len(text.split()) <= 6:
                yield from self._scripted(text, f"{ack} {scripts.pick(scripts.HOW_CAN_I_HELP, self.lang)}")
                return
            say = Say(ack, "script")
            self._said(say)
            yield say
            extra = f'The caller declined recording; you already told them: "{ack}". Now answer the rest of their message.'
        # 5. injection attempts were logged above; the model declines them itself
        if extra:
            self.model_events.append(extra)
        yield from self._llm_turn(text)

    # ------------------------------------------------------------------ deterministic paths

    def _sentinel_escalation(self, flag: lexicon.RedFlag, text: str) -> Iterator[Event]:
        what = flag.id.replace("_", " ")
        reason = f"Safety sentinel: caller reported {what}"
        r = self._engine("escalate", {"urgency": flag.tier, "reason": reason, "verbatim": text}, actor="sentinel")
        yield r
        desk = "emergency" if flag.route == "EMERGENCY_DESK" else "nurse"
        if desk not in self.desks_requested:
            t = self._engine("transfer_to_human", {"reason": reason, "desk": desk}, actor="sentinel")
            yield t
            if t.ok:
                self.desks_requested.add(desk)
        if not r.ok and self.session_id and not self.ended:  # never a silent failure: someone must call back
            yield self._engine("create_callback", {"reason": reason, "urgency": flag.tier}, actor="sentinel")
        if flag.tier == "EMERGENCY":
            self.emergency_route = flag.route
            table = scripts.SELF_HARM if flag.route == "TELE_MANAS_AND_HUMAN" else scripts.EMERGENCY
        else:
            table = scripts.URGENT
        self.model_events.append(f"The safety system escalated this call ({flag.tier}, {what}) and asked the {desk} desk to "
                                 "take over. Booking is frozen.")
        yield from self._scripted(text, scripts.pick(table, self.lang))

    def _model_emergency(self) -> Iterator[Event]:
        if "emergency" not in self.desks_requested:
            t = self._engine("transfer_to_human", {"reason": "AI escalated an emergency", "desk": "emergency"},
                             actor="sentinel")
            yield t
            if t.ok:
                self.desks_requested.add("emergency")
        self.emergency_route = "EMERGENCY_DESK"
        line = scripts.pick(scripts.EMERGENCY, self.lang)
        self.messages.append({"role": "assistant", "content": line})
        say = Say(line, "script")
        self._said(say)
        yield say

    def _transfer(self, text: str, reason: str, desk: str) -> Iterator[Event]:
        if desk in self.desks_requested:  # asked again: the request is already with staff, don't queue a second one
            yield from self._scripted(text, scripts.pick(scripts.HUMAN, self.lang))
            return
        t = self._engine("transfer_to_human", {"reason": reason, "desk": desk}, actor="sentinel")
        yield t
        if t.ok:
            self.desks_requested.add(desk)
            self.model_events.append(f"The caller asked for a person: a transfer to the {desk.replace('_', ' ')} was requested.")
            yield from self._scripted(text, scripts.pick(scripts.HUMAN, self.lang))
            return
        cb = self._engine("create_callback", {"reason": reason}, actor="sentinel") if not self.ended else None
        if cb is not None:
            yield cb
        yield from self._scripted(text, scripts.pick(scripts.TRANSFER_FAILED, self.lang))

    def _scripted(self, caller_text: str, line: str) -> Iterator[Event]:
        """A fixed reply: said as-is, and recorded in the model's history so later turns make sense."""
        self._append_user(caller_text)
        self.messages.append({"role": "assistant", "content": line})
        say = Say(line, "script")
        self._said(say)
        yield say

    # ------------------------------------------------------------------ the model

    def _client(self):
        if self.client is None:
            self.client = default_client()
        return self.client

    def _llm_turn(self, text: str) -> Iterator[Event]:
        self._append_user(text)
        filler_used = False
        blocked: set[str] = set()
        for rnd in range(1, MAX_ROUNDS + 1):
            self._append_note(first_turn=not self._greeting_noted)
            self._greeting_noted = True
            final = None
            for attempt in (1, 2):
                spoken: list[str] = []
                splitter = SentenceSplitter()
                t0 = time.monotonic()
                try:
                    for ev in stream_reply(self._client(), self.cfg, system=SYSTEM_PROMPT, tools=_TOOLS,
                                           messages=self.messages, allow_filler=not filler_used):
                        if ev.kind == "filler":
                            filler_used = True
                            say = Say(scripts.pick(scripts.FILLER, self.lang), "filler")
                            self._said(say)
                            yield say
                        elif ev.kind == "text":
                            for sentence in splitter.feed(ev.text):
                                for out in self._guarded(sentence, blocked):
                                    if isinstance(out, Say):
                                        spoken.append(out.text)
                                    yield out
                        else:
                            final = ev.message
                            self._llm_stat(ev.message, ev.ttft_ms, t0, attempt)
                    for sentence in splitter.flush():
                        for out in self._guarded(sentence, blocked):
                            if isinstance(out, Say):
                                spoken.append(out.text)
                            yield out
                    if splitter.dropped:
                        yield self._safety("internal_text_dropped", {"chars": splitter.dropped})
                    break
                except LLMFailure as e:
                    yield self._safety("llm_failure", {"kind": e.kind, "attempt": attempt, "detail": e.detail[:120]})
                    if e.kind == "refusal":
                        yield from self._close_turn_with(scripts.pick(scripts.CANNOT_HELP, self.lang), spoken)
                        return
                    if attempt == 1 and not spoken:
                        continue  # one retry, as long as the caller hasn't heard half an answer
                    yield from self._llm_down(spoken)
                    return
            assert final is not None
            self.messages.append({"role": "assistant", "content": final.content})
            uses = [b for b in final.content if getattr(b, "type", None) == "tool_use"]
            if not uses:
                return
            results = []
            escalated_now = False
            for block in uses:
                result, use = self._run_model_tool(block)
                results.append(result)
                escalated_now |= use.name == "escalate" and use.ok
                yield use
            if self.ended:  # the model ended the call: keep the history valid, ask for nothing more
                self.messages.append({"role": "user", "content": results})
                return
            self.messages.append({"role": "user", "content": results})
            if escalated_now and self.journey.get("urgency") == "EMERGENCY" and not self.emergency_route:
                # The model spotted an emergency the phrase list missed. The wording is still never the model's.
                yield from self._model_emergency()
                return
        # too many rounds in one turn: hand over rather than loop
        yield self._safety("llm_failure", {"kind": "too_many_rounds"})
        yield from self._transfer_after_failure()

    def _refresh(self) -> None:
        """Pick up anything staff changed between turns (resolved escalation, urgency, dedupe decision)."""
        try:
            self._update_state(self.port.snapshot(self.session_id))
        except Exception:  # keep the last known state; the next tool result will correct it
            log.warning("could not refresh call state", exc_info=True)
            return
        if not self.journey.get("escalated"):
            self.emergency_route = None

    def _repair_history(self) -> None:
        """A turn cut short (the caller talked over the agent, so the generator was closed) can leave the
        history ending in a system note, or in tool calls without results. The API rejects both."""
        if not self.messages:
            return
        last = self.messages[-1]
        if last["role"] == "assistant" and isinstance(last["content"], list):
            ids = [b.id for b in last["content"] if getattr(b, "type", None) == "tool_use"]
            if ids:
                self.messages.append({"role": "user", "content": [
                    self._results_by_id.get(i) or {"type": "tool_result", "tool_use_id": i, "is_error": True,
                                                   "content": json.dumps({"ok": False, "error": {
                                                       "code": "interrupted", "message": "Not run: the caller spoke first."}})}
                    for i in ids]})
                self.messages.append({"role": "assistant", "content": "(interrupted)"})
                return
        if last["role"] == "system":
            self.messages.append({"role": "assistant", "content": "(interrupted)"})

    def _append_user(self, content: Any) -> None:
        if self.messages and self.messages[-1]["role"] == "system":  # keep system notes followed by an assistant turn
            self.messages.append({"role": "assistant", "content": "(no reply)"})
        self.messages.append({"role": "user", "content": content})

    def _append_note(self, first_turn: bool) -> None:
        note = state_note(now=self.port.now(), journey=self.journey, session=self.session, allowed_tools=self.allowed,
                          caller_known=self.caller_known, greeting_names=self.greeting_names, recording=self.recording,
                          events=self.model_events, first_turn=first_turn, greeting=self.greeting_text)
        self.model_events = []
        if self.cfg.system_messages_inline:
            self.messages.append({"role": "system", "content": note})
            return
        last = self.messages[-1]  # older models: the note rides along as a text block in the user turn
        blocks = last["content"] if isinstance(last["content"], list) else [{"type": "text", "text": last["content"]}]
        last["content"] = [*blocks, {"type": "text", "text": note}]

    def _guarded(self, sentence: str, blocked: set[str]) -> Iterator[Event]:
        res = check_sentence(sentence, self.lang)
        if res.ok:
            say = Say(sentence)
            self._said(say)
            yield say
            return
        yield self._safety("reply_blocked", {"category": res.category})
        self.model_events.append(f"The safety guard blocked part of your last reply ({res.category}); the caller did NOT "
                                 f'hear it and heard instead: "{res.text}". Do not repeat it.')
        if res.category == "medication" and not self.med_callback_done and not self.ended:
            self.med_callback_done = True
            yield self._engine("create_callback", {"reason": "Caller has a medicine question for a nurse",
                                                   "urgency": "SOON"}, actor="sentinel")
        if res.category not in blocked:
            blocked.add(res.category)
            say = Say(res.text, "replacement")
            self._said(say)
            yield say

    def _run_model_tool(self, block: Any) -> tuple[dict, ToolUse]:
        name, args = block.name, block.input
        if not isinstance(args, dict):
            args = {}
        refusal: dict | None = None
        if name not in TOOL_SPECS:
            refusal = {"code": "unknown_tool", "message": f"There is no tool called {name}."}
        if refusal is None and name == "escalate" and str(args.get("urgency", "")).upper() not in ("URGENT", "EMERGENCY"):
            refusal = {"code": "use_callback",
                       "message": "escalate freezes booking and is only for URGENT or EMERGENCY. For mild or unclear "
                                  "symptoms or a medicine question, call create_callback with urgency SOON and keep helping."}
        flag = CONFIRM_FLAGS.get(name)
        if refusal is None and flag and args.get(flag) is True:
            if not lexicon.is_clear_yes(self.last_caller_text):
                refusal = {"code": "confirmation_required",
                           "message": "The caller has not said a clear yes in their latest message. Read the details "
                                      "back and wait for their answer before setting the flag."}
            elif name == "book_appointment" and self.hold_turn == self.turn_index:
                refusal = {"code": "confirmation_required",
                           "message": "You held the slot in this same turn: read back the read_back text and wait for the "
                                      "caller's yes in their next message."}
            if refusal:
                self._record_safety("confirmation_blocked", {"tool": name})
        if refusal is not None:
            use = ToolUse(name, args, False, refusal["code"], "ai")
            self.tool_log.append(use)
            if self.trace:
                self.trace.write("tool", name=name, args=args, ok=False, error=refusal["code"], actor="agent-check")
            payload = {"ok": False, "error": refusal}
            result = {"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(payload), "is_error": True}
            self._results_by_id[block.id] = result
            return result, use
        call_args = dict(args)
        if name == "register_patient" and call_args.get("phone") == CALLER_ID:
            call_args["phone"] = self.phone or ""
        for key in ("name", "given", "family"):
            if isinstance(call_args.get(key), str):
                self.known_names.update(p for p in call_args[key].split() if len(p) >= 3)
        if self.trace:
            self.trace.add_names(self.known_names)
        use, res = self._engine_raw(name, call_args, "ai", shown_args=args)
        if name == "hold_slot" and res.get("ok"):
            self.hold_turn = self.turn_index
        payload = {"ok": True, "data": res.get("data")} if res.get("ok") else {"ok": False, "error": res.get("error")}
        result = {"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(payload, default=str),
                  "is_error": not res.get("ok")}
        self._results_by_id[block.id] = result
        return result, use

    def _llm_down(self, spoken: list[str]) -> Iterator[Event]:
        """The model failed twice: be honest and make sure a person calls back."""
        if not self.ended:
            yield self._engine("create_callback", {"reason": "AI receptionist could not respond (model unavailable)",
                                                   "urgency": "SOON"}, actor="system")
        yield from self._close_turn_with(scripts.pick(scripts.LLM_DOWN, self.lang), spoken)

    def _transfer_after_failure(self) -> Iterator[Event]:
        t = self._engine("transfer_to_human", {"reason": "AI could not finish the request", "desk": "front_desk"},
                         actor="system")
        yield t
        line = scripts.pick(scripts.HUMAN if t.ok else scripts.TRANSFER_FAILED, self.lang)
        yield from self._close_turn_with(line, [])

    def _close_turn_with(self, line: str, spoken: list[str]) -> Iterator[Event]:
        if self.messages and self.messages[-1]["role"] == "assistant":
            self.messages.append({"role": "user", "content": "(the caller is waiting)"})
        self.messages.append({"role": "assistant", "content": " ".join([*spoken, line])})
        say = Say(line, "script")
        self._said(say)
        yield say

    # ------------------------------------------------------------------ engine plumbing

    def _engine(self, tool: str, args: dict, actor: str) -> ToolUse:
        return self._engine_raw(tool, args, actor)[0]

    def _engine_raw(self, tool: str, args: dict, actor: str, shown_args: dict | None = None) -> tuple[ToolUse, dict]:
        res = self.port.call(self.session_id, tool, args, actor=actor)
        self._update_state(res)
        err = (res.get("error") or {}).get("code") if not res.get("ok") else None
        use = ToolUse(tool, shown_args if shown_args is not None else args, bool(res.get("ok")), err, actor,
                      res.get("data"))
        self.tool_log.append(use)
        if self.trace:
            self.trace.write("tool", name=tool, args=use.args, ok=use.ok, error=err, actor=actor,
                             state=self.journey.get("state"))
        return use, res

    def _update_state(self, res: dict) -> None:
        if res.get("journey"):
            self.journey = dict(res["journey"])
        if res.get("session"):
            self.session = dict(res["session"])
        if "allowed_tools" in res:
            self.allowed = res["allowed_tools"]

    def _record_safety(self, kind: str, detail: dict) -> None:
        self.safety_log.append(Safety(kind, detail))
        try:
            self.port.record_safety(self.session_id, kind, detail)
        except Exception:  # logging must never break the call
            log.exception("could not record safety event %s", kind)

    def _safety(self, kind: str, detail: dict) -> Safety:
        self._record_safety(kind, detail)
        if self.trace:
            self.trace.write("safety", event=kind, detail={k: v for k, v in detail.items() if k != "matched"})
        return self.safety_log[-1]

    def _said(self, say: Say) -> None:
        self.transcript.append({"role": "agent", "text": say.text, "kind": say.kind})
        if self.trace:
            self.trace.write("say", text=say.text, kind=say.kind)

    def _llm_stat(self, msg: Any, ttft_ms: int | None, t0: float, attempt: int) -> None:
        u = getattr(msg, "usage", None)
        stat = {"ttft_ms": ttft_ms, "ms": int((time.monotonic() - t0) * 1000), "attempt": attempt,
                "stop": getattr(msg, "stop_reason", None),
                "input_tokens": getattr(u, "input_tokens", None), "output_tokens": getattr(u, "output_tokens", None),
                "cache_read": getattr(u, "cache_read_input_tokens", None)}
        self.llm_stats.append(stat)
        if self.trace:
            self.trace.write("llm", **stat)
