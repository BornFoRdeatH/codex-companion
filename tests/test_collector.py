from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from codex_usage_monitor.collector import _handle_message
from codex_usage_monitor.storage import Storage


class CollectorTests(unittest.TestCase):
    def test_account_responses_and_notifications_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "usage.sqlite3")
            try:
                storage.set_meta("request:11", "rates")
                _handle_message(
                    storage,
                    {
                        "id": 11,
                        "result": {
                            "rateLimits": {
                                "limitId": "codex",
                                "primary": {"usedPercent": 42, "windowDurationMins": 300, "resetsAt": time.time() + 60},
                            }
                        },
                    },
                )
                storage.set_meta("request:12", "usage")
                _handle_message(
                    storage,
                    {
                        "id": 12,
                        "result": {
                            "summary": {"lifetimeTokens": 1234, "currentStreakDays": 3},
                            "dailyUsageBuckets": [{"startDate": "2026-07-17", "tokens": 55}],
                        },
                    },
                )
                _handle_message(
                    storage,
                    {
                        "method": "account/rateLimits/updated",
                        "params": {
                            "rateLimits": {
                                "limitId": "codex",
                                "primary": {"usedPercent": 43, "windowDurationMins": 300},
                            }
                        },
                    },
                )
                self.assertEqual(storage.latest_rates()[("codex", "primary")]["used_percent"], 43)
                self.assertEqual(storage.latest_account_usage()["lifetime_tokens"], 1234)
            finally:
                storage.close()


if __name__ == "__main__":
    unittest.main()
