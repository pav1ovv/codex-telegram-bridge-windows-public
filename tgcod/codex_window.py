from __future__ import annotations

import io
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from .config import Settings

logger = logging.getLogger(__name__)


class CodexWindowError(RuntimeError):
    pass


@dataclass(slots=True)
class DetectionProbe:
    has_window: bool
    has_editable_control: bool
    readable_text_controls: int
    has_prosemirror_editor: bool = False
    forced_backend: str | None = None


def choose_backend(probe: DetectionProbe) -> str:
    if probe.forced_backend in {"uia", "ocr"}:
        return probe.forced_backend
    if probe.has_window and (probe.has_editable_control or probe.has_prosemirror_editor):
        return "uia"
    return "ocr"


def build_stop_strategy(has_stop_button: bool) -> list[str]:
    actions = []
    if has_stop_button:
        actions.append("button")
    actions.extend(["esc", "ctrl+c"])
    return actions


class WindowHealthTracker:
    def __init__(self, max_failures: int = 3) -> None:
        self.max_failures = max_failures
        self.failure_count = 0

    @property
    def is_healthy(self) -> bool:
        return self.failure_count < self.max_failures

    def record_failure(self) -> None:
        self.failure_count += 1

    def record_success(self) -> None:
        self.failure_count = 0


class CodexWindowController:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.health = WindowHealthTracker(settings.health_failure_threshold)
        self._window: Any | None = None
        self._backend: BaseBackend | None = None
        self.backend_name = "uninitialized"

    def ensure_ready(self) -> None:
        if self._backend is not None and self._window is not None:
            return
        window = self._find_window()
        probe = self._probe_window(window)
        backend_name = choose_backend(probe)
        self._window = window
        self._backend = UIABackend(window, self.settings) if backend_name == "uia" else OCRBackend(window, self.settings)
        self.backend_name = backend_name
        logger.info("Codex window detected with backend=%s", backend_name)

    def submit_task(self, text: str) -> None:
        self._get_backend().submit_task(text)

    def stop_task(self) -> None:
        self._get_backend().stop_task()

    def read_output_snapshot(self) -> str:
        try:
            text = self._get_backend().read_output_snapshot()
        except Exception as exc:
            self.health.record_failure()
            raise CodexWindowError(f"Failed to read Codex output: {exc}") from exc
        self.health.record_success()
        return text

    def capture_screenshot(self) -> bytes:
        return self._get_backend().capture_screenshot()

    def is_task_running(self, snapshot: str | None = None) -> bool | None:
        return self._get_backend().is_task_running(snapshot)

    def edit_last_submission(self, old_text: str, new_text: str) -> bool:
        backend = self._get_backend()
        if hasattr(backend, "edit_last_submission"):
            return bool(backend.edit_last_submission(old_text, new_text))
        return False

    def _get_backend(self) -> "BaseBackend":
        self.ensure_ready()
        if self._backend is None:
            raise CodexWindowError("Codex backend is not initialized")
        return self._backend

    def _find_window(self) -> Any:
        try:
            from pywinauto import Desktop
        except ImportError as exc:
            raise CodexWindowError("pywinauto is required for Codex window automation") from exc

        regex = self.settings.code_window_title_regex
        windows = Desktop(backend="uia").windows(title_re=regex, visible_only=False)
        candidates = [window for window in windows if _safe_window_text(window)]
        if not candidates:
            candidates = windows
        if not candidates:
            raise CodexWindowError(f"No Codex window matched regex: {regex}")
        return candidates[0]

    def _probe_window(self, window: Any) -> DetectionProbe:
        descendants = _safe_descendants(window)
        window_rect = _rect_to_tuple(window.rectangle())
        readable = 0
        has_editable = False
        has_prosemirror = False
        for control in descendants:
            control_type = _safe_control_type(control)
            if control_type in {"Edit", "Document"}:
                has_editable = True
            rect = _safe_rect_tuple(control)
            if rect is not None and is_probably_prosemirror_editor_rect(
                control_type=control_type,
                class_name=_safe_class_name(control),
                rect=rect,
                window_rect=window_rect,
            ):
                has_prosemirror = True
            if control_type in {"Text", "Edit", "Document"} and _safe_window_text(control).strip():
                readable += 1
        return DetectionProbe(
            has_window=window is not None,
            has_editable_control=has_editable,
            readable_text_controls=readable,
            has_prosemirror_editor=has_prosemirror,
            forced_backend=self.settings.force_backend,
        )


