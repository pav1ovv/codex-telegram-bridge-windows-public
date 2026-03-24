from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

from .config import Settings
from .diffing import chunk_message, extract_increment, normalize_window_text
from .storage import TaskRecord, TaskStorage

logger = logging.getLogger(__name__)

IGNORED_UI_LINES = {
    "Действия беседы",
    "Настроить действие запуска",
    "Переключить терминал",
    "Переключить панель разницы",
    "Open in Popout Window",
    "Запросите внесение дополнительных изменений",
    "Добавляйте файлы и многое другое",
    "GPT-5.4",
    "Высокий",
    "Местный",
    "Пользовательский (config.toml)",
    "Создать репозиторий git",
    "Shell",
    "uia",
    "ocr",
}

IGNORED_UI_SUBSTRINGS = (
    "Действия беседы",
    "Настроить действие запуска",
    "Переключить терминал",
    "Переключить панель разницы",
    "Open in Popout Window",
    "Запросите внесение дополнительных изменений",
    "Добавляйте файлы и многое другое",
)


class Messenger(Protocol):
    async def send_text(self, chat_id: int, text: str) -> None: ...

    async def send_photo(self, chat_id: int, photo_path: str, caption: str | None = None) -> None: ...


@dataclass(slots=True)
class ActiveSession:
    chat_id: int
    record: TaskRecord
    last_user_text: str
    last_snapshot: str = ""
    last_rendered_items: tuple[str, ...] = ()
    baseline_completion_items: tuple[str, ...] = ()
    last_visible_completion_items: tuple[str, ...] = ()
    completion_history_items: tuple[str, ...] = ()
    stream_history_items: tuple[str, ...] = ()
    pending_snapshot: str = ""
    pending_since_monotonic: float = 0.0
    last_change_monotonic: float = 0.0
    idle_candidate_since_monotonic: float = 0.0
    prompt_seen_once: bool = False


