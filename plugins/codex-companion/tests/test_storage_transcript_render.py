from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from codex_usage_monitor.config import load_config
from codex_usage_monitor.render import _delta, _guard, derive, progress, render, render_template
from codex_usage_monitor.storage import Storage
from codex_usage_monitor.transcript import TranscriptParser


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "transcript.jsonl"


class StorageTranscriptRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = load_config(PLUGIN_ROOT, self.root)
        self.storage = Storage(Path(self.config.get("storage.database")))

    def tearDown(self) -> None:
        self.storage.close()
        self.temp.cleanup()

    def test_incremental_transcript_parser_and_context_estimate(self) -> None:
        transcript = self.root / "rollout.jsonl"
        shutil.copyfile(FIXTURE, transcript)
        parser = TranscriptParser(self.storage)
        self.assertEqual(parser.ingest(str(transcript), "fixture-session"), 2)
        self.assertEqual(parser.ingest(str(transcript), "fixture-session"), 0)
        summary = self.storage.summary("fixture-session", None)
        self.assertEqual(summary["token"]["total_tokens"], 95380)
        self.assertEqual(len(summary["rates"]), 2)
        view = derive(summary, self.config)
        self.assertAlmostEqual(view["thread"]["cache_hit"], 64000 / 94469 * 100)
        self.assertAlmostEqual(view["context"]["used_percent"], 37759 / 353400 * 100)
        self.assertEqual(view["context"]["source"], "estimated")
        self.assertIsNone(view["turn"]["total"])

    def test_turn_and_tool_aggregation(self) -> None:
        self.storage.upsert_session("s", None, "gpt-test", None)
        self.storage.start_turn("t", "s")
        self.storage.record_tool_start({"tool_use_id": "u", "tool_name": "apply_patch"}, "s", "t")
        self.storage.record_tool_end(
            {"tool_use_id": "u", "tool_name": "apply_patch", "tool_response": {"exit_code": 0}}, "s", "t"
        )
        summary = self.storage.summary("s", "t")
        self.assertEqual(summary["tools"]["total_calls"], 1)
        self.assertEqual(summary["tools"]["file_edits"], 1)
        self.assertEqual(summary["tools"]["successful_calls"], 1)

    def test_global_summary_uses_latest_turn_for_live_ui(self) -> None:
        self.storage.upsert_session("s", None, "gpt-test", None)
        self.storage.start_turn("t", "s")
        self.assertEqual(self.storage.summary(None, None)["turn"]["turn_id"], "t")

    def test_session_summary_never_falls_back_to_another_live_session(self) -> None:
        for session in ("short", "long"):
            self.storage.upsert_session(session, None, "gpt-test", None)
        self.storage.start_turn("short-turn", "short")
        self.storage.add_tokens(
            "short", "short-turn",
            {"total": {"input_tokens": 22147, "output_tokens": 11, "total_tokens": 22158},
             "last": {"input_tokens": 22147, "output_tokens": 11, "total_tokens": 22158},
             "model_context_window": 258400},
            time.time(), "test",
        )
        self.storage.end_turn("short-turn")
        self.storage.start_turn("long-turn", "long")
        self.storage.add_tokens(
            "long", "long-turn",
            {"total": {"input_tokens": 101_299_000, "output_tokens": 1_000, "total_tokens": 101_300_000},
             "last": {"input_tokens": 200_000, "output_tokens": 1_000, "total_tokens": 201_000}},
            time.time(), "test",
        )
        selected = self.storage.summary("short", None)
        self.assertEqual(selected["turn"]["turn_id"], "short-turn")
        self.assertEqual(selected["token"]["total_tokens"], 22158)

    def test_new_turn_closes_orphaned_predecessor_in_same_session(self) -> None:
        self.storage.upsert_session("s", None, "gpt-test", None)
        self.storage.start_turn("old", "s")
        self.storage.start_turn("new", "s")
        old = self.storage.conn.execute("SELECT ended_at FROM turns WHERE turn_id='old'").fetchone()
        self.assertIsNotNone(old["ended_at"])
        self.assertEqual(self.storage.active_turn("s")["turn_id"], "new")

    def test_historical_turn_uses_its_own_token_snapshot(self) -> None:
        self.storage.upsert_session("s", None, "gpt-test", None)
        self.storage.start_turn("old", "s")
        self.storage.add_tokens(
            "s", "old",
            {"total": {"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
             "last": {"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
             "model_context_window": 1000},
            time.time(), "test",
        )
        self.storage.end_turn("old")
        self.storage.start_turn("new", "s")
        self.storage.add_tokens(
            "s", "new",
            {"total": {"input_tokens": 240, "output_tokens": 60, "total_tokens": 300},
             "last": {"input_tokens": 160, "output_tokens": 40, "total_tokens": 200},
             "model_context_window": 1000},
            time.time(), "test",
        )
        old = self.storage.summary(None, "old")
        new = self.storage.summary(None, "new")
        self.assertEqual(old["token"]["total_tokens"], 100)
        self.assertEqual(derive(old, self.config)["turn"]["total"], 100)
        self.assertEqual(new["token"]["total_tokens"], 300)
        self.assertEqual(derive(new, self.config)["turn"]["total"], 200)

    def test_templates_unicode_and_ascii_progress(self) -> None:
        self.assertEqual(progress(50, 4, True), "██░░")
        self.assertEqual(progress(50, 4, False), "##--")
        summary = self.storage.summary(None, None)
        data = derive(summary, self.config)
        text = render_template("Turn {turn.total_tokens} Ctx {context.used_percent}", data, self.config)
        self.assertEqual(text, "Turn N/A Ctx N/A")
        self.assertIn("Codex usage", render(summary, self.config, "normal"))

    def test_official_app_server_snapshots_win_over_time(self) -> None:
        self.storage.add_rate_limits(
            {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 25, "windowDurationMins": 300}}},
            time.time(),
            "official_app_server",
        )
        rate = self.storage.latest_rates()[("codex", "primary")]
        self.assertEqual(rate["used_percent"], 25)
        self.assertEqual(rate["source"], "official_app_server")

    def test_quota_delta_is_unavailable_across_a_reset(self) -> None:
        self.assertEqual(_delta({"used_percent": 24}, 23), 1)
        self.assertIsNone(_delta({"used_percent": 2}, 99))

    def test_schema_v3_history_is_session_scoped_and_privacy_safe(self) -> None:
        for session in ("one", "two"):
            self.storage.upsert_session(session, None, "gpt-test", None)
            self.storage.start_turn(f"{session}-turn", session)
            self.storage.add_tokens(
                session, f"{session}-turn",
                {"total": {"input_tokens": 80, "cached_input_tokens": 40, "output_tokens": 20, "total_tokens": 100},
                 "last": {"input_tokens": 80, "cached_input_tokens": 40, "output_tokens": 20, "total_tokens": 100},
                 "model_context_window": 1000}, time.time(), "test")
            self.storage.end_turn(f"{session}-turn")
        self.assertEqual(self.storage.get_meta("schema_version"), "5")
        current = self.storage.history("one", None, "current_chat", 500)
        self.assertEqual([row["session_id"] for row in current], ["one"])
        self.assertEqual(len(self.storage.history(None, None, "all_chats", 500)), 2)
        self.assertEqual(self.storage.history(None, time.time() + 1, "all_chats", 500), [])
        self.assertFalse({"prompt", "assistant_text", "title"} & set(current[0]))

    def test_rolling_forecast_requires_samples_and_ignores_reset_drop(self) -> None:
        now = time.time()
        for offset, used in ((-900, 10), (-700, 12), (-500, 14), (-400, 1), (-200, 2), (0, 3)):
            self.storage.add_rate_limits(
                {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": used, "windowDurationMins": 300}}},
                now + offset, "official_app_server")
        forecast = self.storage.rolling_forecast()
        self.assertIn("15", forecast["windows"])
        self.assertGreaterEqual(forecast["windows"]["15"]["burn_percent_per_hour"], 0)

    def test_estimated_context_cannot_create_critical_guard_alert(self) -> None:
        view = {"primary": {}, "context": {"used_percent": 99, "source": "estimated"},
                "turn": {"duration": 0, "total": 1, "input": 1, "cached": 1}, "thread": {}}
        self.assertFalse(any(a["condition"] == "context" for a in _guard(view, self.config)["alerts"]))
        view["context"]["source"] = "observed_renderer"
        alert = next(a for a in _guard(view, self.config)["alerts"] if a["condition"] == "context")
        self.assertEqual(alert["level"], "critical")


if __name__ == "__main__":
    unittest.main()