class BaseBackend:
    def __init__(self, window: Any, settings: Settings) -> None:
        self.window = window
        self.settings = settings

    def submit_task(self, text: str) -> None:
        raise NotImplementedError

    def stop_task(self) -> None:
        raise NotImplementedError

    def read_output_snapshot(self) -> str:
        raise NotImplementedError

    def is_task_running(self, snapshot: str | None = None) -> bool | None:
        return None

    def capture_screenshot(self) -> bytes:
        image = self.window.capture_as_image()
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        return stream.getvalue()

    def edit_last_submission(self, old_text: str, new_text: str) -> bool:
        return False

    def _focus_window(self) -> None:
        if hasattr(self.window, "set_focus"):
            self.window.set_focus()


class UIABackend(BaseBackend):
    def submit_task(self, text: str) -> None:
        from pywinauto.keyboard import send_keys

        prosemirror = self._find_prosemirror_editor()
        if prosemirror is not None:
            self._submit_via_prosemirror(prosemirror, text, clear_existing=False)
            return
        control = self._find_input_control()
        if control is None:
            self._submit_via_click_paste(text, clear_existing=False)
            return
        self._set_input_text(control, text)
        time.sleep(0.1)
        send_keys("{ENTER}")

    def stop_task(self) -> None:
        from pywinauto.keyboard import send_keys

        stop_button = self._find_stop_button()
        for action in build_stop_strategy(stop_button is not None):
            if action == "button" and stop_button is not None:
                try:
                    stop_button.invoke()
                    return
                except Exception:
                    stop_button.click_input()
                    return
            if action == "esc":
                self._focus_window()
                send_keys("{ESC}")
            if action == "ctrl+c":
                self._focus_window()
                send_keys("^c")

    def read_output_snapshot(self) -> str:
        elements: list[tuple[tuple[int, int, int, int], str]] = []
        window_rect = _rect_to_tuple(self.window.rectangle())
        for control in _safe_descendants(self.window):
            control_type = _safe_control_type(control)
            if control_type not in {"Text", "Button"}:
                continue
            rect = _safe_rect_tuple(control)
            if rect is None or not is_probably_chat_content_rect(rect, window_rect):
                continue
            text = sanitize_visible_chat_text(_safe_window_text(control), control_type)
            if text:
                elements.append((rect, text))
        if not elements:
            text = _safe_window_text(self.window).strip()
            return sanitize_visible_chat_text(text, "Text")
        return layout_visible_chat_elements(elements)

    def is_task_running(self, snapshot: str | None = None) -> bool | None:
        return self._find_stop_button() is not None

    def _find_input_control(self) -> Any | None:
        window_rect = _rect_to_tuple(self.window.rectangle())
        candidates = []
        for control in _safe_descendants(self.window):
            if _safe_control_type(control) != "Edit":
                continue
            rect = _safe_rect_tuple(control)
            if rect is None or not is_probably_visible_input_rect(rect, window_rect):
                continue
            candidates.append(control)
        return candidates[-1] if candidates else None

    def _find_prosemirror_editor(self) -> Any | None:
        window_rect = _rect_to_tuple(self.window.rectangle())
        candidates: list[tuple[tuple[int, int, int, int], Any]] = []
        for control in _safe_descendants(self.window):
            rect = _safe_rect_tuple(control)
            if rect is None:
                continue
            if not is_probably_prosemirror_editor_rect(
                control_type=_safe_control_type(control),
                class_name=_safe_class_name(control),
                rect=rect,
                window_rect=window_rect,
            ):
                continue
            candidates.append((rect, control))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0][1], item[0][0]))
        return candidates[-1][1]

    def edit_last_submission(self, old_text: str, new_text: str) -> bool:
        edit_button = self._find_edit_button()
        if edit_button is None:
            return False
        try:
            try:
                edit_button.invoke()
            except Exception:
                edit_button.click_input()
            prosemirror = self._find_prosemirror_editor()
            if prosemirror is not None:
                self._submit_via_prosemirror(prosemirror, new_text, clear_existing=True)
                return True
            control = self._find_input_control()
            if control is None:
                self._submit_via_click_paste(new_text, clear_existing=True)
                return True
            self._set_input_text(control, new_text)
            from pywinauto.keyboard import send_keys

            time.sleep(0.1)
            send_keys("{ENTER}")
            return True
        except Exception:
            logger.exception("Native UIA edit failed")
            return False

    def _find_stop_button(self) -> Any | None:
        for control in _safe_descendants(self.window):
            if _safe_control_type(control) != "Button":
                continue
            name = _safe_window_text(control)
            if re.search(r"(stop|cancel|interrupt)", name, re.IGNORECASE):
                return control
        return None

    def _find_edit_button(self) -> Any | None:
        buttons = []
        for control in _safe_descendants(self.window):
            if _safe_control_type(control) != "Button":
                continue
            name = _safe_window_text(control)
            if re.search(r"(edit|pencil|rewrite)", name, re.IGNORECASE):
                buttons.append(control)
        return buttons[-1] if buttons else None

    def _set_input_text(self, control: Any | None, text: str) -> None:
        from pywinauto.keyboard import send_keys

        if control is not None and hasattr(control, "set_edit_text"):
            try:
                control.set_edit_text(text)
                return
            except Exception:
                pass
        self._focus_window()
        if control is not None:
            try:
                control.set_focus()
            except Exception:
                control = None
        if control is None:
            raise CodexWindowError("Could not focus Codex input control")
        time.sleep(0.1)
        send_keys("^a")
        time.sleep(0.05)
        if _try_copy_to_clipboard(text):
            send_keys("^v")
        else:
            if control is not None and hasattr(control, "type_keys"):
                control.type_keys(text, with_spaces=True, pause=0.02)
            else:
                send_keys(text, with_spaces=True, pause=0.02)

    def _submit_via_prosemirror(self, editor: Any, text: str, clear_existing: bool) -> None:
        from pywinauto.keyboard import send_keys

        self._focus_window()
        editor.click_input()
        time.sleep(0.25)
        if clear_existing:
            try:
                send_keys("^a{BACKSPACE}", pause=0.05)
                time.sleep(0.1)
            except Exception:
                logger.exception("Failed to clear ProseMirror editor before typing")
        send_keys(text, with_spaces=True, pause=0.03, vk_packet=True)
        time.sleep(0.15)
        send_keys("{ENTER}", pause=0.05)

    def _submit_via_click_paste(self, text: str, clear_existing: bool) -> None:
        try:
            import pyautogui
        except ImportError as exc:
            raise CodexWindowError("pyautogui is required for click/paste Codex input fallback") from exc

        self._focus_window()
        click_x, click_y = get_ocr_input_click_point(_rect_to_tuple(self.window.rectangle()))
        pyautogui.click(click_x, click_y, clicks=2, interval=0.2)
        time.sleep(0.25)
        if clear_existing:
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.1)
        if _try_copy_to_clipboard(text):
            pyautogui.hotkey("shift", "insert")
        else:
            _send_unicode_text(text)
        time.sleep(0.15)
        pyautogui.press("enter")


