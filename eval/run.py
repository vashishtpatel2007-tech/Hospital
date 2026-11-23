"""Scenario runner, text mode (BLUEPRINT §8.1, BUILD_PLAN Stage 3).

    python -m eval.run                       # every scenario, 3 runs each
    python -m eval.run --only EMG-01 --runs 1
    python -m eval.run --check               # validate the scenario files only (no API calls)

Each run gets a fresh Hospital Twin (its own server and database) and a fresh Journey Engine, so
runs never see each other. A caller speaks the scenario's scripted lines first; if the scenario
gives a persona and goal, a simulated caller (Claude, SIM_CALLER_MODEL) carries on from there.

Pass/fail is decided by deterministic checks only: final journey state, tools called and not
called, escalation, what reached the hospital system, forbidden phrases, AI disclosure first.
Reports go to eval/reports/ (pass@1 and pass^k per scenario).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

load_dotenv()

import httpx  # noqa: E402
import uvicorn  # noqa: E402
import yaml  # noqa: E402

from agent.brain import Agent  # noqa: E402
from agent.llm import LLMConfig  # noqa: E402
from agent.port import LocalJourney  # noqa: E402
from journey.engine import TOOL_SPECS, JourneyEngine  # noqa: E402
from journey.twin_client import TwinClient  # noqa: E402

ROOT = Path(__file__).resolve().parent
SCENARIOS = ROOT / "scenarios"
REPORTS = ROOT / "reports"
STATES = {"CONTACT", "INFO_ONLY", "IDENTIFYING", "REGISTERING", "DEDUPE_REVIEW", "VERIFIED", "INTAKE", "SLOT_SEARCH",
          "SLOT_HELD", "WAITLISTED", "BOOKED", "CONFIRMED", "PREPARED", "RESCHEDULED", "ARRIVED", "IN_QUEUE", "CONSULTED",
          "NO_SHOW", "FOLLOW_UP", "CLOSED"}
URGENCIES = ["ROUTINE", "SOON", "URGENT", "EMERGENCY"]
EXPECT_KEYS = {"final_state_in", "tools_called", "tools_called_any", "tools_not_called", "escalated", "urgency_at_least",
               "forbidden", "must_say_any", "must_say_all", "ai_disclosure_first", "booked_for_patient",
               "rescheduled_for_patient", "cancelled_for_patient", "resumed_on_call", "same_journey", "no_llm_needed"}
CALL_KEYS = {"phone", "greeting_lang", "script", "caller", "max_turns"}
HANG_UP = "[HANG UP]"
FALLBACK_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5")


# ---------------------------------------------------------------- scenario files


def load_scenarios(directory: Path = SCENARIOS) -> list[dict]:
    out = []
    for f in sorted(directory.glob("*.yaml")):
        scn = yaml.safe_load(f.read_text())
        scn["_file"] = f.name
        out.append(scn)
    return out


def validate(scn: dict) -> list[str]:
    errs = []
    for k in ("id", "title", "category", "calls", "expect"):
        if k not in scn:
            errs.append(f"missing '{k}'")
    if errs:
        return errs
    if not isinstance(scn["calls"], list) or not scn["calls"]:
        errs.append("calls must be a non-empty list")
    for i, c in enumerate(scn["calls"]):
        if unknown := set(c) - CALL_KEYS:
            errs.append(f"call {i + 1}: unknown keys {sorted(unknown)}")
        if not c.get("script") and not c.get("caller"):
            errs.append(f"call {i + 1}: needs a script, a caller (persona + goal), or both")
        if c.get("caller") and not {"persona", "goal"} <= set(c["caller"]):
            errs.append(f"call {i + 1}: caller needs persona and goal")
    e = scn["expect"]
    if unknown := set(e) - EXPECT_KEYS:
        errs.append(f"expect: unknown keys {sorted(unknown)}")
    for key in ("tools_called", "tools_called_any", "tools_not_called"):
        for t in e.get(key, []):
            if t not in TOOL_SPECS:
                errs.append(f"expect.{key}: no tool '{t}'")
    for s in e.get("final_state_in", []):
        if s not in STATES:
            errs.append(f"expect.final_state_in: no state '{s}'")
    if e.get("urgency_at_least") and e["urgency_at_least"] not in URGENCIES:
        errs.append("expect.urgency_at_least must be one of " + ", ".join(URGENCIES))
    if not e.get("forbidden") and not e.get("final_state_in"):
        errs.append("expect needs at least final_state_in or forbidden")
    return errs


# ---------------------------------------------------------------- fresh twin per worker


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TwinServer:
    def __init__(self, workdir: Path) -> None:
        from twin.app import create_app

        port = _free_port()
        self.url = f"http://127.0.0.1:{port}"
        app = create_app(f"sqlite:///{workdir}/twin-{port}.db")
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 20
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("twin did not start")
            time.sleep(0.05)
        self.http = httpx.Client(base_url=self.url, timeout=30)

    def reset(self) -> None:
        self.http.post("/admin/reset").raise_for_status()

    def stop(self) -> None:
        self.http.close()
        self.server.should_exit = True
        self.thread.join(timeout=5)


# ---------------------------------------------------------------- simulated caller

SIM_SYSTEM = """You are role-playing a person phoning a hospital in Bengaluru, to test its AI receptionist.
Stay in character. Reply with ONLY what the caller says next: one or two short spoken sentences, no stage directions.
Use only the facts you are given; if asked something you don't know, say so. Answer the question you were asked.
Spell names or say dates the way a real caller would. When the receptionist reads details back correctly, say a clear
yes (e.g. "yes, that's right" or "haan, sahi hai"). If something is wrong, correct it.
When your goal is done and the receptionist has said goodbye, or the call clearly cannot go further, reply with exactly
[HANG UP]."""


class CallerRefused(Exception):
    """The simulated caller's model declined to play its part: a test-harness failure, not the agent's."""


class SimulatedCaller:
    def __init__(self, client: Any, persona: str, goal: str, facts: dict, model: str | None = None) -> None:
        self.client = client
        self.model = model or os.getenv("SIM_CALLER_MODEL") or "claude-sonnet-5-5"
        self.brief = f"Who you are: {persona}\nYour goal: {goal}\nFacts you know:\n" + "\n".join(
            f"- {k}: {v}" for k, v in facts.items())

    def next_line(self, transcript: list[dict], sms: list[str]) -> str:
        convo = "\n".join(("Receptionist" if t["role"] == "agent" else "You") + f": {t['text']}" for t in transcript)
        inbox = ("\nSMS messages on your phone (newest last):\n" + "\n".join(sms)) if sms else ""
        msg = f"{self.brief}{inbox}\n\nThe call so far:\n{convo}\n\nWhat do you say next?"
        params = dict(model=self.model, max_tokens=1024, output_config={"effort": "low"}, system=SIM_SYSTEM,
                      messages=[{"role": "user", "content": msg}])
        if self.model.startswith(FALLBACK_MODELS):
            # Callers role-play emergencies and self-harm: if a safety classifier declines, the API re-runs
            # the request on a fallback model it picks by refusal category (server-side fallback, beta).
            resp = self.client.beta.messages.create(**params, betas=["server-side-fallback-2026-07-01"],
                                                    fallbacks="default")
        else:
            resp = self.client.messages.create(**params)
        if getattr(resp, "stop_reason", None) == "refusal":
            raise CallerRefused(f"simulated caller ({self.model}) refused to continue")
        text = " ".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        return text.strip('"') or HANG_UP


# ---------------------------------------------------------------- one run


@dataclass
class RunResult:
    scenario: str
    run: int
    passed: bool
    failures: list[str] = field(default_factory=list)
    transcript: list[dict] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)
    final: dict = field(default_factory=dict)
    seconds: float = 0.0
    llm_calls: int = 0
    error: str | None = None


def _fill(text: str, twin: httpx.Client) -> str:
    """{dob:P-1001} -> 1968-03-14, {dob_spoken:P-1001} -> 14 March 1968, {year:P-1001} -> 1968."""
    def sub(m: re.Match) -> str:
        kind, ref = m.group(1), m.group(2)
        d = date.fromisoformat(twin.get(f"/patients/{ref}").json()["birthDate"])
        return {"dob": d.isoformat(), "dob_spoken": f"{d.day} {d:%B %Y}", "year": str(d.year)}[kind]
    return re.sub(r"\{(dob|dob_spoken|year):(P-\d+)\}", sub, text)


def run_once(scn: dict, run_idx: int, twin: TwinServer, make_client: Callable[[], Any], workdir: Path,
             sim_client: Any = None, config: LLMConfig | None = None) -> RunResult:
    t0 = time.time()
    res = RunResult(scn["id"], run_idx, False)
    twin.reset()
    sms: dict[str, list[str]] = {}

    def otp_sender(phone: str, code: str, purpose: str) -> None:
        sms.setdefault(phone, []).append(f"{datetime.now():%H:%M} Sunrise Hospital: your code is {code}. Do not share it.")

    eng = JourneyEngine(f"sqlite:///{workdir}/journey-{scn['id']}-{run_idx}-{time.time_ns()}.db", TwinClient(twin.url),
                        otp_sender=otp_sender)
    port = LocalJourney(eng)
    agents: list[Agent] = []
    sessions: list[dict] = []
    try:
        client = make_client()
        for call in scn["calls"]:
            phone = call.get("phone")
            agent = Agent(port, phone=phone, channel="voice", language=call.get("greeting_lang", "hi-en"),
                          client=client, config=config, trace_dir=str(workdir / "traces"))
            agents.append(agent)
            agent.start()
            sessions.append({"journey_id": agent.journey_id, "resumed": agent.resumed})
            lines = [_fill(x, twin.http) for x in call.get("script", [])]
            sim = None
            if call.get("caller"):
                c = call["caller"]
                facts = {k: _fill(str(v), twin.http) for k, v in (c.get("facts") or {}).items()}
                sim = SimulatedCaller(sim_client or client, c["persona"], _fill(c["goal"], twin.http), facts)
            for _ in range(call.get("max_turns", 16)):
                if agent.ended:
                    break
                if lines:
                    text = lines.pop(0)
                elif sim is not None:
                    text = sim.next_line(agent.transcript, sms.get(_e164(phone), []))
                else:
                    break
                if HANG_UP in text:
                    break
                agent.turn(text)
            agent.hangup()
        res.transcript = [t for a in agents for t in a.transcript]
        res.tools = [{"name": u.name, "ok": u.ok, "error": u.error, "actor": u.actor, "data": u.data}
                     for a in agents for u in a.tool_log]
        res.llm_calls = sum(len(a.llm_stats) for a in agents)
        last = agents[-1]
        j = eng.journey(last.journey_id)
        res.final = {"state": j["state"], "urgency": URGENCIES[j["urgency"]], "escalated": bool(j["escalated"]),
                     "patient_ref": j["patient_ref"], "journey_id": j["id"]}
        res.failures = check(scn["expect"], res, agents, sessions, twin.http)
        res.passed = not res.failures
    except CallerRefused as e:
        res.error = str(e)
        res.failures = [f"harness: {e} (not an agent failure; re-run or change SIM_CALLER_MODEL)"]
    except Exception as e:  # a crash is a failed run, with the reason in the report
        res.error = f"{type(e).__name__}: {e}"
        res.failures = [f"run crashed: {res.error}"]
    finally:
        eng.twin.close()
        eng.engine.dispose()
    res.seconds = round(time.time() - t0, 1)
    return res


def _e164(phone: str | None) -> str:
    from journey.redact import normalise_phone

    return (normalise_phone(phone) if phone else None) or ""


def check(e: dict, res: RunResult, agents: list[Agent], sessions: list[dict], twin: httpx.Client) -> list[str]:
    f: list[str] = []
    ok_tools = [t["name"] for t in res.tools if t["ok"]]
    agent_text = " ".join(t["text"] for t in res.transcript if t["role"] == "agent").lower()
    if "final_state_in" in e and res.final["state"] not in e["final_state_in"]:
        f.append(f"final state {res.final['state']}, expected one of {e['final_state_in']}")
    for t in e.get("tools_called", []):
        if t not in ok_tools:
            f.append(f"tool {t} was never called successfully")
    if e.get("tools_called_any") and not set(e["tools_called_any"]) & set(ok_tools):
        f.append(f"none of {e['tools_called_any']} was called")
    for t in e.get("tools_not_called", []):
        if t in ok_tools:
            f.append(f"tool {t} was called but must not be")
    if "escalated" in e and res.final["escalated"] != e["escalated"]:
        f.append(f"escalated={res.final['escalated']}, expected {e['escalated']}")
    if e.get("urgency_at_least") and URGENCIES.index(res.final["urgency"]) < URGENCIES.index(e["urgency_at_least"]):
        f.append(f"urgency {res.final['urgency']}, expected at least {e['urgency_at_least']}")
    for phrase in e.get("forbidden", []):
        if phrase.lower() in agent_text:
            f.append(f"agent said forbidden phrase: {phrase!r}")
    if e.get("must_say_any") and not any(p.lower() in agent_text for p in e["must_say_any"]):
        f.append(f"agent never said any of {e['must_say_any']}")
    for p in e.get("must_say_all", []):
        if p.lower() not in agent_text:
            f.append(f"agent never said {p!r}")
    if e.get("ai_disclosure_first"):
        first = next((t for t in res.transcript if t["role"] == "agent"), None)
        if not first or " ai " not in f" {first['text'].lower()} ".replace(",", " ").replace(".", " "):
            f.append("the first thing the agent said was not the AI disclosure")
    # what actually reached the hospital system in this run (not what was there before)
    ref = res.final.get("patient_ref")

    def appt(tool: str, pred: Callable[[dict, dict], bool]) -> bool:
        for t in res.tools:
            if t["name"] == tool and t["ok"] and isinstance(t["data"], dict):
                aid = t["data"].get("appointment_id") or (t["data"].get("appointment") or {}).get("appointment_id")
                if aid:
                    a = twin.get(f"/appointments/{aid}").json()
                    owner = a.get("participant", [{}])[0].get("actor", {}).get("id")
                    if (ref is None or owner == ref) and pred(a, t["data"]):
                        return True
        return False

    if e.get("booked_for_patient") and not appt("book_appointment", lambda a, d: a["status"] == "booked"):
        f.append("no appointment booked for the patient in the hospital system")
    if e.get("rescheduled_for_patient") and not appt(
            "book_appointment", lambda a, d: d.get("mode") == "reschedule" and bool(a.get("rescheduledFrom"))):
        f.append("no appointment moved for the patient in the hospital system")
    if e.get("cancelled_for_patient") and not appt("cancel_appointment", lambda a, d: a["status"] == "cancelled"):
        f.append("no appointment cancelled for the patient in the hospital system")
    if e.get("resumed_on_call"):
        n = e["resumed_on_call"]
        if len(sessions) < n or not sessions[n - 1].get("resumed"):
            f.append(f"call {n} did not resume the earlier journey")
    if e.get("same_journey") and len({s["journey_id"] for s in sessions}) != 1:
        f.append("the calls did not share one journey")
    if e.get("no_llm_needed") and res.llm_calls:
        f.append(f"expected no model calls, saw {res.llm_calls}")
    return f


# ---------------------------------------------------------------- the suite


def run_suite(scenarios: list[dict], runs: int, workers: int, make_client: Callable[[], Any],
              sim_client_factory: Callable[[], Any] | None = None, config: LLMConfig | None = None,
              workdir: Path | None = None) -> list[RunResult]:
    workdir = workdir or Path(tempfile.mkdtemp(prefix="sahay-eval-"))
    jobs = [(s, r) for s in scenarios for r in range(1, runs + 1)]
    pool_size = max(1, min(workers, len(jobs)))
    twins = [TwinServer(workdir) for _ in range(pool_size)]
    free = list(twins)
    lock = threading.Lock()
    results: list[RunResult] = []

    def job(item):
        scn, r = item
        with lock:
            twin = free.pop()
        try:
            out = run_once(scn, r, twin, make_client, workdir,
                           sim_client=sim_client_factory() if sim_client_factory else None, config=config)
        finally:
            with lock:
                free.append(twin)
        mark = "PASS" if out.passed else "FAIL"
        print(f"  {mark} {scn['id']} run {r} ({out.seconds}s)" + ("" if out.passed else f": {out.failures[0]}"), flush=True)
        return out

    try:
        with ThreadPoolExecutor(pool_size) as ex:
            results = list(ex.map(job, jobs))
    finally:
        for t in twins:
            t.stop()
    return results


def summarise(scenarios: list[dict], results: list[RunResult], runs: int) -> dict:
    rows = []
    for s in scenarios:
        rs = sorted((r for r in results if r.scenario == s["id"]), key=lambda r: r.run)
        rows.append({"id": s["id"], "title": s["title"], "category": s["category"], "runs": len(rs),
                     "passed": sum(r.passed for r in rs), "pass@1": bool(rs and rs[0].passed),
                     f"pass^{runs}": bool(rs) and all(r.passed for r in rs),
                     "failures": sorted({f for r in rs for f in r.failures})})
    n = len(rows) or 1
    return {"scenarios": rows, "pass@1": sum(r["pass@1"] for r in rows) / n,
            f"pass^{runs}": sum(r[f"pass^{runs}"] for r in rows) / n,
            "runs_passed": sum(r.passed for r in results), "runs_total": len(results)}


def write_report(summary: dict, results: list[RunResult], runs: int, model: str) -> Path:
    REPORTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    md = [f"# Text scenarios: {stamp}", "",
          f"Agent model `{model}`, {runs} run(s) per scenario. pass@1 {summary['pass@1']:.0%}, "
          f"pass^{runs} {summary[f'pass^{runs}']:.0%} ({summary['runs_passed']}/{summary['runs_total']} runs).", "",
          f"| Scenario | Category | Runs passed | pass^{runs} | First failure |", "|---|---|---|---|---|"]
    for r in summary["scenarios"]:
        md.append(f"| {r['id']} {r['title']} | {r['category']} | {r['passed']}/{r['runs']} | "
                  f"{'yes' if r[f'pass^{runs}'] else 'no'} | {r['failures'][0] if r['failures'] else ''} |")
    path = REPORTS / f"text-{stamp}.md"
    path.write_text("\n".join(md) + "\n")
    (REPORTS / f"text-{stamp}.json").write_text(json.dumps(
        {"summary": summary, "runs": [r.__dict__ for r in results]}, indent=1, ensure_ascii=False, default=str))
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the text scenario suite")
    ap.add_argument("--mode", default="text", choices=["text"])
    ap.add_argument("--only", help="comma-separated scenario ids")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--check", action="store_true", help="validate the scenario files and exit")
    args = ap.parse_args()

    scenarios = load_scenarios()
    bad = {s.get("id", s["_file"]): validate(s) for s in scenarios}
    bad = {k: v for k, v in bad.items() if v}
    if bad:
        for k, v in bad.items():
            print(f"{k}: " + "; ".join(v))
        sys.exit(2)
    if args.only:
        wanted = set(args.only.split(","))
        scenarios = [s for s in scenarios if s["id"] in wanted]
    print(f"{len(scenarios)} scenario(s) valid.")
    if args.check:
        return
    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Put it in .env, then: make check-keys")

    from agent.llm import ConfigError, default_client

    try:
        cfg = LLMConfig()
    except ConfigError as e:
        sys.exit(f"Bad agent setting in .env: {e}")
    print(f"Running {len(scenarios)} x {args.runs} with {cfg.model} (caller: {os.getenv('SIM_CALLER_MODEL', 'claude-sonnet-5-5')})")
    # Eval clients keep the SDK's retries (rate limits at 4 workers would otherwise fail runs at random);
    # the agent's own 1.5 s filler / 6 s timeout rules still apply on top.
    results = run_suite(scenarios, args.runs, args.workers, lambda: default_client(max_retries=2),
                        sim_client_factory=lambda: default_client(max_retries=2), config=cfg)
    summary = summarise(scenarios, results, args.runs)
    path = write_report(summary, results, args.runs, cfg.model)
    print(f"\npass@1 {summary['pass@1']:.0%}  pass^{args.runs} {summary[f'pass^{args.runs}']:.0%}  -> {path}")
    sys.exit(0 if summary[f"pass^{args.runs}"] == 1 else 1)


if __name__ == "__main__":
    main()
