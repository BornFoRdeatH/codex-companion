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

    def test_context_optimizer_forecasts_next_turn_and_recommends_checkpoint(self) -> None:
        summary = {
            "view": {"context": {"used_percent": 70, "window": 1000, "source": "observed_renderer"},
                     "compactions": {"count": 0, "last_time": None}},
            "budget_baseline": {"context_delta": {"count": 10, "median": 100, "mad": 0}},
        }
        value = evaluate(summary, self.config, {})["context_optimizer"]
        self.assertEqual(value["next_turn_percent"], 80.0)
        self.assertEqual(value["safe_turns_remaining"], 1)
        self.assertEqual(value["status"], "checkpoint_recommended")
        self.assertEqual(value["recommended_action"], "checkpoint")
        self.assertEqual(value["confidence"], "high")

    def test_context_optimizer_estimated_data_is_informational_only(self) -> None:
        summary = {
            "view": {"context": {"used_percent": 95, "window": 1000, "source": "estimated"},
                     "compactions": {"count": 2, "last_time": 123.0}},
            "budget_baseline": {"context_delta": {"count": 20, "median": 100, "mad": 0}},
        }
        value = evaluate(summary, self.config, {})["context_optimizer"]
        self.assertEqual(value["level"], "info")
        self.assertNotEqual(value["level"], "critical")
        self.assertIn("repeated_compactions", value["reasons"])
        self.assertEqual(value["compactions"]["impact"], "unavailable")

    def test_context_optimizer_status_progression(self) -> None:
        baseline = {"context_delta": {"count": 10, "median": 100, "mad": 0}}
        statuses = []
        for used in (50, 60, 70, 80, 90):
            summary = {"view": {"context": {"used_percent": used, "window": 1000, "source": "official"},
                                 "compactions": {}}, "budget_baseline": baseline}
            statuses.append(evaluate(summary, self.config, {})["context_optimizer"]["status"])
        self.assertEqual(statuses, ["healthy", "watch", "checkpoint_recommended", "handoff_recommended", "new_task_recommended"])

    def test_context_optimizer_handles_missing_context_and_prompt_features(self) -> None:
        summary = {"view": {"context": {"source": "unavailable"}, "compactions": {}}, "budget_baseline": {}}
        value = evaluate(summary, self.config, {"prompt": "secret"})["context_optimizer"]
        self.assertEqual(value["status"], "unavailable")
        self.assertNotIn("prompt", value)

    def test_context_optimizer_compaction_and_prompt_features_are_numeric_only(self) -> None:
        summary = {
            "view": {"context": {"used_percent": 78, "window": 1000, "source": "observed_renderer"},
                     "compactions": {"count": 1, "last_time": 123.0}},
            "budget_baseline": {"context_delta": {"count": 2, "median": 100, "mad": 10}},
        }
        value = evaluate(summary, self.config, {"char_count": 5000, "multi_task": True, "text": "secret"})["context_optimizer"]
        self.assertIn("long_prompt", value["reasons"])
        self.assertIn("multi_task", value["reasons"])
        self.assertNotIn("text", value)

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
