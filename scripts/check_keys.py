"""Check that every service key in .env works, with one tiny call each.

    python scripts/check_keys.py            # everything
    python scripts/check_keys.py --only anthropic postgres

Never prints key values. Exit code 1 if a required service that is configured fails.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

OK, FAIL, SKIP = "✅", "❌", "⚪"
TIMEOUT = 15.0


@dataclass
class Result:
    status: str
    detail: str


def env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def missing(*names: str) -> list[str]:
    return [n for n in names if not env(n)]


def _safe(e: Exception) -> str:
    """The API's error message, shortened, with anything key-like removed."""
    import re

    msg = getattr(e, "message", "") or str(e)
    return re.sub(r"sk-[\w-]+", "[key]", msg)[:160]


# ---------------------------------------------------------------- checks


def check_anthropic() -> Result:
    if missing("ANTHROPIC_API_KEY"):
        return Result(SKIP, "ANTHROPIC_API_KEY not set")
    import anthropic

    sys.path.insert(0, str(ROOT))
    try:  # the agent's own settings, so a typo in AGENT_* shows up here and not mid-call
        from agent.llm import ConfigError, LLMConfig

        params = {**LLMConfig().request_params(), "max_tokens": 256}
    except ConfigError as e:
        return Result(FAIL, str(e))
    except ImportError:  # check_keys copied somewhere on its own: fall back to the defaults
        params = {"model": env("AGENT_MODEL") or "claude-haiku-5-5", "max_tokens": 256,
                  "output_config": {"effort": env("AGENT_EFFORT") or "low"}}
    model = params["model"]
    client = anthropic.Anthropic(timeout=TIMEOUT, max_retries=1)
    try:
        resp = client.messages.create(**params, messages=[{"role": "user", "content": "Reply with the word: ok"}])
    except anthropic.AuthenticationError:
        return Result(FAIL, "key rejected (401) - check ANTHROPIC_API_KEY")
    except anthropic.PermissionDeniedError:
        return Result(FAIL, "key has no permission (403) - check workspace/billing")
    except anthropic.NotFoundError:
        return Result(FAIL, f"model '{model}' not found - check AGENT_MODEL")
    except anthropic.RateLimitError:
        return Result(OK, "key valid, but rate-limited right now (429) - add credit or wait")
    except anthropic.BadRequestError as e:
        return Result(FAIL, f"request rejected (400) - check AGENT_MODEL / AGENT_EFFORT / AGENT_THINKING: {_safe(e)}")
    except anthropic.APIStatusError as e:
        return Result(FAIL, f"API error {e.status_code}")
    except anthropic.APIConnectionError:
        return Result(FAIL, "cannot reach api.anthropic.com - check internet")
    return Result(OK, f"{model} replied ({resp.usage.input_tokens}+{resp.usage.output_tokens} tokens)")


def check_sarvam() -> Result:
    if missing("SARVAM_API_KEY"):
        return Result(SKIP, "SARVAM_API_KEY not set")
    try:
        r = httpx.post(
            "https://api.sarvam.ai/translate",
            headers={"api-subscription-key": env("SARVAM_API_KEY")},
            json={"input": "Hello", "source_language_code": "en-IN", "target_language_code": "hi-IN"},
            timeout=TIMEOUT,
        )
    except httpx.HTTPError:
        return Result(FAIL, "cannot reach api.sarvam.ai - check internet")
    if r.status_code in (401, 403):
        return Result(FAIL, f"key rejected ({r.status_code}) - check SARVAM_API_KEY")
    if r.status_code == 429:
        return Result(OK, "key valid, but rate-limited / out of credit (429)")
    if r.is_success:
        return Result(OK, "translate call succeeded")
    return Result(FAIL, f"unexpected status {r.status_code} - key may be fine, endpoint may have changed")


