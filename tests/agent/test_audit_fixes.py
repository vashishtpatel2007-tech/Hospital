"""Stage 0-3 audit: one test per problem found, so none of them comes back.

Each test names the failure it guards against (what would have gone wrong on a live call).
"""

from __future__ import annotations

import json

import httpx
import pytest

from agent.brain import Agent
from agent.llm import ConfigError, LLMConfig
from agent.port import HttpJourney
from tests.agent.conftest import FAST, RAMESH
from tests.agent.fakes import FakeClient, Reply, call, say, state_note, text, tool


# ---------------------------------------------------------------- settings fail at start, not mid-call


@pytest.mark.parametrize("kw,msg", [
    ({"effort": "hgih"}, "AGENT_EFFORT"),
    ({"thinking": "on"}, "AGENT_THINKING"),
    ({"model": "haiku"}, "AGENT_MODEL"),
    ({"first_output_timeout": "six"}, "AGENT_LLM_TIMEOUT"),
    ({"first_output_timeout": 600}, "AGENT_LLM_TIMEOUT"),
    ({"model": "claude-opus-5-5", "thinking": "off"}, "can't run with thinking off"),
    ({"thinking": "off", "effort": "max"}, "only works with AGENT_EFFORT"),
])
def test_bad_agent_settings_are_refused_at_start(kw, msg):
    with pytest.raises(ConfigError, match=msg):
        LLMConfig(**kw)


def test_thinking_off_uses_each_models_own_switch():
    assert LLMConfig(thinking="off").request_params()["thinking"] == {"type": "disabled"}
    assert LLMConfig(model="claude-sonnet-5-5", thinking="off").request_params()["thinking"] == {"type": "between_tools"}
    assert "thinking" not in LLMConfig().request_params()
    assert LLMConfig(effort=" Medium ").effort == "medium"


# ---------------------------------------------------------------- the call follows what staff do


def test_staff_resolving_an_emergency_hands_the_call_back_to_the_ai(make_agent, eng):
    client = FakeClient([say("Ji, ab main aapki kaise madad karoon?")])
    a = make_agent(client)
    a.turn("papa behosh ho gaye hain")
    assert "108" in a.turn("haan").text and client.requests == []  # still the emergency line
    eng.human_resolve_escalation(a.journey_id, staff_id="N-07", note="ambulance reached, family wants to rebook later")
    r = a.turn("ab theek hai, ambulance aa gayi")
    assert r.text == "Ji, ab main aapki kaise madad karoon?" and len(client.requests) == 1
    assert "ESCALATED" not in state_note(client.requests[0])


def test_a_call_ended_by_staff_takes_no_more_turns(make_agent, eng):
    client = FakeClient([])
    a = make_agent(client)
    eng.call(a.session_id, "end_call", {"summary": "closed from the dashboard"}, actor="human")
    assert a.turn("hello?").says == [] and client.requests == []


# ---------------------------------------------------------------- interruptions leave a valid history


def test_caller_talking_over_the_agent_mid_tool_round_leaves_a_valid_history(make_agent):
    client = FakeClient([call("list_departments", "Ek second."), say("never heard"), say("Ji, bataiye.")])
    a = make_agent(client)
    gen = a.respond("departments batao")
    kinds = []
    for ev in gen:  # stop listening right after the tool ran (barge-in)
        kinds.append(type(ev).__name__)
        if kinds[-1] == "ToolUse":
            break
    gen.close()
    assert a.messages[-1]["role"] == "assistant" and isinstance(a.messages[-1]["content"], list)
    r = a.turn("nahi, rehne do")
    assert r.text == "never heard" or r.text == "Ji, bataiye."
    msgs = client.requests[-1]["messages"]
    for i, m in enumerate(msgs):  # every tool_use is answered, every system note is followed by an assistant turn
        if m["role"] == "assistant" and isinstance(m["content"], list):
            ids = {b.id for b in m["content"] if getattr(b, "type", None) == "tool_use"}
            if ids:
                assert {b["tool_use_id"] for b in msgs[i + 1]["content"]} == ids
        if m["role"] == "system" and i + 1 < len(msgs):
            assert msgs[i + 1]["role"] == "assistant"
    answered = json.loads(msgs[[i for i, m in enumerate(msgs) if m["role"] == "assistant"][0] + 1]["content"][0]["content"])
    assert answered["ok"] is True  # the tool really ran, and the model is told its real result


def test_caller_talking_over_the_agent_while_it_streams(make_agent):
    client = FakeClient([Reply([text("Pehla vaakya. Doosra vaakya. Teesra vaakya.")], chunk=3), say("Ji?")])
    a = make_agent(client)
    gen = a.respond("hello")
    first = next(gen)
    gen.close()
    assert first.text == "Pehla vaakya."
    assert a.turn("suniye").text == "Ji?"
    assert client.requests[-1]["messages"][-2]["role"] == "user"


