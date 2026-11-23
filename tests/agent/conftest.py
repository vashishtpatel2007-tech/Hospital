"""Stage 3 fixtures: an Agent wired to a real Journey Engine and Twin, with a fake model."""

from __future__ import annotations

import pytest

from agent.brain import Agent
from agent.llm import LLMConfig
from agent.port import LocalJourney
from tests.journey.conftest import (KAVYA, LAKSHMI, NEW_CALLER_PHONE, RAMESH, clock, dob, eng, outbox,  # noqa: F401
                                    twin_admin)

FAST = dict(filler_after=0.15, first_output_timeout=0.6, total_timeout=3.0)


@pytest.fixture
def port(eng):
    return LocalJourney(eng)


@pytest.fixture
def make_agent(port, tmp_path):
    def make(client, phone=RAMESH["phone"], language="hi-en", model="claude-haiku-5-5", **cfg):
        a = Agent(port, phone=phone, channel="voice", language=language, client=client,
                  config=LLMConfig(model=model, **{**FAST, **cfg}), trace_dir=str(tmp_path / "logs"))
        a.start()
        return a
    return make
