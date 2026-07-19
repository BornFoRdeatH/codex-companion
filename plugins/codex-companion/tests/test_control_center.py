from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from codex_usage_monitor.budget import evaluate, transient_features
from codex_usage_monitor.config import load_config
from codex_usage_monitor.render import derive
from codex_usage_monitor.storage import Storage
from codex_usage_monitor.task_cockpit import build


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

    def test_task_cockpit_prioritizes_context_risk_and_is_advisory(self) -> None:
        summary = {}
        view = {
            "turn": {"turn_id": "t", "started_at": 90, "ended_at": 100, "total": 1200, "duration": 10},
            "tools": {"total_calls": 4, "failed_calls": 3, "file_edits": 1},
            "context": {"used_percent": 94, "source": "observed_renderer"},
            "budget": {"context_optimizer": {"status": "new_task_recommended"}},
            "advisor": {"items": [{"code": "split_request", "level": "info", "confidence": "medium", "source": "observed"}]},
            "compactions": {"count": 1, "last_time": 95},
        }
        result = build(summary, view, now=101)
        self.assertEqual(result["state"], "context_risk")
        self.assertEqual(result["recommended_action"], "new_task")
        self.assertTrue(result["advisory_only"])
        self.assertEqual(result["activity"]["failure_rate"], 0.75)
        self.assertNotIn("prompt", result)
        self.assertIn("turn_completed", [event["type"] for event in result["activity"]["events"]])

    def test_task_cockpit_estimated_context_does_not_create_critical_context_risk(self) -> None:
        result = build({}, {
            "turn": {"turn_id": "t", "ended_at": 10, "total": 500},
            "tools": {"total_calls": 1, "failed_calls": 0},
            "context": {"used_percent": 98, "source": "estimated"},
            "budget": {"context_optimizer": {"status": "new_task_recommended"}},
            "advisor": {}, "compactions": {"count": 0},
        }, now=11)
        self.assertNotEqual(result["state"], "context_risk")
        self.assertNotEqual(result["primary_recommendation"]["level"], "critical")

    def test_task_cockpit_preserves_canonical_recommendations_and_deduplicates(self) -> None:
        item = {"code": "slow_turn", "dedupe_key": "slow_turn", "level": "warning", "priority": 32,
                "action": "review", "title_key": "slow_turn", "what_happened_key": "slow_turn",
                "why_key": "slow_turn", "benefit_key": "slow_turn", "next_step_key": "slow_turn",
                "scope": "current_turn", "confidence": "high", "source": "observed",
                "evidence": {"duration_seconds": 180}}
        result = build({}, {"turn": {"ended_at": 1}, "tools": {}, "context": {}, "budget": {},
                            "advisor": {"all_items": [item, {**item, "level": "info"}]}, "compactions": {}}, now=2)
        self.assertEqual(len(result["recommendations"]), 1)
        self.assertEqual(result["recommendations"][0]["what_happened_key"], "slow_turn")
        self.assertEqual(result["recommendations"][0]["scope"], "current_turn")

    def test_task_cockpit_events_are_technical_only(self) -> None:
        result = build({}, {"turn": {}, "tools": {}, "context": {"source": "unavailable"},
                            "advisor": {}, "budget": {}, "compactions": {}}, now=1)
        serialized = str(result)
        self.assertNotIn("assistant text", serialized)
        self.assertNotIn("secret prompt", serialized)
        self.assertIn("advisory_only", serialized)

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