class TaskBridgeService:
    def __init__(self, settings: Settings, controller: Any, messenger: Messenger, storage: TaskStorage) -> None:
        self.settings = settings
        self.controller = controller
        self.messenger = messenger
        self.storage = storage
        self.active_session: ActiveSession | None = None
        self.owner_file = self.settings.data_dir / "owner.json"
        self.owner_chat_id = self._load_owner_chat_id()

    async def start_task(self, chat_id: int, task_text: str) -> None:
        self._ensure_chat_allowed(chat_id)
        if self.active_session is not None:
            raise RuntimeError("A Codex task is already running. Stop it before starting a new one.")

        self.controller.ensure_ready()
        baseline_snapshot = normalize_window_text(self.controller.read_output_snapshot())
        record = self.storage.start_task(chat_id=chat_id, task_text=task_text)
        self.storage.append_event(record, "telegram_command", {"command": "/task", "text": task_text})
        self.controller.submit_task(task_text)
        verified_snapshot = await self._wait_for_prompt_echo(task_text, baseline_snapshot)
        if verified_snapshot is None:
            self.storage.append_event(
                record,
                "submit_failed",
                {"reason": "prompt_not_visible_after_submit", "backend": getattr(self.controller, "backend_name", "unknown")},
            )
            self.storage.update_status(record, "submit_failed", last_snapshot=baseline_snapshot)
            raise RuntimeError(
                "Не удалось подтвердить отправку задачи в Codex. Текст не появился в открытом чате приложения."
            )

        prompt_seen = has_prompt_anchor(verified_snapshot, task_text)
        initial_items = tuple(render_stream_items(verified_snapshot, task_text)) if prompt_seen else ()
        initial_completion_items = tuple(build_completion_lines(verified_snapshot, task_text)) if prompt_seen else ()
        self.active_session = ActiveSession(
            chat_id=chat_id,
            record=record,
            last_user_text=task_text,
            last_snapshot=verified_snapshot,
            last_rendered_items=initial_items,
            baseline_completion_items=(),
            last_visible_completion_items=initial_completion_items,
            completion_history_items=initial_completion_items,
            stream_history_items=initial_items,
            pending_snapshot=verified_snapshot,
            pending_since_monotonic=monotonic(),
            last_change_monotonic=monotonic(),
            idle_candidate_since_monotonic=0.0,
            prompt_seen_once=prompt_seen,
        )
        backend_name = getattr(self.controller, "backend_name", "unknown")
        await self.messenger.send_text(chat_id, f"Задача отправлена в Codex ({backend_name}).")
        if initial_items:
            self.storage.append_event(record, "codex_output", {"items": list(initial_items)})
            for item in initial_items:
                for chunk in chunk_message(item, self.settings.telegram_chunk_size):
                    await self.messenger.send_text(chat_id, chunk)

    async def stop_task(self, chat_id: int) -> None:
        self._ensure_chat_allowed(chat_id)
        if self.active_session is None:
            await self.messenger.send_text(chat_id, "Нет активной задачи.")
            return
        self.controller.stop_task()
        self.storage.append_event(self.active_session.record, "telegram_command", {"command": "/stop"})
        self.storage.update_status(self.active_session.record, "stopped")
        self.active_session = None
        await self.messenger.send_text(chat_id, "Задача остановлена.")

    async def send_screenshot(self, chat_id: int) -> None:
        self._ensure_chat_allowed(chat_id)
        self.controller.ensure_ready()
        image_bytes = self.controller.capture_screenshot()
        record = self.active_session.record if self.active_session else None
        path = self.storage.save_screenshot(record, image_bytes, suffix="png")
        await self.messenger.send_photo(chat_id, str(path), caption="Скриншот окна Codex")

    async def edit_task(self, chat_id: int, new_text: str) -> None:
        self._ensure_chat_allowed(chat_id)
        if self.active_session is None:
            await self.messenger.send_text(chat_id, "Нет активной задачи для редактирования.")
            return

        session = self.active_session
        old_text = session.last_user_text
        self.controller.ensure_ready()
        edited_natively = False
        if hasattr(self.controller, "edit_last_submission"):
            edited_natively = bool(self.controller.edit_last_submission(old_text, new_text))

        if edited_natively:
            self.storage.append_event(session.record, "telegram_command", {"command": "/edit", "mode": "native", "old_text": old_text, "new_text": new_text})
            session.last_user_text = new_text
            await self.messenger.send_text(chat_id, "Последнее сообщение обновлено через native edit.")
            return

        fallback_prompt = build_edit_fallback_prompt(old_text, new_text)
        self.controller.submit_task(fallback_prompt)
        self.storage.append_event(
            session.record,
            "telegram_command",
            {"command": "/edit", "mode": "fallback", "old_text": old_text, "new_text": new_text},
        )
        session.last_user_text = new_text
        await self.messenger.send_text(chat_id, "Native edit недоступен, отправил в Codex корректирующую инструкцию.")

    async def stream_once(self) -> bool:
        if self.active_session is None:
            return False

        session = self.active_session
        now = monotonic()
        snapshot = self.controller.read_output_snapshot()
        if snapshot != session.pending_snapshot:
            session.pending_snapshot = snapshot
            session.pending_since_monotonic = now
            session.last_change_monotonic = now
            session.idle_candidate_since_monotonic = 0.0
            return False

        if snapshot == session.last_snapshot:
            if now - session.last_change_monotonic >= self.settings.task_idle_finish_seconds:
                running_indicator = self.controller.is_task_running(snapshot)
                if running_indicator:
                    session.idle_candidate_since_monotonic = 0.0
                    return False
                if session.idle_candidate_since_monotonic <= 0.0:
                    session.idle_candidate_since_monotonic = now
                    if self.settings.task_finish_confirm_seconds > 0:
                        return False
                if now - session.idle_candidate_since_monotonic < self.settings.task_finish_confirm_seconds:
                    return False
                final_snapshot = normalize_window_text(session.last_snapshot)
                self.storage.update_status(session.record, "idle_finished", last_snapshot=final_snapshot)
                await self.messenger.send_text(session.chat_id, "Задача завершена.")
                if session.prompt_seen_once:
                    current_completion_items = tuple(build_completion_lines(final_snapshot, session.last_user_text))
                else:
                    current_completion_items = ()
                history_items = append_stream_history(session.completion_history_items, current_completion_items)
                final_items = extract_final_assistant_items(history_items)
                final_text = "\n".join(final_items).strip()
                if final_text:
                    self.storage.append_event(session.record, "codex_final", {"text": final_text})
                    for chunk in chunk_message(final_text, self.settings.telegram_chunk_size):
                        await self.messenger.send_text(session.chat_id, chunk)
                self.active_session = None
            return False

        if now - session.pending_since_monotonic < get_stream_settle_seconds(self.settings.poll_interval_seconds):
            return False

        prompt_found = has_prompt_anchor(snapshot, session.last_user_text)
        session.prompt_seen_once = session.prompt_seen_once or prompt_found
        if prompt_found:
            current_items = tuple(render_stream_items(snapshot, session.last_user_text))
            current_completion_items = tuple(build_completion_lines(snapshot, session.last_user_text))
            new_items = extract_new_stream_items(session.last_rendered_items, current_items)
            new_completion_items = extract_new_stream_items(session.last_visible_completion_items, current_completion_items)
        else:
            increment_snapshot = extract_increment(session.last_snapshot, snapshot)
            current_items = session.last_rendered_items
            current_completion_items = session.last_visible_completion_items
            new_items = render_stream_items(increment_snapshot, "")
            new_completion_items = build_completion_lines(increment_snapshot, "")

        if new_items:
            session.last_snapshot = snapshot
            if prompt_found:
                session.last_rendered_items = current_items
                session.last_visible_completion_items = current_completion_items
            session.completion_history_items = append_stream_history(session.completion_history_items, new_completion_items)
            session.stream_history_items = append_stream_history(session.stream_history_items, tuple(new_items))
            session.last_change_monotonic = now
            session.idle_candidate_since_monotonic = 0.0
            self.storage.append_event(session.record, "codex_output", {"items": new_items})
            for item in new_items:
                for chunk in chunk_message(item, self.settings.telegram_chunk_size):
                    await self.messenger.send_text(session.chat_id, chunk)
            return True

        session.last_snapshot = snapshot
        if prompt_found:
            session.last_rendered_items = current_items
            session.last_visible_completion_items = current_completion_items
        session.completion_history_items = append_stream_history(session.completion_history_items, new_completion_items)
        return False

    def _ensure_chat_allowed(self, chat_id: int) -> None:
        if self.settings.allowed_chat_ids and chat_id not in self.settings.allowed_chat_ids:
            raise PermissionError(f"Chat {chat_id} is not allowed")
        if self.settings.allowed_chat_ids:
            return
        if self.owner_chat_id is None:
            self.owner_chat_id = chat_id
            self.owner_file.write_text(json.dumps({"chat_id": chat_id}, ensure_ascii=False, indent=2), encoding="utf-8")
            return
        if chat_id != self.owner_chat_id:
            raise PermissionError("This bot is locked to a different Telegram chat.")

    def _load_owner_chat_id(self) -> int | None:
        if not self.owner_file.exists():
            return None
        try:
            data = json.loads(self.owner_file.read_text(encoding="utf-8"))
        except Exception:
            return None
        chat_id = data.get("chat_id")
        return int(chat_id) if chat_id is not None else None

    async def _wait_for_prompt_echo(self, task_text: str, baseline_snapshot: str) -> str | None:
        timeout = self.settings.submit_verify_timeout_seconds
        if timeout <= 0:
            return baseline_snapshot

        deadline = monotonic() + timeout
        latest_snapshot = baseline_snapshot
        sleep_step = min(max(self.settings.poll_interval_seconds, 0.15), 0.35)
        while monotonic() < deadline:
            await asyncio.sleep(sleep_step)
            latest_snapshot = normalize_window_text(self.controller.read_output_snapshot())
            if has_prompt_anchor(latest_snapshot, task_text):
                return latest_snapshot
        return None


