"""Hash chain for the append-only event log.

Each event stores prev_hash and hash = sha256(prev_hash + canonical JSON of the event).
Changing, deleting or reordering any stored event breaks every hash after it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

GENESIS = "0" * 64


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def event_body(journey_id: str, seq: int, ts: str, type_: str, actor: str, payload: Mapping[str, Any]) -> dict:
    return {"journey_id": journey_id, "seq": seq, "ts": ts, "type": type_, "actor": actor, "payload": payload}


def compute_hash(prev_hash: str, body: Mapping[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical(body)).encode("utf-8")).hexdigest()


@dataclass
class ChainReport:
    ok: bool
    events: int
    first_bad_seq: int | None = None
    reason: str | None = None

    def as_dict(self) -> dict:
        return {"ok": self.ok, "events": self.events, "first_bad_seq": self.first_bad_seq, "reason": self.reason}


def verify(rows: Iterable[Mapping[str, Any]]) -> ChainReport:
    """rows: stored events for ONE journey, in seq order (journey_id, seq, ts, type, actor, payload, prev_hash, hash)."""
    prev = GENESIS
    n = 0
    for expected_seq, r in enumerate(rows, start=1):
        n += 1
        if r["seq"] != expected_seq:
            return ChainReport(False, n, r["seq"], f"sequence gap: expected {expected_seq}, found {r['seq']}")
        if r["prev_hash"] != prev:
            return ChainReport(False, n, r["seq"], "prev_hash does not match the previous event")
        payload = r["payload"] if isinstance(r["payload"], Mapping) else json.loads(r["payload"])
        body = event_body(r["journey_id"], r["seq"], r["ts"], r["type"], r["actor"], payload)
        if compute_hash(prev, body) != r["hash"]:
            return ChainReport(False, n, r["seq"], "event content does not match its hash")
        prev = r["hash"]
    return ChainReport(True, n)
