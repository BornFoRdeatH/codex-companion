from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from codex_usage_monitor.advisor import analyze_prompt, evaluate, robust_stats
from codex_usage_monitor.config import load_config
from codex_usage_monitor.storage import Storage


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class AdvisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = load_config(PLUGIN_ROOT, self.root)
        self.storage = Storage(Path(self.config.get("storage.database")))

    def tearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()

    def test_prompt_coach_uk_en_and_no_text_features(self) -> None:
        uk = analyze_prompt("Зроби зміни.\n- Додай API\n- Онови UI\n- Напиши документацію")
        en = analyze_prompt("Implement the change in app.py without dependencies and verify with tests.")
        self.assertIn("split_request", uk["recommendation_codes"])
        self.assertNotIn("add_target", en["recommendation_codes"])
        self.assertFalse(any(key in uk for key in ("prompt", "text", "hash", "fragments")))

    def test_context_thresholds_and_estimated_restriction(self) -> None:
        base = {"turn": {}, "tools": {}, "compactions": {}, "primary": {}}
        estimated = evaluate({}, {**base, "context": {"used_percent": 95, "source": "estimated"}}, self.config)
        self.assertNotIn("start_new_chat", [item["code"] for item in estimated["items"]])
        observed = evaluate({}, {**base, "context": {"used_percent": 92, "source": "observed_renderer"}}, self.config)
        item = next(item for item in observed["items"] if item["code"] == "start_new_chat")
        self.assertEqual(item["level"], "critical")

    def test_robust_baseline_is_model_isolated_and_outlier_resistant(self) -> None:
        now = time.time()
        for model, values in (("model-a", [100] * 10 + [10000]), ("model-b", [900] * 10)):
            for index, value in enumerate(values):
                self.storage.conn.execute(
                    """INSERT INTO turn_aggregates(turn_id,session_id,started_at,ended_at,model,total_tokens,
                       input_tokens,cached_input_tokens,output_tokens,reasoning_tokens,duration_seconds,tool_seconds,
                       tool_calls,failed_tool_calls,file_edits,compaction_count,materialized_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"{model}-{index}", model, now-index-1, now-index, model, value, value, 0, 0, 0, 1, 0, 0, 0, 0, 0, now),
                )
        self.storage.conn.commit()
        baseline = self.storage.advisor_baseline("model-a", 50)
        self.assertEqual(baseline["total_tokens"]["median"], 100)
        self.assertEqual(baseline["total_tokens"]["mad"], 0)
        self.assertEqual(self.storage.advisor_baseline("model-b", 50)["total_tokens"]["median"], 900)
        self.assertEqual(robust_stats([1, 1, 1, 999])["median"], 1)

    def test_personal_threshold_triggers_below_fixed_expensive_threshold(self) -> None:
        stats = {"count": 10, "median": 1000, "mad": 50}
        summary = {"advisor_baseline": {"total_tokens": stats, "quota_delta": {}, "tool_calls": {},
                                         "tool_seconds": {}, "reasoning_tokens": {}}, "prompt_features": None}
        view = {"turn": {"total": 3000}, "tools": {}, "compactions": {}, "context": {}, "primary": {}}
        result = evaluate(summary, view, self.config)
        self.assertEqual(result["items"][0]["code"], "narrow_request")
        self.assertEqual(result["items"][0]["confidence"], "high")

    def test_exploration_and_advice_persistence_are_numeric_only(self) -> None:
        summary = {"advisor_baseline": {}, "prompt_features": None}
        view = {"turn": {"total": 10}, "tools": {"total_calls": 5, "failed_calls": 1, "tool_seconds": 2},
                "compactions": {}, "context": {}, "primary": {}}
        advice = evaluate(summary, view, self.config)
        item = next(item for item in advice["items"] if item["code"] == "reduce_exploration")
        self.storage.save_advice("s", "t", [item])
        row = self.storage.conn.execute("SELECT * FROM turn_advice").fetchone()
        evidence = json.loads(row["evidence_json"])
        self.assertTrue(all(value is None or isinstance(value, (bool, int, float)) for value in evidence.values()))
        dump = " ".join(str(value) for value in row)
        self.assertNotIn("prompt", dump.casefold())

    def test_schema_v2_to_v3_backfills_materialized_metrics(self) -> None:
        self.storage.upsert_session("migration", None, "model-a", None)
        self.storage.start_turn("migration-turn", "migration")
        self.storage.record_tool_start({"tool_use_id": "failed", "tool_name": "shell_command"}, "migration", "migration-turn")
        self.storage.record_tool_end({"tool_use_id": "failed", "tool_name": "shell_command",
                                      "tool_response": {"exit_code": 1}}, "migration", "migration-turn")
        self.storage.add_tokens("migration", "migration-turn", {
            "total": {"input_tokens": 80, "cached_input_tokens": 40, "output_tokens": 20, "total_tokens": 100},
            "last": {"input_tokens": 80, "cached_input_tokens": 40, "output_tokens": 20, "total_tokens": 100},
            "model_context_window": 1000,
        }, time.time(), "test")
        self.storage.end_turn("migration-turn")
        self.storage.conn.execute("UPDATE turn_aggregates SET tool_calls=NULL,failed_tool_calls=NULL,cache_hit_percent=NULL")
        self.storage.set_meta("schema_version", "2")
        path = self.storage.path
        self.storage.close()
        self.storage = Storage(path)
        row = self.storage.conn.execute("SELECT * FROM turn_aggregates WHERE turn_id='migration-turn'").fetchone()
        self.assertEqual(self.storage.get_meta("schema_version"), "3")
        self.assertEqual(row["tool_calls"], 1)
        self.assertEqual(row["failed_tool_calls"], 1)
        self.assertEqual(row["cache_hit_percent"], 50)


if __name__ == "__main__":
    unittest.main()
