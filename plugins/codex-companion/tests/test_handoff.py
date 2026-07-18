from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from codex_usage_monitor.storage import Storage
from codex_usage_monitor.config import load_config
from codex_usage_monitor.ui_host import UiHost


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HOOK = PLUGIN_ROOT / "scripts" / "hook.py"


class HandoffTests(unittest.TestCase):
    def test_marker_records_only_metadata_and_not_prompt(self) -> None:
        nonce = "0123456789abcdef0123456789abcdef"
        secret = "PRIVATE HANDOFF INSTRUCTION"
        prompt = f"<!-- codex-companion-handoff:{nonce} -->\n{secret}"
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env.update({"PLUGIN_ROOT": str(PLUGIN_ROOT), "PLUGIN_DATA": directory,
                        "CODEX_USAGE_MONITOR_NO_COLLECTOR": "1"})
            result = subprocess.run(
                [sys.executable, str(HOOK)], input=json.dumps({"session_id": "s", "turn_id": "t",
                    "hook_event_name": "UserPromptSubmit", "model": "gpt-test", "prompt": prompt}),
                capture_output=True, text=True, encoding="utf-8", env=env, timeout=10, check=True,
            )
            self.assertEqual(json.loads(result.stdout), {"continue": True})
            connection = sqlite3.connect(Path(directory) / "usage.sqlite3")
            try:
                row = connection.execute(
                    "SELECT session_id,turn_id,nonce,state FROM handoff_requests"
                ).fetchone()
                self.assertEqual(row, ("s", "t", nonce, "pending"))
                columns = [item[1] for item in connection.execute("PRAGMA table_info(handoff_requests)")]
                self.assertFalse({"prompt", "summary", "content", "hash"} & set(columns))
            finally:
                connection.close()
            self.assertNotIn(secret.encode(), (Path(directory) / "usage.sqlite3").read_bytes())

    def test_invalid_marker_is_ignored_and_reload_cancels_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "usage.sqlite3")
            storage.register_handoff("s", "t", "a" * 32)
            self.assertEqual(len(storage.pending_handoffs("s")), 1)
            self.assertEqual(storage.cancel_pending_handoffs(), 1)
            self.assertEqual(storage.pending_handoffs("s"), [])
            self.assertEqual(storage.handoff_lifecycle("a" * 32)[0]["state"], "expired")
            with self.assertRaises(ValueError):
                storage.register_handoff("s", "bad", "not-a-nonce")
            storage.close()

    def test_lifecycle_stores_only_metadata_and_supports_target_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "usage.sqlite3")
            nonce = "b" * 32
            storage.create_handoff("source", nonce, "handoff")
            self.assertEqual(storage.handoff_lifecycle(nonce)[0]["state"], "created")
            storage.transition_handoff(nonce, "opened", source_turn_id="turn")
            storage.transition_handoff(nonce, "prefilling")
            storage.transition_handoff(nonce, "ready", target_session_id="target")
            row = storage.handoff_lifecycle(nonce)[0]
            self.assertEqual((row["source_turn_id"], row["target_session_id"], row["state"]), ("turn", "target", "ready"))
            columns = {item[1] for item in storage.conn.execute("PRAGMA table_info(handoff_lifecycle)")}
            self.assertFalse({"prompt", "summary", "content", "markdown", "text", "diff"} & columns)
            storage.close()

    def test_checkpoint_mode_is_preserved_when_marker_is_registered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "usage.sqlite3")
            nonce = "c" * 32
            storage.create_handoff("source", nonce, "checkpoint")
            storage.register_handoff("source", "turn", nonce)
            row = storage.handoff_lifecycle(nonce)[0]
            self.assertEqual((row["mode"], row["state"], row["source_turn_id"]), ("checkpoint", "submitted", "turn"))
            storage.close()

    def test_git_summary_uses_verified_cwd_and_returns_no_diff_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "tracked.txt").write_text("secret file content", encoding="utf-8")
            transcript = root / "session.jsonl"
            transcript.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": str(repo)}}) + "\n", encoding="utf-8")
            config = load_config(PLUGIN_ROOT, root / "data")
            storage = Storage(root / "data" / "usage.sqlite3")
            digest = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:16]
            storage.upsert_session("s", str(transcript), "gpt-test", digest)
            host = UiHost(PLUGIN_ROOT, root / "data", config, storage)
            host.active_thread_id = "s"

            class Connection:
                expression = ""
                def call(self, method, params, timeout=1.0):
                    self.expression = params["expression"]

            connection = Connection()
            host._respond_git_summary(connection, {"requestId": "request"})
            self.assertIn("tracked.txt", connection.expression)
            self.assertNotIn("secret file content", connection.expression)
            self.assertNotIn(str(repo), connection.expression)
            storage.close()


if __name__ == "__main__":
    unittest.main()