class OCRBackend(BaseBackend):
    def __init__(self, window: Any, settings: Settings) -> None:
        super().__init__(window, settings)
        self._ocr_reader = OcrReader(settings)

    def submit_task(self, text: str) -> None:
        try:
            import pyautogui
        except ImportError as exc:
            raise CodexWindowError("pyautogui is required for OCR mode input") from exc

        self._focus_window()
        click_x, click_y = get_ocr_input_click_point(_rect_to_tuple(self.window.rectangle()))
        pyautogui.click(click_x, click_y, clicks=2, interval=0.2)
        time.sleep(0.25)
        if _try_copy_to_clipboard(text):
            pyautogui.hotkey("shift", "insert")
        else:
            _send_unicode_text(text)
        time.sleep(0.15)
        pyautogui.press("enter")

    def stop_task(self) -> None:
        try:
            import pyautogui
        except ImportError as exc:
            raise CodexWindowError("pyautogui is required for OCR mode stop control") from exc

        self._focus_window()
        pyautogui.press("esc")
        pyautogui.hotkey("ctrl", "c")

    def read_output_snapshot(self) -> str:
        image = self.window.capture_as_image()
        cropped = image.crop(get_ocr_content_crop_box(image.size))
        return self._ocr_reader.read_text(cropped)

    def is_task_running(self, snapshot: str | None = None) -> bool | None:
        text = (snapshot or "").lower()
        if not text:
            return None
        running_markers = (
            "cancel send",
            "interrupt",
            "stop generating",
            "остановить",
            "прервать",
        )
        return any(marker in text for marker in running_markers)


