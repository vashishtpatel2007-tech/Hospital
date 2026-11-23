"""Talk to Sahay by typing (BUILD_PLAN Stage 3).

    python -m agent.textchat --phone +919845012345          # Ramesh, a returning patient
    python -m agent.textchat --phone +917700011122 --lang en # a new caller
    python -m agent.textchat                                 # caller ID withheld

Uses the Journey Engine at JOURNEY_URL if it is running (make journey), otherwise starts one
in this process against the twin at TWIN_URL (make twin). Needs ANTHROPIC_API_KEY in .env.
Type /quit (or Ctrl-D) to hang up, /state to see the journey, /trace to see the log file.
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()

import httpx  # noqa: E402

from agent.brain import Agent, Safety, Say, ToolUse  # noqa: E402
from agent.port import HttpJourney, LocalJourney  # noqa: E402

DIM, BOLD, RED, YELLOW, GREEN, RESET = "\033[2m", "\033[1m", "\033[31m", "\033[33m", "\033[32m", "\033[0m"


def make_port(prefer_server: bool = True):
    journey_url = os.getenv("JOURNEY_URL", "http://localhost:8002")
    twin_url = os.getenv("TWIN_URL", "http://localhost:8001")
    if prefer_server:
        try:
            if httpx.get(f"{journey_url}/health", timeout=1).status_code == 200:
                port = HttpJourney(journey_url, twin_url)
                try:
                    port.check_contract()
                except RuntimeError as e:
                    sys.exit(str(e))
                return port, f"journey server {journey_url}"
        except httpx.HTTPError:
            pass
    try:
        httpx.get(f"{twin_url}/health", timeout=2)
    except httpx.HTTPError:
        sys.exit(f"The Hospital Twin is not running at {twin_url}. Start it with: make twin")
    from journey.engine import JourneyEngine

    return LocalJourney(JourneyEngine()), "in-process journey engine"


def show(ev, debug: bool) -> None:
    if isinstance(ev, Say):
        colour = {"script": YELLOW, "replacement": YELLOW, "filler": DIM}.get(ev.kind, "")
        print(f"{BOLD}Sahay:{RESET} {colour}{ev.text}{RESET}", flush=True)
    elif isinstance(ev, ToolUse) and debug:
        mark = f"{GREEN}ok{RESET}" if ev.ok else f"{RED}{ev.error}{RESET}"
        print(f"{DIM}  [{ev.actor}] {ev.name} -> {RESET}{mark}", flush=True)
    elif isinstance(ev, Safety) and debug:
        print(f"{RED}  [safety] {ev.kind} {ev.detail}{RESET}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Chat with Sahay by text")
    ap.add_argument("--phone", default=None, help="caller ID to simulate, e.g. +919845012345")
    ap.add_argument("--lang", default="hi-en", choices=["hi-en", "en", "hi", "kn"], help="language of the opening line")
    ap.add_argument("--local", action="store_true", help="run the journey engine in this process")
    ap.add_argument("--quiet", action="store_true", help="hide tool calls and safety events")
    args = ap.parse_args()
    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Put it in .env (never paste it into chat), then run: make check-keys")

    port, where = make_port(prefer_server=not args.local)
    agent = Agent(port, phone=args.phone, channel="text", language=args.lang)
    print(f"{DIM}Connected to {where}. Caller ID: {args.phone or 'withheld'}. /quit to hang up.{RESET}")
    for say in agent.start():
        show(say, True)
    print(f"{DIM}  trace: {agent.trace.path if agent.trace else '-'}")
    print(f"  SMS codes (registration, reports) print on the "
          f"{'journey server console' if isinstance(port, HttpJourney) else 'this screen as [DEV OTP]'}.{RESET}")
    while not agent.ended:
        try:
            text = input(f"{BOLD}You:{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in ("/quit", "/exit", "/q"):
            break
        if text == "/state":
            print(f"{DIM}  journey {agent.journey} | session {agent.session} | allowed {agent.allowed}{RESET}")
            continue
        if text == "/trace":
            print(f"{DIM}  {agent.trace.path if agent.trace else '-'}{RESET}")
            continue
        for ev in agent.respond(text):
            show(ev, not args.quiet)
    agent.hangup()
    print(f"{DIM}Call ended. Journey {agent.journey_id} is in state {agent.journey.get('state')}.{RESET}")


if __name__ == "__main__":
    main()
