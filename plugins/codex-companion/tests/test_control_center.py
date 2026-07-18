from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from codex_usage_monitor.budget import evaluate, transient_features
from codex_usage_monitor.config import load_config
from codex_usage_monitor.render import derive
from codex_usage_monitor.storage import Storage


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class ControlCenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = load_config(PLUGIN_ROOT, self.root)
        self.storage = Storage(self.root / "control.sqlite3")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()

    def test_transient_budget_features_drop_text_and_clamp_numbers(self) -> None:
        result = transient_features({"char_count": 500, "line_count": 4, "multi_task": True, "prompt": "secret"})
        self.assertEqual(result["char_count"], 500)
        self.assertTrue(result["multi_task"])
        self.assertNotIn("prompt", result)

    def test_budget_is_model_and_project_scoped_and_advisory(self) -> None:
        self.storage.upsert_session("s", None, "gpt-test", "project-hash")
        now = time.time()
        for index in range(12):
            self.storage.conn.execute(
                """INSERT INTO turn_aggregates(turn_id,session_id,started_at,ended_at,model,total_tokens,
                   primary_quota_delta,materialized_at) VALUES(?,?,?,?,?,?,?,?)""",
                (f"t{index}", "s", now-index, now-index, "gpt-test", 1000+index*10, 0.5, now),
            )
        self.storage.conn.commit()
        summary = self.storage.summary("s", None)
        summary["view"] = derive(summary, self.config)
        value = evaluate(summary, self.config, {"char_count": 5000, "multi_task": True})
        self.assertTrue(value["advisory_only"])
        self.assertEqual(value["baseline_samples"], 12)
        self.assertEqual(value["confidence"], "high")
        self.assertIn("long_prompt", value["reasons"])

    def test_project_alias_and_aggregates_do_not_expose_cwd(self) -> None:
        self.storage.upsert_session("s", None, "gpt-test", "abcdef1234567890")
        now = time.time()
        self.storage.conn.execute(
            """INSERT INTO turn_aggregates(turn_id,session_id,started_at,ended_at,model,total_tokens,
               duration_seconds,tool_calls,failed_tool_calls,file_edits,compaction_count,materialized_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("t", "s", now-2, now, "gpt-test", 1234, 2, 3, 1, 1, 0, now),
        )
        self.storage.conn.commit()
        self.storage.set_project_alias("abcdef1234567890", "My Project")
        payload = self.storage.project_insights("abcdef1234567890")
        self.assertEqual(payload["project"]["alias"], "My Project")
        self.assertEqual(payload["totals"]["total_tokens"], 1234)
        self.assertNotIn("cwd", payload["project"])


if __name__ == "__main__":
    unittest.main()
