import json
import tempfile
import unittest
from pathlib import Path


class StorageTests(unittest.TestCase):
    def test_start_task_creates_summary_and_event_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tgcod.storage import TaskStorage

            storage = TaskStorage(Path(tmpdir))
            record = storage.start_task(chat_id=123, task_text="write code")

            self.assertTrue(record.task_dir.exists())
            self.assertTrue(record.summary_path.exists())
            self.assertTrue(record.events_path.exists())

            summary = json.loads(record.summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["chat_id"], 123)
            self.assertEqual(summary["task_text"], "write code")
            self.assertEqual(summary["status"], "running")

    def test_append_event_writes_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tgcod.storage import TaskStorage

            storage = TaskStorage(Path(tmpdir))
            record = storage.start_task(chat_id=55, task_text="hello")
            storage.append_event(record, "codex_output", {"text": "line1"})

            rows = record.events_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(rows), 1)
            event = json.loads(rows[0])
            self.assertEqual(event["event_type"], "codex_output")
            self.assertEqual(event["payload"]["text"], "line1")

    def test_save_screenshot_persists_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tgcod.storage import TaskStorage

            storage = TaskStorage(Path(tmpdir))
            record = storage.start_task(chat_id=7, task_text="snap")
            path = storage.save_screenshot(record, b"fake-image-bytes", suffix="png")

            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), b"fake-image-bytes")


if __name__ == "__main__":
    unittest.main()
