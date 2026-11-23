"""Shared fixtures: a real Hospital Twin server on a free port with a throwaway database."""

from __future__ import annotations

import os

# The suite never reads the developer's .env: modules call load_dotenv() on import, and it never
# overrides a variable that is already set. So pin every setting here, with no API key (no test may
# reach a paid API) and dead addresses for the live servers (no test may touch `make twin` on :8001).
os.environ.update({
    "ANTHROPIC_API_KEY": "", "AGENT_MODEL": "claude-haiku-5-5", "AGENT_EFFORT": "low", "AGENT_THINKING": "adaptive",
    "AGENT_LLM_TIMEOUT": "6", "SIM_CALLER_MODEL": "claude-sonnet-5-5", "HOSPITAL_NAME": "Sunrise Multispeciality Hospital",
    "SAHAY_TRACE_STDOUT": "", "PHI_HASH_SALT": "test-salt", "TWIN_URL": "http://127.0.0.1:9", "JOURNEY_URL": "http://127.0.0.1:9",
    "JOURNEY_DB_URL": "sqlite://", "TWIN_DB_URL": "sqlite://", "DATABASE_URL": "postgresql://test@127.0.0.1:9/none",
})

import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from twin.app import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def twin_server(tmp_path_factory) -> str:
    db_path: Path = tmp_path_factory.mktemp("twin") / "twin.db"
    app = create_app(f"sqlite:///{db_path}")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("Twin server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
