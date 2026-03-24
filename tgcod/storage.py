from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    chat_id: int
    task_text: str
    task_dir: Path
    summary_path: Path
    events_path: Path


class TaskStorage:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def start_task(self, chat_id: int, task_text: str) -> TaskRecord:
        task_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
        task_dir = self.root_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        summary_path = task_dir / "summary.json"
        events_path = task_dir / "events.jsonl"
        events_path.touch()

        summary = {
            "task_id": task_id,
            "chat_id": chat_id,
            "task_text": task_text,
            "status": "running",
            "started_at": utc_now_iso(),
            "finished_at": None,
        }
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return TaskRecord(task_id, chat_id, task_text, task_dir, summary_path, events_path)

    def append_event(self, record: TaskRecord, event_type: str, payload: dict) -> None:
        row = {
            "timestamp": utc_now_iso(),
            "event_type": event_type,
            "payload": payload,
        }
        with record.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def save_screenshot(
        self,
        record: TaskRecord | None,
        image_bytes: bytes,
        suffix: str = "png",
    ) -> Path:
        target_dir = record.task_dir if record else (self.root_dir / "adhoc")
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"screenshot-{datetime.now().strftime('%H%M%S-%f')}.{suffix}"
        path.write_bytes(image_bytes)
        return path

    def update_status(self, record: TaskRecord, status: str, **extra: object) -> None:
        summary = json.loads(record.summary_path.read_text(encoding="utf-8"))
        summary["status"] = status
        summary["finished_at"] = utc_now_iso()
        summary.update(extra)
        record.summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
