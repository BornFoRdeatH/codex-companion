from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from codex_usage_monitor.config import load_config
from codex_usage_monitor.render import _delta, derive, progress, render, render_template
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
        self.assertEqual(view["turn"]["total"], 0)

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

    def test_templates_unicode_and_ascii_progress(self) -> None:
        self.assertEqual(progress(50, 4, True), "██░░")
        self.assertEqual(progress(50, 4, False), "##--")
        summary = self.storage.summary(None, None)
        data = derive(summary, self.config)
        text = render_template("Turn {turn.total_tokens} Ctx {context.used_percent}", data, self.config)
        self.assertEqual(text, "Turn 0 Ctx N/A")
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


if __name__ == "__main__":
    unittest.main()
