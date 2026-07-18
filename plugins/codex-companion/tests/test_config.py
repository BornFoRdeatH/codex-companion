from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_usage_monitor.config import ConfigError, load_config
from codex_usage_monitor.cli import console_safe


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_console_output_falls_back_without_crashing_on_cp1251(self) -> None:
        self.assertEqual(console_safe("╭─│≈█░", "cp1251"), "+-|~#-")

    def test_legacy_fixed_rate_window_labels_are_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "config.toml").write_text(
                'schema_version=1\n[format.compact]\ntemplate="5h {primary.used_percent} Week {secondary.used_percent}"\n',
                encoding="utf-8",
            )
            loaded = load_config(PLUGIN_ROOT, path)
            self.assertEqual(
                loaded.get("format.compact.template"),
                "{primary.label} {primary.used_percent} {secondary.label} {secondary.used_percent}",
            )

    def test_default_config_is_valid_and_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = load_config(PLUGIN_ROOT, Path(directory))
            self.assertEqual(loaded.get("schema_version"), 1)
            self.assertTrue(loaded.path.exists())
            self.assertTrue(loaded.get("privacy.never_store_prompt_contents"))
            self.assertTrue(loaded.get("privacy.never_store_assistant_text"))
            self.assertTrue(loaded.get("ui.auto_locale"))
            self.assertTrue(loaded.get("ui.guard.enabled"))
            self.assertEqual(loaded.get("ui.guard.cooldown_minutes"), 15)
            self.assertEqual(loaded.get("ui.history.default_scope"), "current_chat")
            self.assertEqual(loaded.get("ui.history.default_range"), "7d")
            self.assertTrue(loaded.get("ui.advisor.enabled"))
            self.assertFalse(loaded.get("ui.advisor.prompt_coach.enabled"))
            self.assertEqual(loaded.get("ui.advisor.baseline_window"), 50)
            self.assertTrue(loaded.get("ui.focus_mode.enabled"))
            self.assertEqual(loaded.get("ui.focus_mode.visible_turns"), 10)
            self.assertEqual(loaded.get("ui.focus_mode.load_batch"), 10)
            self.assertTrue(loaded.get("ui.focus_mode.scroll_guard"))
            self.assertEqual(loaded.get("ui.focus_mode.unknown_version_policy"), "probe")
        self.assertTrue(loaded.get("ui.handoff.enabled"))
        self.assertEqual(loaded.get("ui.handoff.generation"), "marked_current_turn")
        self.assertEqual(loaded.get("ui.handoff.max_summary_chars"), 20000)
        self.assertTrue(loaded.get("ui.budget.optimizer_enabled"))
        self.assertEqual(loaded.get("ui.budget.optimizer_action_mode"), "advisory")
        self.assertEqual(loaded.get("ui.budget.context_new_task_percent"), 93)

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

    def test_locale_supports_only_ukrainian_and_english(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "config.toml").write_text(
                'schema_version=1\n[locale]\nlanguage="ru"\n', encoding="utf-8"
            )
            loaded = load_config(PLUGIN_ROOT, path)
            self.assertEqual(loaded.get("locale.language"), "en")
            self.assertTrue(any("locale.language" in item for item in loaded.warnings))

    def test_invalid_focus_mode_values_fall_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "config.toml").write_text(
                'schema_version=1\n[ui.focus_mode]\nvisible_turns=4\nload_batch=101\nunknown_version_policy="force"\n',
                encoding="utf-8",
            )
            loaded = load_config(PLUGIN_ROOT, path)
            self.assertEqual(loaded.get("ui.focus_mode.visible_turns"), 10)
            self.assertEqual(loaded.get("ui.focus_mode.load_batch"), 10)
            self.assertEqual(loaded.get("ui.focus_mode.unknown_version_policy"), "probe")
            self.assertEqual(sum("ui.focus_mode" in item for item in loaded.warnings), 3)

    def test_existing_config_receives_focus_mode_section(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config_path = path / "config.toml"
            config_path.write_text("schema_version=1\n", encoding="utf-8")
            loaded = load_config(PLUGIN_ROOT, path)
            self.assertTrue(loaded.get("ui.focus_mode.enabled"))
            migrated = config_path.read_text(encoding="utf-8")
            self.assertIn("[ui.focus_mode]", migrated)
            self.assertIn("Added ui.focus_mode", "\n".join(loaded.warnings))

    def test_chat_virtualization_alias_migrates_for_one_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config_path = path / "config.toml"
            config_path.write_text(
                'schema_version=1\n[ui.chat_virtualization]\nenabled=false\nvisible_turns=20\nload_batch=15\nreset_on_thread_switch=false\nunknown_version_policy="disable"\n',
                encoding="utf-8",
            )
            loaded = load_config(PLUGIN_ROOT, path)
            self.assertFalse(loaded.get("ui.focus_mode.enabled"))
            self.assertEqual(loaded.get("ui.focus_mode.visible_turns"), 20)
            self.assertEqual(loaded.get("ui.focus_mode.load_batch"), 15)
            self.assertFalse(loaded.get("ui.focus_mode.reset_on_thread_switch"))
            self.assertEqual(loaded.get("ui.focus_mode.unknown_version_policy"), "disable")
            migrated = config_path.read_text(encoding="utf-8")
            self.assertIn("[ui.focus_mode]", migrated)
            self.assertNotIn("[ui.chat_virtualization]", migrated)
            self.assertTrue(any("deprecated" in warning for warning in loaded.warnings))
            reloaded = load_config(PLUGIN_ROOT, path)
            self.assertFalse(any("deprecated" in warning for warning in reloaded.warnings))


if __name__ == "__main__":
    unittest.main()
