#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Entry point utama SMC HYBRID SCALPING HUNTER V1.
Menjalankan Telegram polling secara berkelanjutan hingga batas aman runner GitHub Actions.
"""

import argparse
import logging
import os
import signal
import sys
import threading

from config import APP_NAME, STATE_FILE_PATH, TELEGRAM_BOT_TOKEN
from telegram_bot import BotStateStore, create_telegram_app

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("smc.main")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=int(os.getenv("RUNNER_POLL_SECONDS", "20400")),
        help="Durasi aktif polling sebelum berhenti bersih (detik, 0 = tanpa batas).",
    )
    return parser.parse_args()


def _schedule_clean_stop(seconds: int) -> None:
    if seconds <= 0:
        return

    def _trigger_sigint() -> None:
        logger.info("Mencapai batas sesi %d detik, menghentikan bot dengan bersih...", seconds)
        os.kill(os.getpid(), signal.SIGINT)

    timer = threading.Timer(seconds, _trigger_sigint)
    timer.daemon = True
    timer.start()


def main() -> None:
    args = parse_args()

    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.startswith("ISI_"):
        logger.critical("TELEGRAM_BOT_TOKEN belum diset pada GitHub Secrets / .env.")
        sys.exit(1)

    state_store = BotStateStore(STATE_FILE_PATH)
    app = create_telegram_app(TELEGRAM_BOT_TOKEN, state_store)

    _schedule_clean_stop(args.poll_seconds)
    logger.info("%s aktif mendengarkan Telegram (durasi sesi: %d detik)...", APP_NAME, args.poll_seconds)

    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=False,
    )
    state_store.save()


if __name__ == "__main__":
    main()
