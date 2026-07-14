from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import Api
from chaoxing_auto import resolve_browser_choice


class BrowserSettingsTests(unittest.TestCase):
    def test_browser_choice_and_visibility_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "settings.json"
            api = Api()
            with (
                patch("backend._prepare_config_path", return_value=settings_path),
                patch("backend._protect_text", return_value="encrypted"),
                patch("backend._unprotect_text", return_value="secret"),
            ):
                saved = api.save_settings(
                    "student",
                    "secret",
                    True,
                    {
                        "credential_action": "save",
                        "online_browser_choice": "edge:test",
                        "online_browser_visible": True,
                    },
                )
                loaded = api.get_settings()

            payload = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertTrue(saved["ok"])
            self.assertEqual(payload["online_browser_choice"], "edge:test")
            self.assertTrue(payload["online_browser_visible"])
            self.assertEqual(loaded["settings"]["online_browser_choice"], "edge:test")
            self.assertTrue(loaded["settings"]["online_browser_visible"])

    def test_missing_saved_browser_falls_back_to_first_detected_choice(self) -> None:
        choices = [
            {"id": "chrome:test", "name": "Google Chrome", "path": "C:/Chrome/chrome.exe", "kind": "chrome"},
            {"id": "edge:test", "name": "Microsoft Edge", "path": "C:/Edge/msedge.exe", "kind": "edge"},
        ]
        with patch("chaoxing_auto.detect_browser_choices", return_value=choices):
            selected = resolve_browser_choice("browser:no-longer-installed")

        self.assertEqual(selected["id"], "chrome:test")


if __name__ == "__main__":
    unittest.main()
