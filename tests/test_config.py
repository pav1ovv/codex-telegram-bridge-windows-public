import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ConfigTests(unittest.TestCase):
    def test_load_settings_parses_values_and_creates_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            env = {
                "TELEGRAM_BOT_TOKEN": "token",
                "ALLOWED_CHAT_IDS": "1001, 1002 ,1003",
                "DATA_DIR": str(base / "data"),
                "LOG_DIR": str(base / "logs"),
                "TASK_IDLE_FINISH_SECONDS": "42",
            }

            with patch.dict(os.environ, env, clear=True):
                from tgcod.config import load_settings

                settings = load_settings()

            self.assertEqual(settings.telegram_bot_token, "token")
            self.assertEqual(settings.allowed_chat_ids, (1001, 1002, 1003))
            self.assertEqual(settings.task_idle_finish_seconds, 42)
            self.assertTrue(settings.data_dir.exists())
            self.assertTrue(settings.log_dir.exists())
            self.assertTrue(settings.history_dir.exists())

    def test_blank_allowed_chat_ids_becomes_empty_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "TELEGRAM_BOT_TOKEN": "token",
                "ALLOWED_CHAT_IDS": "",
                "DATA_DIR": str(Path(tmpdir) / "data"),
                "LOG_DIR": str(Path(tmpdir) / "logs"),
            }

            with patch.dict(os.environ, env, clear=True):
                from tgcod.config import load_settings

                settings = load_settings()

            self.assertEqual(settings.allowed_chat_ids, ())


if __name__ == "__main__":
    unittest.main()