class OcrReader:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._rapidocr = None

    def read_text(self, image: Any) -> str:
        pytesseract_error: Exception | None = None
        try:
            import pytesseract

            if self.settings.tesseract_cmd:
                pytesseract.pytesseract.tesseract_cmd = self.settings.tesseract_cmd
            text = pytesseract.image_to_string(image, lang=self.settings.ocr_language)
            if text.strip():
                return text
        except Exception as exc:
            pytesseract_error = exc

        try:
            import numpy as np
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise CodexWindowError(
                "No OCR engine is available. Install pytesseract/Tesseract or rapidocr-onnxruntime."
            ) from (pytesseract_error or exc)

        if self._rapidocr is None:
            self._rapidocr = RapidOCR()
        result, _elapsed = self._rapidocr(np.array(image))
        if not result:
            return ""
        return "\n".join(item[1] for item in result)


def _safe_descendants(window: Any) -> list[Any]:
    try:
        return list(window.descendants())
    except Exception:
        return []


def _safe_control_type(control: Any) -> str:
    try:
        return getattr(control.element_info, "control_type", "") or ""
    except Exception:
        return ""


def _safe_class_name(control: Any) -> str:
    try:
        return getattr(control.element_info, "class_name", "") or ""
    except Exception:
        return ""


def _safe_window_text(control: Any) -> str:
    try:
        text = control.window_text()
        if text:
            return str(text)
    except Exception:
        pass
    try:
        texts = control.texts()
        if texts:
            return "\n".join(str(item) for item in texts if item)
    except Exception:
        pass
    return ""


def _rect_to_tuple(rect: Any) -> tuple[int, int, int, int]:
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def _safe_rect_tuple(control: Any) -> tuple[int, int, int, int] | None:
    try:
        return _rect_to_tuple(control.rectangle())
    except Exception:
        return None


def is_probably_chat_content_rect(
    rect: tuple[int, int, int, int],
    window_rect: tuple[int, int, int, int],
) -> bool:
    left, top, right, bottom = rect
    win_left, win_top, win_right, win_bottom = window_rect

    if right <= left or bottom <= top:
        return False
    if top < win_top + 80 or bottom > win_bottom - 120:
        return False
    if left < win_left + 300:
        return False
    if top >= win_bottom:
        return False
    if bottom <= win_top + 80:
        return False
    if right <= win_left + 320:
        return False
    return True


IGNORED_CONTENT_BUTTONS = {
    "Копировать",
    "Скопировать сообщение",
    "Отменить",
    "Открыть",
    "Вторичное действие",
    "Приложение пользователя",
}

IGNORED_CONTENT_SUBSTRINGS = (
    "Переключить отображение различий в файлах",
    "Скопировать сообщение",
)


def sanitize_visible_chat_text(text: str, control_type: str) -> str:
    normalized = re.sub(r"\s+", " ", text.replace("\r", " ").replace("\n", " ")).strip()
    if not normalized:
        return ""
    if control_type == "Button":
        if normalized in IGNORED_CONTENT_BUTTONS:
            return ""
        for marker in IGNORED_CONTENT_SUBSTRINGS:
            normalized = normalized.replace(marker, "").strip()
        if not normalized:
            return ""
    return normalized


