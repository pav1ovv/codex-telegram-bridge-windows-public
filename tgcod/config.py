from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv_if_available(env_file: Path | None = None) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    target = env_file or Path.cwd() / ".env"
    if target.exists():
        load_dotenv(target)


def _parse_chat_ids(raw_value: str) -> tuple[int, ...]:
    if not raw_value.strip():
        return ()
    return tuple(int(item.strip()) for item in raw_value.split(",") if item.strip())


@dataclass(slots=True)
class Settings:
    telegram_bot_token: str
    allowed_chat_ids: tuple[int, ...]
    base_dir: Path
    data_dir: Path
    log_dir: Path
    history_dir: Path
    code_window_title_regex: str
    force_backend: str | None
    poll_interval_seconds: float
    submit_verify_timeout_seconds: float
    task_idle_finish_seconds: int
    task_finish_confirm_seconds: int
    telegram_chunk_size: int
    ocr_language: str
    tesseract_cmd: str | None
    health_failure_threshold: int

    @classmethod
    def for_tests(cls, telegram_bot_token: str, base_dir: Path) -> "Settings":
        data_dir = base_dir / "data"
        log_dir = base_dir / "logs"
        history_dir = data_dir / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            telegram_bot_token=telegram_bot_token,
            allowed_chat_ids=(),
            base_dir=base_dir,
            data_dir=data_dir,
            log_dir=log_dir,
            history_dir=history_dir,
            code_window_title_regex=r"^Codex$",
            force_backend=None,
            poll_interval_seconds=0.1,
            submit_verify_timeout_seconds=0.0,
            task_idle_finish_seconds=3600,
            task_finish_confirm_seconds=0,
            telegram_chunk_size=3500,
            ocr_language="eng",
            tesseract_cmd=None,
            health_failure_threshold=3,
        )


def load_settings(env_file: Path | None = None) -> Settings:
    _load_dotenv_if_available(env_file)

    base_dir = Path.cwd()
    data_dir = Path(os.getenv("DATA_DIR", base_dir / "data"))
    log_dir = Path(os.getenv("LOG_DIR", base_dir / "logs"))
    history_dir = data_dir / "history"

    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    history_dir.mkdir(parents=True, exist_ok=True)

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")

    force_backend = os.getenv("CODEX_FORCE_BACKEND", "").strip() or None

    return Settings(
        telegram_bot_token=token,
        allowed_chat_ids=_parse_chat_ids(os.getenv("ALLOWED_CHAT_IDS", "")),
        base_dir=base_dir,
        data_dir=data_dir,
        log_dir=log_dir,
        history_dir=history_dir,
        code_window_title_regex=os.getenv("CODEX_WINDOW_TITLE_REGEX", r"^Codex$"),
        force_backend=force_backend,
        poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", "1.5")),
        submit_verify_timeout_seconds=float(os.getenv("SUBMIT_VERIFY_TIMEOUT_SECONDS", "3.0")),
        task_idle_finish_seconds=int(os.getenv("TASK_IDLE_FINISH_SECONDS", "20")),
        task_finish_confirm_seconds=int(os.getenv("TASK_FINISH_CONFIRM_SECONDS", "30")),
        telegram_chunk_size=int(os.getenv("TELEGRAM_CHUNK_SIZE", "3500")),
        ocr_language=os.getenv("OCR_LANGUAGE", "eng"),
        tesseract_cmd=os.getenv("TESSERACT_CMD", "").strip() or None,
        health_failure_threshold=int(os.getenv("HEALTH_FAILURE_THRESHOLD", "3")),
    )
