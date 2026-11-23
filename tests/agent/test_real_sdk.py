"""The agent against the real anthropic SDK (2.12.0), with a fake API server instead of a key.

The other agent tests use a stand-in client. These run the actual SDK: its streaming parser,
event types, get_final_message(), its retry setting, and above all how it serialises the next
request (thinking blocks with signatures, tool_use / tool_result pairs, the state note as a
mid-conversation system message, top-level cache_control, effort). If an SDK upgrade or a
change in the agent breaks the wire format, this is where it shows, before a live call does.
"""

from __future__ import annotations

import json

import anthropic
import httpx2
import pytest

from agent.llm import LLMConfig
from agent.prompt import SYSTEM_PROMPT
from tests.agent.conftest import FAST, RAMESH


def _sse(blocks: list[dict], stop_reason: str) -> bytes:
    ev = [{"type": "message_start", "message": {
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-haiku-5-5", "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 120, "output_tokens": 1, "cache_read_input_tokens": 80, "cache_creation_input_tokens": 0}}}]
    for i, b in enumerate(blocks):
        if b["type"] == "text":
            ev.append({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
            for k in range(0, len(b["text"]), 9):
                ev.append({"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b["text"][k:k + 9]}})
        elif b["type"] == "thinking":
            ev.append({"type": "content_block_start", "index": i, "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
            ev.append({"type": "content_block_delta", "index": i, "delta": {"type": "signature_delta", "signature": b["signature"]}})
        else:
            ev.append({"type": "content_block_start", "index": i,
                       "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}})
            ev.append({"type": "content_block_delta", "index": i,
                       "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}})
        ev.append({"type": "content_block_stop", "index": i})
    ev.append({"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
               "usage": {"output_tokens": 30}})
    ev.append({"type": "message_stop"})
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in ev).encode()


class FakeAPI:
    """Answers POST /v1/messages from a script; keeps every request body."""

    def __init__(self, script: list) -> None:
        self.script = script
        self.bodies: list[dict] = []
        self.headers: list[dict] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        self.headers.append(dict(request.headers))
        item = self.script.pop(0)
        if isinstance(item, int):
            return httpx2.Response(item, json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
        blocks, stop = item
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(blocks, stop))

    def client(self) -> anthropic.Anthropic:
        return anthropic.Anthropic(api_key="sk-ant-test-not-a-real-key", max_retries=0,
                                   http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(self.handler)))


def text(t):
    return {"type": "text", "text": t}


def use(id_, tool_name, /, **inp):
    return {"type": "tool_use", "id": id_, "name": tool_name, "input": inp}


THINK = {"type": "thinking", "signature": "sig-abc-123"}


@pytest.fixture
def real_agent(make_agent):
    def make(script, **kw):
        api = FakeAPI(script)
        return make_agent(api.client(), **kw), api
    return make


def test_wire_format_of_a_tool_round_trip(real_agent, twin_admin):
    birth = twin_admin.get(f"/patients/{RAMESH['ref']}").json()["birthDate"]
    a, api = real_agent([
        ([THINK, text("Ek second, main check karti hoon."), use("toolu_01", "verify_identity", name="Ramesh Sharma",
                                                                    birth_date=birth)], "tool_use"),
        ([text("Dhanyavaad Ramesh ji, aapki pehchaan ho gayi. Kya madad karoon?")], "end_turn"),
    ])
    r = a.turn(f"Ramesh Sharma, {birth}")
    assert r.text == "Ek second, main check karti hoon. Dhanyavaad Ramesh ji, aapki pehchaan ho gayi. Kya madad karoon?"
    assert a.session["level"] == "MEDIUM"

    first, second = api.bodies
    assert first["model"] == "claude-haiku-5-5" and first["stream"] is True
    assert first["output_config"] == {"effort": "low"} and "thinking" not in first  # adaptive is the default
    assert first["cache_control"] == {"type": "ephemeral"}
    assert first["system"] == SYSTEM_PROMPT
    assert len(first["tools"]) == 24 and all(t["input_schema"]["type"] == "object" for t in first["tools"])
    assert [m["role"] for m in first["messages"]] == ["user", "system"]
    assert first["messages"][1]["content"].startswith("[State note")

    roles = [m["role"] for m in second["messages"]]
    assert roles == ["user", "system", "assistant", "user", "system"]
    assistant = second["messages"][2]["content"]
    assert assistant[0] == {"type": "thinking", "thinking": "", "signature": "sig-abc-123"}  # passed back untouched
    assert assistant[2]["type"] == "tool_use" and assistant[2]["id"] == "toolu_01"
    assert assistant[2]["input"] == {"name": "Ramesh Sharma", "birth_date": birth}
    result = second["messages"][3]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "toolu_01" and result["is_error"] is False
    assert json.loads(result["content"])["ok"] is True
    assert "Journey state: VERIFIED" in second["messages"][4]["content"]

    h = api.headers[0]
    assert h["authorization"] == "Bearer sk-ant-test-not-a-real-key" and h["anthropic-version"] == "2026-10-21"
    # API version 2026-10-21 rejects sampling parameters: the agent must never send them
    assert not {"temperature", "top_p", "top_k"} & set(first)
    assert a.llm_stats[0]["cache_read"] == 80 and a.llm_stats[0]["stop"] == "tool_use"


def test_overloaded_api_is_retried_once_by_the_agent_not_the_sdk(real_agent):
    a, api = real_agent([529, ([text("Ji, bataiye.")], "end_turn")])
    r = a.turn("hello")
    assert r.text == "Ji, bataiye." and len(api.bodies) == 2
    assert [s.detail["kind"] for s in r.safety if s.kind == "llm_failure"] == ["api_error"]


def test_two_server_errors_end_in_a_callback(real_agent, eng):
    a, api = real_agent([500, 529])
    r = a.turn("hello")
    assert len(api.bodies) == 2 and "front desk" in r.text
    assert [t["kind"] for t in eng.tasks(a.journey_id)] == ["callback"]


def test_refusal_from_the_real_parser(real_agent):
    a, api = real_agent([([text("")], "refusal")], language="en")
    assert "connect you to our staff" in a.turn("hello").text and len(api.bodies) == 1


def test_thinking_off_and_older_models_change_the_request(make_agent):
    api = FakeAPI([([text("Ji.")], "end_turn")])
    make_agent(api.client(), thinking="off").turn("hello")
    assert api.bodies[0]["thinking"] == {"type": "disabled"}
    old = FakeAPI([([text("Ji.")], "end_turn")])
    make_agent(old.client(), model="claude-sonnet-5").turn("hello")
    msgs = old.bodies[0]["messages"]
    assert [m["role"] for m in msgs] == ["user"]
    assert msgs[0]["content"][-1]["text"].startswith("[State note")


def test_sdk_is_the_pinned_version():
    lock = {l.split("==")[0]: l.split("==")[1] for l in open("requirements.lock").read().split() if "==" in l}
    assert anthropic.__version__ == lock["anthropic"]
    assert LLMConfig(**FAST).first_output_timeout == 0.6
