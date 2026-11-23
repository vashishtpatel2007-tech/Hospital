"""One streamed Claude request with the timing rules a phone call needs (BUILD_PLAN Stage 3, BLUEPRINT §9).

- No text or tool call within FILLER_AFTER (1.5 s): tell the caller to wait ("Ek second…"), once per turn.
- No text or tool call within AGENT_LLM_TIMEOUT: give up on this attempt (the agent retries once).
- Errors, refusals and truncated tool calls come back as LLMFailure, never as a crash.

The stream runs on a worker thread and hands events over a queue, so the call can wait with a
timeout. The SDK's own retries are off: on a live call one quick retry beats three slow ones.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

FILLER_AFTER = 1.5

# Models that accept {"role": "system"} inside messages (the state note). Others get it as a text block.
MID_CONVERSATION_SYSTEM = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8",
                           "claude-fable-5", "claude-mythos-5")


EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Models on which thinking cannot be switched off at all (only effort lowers it).
_NO_THINKING_OFF = ("claude-opus-5-5", "claude-fable-5", "claude-mythos-5")


class ConfigError(ValueError):
    """A setting in .env that would make every model call fail. Raised at start, not mid-call."""


@dataclass
class LLMConfig:
    model: str = field(default_factory=lambda: os.getenv("AGENT_MODEL") or "claude-haiku-5-5")
    effort: str = field(default_factory=lambda: os.getenv("AGENT_EFFORT") or "low")
    thinking: str = field(default_factory=lambda: os.getenv("AGENT_THINKING") or "adaptive")  # "adaptive" or "off"
    max_tokens: int = 2048
    first_output_timeout: float = field(default_factory=lambda: os.getenv("AGENT_LLM_TIMEOUT") or 6.0)
    total_timeout: float = 25.0
    filler_after: float = FILLER_AFTER

    def __post_init__(self) -> None:
        self.model, self.effort, self.thinking = self.model.strip(), self.effort.strip().lower(), self.thinking.strip().lower()
        if not self.model.startswith("claude-"):
            raise ConfigError(f"AGENT_MODEL={self.model!r} does not look like a Claude model id (e.g. claude-haiku-5-5)")
        if self.effort not in EFFORTS:
            raise ConfigError(f"AGENT_EFFORT={self.effort!r}: use one of {', '.join(EFFORTS)}")
        if self.thinking not in ("adaptive", "off"):
            raise ConfigError(f"AGENT_THINKING={self.thinking!r}: use adaptive or off")
        try:
            self.first_output_timeout = float(self.first_output_timeout)
        except (TypeError, ValueError):
            raise ConfigError(f"AGENT_LLM_TIMEOUT={self.first_output_timeout!r} is not a number of seconds") from None
        if not 0.1 <= self.first_output_timeout <= 60:
            raise ConfigError("AGENT_LLM_TIMEOUT must be between 1 and 60 seconds")
        if self.thinking == "off":
            if self.model.startswith(_NO_THINKING_OFF):
                raise ConfigError(f"{self.model} can't run with thinking off; keep AGENT_THINKING=adaptive and lower AGENT_EFFORT")
            if self.effort in ("xhigh", "max"):
                raise ConfigError("AGENT_THINKING=off only works with AGENT_EFFORT low, medium or high")

    @property
    def system_messages_inline(self) -> bool:
        return self.model.startswith(MID_CONVERSATION_SYSTEM)

    def request_params(self) -> dict:
        p: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens,
                             "output_config": {"effort": self.effort}}
        if self.thinking == "off":
            # Sonnet 5.5 rejects "disabled"; "between_tools" is its thinking-off mode.
            p["thinking"] = {"type": "between_tools" if self.model.startswith("claude-sonnet-5-5") else "disabled"}
        return p


class LLMFailure(Exception):
    def __init__(self, kind: str, detail: str = "", partial_text: str = ""):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail, self.partial_text = kind, detail, partial_text


@dataclass
class StreamEvent:
    kind: str  # "text", "filler", "final"
    text: str = ""
    message: Any = None
    ttft_ms: int | None = None


def default_client(max_retries: int = 0):
    """Live calls: no SDK retries (the agent retries once itself, faster). A request that hangs is cut by
    the agent's own timers; the SDK timeout only stops an abandoned connection lingering for minutes."""
    import anthropic

    return anthropic.Anthropic(max_retries=max_retries, timeout=anthropic.Timeout(30.0, connect=5.0))


