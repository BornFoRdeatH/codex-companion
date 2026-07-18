from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_usage_monitor.paths import resolve_plugin_data


class PathTests(unittest.TestCase):
    def test_explicit_data_path_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = resolve_plugin_data(Path(directory), Path(directory) / "data")
            self.assertEqual(value, (Path(directory) / "data").resolve())

    def test_marketplace_data_is_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".codex" / "plugins" / "data" / "codex-usage-monitor-bornfordeath-plugins"
            target.mkdir(parents=True)
            with mock.patch("pathlib.Path.home", return_value=home), mock.patch.dict("os.environ", {}, clear=True):
                self.assertEqual(resolve_plugin_data(home), target)

    def test_cache_path_selects_its_marketplace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / ".codex" / "plugins" / "cache" / "personal-plugins" / "codex-usage-monitor" / "0.2.0"
            with mock.patch("pathlib.Path.home", return_value=home), mock.patch.dict("os.environ", {}, clear=True):
                expected = home / ".codex" / "plugins" / "data" / "codex-usage-monitor-personal-plugins"
                self.assertEqual(resolve_plugin_data(root), expected)


if __name__ == "__main__":
    unittest.main()
