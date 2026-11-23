"""Stage 3: the scenario runner itself, run end to end with a stand-in model (no API key needed).

The real suite (make scenarios) uses Claude for the agent and the simulated caller; this checks
the plumbing: fresh twin per run, scripted callers, checks, pass@1 / pass^k, reports.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from eval import run as runner
from tests.agent.fakes import FakeClient, call, last_tool_results, say

ROOT = Path(__file__).resolve().parents[2]


def responder(params: dict):
    """A tiny pretend receptionist: enough to drive the scripted scenarios."""
    msgs = params["messages"]
    results = last_tool_results(params)
    if results:
        data = results[0].get("data") or {}
        if "departments" in data:
            loc = next(d["location"] for d in data["departments"] if d["id"] == "CARD")
            return say(f"Cardiology is on the {loc}.")
        if "doctors" in data:
            return say("Kannada-speaking cardiologists: " + ", ".join(d["name"] for d in data["doctors"]) + ".")
        return say("Thank you for calling.")
    caller = next(m["content"] for m in reversed(msgs) if m["role"] == "user" and isinstance(m["content"], str))
    low = caller.lower()
    if "where is the cardiology" in low:
        return call("list_departments")
    if "kannada" in low:
        return call("find_doctors", department_id="CARD", language="kn")
    if "that's all" in low:
        return call("end_call", "You're welcome, goodbye!", summary="information given")
    return say("Ji, main dekhti hoon. Aapka poora naam aur date of birth bataiye?")


@pytest.fixture(scope="module")
def scenarios():
    return {s["id"]: s for s in runner.load_scenarios()}


def test_every_scenario_file_is_valid(scenarios):
    assert len(scenarios) >= 10
    for s in scenarios.values():
        assert runner.validate(s) == [], (s["id"], runner.validate(s))
    cats = {s["category"] for s in scenarios.values()}
    assert {"routine_booking", "reschedule", "cancellation", "waitlist", "information", "human_handoff",
            "emergency_red_flag", "resume"} <= cats


def test_validator_catches_mistakes():
    bad = {"id": "X", "title": "x", "category": "x", "calls": [{"phone": "1", "scrpt": []}],
           "expect": {"tools_called": ["book_it"], "final_state_in": ["DONE"], "wat": 1}}
    errs = " | ".join(runner.validate(bad))
    for needle in ["unknown keys ['scrpt']", "needs a script", "no tool 'book_it'", "no state 'DONE'", "unknown keys ['wat']"]:
        assert needle in errs


def test_scripted_scenarios_pass_end_to_end_with_a_stand_in_model(scenarios, tmp_path):
    chosen = [scenarios[i] for i in ("HUMAN-01", "EMG-01", "INFO-01")]
    results = runner.run_suite(chosen, runs=2, workers=2, make_client=lambda: FakeClient(responder), workdir=tmp_path)
    for r in results:
        assert r.passed, (r.scenario, r.run, r.failures, r.transcript)
    summary = runner.summarise(chosen, results, 2)
    assert summary["pass@1"] == 1 and summary["pass^2"] == 1 and summary["runs_total"] == 6
    emg = next(r for r in results if r.scenario == "EMG-01")
    assert [t["actor"] for t in emg.tools if t["name"] in ("escalate", "transfer_to_human")] == ["sentinel", "sentinel"]
    assert emg.final["escalated"] and emg.final["urgency"] == "EMERGENCY"
    human = next(r for r in results if r.scenario == "HUMAN-01")
    assert human.llm_calls == 1  # only the first turn needed the model
    assert list((tmp_path / "traces").glob("*.jsonl"))


def test_a_broken_expectation_fails_with_a_reason(scenarios, tmp_path):
    scn = json.loads(json.dumps(scenarios["EMG-01"]))
    scn["expect"]["tools_not_called"] = ["escalate"]
    scn["expect"]["must_say_any"] = ["this phrase is never said"]
    [res] = runner.run_suite([scn], runs=1, workers=1, make_client=lambda: FakeClient(responder), workdir=tmp_path)
    assert not res.passed
    assert "tool escalate was called but must not be" in res.failures
    assert any("never said any of" in f for f in res.failures)


def test_a_crashing_run_is_a_failure_not_a_crash(scenarios, tmp_path):
    def boom():
        raise RuntimeError("no client")

    [res] = runner.run_suite([scenarios["INFO-01"]], runs=1, workers=1, make_client=boom, workdir=tmp_path)
    assert not res.passed and "run crashed: RuntimeError: no client" in res.failures[0]


def test_report_is_written(scenarios, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REPORTS", tmp_path / "reports")
    results = runner.run_suite([scenarios["HUMAN-01"]], runs=1, workers=1, make_client=lambda: FakeClient(responder),
                               workdir=tmp_path)
    path = runner.write_report(runner.summarise([scenarios["HUMAN-01"]], results, 1), results, 1, "fake")
    assert "HUMAN-01" in path.read_text() and path.with_suffix(".json").exists()


def test_cli_check_mode_needs_no_key():
    r = subprocess.run([sys.executable, "-m", "eval.run", "--check"], cwd=ROOT, capture_output=True, text=True,
                       timeout=120, env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())})
    assert r.returncode == 0 and "scenario(s) valid" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------- the simulated caller, through the real SDK


def _caller_api(stop_reason: str):
    import anthropic
    import httpx2

    seen: dict = {}

    def handler(req):
        seen["body"], seen["headers"] = json.loads(req.content), dict(req.headers)
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": "Hi, I need a doctor for my knee."}] if stop_reason != "refusal" else [],
            "stop_reason": stop_reason, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 9}})

    client = anthropic.Anthropic(api_key="sk-ant-test", max_retries=0,
                                 http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    return client, seen


def test_simulated_caller_asks_for_server_side_fallback():
    client, seen = _caller_api("end_turn")
    sim = runner.SimulatedCaller(client, "a patient", "book a knee appointment", {"name": "Asha"})
    assert sim.next_line([{"role": "agent", "text": "Hello, how can I help?"}], []) == "Hi, I need a doctor for my knee."
    assert seen["body"]["fallbacks"] == "default" and seen["body"]["model"] == "claude-sonnet-5-5"
    assert "server-side-fallback-2026-07-01" in seen["headers"]["anthropic-beta"]
    assert not {"temperature", "top_p", "top_k"} & set(seen["body"])


def test_a_refusing_simulated_caller_is_a_harness_failure_not_an_agent_failure(scenarios, tmp_path, monkeypatch):
    client, _ = _caller_api("refusal")
    scn = scenarios["BOOK-02"]
    [res] = runner.run_suite([scn], runs=1, workers=1, make_client=lambda: FakeClient(responder),
                             sim_client_factory=lambda: client, workdir=tmp_path)
    assert not res.passed and res.failures[0].startswith("harness: simulated caller")
