"""Per-call trace log: logs/<session>.jsonl, one line per event (BUILD_PLAN §5).

What was said, tools, safety events, model timings. Personal data is masked in the file
(phone numbers, dates, long numbers, and every name learned so far on the call). Names are
often learned after they were first said ("I'm Ramesh…" comes before verify_identity), so the
raw rows stay in memory and the file is rewritten whenever a new name is learned; otherwise
lines are simply appended. If the caller declines recording, no words are kept from then on.

Writing the trace must never break a call: on a disk error it switches itself off and says so once.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable

from journey.redact import redact_payload, redact_text

log = logging.getLogger("sahay.trace")


class Trace:
    def __init__(self, directory: str, session_id: str) -> None:
        self.path = Path(directory) / f"{session_id}.jsonl"
        self.session_id = session_id
        self.recording = True
        self.names: set[str] = set()
        self.rows: list[dict] = []
        self.broken = False
        self._t0 = time.monotonic()
        self._safe(lambda: Path(directory).mkdir(parents=True, exist_ok=True))

    def add_names(self, names: Iterable[str]) -> None:
        new = {n for n in names if n and len(n) >= 3} - self.names
        if new:
            self.names |= new
            self._safe(self._render)  # earlier lines may contain the new names

    def write(self, type_: str, **fields: Any) -> None:
        row: dict[str, Any] = {"t_ms": int((time.monotonic() - self._t0) * 1000), "type": type_, **fields}
        if not self.recording:
            if isinstance(row.get("text"), str):
                row["chars"], row["text"] = len(row["text"]), None
            if "args" in row:
                row["args"] = "[not recorded]"
        self.rows.append(row)
        line = json.dumps(self._masked(row), ensure_ascii=False, default=str)
        self._safe(lambda: self._append(line))
        if os.getenv("SAHAY_TRACE_STDOUT"):
            print(line)

    def _masked(self, row: dict) -> dict:
        out = dict(row)
        if isinstance(out.get("text"), str):
            out["text"] = redact_text(out["text"], self.names)
        if isinstance(out.get("args"), dict):
            out["args"] = redact_payload(out["args"], self.names)
        return out

    def _append(self, line: str) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _render(self) -> None:
        body = "".join(json.dumps(self._masked(r), ensure_ascii=False, default=str) + "\n" for r in self.rows)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(self.path)

    def _safe(self, fn) -> None:
        if self.broken:
            return
        try:
            fn()
        except OSError:
            self.broken = True
            log.exception("trace %s switched off: cannot write to %s", self.session_id, self.path)
