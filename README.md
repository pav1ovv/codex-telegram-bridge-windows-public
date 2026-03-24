# Codex Telegram Bridge for Windows

Python bridge for controlling the Codex desktop app from Telegram on Windows.

## What it does

- Sends Telegram tasks into the currently open Codex chat
- Streams new Codex output back into Telegram
- Supports `/task`, `/stop`, `/screenshot`, and `/edit`
- Uses UI Automation first and falls back to OCR only when needed
- Stores per-task JSONL history and local logs
- Restricts access to a single private Telegram chat by default

## Requirements

- Windows 10 or 11
- Python 3.11+
- Codex desktop app installed and signed in
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

Optional but recommended for OCR fallback:

- Tesseract OCR installed and available on `PATH`
- or set `TESSERACT_CMD` explicitly in `.env`

## Install

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Configure

1. Copy `.env.example` to `.env`
2. Fill in at least:

```env
TELEGRAM_BOT_TOKEN=your-bot-token
ALLOWED_CHAT_IDS=
CODEX_WINDOW_TITLE_REGEX=^Codex$
CODEX_FORCE_BACKEND=
POLL_INTERVAL_SECONDS=0.5
TASK_IDLE_FINISH_SECONDS=20
TASK_FINISH_CONFIRM_SECONDS=30
TELEGRAM_CHUNK_SIZE=3500
OCR_LANGUAGE=eng
TESSERACT_CMD=
```

Notes:

- Leave `ALLOWED_CHAT_IDS` empty if you want the first private chat that writes to the bot to become the permanent owner.
- Set `ALLOWED_CHAT_IDS` if you want to lock the bot to specific Telegram chat IDs from the start.
- Keep `CODEX_WINDOW_TITLE_REGEX=^Codex$` unless your local Codex window title is different.
- `TASK_IDLE_FINISH_SECONDS` is the first idle threshold.
- `TASK_FINISH_CONFIRM_SECONDS` is an extra confirmation window before the bot declares the task finished.

## Run

```powershell
python -m tgcod.main
```

Or:

```powershell
run_bot.bat
```

## Telegram commands

- `/task <text>`: send a task into the currently open Codex chat
- `/stop`: try to interrupt the active task
- `/screenshot`: send a screenshot of the Codex window
- `/edit <new text>`: try to edit the last task or send a corrective follow-up

## How it works

1. The bot finds the Codex desktop window by title regex.
2. If the window exposes usable UI Automation controls, it uses `pywinauto`.
3. If not, it falls back to OCR mode with `pyautogui` plus OCR.
4. It reads the visible Codex output, filters UI noise, classifies commands and MCP calls, and forwards only the useful task stream to Telegram.
5. It writes task history to `data/history/<task-id>/events.jsonl`.

## Access control

- The bot accepts commands only in a private Telegram chat.
- If `ALLOWED_CHAT_IDS` is empty, the first private chat that sends a command becomes the owner.
- All other chats are rejected after that.

## Logs and task history

- Process log: `logs/tgcod.log`
- Task history: `data/history/<task-id>/`

## Test

```powershell
python -m unittest discover -s tests -v
python -m tgcod.main --check-config
```

## Practical usage notes

- Keep the Codex window open and visible on the intended chat.
- UIA mode is preferred and more reliable than OCR mode.
- OCR mode is noisier and depends on the visible desktop surface.
- Desktop automation will not be reliable on a locked Windows session.