def check_livekit() -> Result:
    if m := missing("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        return Result(SKIP, f"{', '.join(m)} not set")
    from livekit import api

    async def _run() -> int:
        lk = api.LiveKitAPI(env("LIVEKIT_URL"), env("LIVEKIT_API_KEY"), env("LIVEKIT_API_SECRET"))
        try:
            rooms = await lk.room.list_rooms(api.ListRoomsRequest())
            return len(rooms.rooms)
        finally:
            await lk.aclose()

    try:
        n = asyncio.run(asyncio.wait_for(_run(), TIMEOUT))
    except asyncio.TimeoutError:
        return Result(FAIL, "timed out - check LIVEKIT_URL")
    except Exception as e:  # livekit raises its own TwirpError types
        msg = str(e).lower()
        if "unauthorized" in msg or "401" in msg or "permission" in msg:
            return Result(FAIL, "key/secret rejected - check LIVEKIT_API_KEY / LIVEKIT_API_SECRET")
        return Result(FAIL, f"{type(e).__name__} - check LIVEKIT_URL")
    return Result(OK, f"connected, {n} active room(s)")


def _http_get(url: str, ok_detail: str, **kw) -> Result:
    try:
        r = httpx.get(url, timeout=TIMEOUT, **kw)
    except httpx.HTTPError:
        return Result(FAIL, f"cannot reach {httpx.URL(url).host}")
    if r.status_code in (401, 403):
        return Result(FAIL, f"credentials rejected ({r.status_code})")
    if r.is_success:
        return Result(OK, ok_detail)
    return Result(FAIL, f"unexpected status {r.status_code}")


def check_whatsapp() -> Result:
    if m := missing("WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID"):
        return Result(SKIP, f"{', '.join(m)} not set (optional - simulated in dashboard)")
    return _http_get(
        f"https://graph.facebook.com/{env('WHATSAPP_PHONE_NUMBER_ID')}",
        "phone number ID readable with this token",
        headers={"Authorization": f"Bearer {env('WHATSAPP_TOKEN')}"},
    )


def check_plivo() -> Result:
    if m := missing("PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN"):
        return Result(SKIP, f"{', '.join(m)} not set (optional - browser call is the backup)")
    return _http_get(
        f"https://api.plivo.com/v1/Account/{env('PLIVO_AUTH_ID')}/",
        "account readable",
        auth=(env("PLIVO_AUTH_ID"), env("PLIVO_AUTH_TOKEN")),
    )


def check_deepgram() -> Result:
    if missing("DEEPGRAM_API_KEY"):
        return Result(SKIP, "DEEPGRAM_API_KEY not set (optional fallback)")
    return _http_get(
        "https://api.deepgram.com/v1/projects",
        "projects readable",
        headers={"Authorization": f"Token {env('DEEPGRAM_API_KEY')}"},
    )


def check_elevenlabs() -> Result:
    if missing("ELEVENLABS_API_KEY"):
        return Result(SKIP, "ELEVENLABS_API_KEY not set (optional fallback)")
    return _http_get(
        "https://api.elevenlabs.io/v1/models",
        "models readable",
        headers={"xi-api-key": env("ELEVENLABS_API_KEY")},
    )


FORCE: set[str] = set()  # checks asked for by name with --only always run


def postgres_in_use() -> bool:
    return env("JOURNEY_DB_URL").startswith("postgres")


def check_postgres() -> Result:
    if not postgres_in_use() and "postgres" not in FORCE:
        return Result(SKIP, "not used yet: the journey store is SQLite (JOURNEY_DB_URL). Run make db-check once Docker is in")
    url = env("DATABASE_URL") or "postgresql://sahay:sahay@localhost:5433/sahay"
    import psycopg

    try:
        with psycopg.connect(url, connect_timeout=5) as conn:
            row = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
    except psycopg.OperationalError:
        return Result(FAIL, "cannot connect - is Docker running? try: make up")
    if not row:
        return Result(FAIL, "connected, but pgvector extension missing")
    return Result(OK, f"connected, pgvector {row[0]}")


# name -> (check, needed by, required?)
CHECKS: dict[str, tuple[Callable[[], Result], str, bool]] = {
    "postgres": (check_postgres, "Day 1", True),
    "anthropic": (check_anthropic, "Day 1", True),
    "sarvam": (check_sarvam, "Day 1 evening", True),
    "livekit": (check_livekit, "Day 2", True),
    "whatsapp": (check_whatsapp, "Day 3", False),
    "plivo": (check_plivo, "Day 3", False),
    "deepgram": (check_deepgram, "Day 2", False),
    "elevenlabs": (check_elevenlabs, "Day 2", False),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", choices=list(CHECKS), help="run only these checks")
    args = ap.parse_args()
    names = args.only or list(CHECKS)
    FORCE.update(args.only or ())

    if not (ROOT / ".env").exists():
        print("No .env yet - run: cp .env.example .env  (then fill in your keys)\n")

    failed = False
    width = max(len(n) for n in names)
    for name in names:
        fn, when, required = CHECKS[name]
        try:
            res = fn()
        except Exception as e:  # a checker bug must not hide the other results
            res = Result(FAIL, f"checker crashed: {type(e).__name__}")
        tag = "required" if required else "optional"
        print(f"{res.status} {name:<{width}}  [{tag}, by {when}]  {res.detail}")
        if res.status == FAIL and required:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