class TelegramMessenger:
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    async def send_text(self, chat_id: int, text: str) -> None:
        await self.bot.send_message(chat_id=chat_id, text=text)

    async def send_photo(self, chat_id: int, photo_path: str, caption: str | None = None) -> None:
        with Path(photo_path).open("rb") as handle:
            await self.bot.send_photo(chat_id=chat_id, photo=handle, caption=caption)


class TelegramBotRunner:
    def __init__(self, settings: Settings, controller: Any, storage: TaskStorage) -> None:
        self.settings = settings
        self.controller = controller
        self.storage = storage
        self.service: TaskBridgeService | None = None
        self._stream_task: asyncio.Task[None] | None = None

    def run(self) -> None:
        try:
            from telegram import Update
            from telegram.ext import Application, CommandHandler, ContextTypes
        except ImportError as exc:
            raise RuntimeError("python-telegram-bot is required to run the bot") from exc

        async def post_init(application: Any) -> None:
            messenger = TelegramMessenger(application.bot)
            self.service = TaskBridgeService(self.settings, self.controller, messenger, self.storage)
            self._stream_task = asyncio.create_task(self._stream_loop())

        async def post_shutdown(_application: Any) -> None:
            if self._stream_task:
                self._stream_task.cancel()
                try:
                    await self._stream_task
                except asyncio.CancelledError:
                    pass

        async def on_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if update.effective_chat is None or update.message is None:
                return
            if update.effective_chat.type != "private":
                await update.message.reply_text("Этот бот принимает команды только в личном чате.")
                return
            task_text = " ".join(context.args).strip()
            if not task_text:
                await update.message.reply_text("Использование: /task <текст задачи>")
                return
            await self._safe_call(update.effective_chat.id, self.service.start_task(update.effective_chat.id, task_text))

        async def on_stop(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
            if update.effective_chat is None:
                return
            if update.effective_chat.type != "private":
                if update.message is not None:
                    await update.message.reply_text("Этот бот принимает команды только в личном чате.")
                return
            await self._safe_call(update.effective_chat.id, self.service.stop_task(update.effective_chat.id))

        async def on_screenshot(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
            if update.effective_chat is None:
                return
            if update.effective_chat.type != "private":
                if update.message is not None:
                    await update.message.reply_text("Этот бот принимает команды только в личном чате.")
                return
            await self._safe_call(update.effective_chat.id, self.service.send_screenshot(update.effective_chat.id))

        async def on_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if update.effective_chat is None or update.message is None:
                return
            if update.effective_chat.type != "private":
                await update.message.reply_text("Этот бот принимает команды только в личном чате.")
                return
            new_text = " ".join(context.args).strip()
            if not new_text:
                await update.message.reply_text("Использование: /edit <новый текст>")
                return
            await self._safe_call(update.effective_chat.id, self.service.edit_task(update.effective_chat.id, new_text))

        application = (
            Application.builder()
            .token(self.settings.telegram_bot_token)
            .post_init(post_init)
            .post_shutdown(post_shutdown)
            .build()
        )
        application.add_handler(CommandHandler("task", on_task))
        application.add_handler(CommandHandler("stop", on_stop))
        application.add_handler(CommandHandler("screenshot", on_screenshot))
        application.add_handler(CommandHandler("edit", on_edit))
        application.add_error_handler(self._error_handler)
        application.run_polling(close_loop=False)

    async def _stream_loop(self) -> None:
        while True:
            try:
                if self.service is not None:
                    await self.service.stream_once()
            except Exception:
                logger.exception("Live Codex stream iteration failed")
                if self.service and self.service.active_session:
                    chat_id = self.service.active_session.chat_id
                    await self.service.messenger.send_text(chat_id, "Ошибка live-логирования: окно Codex не отвечает или недоступно.")
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def _safe_call(self, chat_id: int, operation: Any) -> None:
        try:
            await operation
        except Exception as exc:
            logger.exception("Telegram command handling failed")
            if self.service is not None:
                await self.service.messenger.send_text(chat_id, f"Ошибка: {exc}")

    async def _error_handler(self, update: object, context: Any) -> None:
        logger.exception("Unhandled Telegram error", exc_info=context.error)


def build_edit_fallback_prompt(old_text: str, new_text: str) -> str:
    return (
        "Replace my previous user request with the revised version below. "
        "Ignore the old request and continue from the revised one only.\n\n"
        f"Old request:\n{old_text}\n\n"
        f"Revised request:\n{new_text}"
    )


def render_stream_items(snapshot: str, user_text: str) -> list[str]:
    return _build_stream_blocks(snapshot, user_text, hold_back_trailing_assistant=True)


def build_completion_text(snapshot: str, user_text: str) -> str:
    return "\n".join(build_completion_lines(snapshot, user_text)).strip()


def build_completion_lines(snapshot: str, user_text: str) -> list[str]:
    return [
        item
        for item in _build_stream_blocks(snapshot, user_text, hold_back_trailing_assistant=False)
        if item not in {"Запущена команда", "Вызван MCP", "Вызван skill"}
    ]


def extract_new_stream_items(previous: tuple[str, ...], current: tuple[str, ...]) -> list[str]:
    if not current:
        return []
    if not previous:
        return list(current)
    if current[: len(previous)] == previous:
        return list(current[len(previous) :])

    matcher = SequenceMatcher(a=list(previous), b=list(current))
    new_items: list[str] = []
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag not in {"insert", "replace"}:
            continue
        for item in current[j1:j2]:
            if not new_items or new_items[-1] != item:
                new_items.append(item)
    return new_items


def strip_echoed_user_prompt(increment: str, user_text: str) -> str:
    normalized_increment = normalize_window_text(increment)
    normalized_user_text = normalize_window_text(user_text)

    if not normalized_increment or not normalized_user_text:
        return strip_non_codex_ui_text(normalized_increment)
    if normalized_increment == normalized_user_text:
        return ""

    stripped = normalized_increment.replace(normalized_user_text, "").strip()
    return strip_non_codex_ui_text(stripped)


def strip_non_codex_ui_text(text: str) -> str:
    normalized = normalize_window_text(text)
    if not normalized:
        return ""
    kept_lines = []
    for line in normalized.splitlines():
        stripped = line.strip()
        if not stripped or stripped in IGNORED_UI_LINES:
            continue
        if any(marker in stripped for marker in IGNORED_UI_SUBSTRINGS):
            continue
        kept_lines.append(stripped)
    return "\n".join(kept_lines).strip()


def classify_codex_line(line: str) -> str:
    stripped = line.strip()
    lowered = stripped.lower()
    if not stripped:
        return "ignore"
    if stripped in {"Shell", "ProseMirror", "Cancel Send", "Send", "Успех", "Нет вывода", "uia", "ocr"}:
        return "ignore"
    if any(marker in stripped for marker in IGNORED_UI_SUBSTRINGS):
        return "ignore"
    if _is_probably_artifact_line(stripped, lowered):
        return "ignore"
    if "mcp" in lowered or "инструмент из" in lowered:
        return "mcp"
    if "skill" in lowered or "навык" in lowered:
        return "skill"
    if _is_probably_command_line(stripped, lowered):
        return "command"
    return "assistant"


def _iter_codex_lines(snapshot: str, user_text: str) -> list[str]:
    cleaned = strip_non_codex_ui_text(slice_after_user_prompt(snapshot, user_text))
    if not cleaned:
        return []
    return [line.strip() for line in cleaned.splitlines() if line.strip()]


def slice_after_user_prompt(text: str, user_text: str) -> str:
    normalized_text = normalize_window_text(text)
    normalized_user_text = normalize_window_text(user_text)
    if not normalized_text or not normalized_user_text:
        return normalized_text

    prompt_end = _find_prompt_end_index(normalized_text, normalized_user_text)
    if prompt_end is None:
        stripped = normalized_text.replace(normalized_user_text, "")
        return normalize_window_text(stripped)
    return normalize_window_text(normalized_text[prompt_end:].lstrip("\n"))


def _find_prompt_end_index(text: str, user_text: str) -> int | None:
    compact_text, text_map = _compact_with_index(text)
    compact_user, _user_map = _compact_with_index(user_text)
    if not compact_text or not compact_user:
        return None
    anchor_index = compact_text.rfind(compact_user)
    if anchor_index == -1:
        return None
    anchor_end = anchor_index + len(compact_user) - 1
    if anchor_end >= len(text_map):
        return None
    return text_map[anchor_end] + 1


def has_prompt_anchor(text: str, user_text: str) -> bool:
    return _find_prompt_end_index(normalize_window_text(text), normalize_window_text(user_text)) is not None


def _compact_with_index(text: str) -> tuple[str, list[int]]:
    compact_chars: list[str] = []
    index_map: list[int] = []
    for index, char in enumerate(text):
        if char.isalnum():
            compact_chars.append(char.lower())
            index_map.append(index)
    return "".join(compact_chars), index_map


def _is_probably_command_line(stripped: str, lowered: str) -> bool:
    if stripped.startswith("Запущен ") or stripped.startswith("Выполняется команда"):
        return True
    if stripped.startswith("$ ") or stripped.startswith("@'"):
        return True
    if re.match(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", stripped):
        return True
    if lowered.startswith("python ") or lowered.startswith("powershell "):
        return True
    if "| python" in lowered or "| powershell" in lowered:
        return True
    if "__main__" in lowered or "configuration loaded successfully" in lowered:
        return True
    if any(token in lowered for token in ("get-ciminstance", "get-content", "start-sleep", "where-object", "select-object", "format-list")):
        return True
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", stripped):
        return True
    return False


def _is_probably_artifact_line(stripped: str, lowered: str) -> bool:
    if re.match(r"^Изменено \d+ файл", stripped):
        return True
    if re.match(r"^[A-Za-z0-9_./\\\\-]+\s+\+\s*\d+\s+-\s*\d+(?:\s+.+)?$", stripped):
        return True
    if re.match(r"^[A-Za-z0-9_./\\\\-]+\.(py|md|txt|json|yml|yaml)$", stripped):
        return True
    if re.match(r"^[A-Za-z0-9_./\\\\-]+$", stripped) and "/" in stripped:
        return True
    if re.match(r"^[A-Za-z0-9_.-]+$", stripped) and len(stripped) <= 32 and stripped.lower() in {"tests", "tgcod", "docs", "data"}:
        return True
    if re.match(r"^Работал на протяжении \d+", stripped):
        return True
    if stripped in {"README.md", "requirements.txt"}:
        return True
    return False


def append_stream_history(existing: tuple[str, ...], new_items: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    items = [item for item in existing if item]
    pending = [item for item in new_items if item]
    if not pending:
        return tuple(items)

    max_overlap = min(len(items), len(pending))
    overlap = 0
    for size in range(max_overlap, 0, -1):
        if tuple(items[-size:]) == tuple(pending[:size]):
            overlap = size
            break

    for item in pending[overlap:]:
        if items and items[-1] == item:
            continue
        items.append(item)
    return tuple(items)


def append_unique_items(existing: tuple[str, ...], new_items: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    items = list(existing)
    for item in new_items:
        if not item:
            continue
        if item in {"Запущена команда", "Вызван MCP", "Вызван skill"} and items and items[-1] == item:
            continue
        if item not in items:
            items.append(item)
    return tuple(items)


def extract_final_assistant_items(items: tuple[str, ...] | list[str]) -> list[str]:
    assistant_items = [item for item in items if item and item not in {"Запущена команда", "Вызван MCP", "Вызван skill"}]
    if not assistant_items:
        return []
    start = len(assistant_items) - 1
    while start > 0 and not _is_progress_assistant_line(assistant_items[start - 1]):
        start -= 1
    return assistant_items[start:]


def _build_stream_blocks(snapshot: str, user_text: str, hold_back_trailing_assistant: bool) -> list[str]:
    items: list[str] = []
    assistant_line: str | None = None

    def flush_assistant() -> None:
        nonlocal assistant_line
        if not assistant_line:
            return
        if not items or items[-1] != assistant_line:
            items.append(assistant_line)
        assistant_line = None

    for line in _iter_codex_lines(snapshot, user_text):
        kind = classify_codex_line(line)
        if kind == "ignore":
            continue
        if kind == "assistant":
            cleaned_line = sanitize_assistant_line(line)
            if not cleaned_line:
                continue
            if assistant_line and _should_merge_assistant_lines(assistant_line, cleaned_line):
                assistant_line = _merge_assistant_line(assistant_line, cleaned_line)
            else:
                flush_assistant()
                assistant_line = cleaned_line
            continue

        flush_assistant()
        rendered = "Запущена команда" if kind == "command" else "Вызван MCP" if kind == "mcp" else "Вызван skill"
        if not items or items[-1] != rendered:
            items.append(rendered)

    if assistant_line:
        if hold_back_trailing_assistant and _looks_incomplete_assistant_line(assistant_line):
            assistant_line = None
        else:
            flush_assistant()
    return items


def _is_wrapped_continuation_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return stripped.startswith((",", ".", ";", ":", ")", "]"))


def _should_merge_assistant_lines(previous: str, current: str) -> bool:
    previous_stripped = previous.strip()
    current_stripped = current.strip()
    if not previous_stripped or not current_stripped:
        return False
    if _is_wrapped_continuation_line(current_stripped):
        return True
    return _looks_incomplete_assistant_line(previous_stripped)


def sanitize_assistant_line(line: str) -> str:
    cleaned = line.strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"\b(?:Shell|uia|ocr)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b[A-Za-z0-9_./\\-]+\.(?:py|md|txt|json|yml|yaml)\s*\+\s*\d+\s*-\s*\d+\b", "", cleaned)
    cleaned = re.sub(r"\b[A-Za-z0-9_./\\-]+\.(?:py|md|txt|json|yml|yaml)\b", "", cleaned)
    cleaned = re.sub(r"\bpython\s+-m\s+tgcod\.main\s+--check-config\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def _merge_assistant_line(previous: str, current: str) -> str:
    stripped = current.strip()
    if not stripped:
        return previous.strip()
    if stripped.startswith((",", ".", ";", ":", ")", "]")):
        return (previous.rstrip() + stripped).strip()
    if previous.rstrip().endswith(("-", "—", "–")):
        return (previous.rstrip() + stripped).strip()
    return f"{previous.rstrip()} {stripped}".strip()


def _looks_incomplete_assistant_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith(("-", "•", "*")):
        return False
    if stripped.endswith((".", "!", "?", ":", ";", "…", ")", "]", "\"", "”")):
        return False
    if len(stripped.split()) <= 4:
        return False
    return True


def _is_progress_assistant_line(line: str) -> bool:
    stripped = line.strip()
    lowered = stripped.lower()
    if not stripped:
        return True
    if lowered in {"думаю", "делаю", "проверяю", "ищу", "фиксирую", "готово"}:
        return True
    progress_prefixes = (
        "думаю",
        "делаю",
        "проверяю",
        "ищу",
        "сейчас ",
        "теперь ",
        "сначала ",
        "следующий ",
        "нашёл ",
        "нашел ",
        "сделал ",
        "запускаю ",
        "запустил ",
        "локализую ",
        "локализирую ",
        "добавляю ",
        "сдвигаю ",
        "измеряю ",
        "продолжаю ",
    )
    return lowered.startswith(progress_prefixes)


def get_stream_settle_seconds(poll_interval_seconds: float) -> float:
    return max(0.8, poll_interval_seconds * 1.5)
