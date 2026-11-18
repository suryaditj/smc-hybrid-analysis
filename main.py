#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Entry point utama SMC HYBRID SCALPING HUNTER V1.
Mendukung dua mode eksekusi:
- `--mode github-actions` (default): Siklus bounded polling + watchlist scan + persistensi state.
- `--mode polling`: Mode polling berkelanjutan untuk pengujian lokal.
"""

import argparse
import asyncio
import logging
import sys

from config import (
    APP_NAME,
    RUNNER_POLL_SECONDS,
    STATE_FILE_PATH,
    TELEGRAM_BOT_TOKEN,
)
from telegram_bot import BotStateStore, create_telegram_app, run_github_actions_cycle

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("smc.main")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument(
        "--mode",
        choices=["github-actions", "polling"],
        default="github-actions",
        help="Mode eksekusi bot (github-actions atau polling lokal).",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=RUNNER_POLL_SECONDS,
        help="Durasi window polling dalam detik pada mode github-actions.",
    )
    parser.add_argument(
        "--no-watchlist",
        action="store_true",
        help="Lewati pemindaian watchlist otomatis pada siklus ini.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.startswith("ISI_"):
        logger.critical("TELEGRAM_BOT_TOKEN belum diset pada environment / GitHub Secrets.")
        sys.exit(1)

    state_store = BotStateStore(STATE_FILE_PATH)

    if args.mode == "github-actions":
        logger.info("Menjalankan %s dalam mode GitHub Actions...", APP_NAME)
        asyncio.run(
            run_github_actions_cycle(
                token=TELEGRAM_BOT_TOKEN,
                state_store=state_store,
                poll_seconds=args.poll_seconds,
                run_watchlist=not args.no_watchlist,
            )
        )
    else:
        logger.info("Menjalankan %s dalam mode polling lokal...", APP_NAME)
        app = create_telegram_app(TELEGRAM_BOT_TOKEN, state_store)
        app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
