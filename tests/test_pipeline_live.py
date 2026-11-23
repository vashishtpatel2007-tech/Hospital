"""Stages 0-3 in the shape they run live: three separate processes, wired only by env vars.

    twin server (uvicorn, its own DB)  <-  journey server (uvicorn, TWIN_URL)  <-  agent (HttpJourney)

Catches what in-process tests can't: env wiring, the journey server's in-memory vault living in
another process, OTP codes arriving only on the journey server's console, the Stage 2 demo script.
The model is the scripted stand-in, so no key is needed.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from agent.brain import Agent
from agent.llm import LLMConfig
from agent.port import HttpJourney
from tests.agent.conftest import FAST
from tests.agent.fakes import FakeClient, call, last_tool_results, say

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url: str, proc: subprocess.Popen, log: Path) -> None:
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{url} exited early:\n{log.read_text()[-2000:]}")
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise RuntimeError(f"{url} did not start:\n{log.read_text()[-2000:]}")


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    d = tmp_path_factory.mktemp("live")
    tp, jp = _port(), _port()
    twin_url, journey_url = f"http://127.0.0.1:{tp}", f"http://127.0.0.1:{jp}"
    base = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT")}
    env = base | {"ANTHROPIC_API_KEY": "", "TWIN_DB_URL": f"sqlite:///{d}/twin.db", "TWIN_URL": twin_url, "JOURNEY_URL": journey_url,
                  "JOURNEY_DB_URL": f"sqlite:///{d}/journey.db", "PHI_HASH_SALT": "live-test-salt",
                  "PYTHONUNBUFFERED": "1"}
    procs = []
    logs = {}
    for name, target, port in (("twin", "twin.app:app", tp), ("journey", "journey.api:create_app", jp)):
        logs[name] = d / f"{name}.log"
        args = [PY, "-m", "uvicorn", target, "--port", str(port), "--host", "127.0.0.1"]
        if name == "journey":
            args.insert(4, "--factory")
        p = subprocess.Popen(args, cwd=ROOT, env=env, stdout=open(logs[name], "w"), stderr=subprocess.STDOUT)
        procs.append(p)
        _wait(twin_url if name == "twin" else journey_url, p, logs[name])
    try:
        yield {"twin": twin_url, "journey": journey_url, "env": env, "logs": logs, "dir": d}
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


def _otp(log: Path, after: int) -> str:
    deadline = time.time() + 5
    while time.time() < deadline:
        codes = re.findall(r"\[DEV OTP\] \w+ code for \S+: (\d{6})", log.read_text()[after:])
        if codes:
            return codes[-1]
        time.sleep(0.1)
    raise AssertionError("no OTP printed on the journey server console")


def test_stage_2_demo_script_runs_against_separate_servers(live):
    r = subprocess.run([PY, "scripts/demo_journey.py"], cwd=ROOT, env=live["env"], capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert "Traceback" not in r.stdout + r.stderr


def test_new_patient_registers_and_books_through_three_processes(live, tmp_path):
    httpx.post(f"{live['twin']}/admin/reset", timeout=30).raise_for_status()
    port = HttpJourney(live["journey"], live["twin"])
    picked: dict = {}

    def offer(p):
        picked["slot"] = last_tool_results(p)[0]["data"]["slots"][0]
        return say(f"{picked['slot']['when']} with {picked['slot']['doctor']}. Does that work?")

    client = FakeClient([
        say("Welcome. Your first name, surname, gender, date of birth and PIN code, please?"),
        say("Asha Kamath, female, born 5 June 1991, PIN 560034, this number. Shall I register you?"),
        call("register_patient", given="Asha", family="Kamath", gender="female", birth_date="1991-06-05",
             phone="CALLER_ID", postal_code="560034", language="en", caller_confirmed=True),
        say("I've sent a 6-digit code to your phone. Please read it out."),
        lambda p: call("check_otp", code=picked["otp"]),
        lambda p: call("start_booking", reason="fever for three days", department_id="GENM")
        if last_tool_results(p)[0]["ok"] else say("That code didn't work."),
        call("get_slots", part_of_day="morning"),
        offer,
        lambda p: call("hold_slot", slot_id=picked["slot"]["slot_id"]),
        lambda p: say(last_tool_results(p)[0]["data"]["read_back"] + ". Shall I book it?"),
        call("book_appointment", patient_confirmed=True),
        say("Done, you're booked."),
        call("end_call", "Thank you for calling, goodbye!", summary="new patient registered and booked"),
    ])
    a = Agent(port, phone="+917700011122", language="en", client=client, config=LLMConfig(**FAST),
              trace_dir=str(tmp_path))
    a.start()
    a.turn("Hi, I'm new and want to see a general physician")
    a.turn("Asha Kamath, female, 5 June 1991, PIN 560034, use this number")
    mark = len(live["logs"]["journey"].read_text())
    a.turn("Yes, that's right")
    picked["otp"] = _otp(live["logs"]["journey"], mark)  # the dev "SMS" arrives on the journey server console
    a.turn(f"The code is {picked['otp']}")
    assert a.session["level"] == "STRONG" and a.journey["state"] == "SLOT_SEARCH"
    a.turn("The first one is fine")
    assert a.journey["state"] == "SLOT_HELD"
    a.turn("Yes please")
    assert a.journey["state"] == "BOOKED"
    a.turn("No, that's all, thanks")
    assert a.ended

    booked = next(t for t in a.tool_log if t.name == "book_appointment" and t.ok)
    appt = httpx.get(f"{live['twin']}/appointments/{booked.data['appointment_id']}").json()
    assert appt["status"] == "booked"
    assert httpx.get(f"{live['journey']}/journeys/{a.journey_id}/verify").json()["ok"]
    body = a.trace.path.read_text()
    assert "Kamath" not in body and "1991-06-05" not in body and picked["otp"] not in body
    port.close()


def test_emergency_and_human_exit_through_three_processes(live, tmp_path):
    port = HttpJourney(live["journey"], live["twin"])
    a = Agent(port, phone="+919845012345", client=FakeClient([]), config=LLMConfig(**FAST), trace_dir=str(tmp_path))
    a.start()
    r = a.turn("mujhe kisi insaan se baat karni hai")
    assert [(t.name, t.actor, t.ok) for t in r.tools] == [("transfer_to_human", "sentinel", True)]
    r = a.turn("aur seene mein bahut dard ho raha hai")
    assert "108" in r.text and a.journey["escalated"] and a.journey["urgency"] == "EMERGENCY"
    events = httpx.get(f"{live['journey']}/journeys/{a.journey_id}/events").json()
    actors = {e["type"]: e["actor"] for e in events}
    assert actors["escalated"] == "sentinel" and actors["safety_event"] == "sentinel"
    tasks = httpx.get(f"{live['journey']}/tasks", params={"journey_id": a.journey_id}).json()
    assert {t["kind"] for t in tasks} >= {"transfer", "escalation"}
    port.close()


def test_textchat_refuses_cleanly_without_a_key(live):
    env = live["env"] | {"ANTHROPIC_API_KEY": ""}  # set-but-empty: a key in the developer's .env can't fill it in
    r = subprocess.run([PY, "-m", "agent.textchat", "--phone", "+919845012345"], cwd=live["dir"], env=env | {
        "PYTHONPATH": str(ROOT)}, capture_output=True, text=True, timeout=60, input="")
    assert r.returncode == 1 and "ANTHROPIC_API_KEY is not set" in r.stderr + r.stdout
    assert "Traceback" not in r.stderr
