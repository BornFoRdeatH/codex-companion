from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import sqlite3
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HOOK = PLUGIN_ROOT / "scripts" / "hook.py"


class HookContractTests(unittest.TestCase):
    def run_hook(self, event: str, extra: dict | None = None) -> dict:
        payload = {
            "session_id": "session-contract",
            "turn_id": "turn-contract",
            "cwd": "/secret/project",
            "hook_event_name": event,
            "model": "gpt-test",
            "prompt": "SECRET PROMPT MUST NOT APPEAR",
            "tool_input": {"command": "SECRET COMMAND MUST NOT APPEAR"},
        }
        payload.update(extra or {})
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env.update(
                {
                    "PLUGIN_ROOT": str(PLUGIN_ROOT),
                    "PLUGIN_DATA": directory,
                    "CODEX_USAGE_MONITOR_NO_COLLECTOR": "1",
                }
            )
            result = subprocess.run(
                [sys.executable, str(HOOK)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=10,
                check=True,
            )
            self.assertEqual(result.stderr, "")
            self.assertNotIn("SECRET", result.stdout)
            self.assertEqual(len(result.stdout.strip().splitlines()), 1)
            value = json.loads(result.stdout)
            self.assertNotIn("additionalContext", value)
            self.assertTrue(value["continue"])
            return value

    def test_display_events_return_only_supported_fields(self) -> None:
        for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "PreCompact", "PostCompact", "SubagentStop", "Stop"):
            with self.subTest(event=event):
                value = self.run_hook(event, {"tool_use_id": "tool-1", "tool_name": "Bash"})
                self.assertLessEqual(set(value), {"continue", "systemMessage"})

    def test_non_display_events_are_silent(self) -> None:
        value = self.run_hook("PreToolUse", {"tool_use_id": "tool-1", "tool_name": "Bash"})
        self.assertEqual(value, {"continue": True})

    def test_opt_in_prompt_coach_persists_only_derived_features(self) -> None:
        secret = "ULTRA SECRET PROMPT: implement three things without dependencies"
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            config = (PLUGIN_ROOT / "config.default.toml").read_text(encoding="utf-8")
            config = config.replace("[ui.advisor.prompt_coach]\nenabled = false", "[ui.advisor.prompt_coach]\nenabled = true")
            (data / "config.toml").write_text(config, encoding="utf-8")
            env = os.environ.copy()
            env.update({"PLUGIN_ROOT": str(PLUGIN_ROOT), "PLUGIN_DATA": str(data),
                        "CODEX_USAGE_MONITOR_NO_COLLECTOR": "1"})
            result = subprocess.run(
                [sys.executable, str(HOOK)], input=json.dumps({"session_id": "s", "turn_id": "t",
                    "hook_event_name": "UserPromptSubmit", "model": "gpt-test", "prompt": secret}),
                capture_output=True, text=True, encoding="utf-8", env=env, timeout=10, check=True,
            )
            self.assertNotIn(secret, result.stdout)
            database = data / "usage.sqlite3"
            connection = sqlite3.connect(database)
            try:
                row = connection.execute("SELECT * FROM prompt_features").fetchone()
                self.assertIsNotNone(row)
            finally:
                connection.close()
            for path in data.rglob("*"):
                if path.is_file() and path.suffix not in {".sqlite3", ".db", ".wal", ".shm"}:
                    self.assertNotIn(secret, path.read_text(encoding="utf-8", errors="ignore"))


if __name__ == "__main__":
    unittest.main()