def layout_visible_chat_elements(elements: list[tuple[tuple[int, int, int, int], str]]) -> str:
    sorted_elements = sorted(elements, key=lambda item: (item[0][1], item[0][0], -(item[0][2] - item[0][0])))
    rows: list[dict[str, Any]] = []
    seen_blocks: set[tuple[int, int, str]] = set()

    for rect, text in sorted_elements:
        left, top, right, bottom = rect
        height = max(bottom - top, 1)
        key = (top, left, text)
        if key in seen_blocks:
            continue
        seen_blocks.add(key)

        row = None
        for candidate in rows:
            if abs(candidate["mid_y"] - (top + bottom) / 2) <= max(10, height * 0.6):
                row = candidate
                break
        if row is None:
            row = {"parts": [], "mid_y": (top + bottom) / 2, "top": top}
            rows.append(row)
        row["parts"].append((left, text))

    lines: list[str] = []
    for row in sorted(rows, key=lambda item: item["top"]):
        parts = []
        for _left, text in sorted(row["parts"], key=lambda item: item[0]):
            if text not in parts:
                parts.append(text)
        line = " ".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def is_probably_visible_input_rect(
    rect: tuple[int, int, int, int],
    window_rect: tuple[int, int, int, int],
) -> bool:
    left, top, right, bottom = rect
    win_left, win_top, win_right, win_bottom = window_rect

    if right <= left or bottom <= top:
        return False
    if left < win_left or right > win_right + 5:
        return False
    if top < win_top + 80 or bottom > win_bottom:
        return False
    if top >= win_bottom - 20:
        return False
    if bottom <= win_top:
        return False
    return True


def is_probably_prosemirror_editor_rect(
    control_type: str,
    class_name: str,
    rect: tuple[int, int, int, int],
    window_rect: tuple[int, int, int, int],
) -> bool:
    if control_type != "Group":
        return False
    if "prosemirror" not in class_name.lower():
        return False

    left, top, right, bottom = rect
    win_left, win_top, win_right, win_bottom = window_rect
    height = max(win_bottom - win_top, 1)

    if right <= left or bottom <= top:
        return False
    if left < win_left + 300:
        return False
    if right > win_right + 5:
        return False
    if top < win_top + max(int(height * 0.75), 650):
        return False
    if bottom > win_bottom - 20:
        return False
    return True


def get_ocr_content_crop_box(size: tuple[int, int]) -> tuple[int, int, int, int]:
    width, height = size
    left = min(max(int(width * 0.18), 300), max(width - 200, 0))
    top = min(max(int(height * 0.04), 40), max(height - 200, 0))
    right = max(left + 1, width)
    bottom = max(top + 1, height - max(int(height * 0.12), 120))
    return left, top, right, bottom


def get_ocr_input_click_point(window_rect: tuple[int, int, int, int]) -> tuple[int, int]:
    left, top, right, bottom = window_rect
    width = max(right - left, 1)
    height = max(bottom - top, 1)
    pane_left = left + min(max(int(width * 0.18), 300), max(width - 200, 0))
    pane_right = right - max(int(width * 0.04), 40)
    pane_width = max(pane_right - pane_left, 1)
    click_x = max(pane_left + 40, min(pane_left + int(pane_width * 0.40), right - 120))
    click_y = max(top + 80, bottom - max(int(height * 0.11), 110))
    return click_x, click_y


def _try_copy_to_clipboard(text: str) -> bool:
    try:
        import pyperclip
    except ImportError:
        return False
    try:
        pyperclip.copy(text)
        return True
    except Exception:
        return False


def _send_unicode_text(text: str) -> None:
    from pywinauto.keyboard import send_keys

    encoded: list[str] = []
    special = {
        "{": "{{}",
        "}": "{}}",
        "+": "{+}",
        "^": "{^}",
        "%": "{%}",
        "~": "{~}",
        "(": "{(}",
        ")": "{)}",
        "[": "{[}",
        "]": "{]}",
    }
    for char in text:
        if char == "\n":
            encoded.append("{ENTER}")
        elif char == "\t":
            encoded.append("{TAB}")
        else:
            encoded.append(special.get(char, char))
    send_keys("".join(encoded), with_spaces=True, with_tabs=True, with_newlines=True, pause=0.02)
