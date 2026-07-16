from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_usage_monitor.config import ConfigError, load_config


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_default_config_is_valid_and_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = load_config(PLUGIN_ROOT, Path(directory))
            self.assertEqual(loaded.get("schema_version"), 1)
            self.assertTrue(loaded.path.exists())
            self.assertTrue(loaded.get("privacy.never_store_prompt_contents"))

    def test_unknown_key_warns_and_privacy_is_forced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "config.toml").write_text(
                "schema_version=1\nmystery=true\n"
                "[privacy]\nnever_store_auth_tokens=false\nnever_store_prompt_contents=false\n"
                "[storage]\nstore_prompt_text=true\n",
                encoding="utf-8",
            )
            loaded = load_config(PLUGIN_ROOT, path)
            self.assertTrue(any("Unknown config key" in item for item in loaded.warnings))
            self.assertTrue(loaded.get("privacy.never_store_auth_tokens"))
            self.assertFalse(loaded.get("storage.store_prompt_text"))

    def test_future_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "config.toml").write_text("schema_version=2\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(PLUGIN_ROOT, path)

    def test_invalid_ui_layout_falls_back_to_reserved_space(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "config.toml").write_text('schema_version=1\n[ui]\nlayout_mode="bad"\n', encoding="utf-8")
            loaded = load_config(PLUGIN_ROOT, path)
            self.assertEqual(loaded.get("ui.layout_mode"), "reserve_space")
            self.assertTrue(any("ui.layout_mode" in item for item in loaded.warnings))


if __name__ == "__main__":
    unittest.main()
