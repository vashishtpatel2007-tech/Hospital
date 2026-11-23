"""A stand-in for anthropic.Anthropic: scripted replies, tool calls, delays, errors and refusals.

Only the surface the agent uses: client.messages.stream(**params) as a context manager that
yields "text" / "content_block_start" events and has get_final_message() and close().
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

_ids = itertools.count(1)


@dataclass
class Reply:
    items: list = field(default_factory=list)  # ("text", str) | ("tool", name, input)
    stop_reason: str | None = None
    delay: float = 0.0  # before the first event
    error: Exception | None = None  # raised instead of streaming
    chunk: int = 7  # stream text in pieces this long (tests sentence splitting)


def text(t: str) -> tuple:
    return ("text", t)


def tool(tool_name: str, /, **inp) -> tuple:
    return ("tool", tool_name, inp)


def say(*parts: str, **kw) -> Reply:
    return Reply([text(" ".join(parts))], **kw)


def call(tool_name: str, preamble: str | None = None, /, **inp) -> Reply:
    return Reply(([text(preamble)] if preamble else []) + [tool(tool_name, **inp)])


class _Stream:
    def __init__(self, reply: Reply) -> None:
        self.reply = reply
        self.closed = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self) -> None:
        self.closed.set()

    def __iter__(self):
        r = self.reply
        if r.delay:
            if self.closed.wait(r.delay):
                return
        if r.error:
            raise r.error
        for item in r.items:
            if item[0] == "text":
                for i in range(0, len(item[1]), r.chunk):
                    yield SimpleNamespace(type="text", text=item[1][i:i + r.chunk])
            else:
                yield SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="tool_use"))

    def get_final_message(self):
        blocks = []
        for item in self.reply.items:
            if item[0] == "text":
                blocks.append(SimpleNamespace(type="text", text=item[1]))
            else:
                blocks.append(SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids):04d}", name=item[1], input=item[2]))
        stop = self.reply.stop_reason or ("tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")
        return SimpleNamespace(content=blocks, stop_reason=stop, stop_details=None,
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0))


class FakeClient:
    """script: a list of Reply, or callables (params) -> Reply, used in order. Records every request."""

    def __init__(self, script: list[Reply | Callable[[dict], Reply]] | Callable[[dict], Reply]) -> None:
        self.script = script
        self.requests: list[dict] = []
        self.messages = self

    def stream(self, **params):
        snap = dict(params)
        snap["messages"] = [dict(m) for m in params["messages"]]
        self.requests.append(snap)
        if callable(self.script):
            reply = self.script(params)
        else:
            if not self.script:
                raise AssertionError("the agent asked the model more times than the test scripted")
            entry = self.script.pop(0)
            reply = entry(params) if callable(entry) else entry
        return _Stream(reply)


def last_tool_results(params: dict) -> list[dict]:
    """The tool results the agent sent back in this request, decoded."""
    for m in reversed(params["messages"]):
        if m["role"] == "user" and isinstance(m["content"], list):
            out = [json.loads(b["content"]) for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
            if out:
                return out
        if m["role"] == "user":
            break
    return []


def state_note(params: dict) -> str:
    for m in reversed(params["messages"]):
        if m["role"] == "system":
            return m["content"]
        if m["role"] == "user" and isinstance(m["content"], list):
            for b in reversed(m["content"]):
                if isinstance(b, dict) and b.get("type") == "text" and b["text"].startswith("[State note"):
                    return b["text"]
    return ""


def wait(seconds: float) -> None:
    time.sleep(seconds)