# ---------------------------------------------------------------- consent, duplicates, freezing


def test_dont_record_inside_an_emergency_message_is_still_honoured(make_agent, eng):
    a = make_agent(FakeClient([]))
    r = a.turn("recording band karo, aur papa ko seene mein dard hai")
    assert not a.recording and "108" in r.text
    kinds = [s.kind for s in r.safety]
    assert kinds[:2] == ["recording_declined", "red_flag"]
    rows = [json.loads(x) for x in a.trace.path.read_text().splitlines()]
    after = rows[next(i for i, r_ in enumerate(rows) if r_.get("event") == "recording_declined"):]
    assert all(r_.get("text") is None for r_ in after if r_["type"] in ("caller", "say"))


def test_asking_for_staff_twice_queues_one_transfer(make_agent, eng):
    a = make_agent(FakeClient([]))
    a.turn("staff")
    r = a.turn("staff please, jaldi")
    assert r.tools == [] and "staff" in r.text.lower()
    assert len([t for t in eng.tasks(a.journey_id) if t["kind"] == "transfer"]) == 1


def test_model_escalating_a_mild_symptom_does_not_freeze_booking(make_agent, eng):
    client = FakeClient([
        call("escalate", urgency="SOON", reason="stomach ache 3 days"),
        lambda p: call("create_callback", reason="stomach ache 3 days, nurse to call", urgency="SOON"),
        say("Nurse aapko call karengi. Appointment bhi book kar doon?"),
    ])
    a = make_agent(client)
    a.turn("teen din se pet mein dard hai")
    assert [(t.name, t.ok, t.error) for t in a.tool_log] == [("escalate", False, "use_callback"),
                                                            ("create_callback", True, None)]
    assert not eng.journey(a.journey_id)["escalated"]


def test_blocked_reply_words_never_reach_the_audit_log(make_agent, eng):
    a = make_agent(FakeClient([say("Your Aadhaar 2345 6789 0123 is linked to sugar level 240.")]), language="en")
    a.turn("is my aadhaar linked?")
    payloads = json.dumps([e["payload"] for e in eng.events(a.journey_id)])
    assert "2345" not in payloads and "240" not in payloads
    assert any(e["payload"].get("category") == "sensitive" for e in eng.events(a.journey_id)
               if e["type"] == "safety_event")


def test_hospital_system_down_at_call_start_is_told_to_the_model(make_agent, twin_admin):
    twin_admin.post("/admin/chaos", json={"his_down": True})
    try:
        client = FakeClient([say("Abhi system band hai, main callback karwa deti hoon.")])
        a = make_agent(client)
        a.turn("appointment chahiye")
        assert "hospital system is not responding" in state_note(client.requests[0])
    finally:
        twin_admin.post("/admin/chaos", json={"his_down": False})


# ---------------------------------------------------------------- the trace never breaks a call


def test_an_unwritable_log_folder_does_not_break_the_call(port, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    a = Agent(port, phone=RAMESH["phone"], client=FakeClient([say("Ji.")]), config=LLMConfig(**FAST),
              trace_dir=str(blocker / "logs"))
    a.start()
    assert a.turn("hello").text == "Ji." and a.trace.broken


def test_trace_appends_and_only_rewrites_when_a_name_is_learned(make_agent, twin_admin):
    birth = twin_admin.get(f"/patients/{RAMESH['ref']}").json()["birthDate"]
    a = make_agent(FakeClient([say("Ji."), call("verify_identity", name="Ramesh Sharma", birth_date=birth), say("Ok.")]))
    a.turn("Main Sharma ji bol raha hoon")
    assert "Sharma" in a.trace.path.read_text()  # not known as a name yet
    a.turn(f"Ramesh Sharma, {birth}")
    assert "Sharma" not in a.trace.path.read_text()  # rewritten once the name was learned


# ---------------------------------------------------------------- a stale journey server is refused


def test_agent_refuses_a_journey_server_running_older_code():
    port = HttpJourney("http://journey.test")
    port.http = httpx.Client(base_url="http://journey.test",
                             transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "ok"})))
    with pytest.raises(RuntimeError, match="older code"):
        port.check_contract()
    port.http = httpx.Client(base_url="http://journey.test", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"status": "ok", "contract": 3})))
    port.check_contract()


def test_http_errors_from_the_journey_server_are_tool_errors_not_crashes():
    port = HttpJourney("http://journey.test")
    port.http = httpx.Client(base_url="http://journey.test",
                             transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    res = port.call("C-1", "list_departments", {})
    assert res == {"ok": False, "tool": "list_departments", "data": None,
                   "error": {"code": "internal_error", "message": "boom"}}