def stream_reply(client, cfg: LLMConfig, *, system: Any, tools: list[dict], messages: list[dict],
                 allow_filler: bool) -> Iterator[StreamEvent]:
    """Yield text deltas as they arrive (and at most one filler), then one "final" event with the message.

    Raises LLMFailure on timeout, API error, refusal, or a tool call cut off by max_tokens.
    """
    q: queue.Queue = queue.Queue()
    holder: dict[str, Any] = {}
    params = {**cfg.request_params(), "system": system, "tools": tools, "messages": messages,
              "cache_control": {"type": "ephemeral"}}

    def worker() -> None:
        try:
            with client.messages.stream(**params) as stream:
                holder["stream"] = stream
                for ev in stream:
                    if holder.get("abandoned"):
                        return
                    etype = getattr(ev, "type", None)
                    if etype == "text":
                        q.put(("text", ev.text))
                    elif etype == "content_block_start" and getattr(ev.content_block, "type", None) == "tool_use":
                        q.put(("alive", None))
                q.put(("final", stream.get_final_message()))
        except Exception as e:  # noqa: BLE001 - handed to the caller as an LLMFailure
            q.put(("error", e))

    t0 = time.monotonic()
    threading.Thread(target=worker, daemon=True, name="llm-stream").start()
    text_so_far: list[str] = []

    def abandon(kind: str, detail: str) -> LLMFailure:
        holder["abandoned"] = True
        s = holder.get("stream")
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
        return LLMFailure(kind, detail, "".join(text_so_far))

    try:
        yield from _pump(q, holder, cfg, t0, allow_filler, abandon, text_so_far)
    finally:  # normal end, an LLMFailure, or the caller interrupting (generator closed): stop the worker
        holder["abandoned"] = True
        s = holder.get("stream")
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def _pump(q, holder, cfg, t0, allow_filler, abandon, text_so_far) -> Iterator[StreamEvent]:
    got_output = False
    filler_sent = not allow_filler
    ttft: int | None = None
    while True:
        now = time.monotonic() - t0
        if not got_output:
            deadline = cfg.first_output_timeout if filler_sent else min(cfg.filler_after, cfg.first_output_timeout)
        else:
            deadline = cfg.total_timeout
        try:
            kind, val = q.get(timeout=max(0.0, deadline - now))
        except queue.Empty:
            if not got_output and not filler_sent and time.monotonic() - t0 >= cfg.filler_after:
                filler_sent = True
                yield StreamEvent("filler")
                continue
            raise abandon("timeout", f"no reply within {deadline:.1f}s")
        if kind == "error":
            raise abandon("api_error", f"{type(val).__name__}: {str(val)[:200]}")
        if kind in ("text", "alive"):
            if not got_output:
                ttft = int((time.monotonic() - t0) * 1000)
            got_output = True
            if kind == "text":
                text_so_far.append(val)
                yield StreamEvent("text", val, ttft_ms=ttft)
            continue
        # final
        msg = val
        stop = getattr(msg, "stop_reason", None)
        if stop == "refusal":
            raise LLMFailure("refusal", str(getattr(msg, "stop_details", "") or ""), "".join(text_so_far))
        if stop == "max_tokens" and any(getattr(b, "type", None) == "tool_use" for b in msg.content):
            raise LLMFailure("truncated", "tool call cut off by max_tokens", "".join(text_so_far))
        if ttft is None:
            ttft = int((time.monotonic() - t0) * 1000)
        yield StreamEvent("final", message=msg, ttft_ms=ttft)
        return
