from __future__ import annotations

import argparse
import logging

from .bot import TelegramBotRunner
from .codex_window import CodexWindowController
from .config import load_settings
from .logging_utils import configure_logging
from .storage import TaskStorage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control the Codex Windows app from Telegram.")
    parser.add_argument("--check-config", action="store_true", help="Load configuration and exit.")
    return parser


def _run(args: argparse.Namespace) -> None:
    settings = load_settings()
    configure_logging(settings.log_dir)

    if args.check_config:
        logging.getLogger(__name__).info("Configuration loaded successfully.")
        return

    controller = CodexWindowController(settings)
    storage = TaskStorage(settings.history_dir)
    runner = TelegramBotRunner(settings, controller, storage)
    runner.run()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    _run(args)


if __name__ == "__main__":
    main()
