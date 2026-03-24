import asyncio
import tempfile
import unittest
from pathlib import Path


class FakeController:
    def __init__(self) -> None:
        self.submitted_tasks = []
        self.stop_calls = 0
        self.snapshots = []
        self.screenshot_bytes = b"image"
        self.edit_calls = []
        self.edit_supported = True
        self.running_states = []

    def ensure_ready(self) -> None:
        return None

    def submit_task(self, text: str) -> None:
        self.submitted_tasks.append(text)

    def stop_task(self) -> None:
        self.stop_calls += 1

    def read_output_snapshot(self) -> str:
        if self.snapshots:
            return self.snapshots.pop(0)
        return ""

    def capture_screenshot(self) -> bytes:
        return self.screenshot_bytes

    def edit_last_submission(self, old_text: str, new_text: str) -> bool:
        self.edit_calls.append((old_text, new_text))
        return self.edit_supported

    def is_task_running(self, snapshot: str | None = None) -> bool | None:
        if self.running_states:
            return self.running_states.pop(0)
        return False


class FakeMessenger:
    def __init__(self) -> None:
        self.texts = []
        self.photos = []

    async def send_text(self, chat_id: int, text: str) -> None:
        self.texts.append((chat_id, text))

    async def send_photo(self, chat_id: int, photo_path: str, caption: str | None = None) -> None:
        self.photos.append((chat_id, photo_path, caption))


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_chat_claims_owner_and_other_chat_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tgcod.bot import TaskBridgeService
            from tgcod.config import Settings
            from tgcod.storage import TaskStorage

            controller = FakeController()
            messenger = FakeMessenger()
            settings = Settings.for_tests(
                telegram_bot_token="token",
                base_dir=Path(tmpdir),
            )
            service = TaskBridgeService(settings, controller, messenger, TaskStorage(settings.history_dir))

            await service.start_task(chat_id=101, task_text="claim owner")

            with self.assertRaises(PermissionError):
                await service.send_screenshot(chat_id=202)

    async def test_start_task_submits_text_and_creates_active_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tgcod.bot import TaskBridgeService
            from tgcod.config import Settings
            from tgcod.storage import TaskStorage

            controller = FakeController()
            controller.snapshots = ["existing conversation", "existing conversation"]
            messenger = FakeMessenger()
            settings = Settings.for_tests(
                telegram_bot_token="token",
                base_dir=Path(tmpdir),
            )
            service = TaskBridgeService(settings, controller, messenger, TaskStorage(settings.history_dir))

            await service.start_task(chat_id=1, task_text="build project")

            self.assertEqual(controller.submitted_tasks, ["build project"])
            self.assertIsNotNone(service.active_session)
            self.assertEqual(service.active_session.last_snapshot, "existing conversation")
            self.assertEqual(messenger.texts, [(1, "Задача отправлена в Codex (unknown).")])

    async def test_start_task_fails_when_prompt_cannot_be_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tgcod.bot import TaskBridgeService
            from tgcod.config import Settings
            from tgcod.storage import TaskStorage

            controller = FakeController()
            controller.snapshots = [
                "existing conversation",
                "existing conversation",
                "existing conversation",
                "existing conversation",
            ]
            messenger = FakeMessenger()
            settings = Settings.for_tests(
                telegram_bot_token="token",
                base_dir=Path(tmpdir),
            )
            settings.submit_verify_timeout_seconds = 0.2
            service = TaskBridgeService(settings, controller, messenger, TaskStorage(settings.history_dir))

            with self.assertRaises(RuntimeError):
                await service.start_task(chat_id=1, task_text="build project")

            self.assertIsNone(service.active_session)

    async def test_stream_once_sends_only_new_increment_after_prompt_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tgcod.bot import TaskBridgeService
            from tgcod.config import Settings
            from tgcod.storage import TaskStorage

            controller = FakeController()
            controller.snapshots = [
                "history before task",
                "history before task\ndemo\nЗапросите внесение дополнительных изменений\nassistant line 1",
                "history before task\ndemo\nЗапросите внесение дополнительных изменений\nassistant line 1",
                "history before task\ndemo\nassistant line 1\nassistant line 2",
                "history before task\ndemo\nassistant line 1\nassistant line 2",
            ]
            messenger = FakeMessenger()
            settings = Settings.for_tests(
                telegram_bot_token="token",
                base_dir=Path(tmpdir),
            )
            service = TaskBridgeService(settings, controller, messenger, TaskStorage(settings.history_dir))
            await service.start_task(chat_id=7, task_text="demo")

            await service.stream_once()
            service.active_session.pending_since_monotonic -= 1.0
            await service.stream_once()
            service.active_session.pending_since_monotonic -= 1.0
            await service.stream_once()
            service.active_session.pending_since_monotonic -= 1.0
            await service.stream_once()

            sent_texts = [item[1] for item in messenger.texts]
            self.assertFalse(any(text.strip() == "demo" for text in sent_texts))
            self.assertFalse(any("Запросите внесение дополнительных изменений" in text for text in sent_texts))
            self.assertTrue(any("assistant line 1" in text for text in sent_texts))
            self.assertTrue(any("assistant line 2" in text for text in sent_texts))
            self.assertEqual(sum("assistant line 2" in text for text in sent_texts), 1)
            self.assertFalse(any("assistant line 1\nassistant line 2" in text for text in sent_texts))

    async def test_stop_task_interrupts_controller(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tgcod.bot import TaskBridgeService
            from tgcod.config import Settings
            from tgcod.storage import TaskStorage

            controller = FakeController()
            messenger = FakeMessenger()
            settings = Settings.for_tests(
                telegram_bot_token="token",
                base_dir=Path(tmpdir),
            )
            service = TaskBridgeService(settings, controller, messenger, TaskStorage(settings.history_dir))
            await service.start_task(chat_id=1, task_text="run")

            await service.stop_task(chat_id=1)

            self.assertEqual(controller.stop_calls, 1)
            self.assertIsNone(service.active_session)

    async def test_send_screenshot_uses_controller_and_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tgcod.bot import TaskBridgeService
            from tgcod.config import Settings
            from tgcod.storage import TaskStorage

            controller = FakeController()
            messenger = FakeMessenger()
            settings = Settings.for_tests(
                telegram_bot_token="token",
                base_dir=Path(tmpdir),
            )
            service = TaskBridgeService(settings, controller, messenger, TaskStorage(settings.history_dir))

            await service.send_screenshot(chat_id=9)

            self.assertEqual(len(messenger.photos), 1)
            self.assertTrue(Path(messenger.photos[0][1]).exists())

    async def test_edit_task_uses_native_edit_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tgcod.bot import TaskBridgeService
            from tgcod.config import Settings
            from tgcod.storage import TaskStorage

            controller = FakeController()
            messenger = FakeMessenger()
            settings = Settings.for_tests(
                telegram_bot_token="token",
                base_dir=Path(tmpdir),
            )
            service = TaskBridgeService(settings, controller, messenger, TaskStorage(settings.history_dir))
            await service.start_task(chat_id=1, task_text="old prompt")

            await service.edit_task(chat_id=1, new_text="new prompt")

            self.assertEqual(controller.edit_calls, [("old prompt", "new prompt")])
            self.assertEqual(controller.submitted_tasks, ["old prompt"])

    async def test_edit_task_falls_back_to_correction_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tgcod.bot import TaskBridgeService
            from tgcod.config import Settings
            from tgcod.storage import TaskStorage

            controller = FakeController()
            controller.edit_supported = False
            messenger = FakeMessenger()
            settings = Settings.for_tests(
                telegram_bot_token="token",
                base_dir=Path(tmpdir),
            )
            service = TaskBridgeService(settings, controller, messenger, TaskStorage(settings.history_dir))
            await service.start_task(chat_id=1, task_text="old prompt")

            await service.edit_task(chat_id=1, new_text="new prompt")

            self.assertEqual(len(controller.submitted_tasks), 2)
            self.assertIn("Replace my previous user request", controller.submitted_tasks[1])

    async def test_strip_non_codex_ui_text_filters_combined_toolbar_line(self) -> None:
        from tgcod.bot import strip_non_codex_ui_text

        text = (
            "Действия беседы Настроить действие запуска Переключить терминал "
            "Переключить панель разницы Open in Popout Window\n"
            "Нормальный ответ Codex"
        )

        self.assertEqual(strip_non_codex_ui_text(text), "Нормальный ответ Codex")

    async def test_render_stream_items_maps_command_and_mcp_lines_to_short_statuses(self) -> None:
        from tgcod.bot import build_completion_text, render_stream_items

        snapshot = (
            "Думаю\n"
            "Запущен powershell -NoProfile -Command \"Get-Content ...\"\n"
            "Вызвал Read Text File инструмент из Filesystem MCP\n"
            "Готовлю финальный фикс\n"
            "Cancel Send"
        )

        self.assertEqual(
            render_stream_items(snapshot, ""),
            ["Думаю", "Запущена команда", "Вызван MCP", "Готовлю финальный фикс"],
        )
        self.assertEqual(
            build_completion_text(snapshot, ""),
            "Думаю\nГотовлю финальный фикс",
        )

    async def test_render_stream_items_cleans_inline_artifacts_and_backend_labels(self) -> None:
        from tgcod.bot import build_completion_text, render_stream_items

        snapshot = (
            "выполню tgcod/config.py + 3 - 0 локальную проверку конфигурации и после этого напишу длинный финальный блок,\n"
            "нормальный tgcod/config.py поток из текста, команды и завершения.\n"
            "Запущен python -m tgcod.main --check-config\n"
            "2026-03-24 16:20:48,291 | INFO | __main__ | Configuration loaded successfully.\n"
            "Сделал ещё один короткий прогон поверх текущей версии. Shell\n"
            "uia\n"
            "ocr"
        )

        self.assertEqual(
            render_stream_items(snapshot, ""),
            [
                "выполню локальную проверку конфигурации и после этого напишу длинный финальный блок, нормальный поток из текста, команды и завершения.",
                "Запущена команда",
                "Сделал ещё один короткий прогон поверх текущей версии.",
            ],
        )
        self.assertEqual(
            build_completion_text(snapshot, ""),
            "выполню локальную проверку конфигурации и после этого напишу длинный финальный блок, нормальный поток из текста, команды и завершения.\nСделал ещё один короткий прогон поверх текущей версии.",
        )

    async def test_render_stream_items_merges_wrapped_assistant_lines_without_leaking_prompt(self) -> None:
        from tgcod.bot import render_stream_items

        prompt = "я сейчас тещу то что ты только что реализовал"
        snapshot = (
            f"{prompt}\n"
            "выполню локальную проверку конфигурации и после этого напишу длинный финальный блок,\n"
            "нормальный поток из текста, команды и завершения.\n"
            "Вызвал Read Text File инструмент из Filesystem MCP"
        )

        self.assertEqual(
            render_stream_items(snapshot, prompt),
            [
                "выполню локальную проверку конфигурации и после этого напишу длинный финальный блок, нормальный поток из текста, команды и завершения.",
                "Вызван MCP",
            ],
        )

    async def test_slice_after_user_prompt_discards_old_visible_content_above_prompt(self) -> None:
        from tgcod.bot import build_completion_text, render_stream_items, slice_after_user_prompt

        snapshot = (
            "Что нужно от вас сейчас:\n"
            "Закрыто:\n"
            "tests/test_bot.py + 54 - 0 tests/test_bot.py "
            "я сейчас тещу то что ты только что реализовал, сымитируй немного какую то работу\n"
            "Думаю\n"
            "Наблюдения из этого теста:\n"
            "Первый пункт\n"
            "Большой финал для вашего теста:\n"
            "Финальный абзац"
        )
        prompt = "я сейчас тещу то что ты только что реализовал, сымитируй немного какую то работу"

        self.assertEqual(
            slice_after_user_prompt(snapshot, prompt),
            "Думаю\nНаблюдения из этого теста:\nПервый пункт\nБольшой финал для вашего теста:\nФинальный абзац",
        )
        self.assertEqual(
            render_stream_items(snapshot, prompt),
            ["Думаю", "Наблюдения из этого теста:", "Первый пункт", "Большой финал для вашего теста:", "Финальный абзац"],
        )
        self.assertEqual(
            build_completion_text(snapshot, prompt),
            "Думаю\nНаблюдения из этого теста:\nПервый пункт\nБольшой финал для вашего теста:\nФинальный абзац",
        )

    async def test_render_stream_items_ignores_diff_artifacts_and_merges_split_paragraph(self) -> None:
        from tgcod.bot import build_completion_text, render_stream_items

        snapshot = (
            "tgcod/bot.py + 61 - 7 tgcod/bot.py\n"
            "Думаю\n"
            "Сделал ещё один синтетический прогон уже на новой версии: прочитал README и ключевые runtime-модули, проверил\n"
            ", посмотрел тестовый каталог через Serena. Теперь завершаю это длинным финальным блоком. --check-config\n"
            "Работал на протяжении 17s\n"
            "Вызвал List Dir инструмент из Serena MCP\n"
            "tests\n"
            "Синтетический прогон завершён."
        )

        self.assertEqual(
            render_stream_items(snapshot, ""),
            [
                "Думаю",
                "Сделал ещё один синтетический прогон уже на новой версии: прочитал README и ключевые runtime-модули, проверил, посмотрел тестовый каталог через Serena. Теперь завершаю это длинным финальным блоком. --check-config",
                "Вызван MCP",
                "Синтетический прогон завершён.",
            ],
        )
        self.assertEqual(
            build_completion_text(snapshot, ""),
            "Думаю\nСделал ещё один синтетический прогон уже на новой версии: прочитал README и ключевые runtime-модули, проверил, посмотрел тестовый каталог через Serena. Теперь завершаю это длинным финальным блоком. --check-config\nСинтетический прогон завершён.",
        )

    async def test_render_stream_items_preserves_distinct_assistant_lines(self) -> None:
        from tgcod.bot import build_completion_text, render_stream_items

        snapshot = (
            "Наблюдения из этого теста:\n"
            "Первая отдельная строка\n"
            "Вторая отдельная строка\n"
            "Большой финал для вашего теста:\n"
            "Финальный абзац"
        )

        self.assertEqual(
            render_stream_items(snapshot, ""),
            [
                "Наблюдения из этого теста:",
                "Первая отдельная строка",
                "Вторая отдельная строка",
                "Большой финал для вашего теста:",
                "Финальный абзац",
            ],
        )
        self.assertEqual(
            build_completion_text(snapshot, ""),
            "Наблюдения из этого теста:\nПервая отдельная строка\nВторая отдельная строка\nБольшой финал для вашего теста:\nФинальный абзац",
        )

    async def test_append_stream_history_keeps_non_consecutive_duplicate_assistant_lines(self) -> None:
        from tgcod.bot import append_stream_history

        self.assertEqual(
            append_stream_history(("Думаю", "Вызван MCP"), ("Думаю",)),
            ("Думаю", "Вызван MCP", "Думаю"),
        )

    async def test_extract_final_assistant_items_returns_tail_without_progress_lines(self) -> None:
        from tgcod.bot import extract_final_assistant_items

        items = (
            "Думаю",
            "Проверяю поток",
            "Вызван MCP",
            "Синтетический прогон завершён.",
            "За этот тест я специально прошёл через несколько разных типов активности.",
            "Если текущая версия работает правильно, то Telegram-лента должна выглядеть как связный журнал.",
        )

        self.assertEqual(
            extract_final_assistant_items(items),
            [
                "Синтетический прогон завершён.",
                "За этот тест я специально прошёл через несколько разных типов активности.",
                "Если текущая версия работает правильно, то Telegram-лента должна выглядеть как связный журнал.",
            ],
        )

    async def test_append_stream_history_avoids_reappending_visible_tail(self) -> None:
        from tgcod.bot import append_stream_history

        self.assertEqual(
            append_stream_history(
                ("Думаю", "Синтетический прогон завершён.", "Финальный блок первая строка", "Финальный блок вторая строка"),
                ("Синтетический прогон завершён.", "Финальный блок первая строка", "Финальный блок вторая строка"),
            ),
            ("Думаю", "Синтетический прогон завершён.", "Финальный блок первая строка", "Финальный блок вторая строка"),
        )

    async def test_idle_finish_sends_single_filtered_completion_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tgcod.bot import TaskBridgeService
            from tgcod.config import Settings
            from tgcod.storage import TaskStorage

            controller = FakeController()
            controller.snapshots = [
                "history before task",
                "history before task\nДумаю\nЗапущен powershell -NoProfile -Command \"Get-Content ...\"\nГотово\n- пункт",
                "history before task\nДумаю\nЗапущен powershell -NoProfile -Command \"Get-Content ...\"\nГотово\n- пункт",
                "history before task\nДумаю\nЗапущен powershell -NoProfile -Command \"Get-Content ...\"\nГотово\n- пункт",
            ]
            messenger = FakeMessenger()
            settings = Settings.for_tests(
                telegram_bot_token="token",
                base_dir=Path(tmpdir),
            )
            settings.task_idle_finish_seconds = 1
            settings.task_finish_confirm_seconds = 0
            service = TaskBridgeService(settings, controller, messenger, TaskStorage(settings.history_dir))
            await service.start_task(chat_id=5, task_text="demo")

            await service.stream_once()
            service.active_session.pending_since_monotonic -= 1.0
            await service.stream_once()
            service.active_session.last_change_monotonic -= 2.0
            await service.stream_once()

            sent_texts = [item[1] for item in messenger.texts]
            self.assertTrue(any(text == "Задача завершена." for text in sent_texts))
            self.assertFalse(any("Запущен powershell" in text for text in sent_texts))

    async def test_idle_finish_waits_while_controller_reports_task_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tgcod.bot import TaskBridgeService
            from tgcod.config import Settings
            from tgcod.storage import TaskStorage

            controller = FakeController()
            controller.snapshots = [
                "history before task",
                "history before task\ndemo\nДумаю",
                "history before task\ndemo\nДумаю",
                "history before task\ndemo\nДумаю",
                "history before task\ndemo\nДумаю",
                "history before task\ndemo\nДумаю",
            ]
            controller.running_states = [True, False, False]
            messenger = FakeMessenger()
            settings = Settings.for_tests(
                telegram_bot_token="token",
                base_dir=Path(tmpdir),
            )
            settings.task_idle_finish_seconds = 1
            settings.task_finish_confirm_seconds = 2
            service = TaskBridgeService(settings, controller, messenger, TaskStorage(settings.history_dir))
            await service.start_task(chat_id=5, task_text="demo")

            await service.stream_once()
            service.active_session.pending_since_monotonic -= 1.0
            await service.stream_once()
            service.active_session.last_change_monotonic -= 2.0
            await service.stream_once()

            self.assertIsNotNone(service.active_session)
            self.assertFalse(any(text == "Задача завершена." for _chat_id, text in messenger.texts))

            service.active_session.last_change_monotonic -= 2.0
            await service.stream_once()
            self.assertIsNotNone(service.active_session)

            service.active_session.idle_candidate_since_monotonic -= 3.0
            await service.stream_once()
            self.assertIsNone(service.active_session)
            self.assertTrue(any(text == "Задача завершена." for _chat_id, text in messenger.texts))


if __name__ == "__main__":
    unittest.main()
