from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .storage import Storage


def parse_timestamp(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return 0.0


class TranscriptParser:
    """Incremental, content-minimizing parser for unstable rollout JSONL."""

    def __init__(self, storage: Storage):
        self.storage = storage

    def ingest(self, transcript_path: str | None, session_id: str) -> int:
        if not transcript_path:
            return 0
        path = Path(transcript_path)
        if not path.is_file():
            return 0
        offset_key = f"transcript_offset:{path}"
        try:
            offset = int(self.storage.get_meta(offset_key, "0"))
        except ValueError:
            offset = 0
        size = path.stat().st_size
        if offset < 0 or offset > size:
            offset = 0
        count = 0
        active = self.storage.active_turn(session_id)
        turn_id = active["turn_id"] if active else None
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            while True:
                line = handle.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if self._ingest_event(event, session_id, turn_id):
                    count += 1
            self.storage.set_meta(offset_key, str(handle.tell()))
        return count

    def _ingest_event(self, event: dict[str, Any], session_id: str, turn_id: str | None) -> bool:
        observed_at = parse_timestamp(event.get("timestamp"))
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return False
        if event.get("type") == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info")
            if isinstance(info, dict):
                self.storage.add_tokens(session_id, turn_id, info, observed_at, "experimental_transcript")
            rates = payload.get("rate_limits")
            if isinstance(rates, dict):
                self.storage.add_rate_limits(rates, observed_at, "experimental_transcript")
            return True
        # Only metadata is inspected below. Text and tool arguments are intentionally ignored.
        payload_type = str(payload.get("type") or "")
        if payload_type in {"compact", "compaction", "context_compacted"}:
            self.storage.record_compaction(session_id, turn_id, "observed", payload.get("trigger"))
            return True
        return False
