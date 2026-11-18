#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antarmuka Telegram Bot, Inline Keyboard, State Persistence untuk GitHub Actions,
serta eksekusi analisis terpadu.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from config import (
    ALLOWED_USER_IDS,
    DEFAULT_SYMBOL,
    DEFAULT_TIMEFRAME,
    STATE_FILE_PATH,
    SYMBOLS,
    TF_CONFIG,
    WATCHLIST_MIN_SCORE,
)
from data_provider import MarketDataError, fetch_candles, fetch_live_price
from entry_engine import detect_scalping_setups
from formatter import format_analysis_report
from risk_manager import build_trade_plan, compute_deterministic_score
from smc_engine import analyze_htf_context, run_smc_snapshot

logger = logging.getLogger("smc.bot")

_busy_chats: Set[int] = set()


# =============================================================================
# PERSISTENSI USER STATE (UNTUK GITHUB ACTIONS STATELESS RUNNER)
# =============================================================================
class BotStateStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: Dict[str, Any] = {
            "last_update_id": 0,
            "users": {},
            "sent_alerts": {},
        }
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except Exception as exc:
                logger.warning("Gagal membaca state file (%s), memakai state baru.", exc)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Batasi ukuran riwayat alert agar file JSON tetap ringan
        alerts = self.data.get("sent_alerts", {})
        if len(alerts) > 200:
            sorted_keys = sorted(alerts.keys(), key=lambda k: alerts[k])
            for old_k in sorted_keys[:-100]:
                alerts.pop(old_k, None)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")

    def get_user(self, chat_id: int) -> Dict[str, Any]:
        cid = str(chat_id)
        users = self.data.setdefault("users", {})
        if cid not in users:
            users[cid] = {
                "symbol": DEFAULT_SYMBOL,
                "tf": DEFAULT_TIMEFRAME,
                "watchlist": False,
            }
        return users[cid]

    def set_user_symbol(self, chat_id: int, symbol: str) -> None:
        u = self.get_user(chat_id)
        u["symbol"] = symbol
        self.save()

    def set_user_tf(self, chat_id: int, tf: str) -> None:
        u = self.get_user(chat_id)
        u["tf"] = tf
        self.save()

    def toggle_watchlist(self, chat_id: int) -> bool:
        u = self.get_user(chat_id)
        u["watchlist"] = not bool(u.get("watchlist", False))
        self.save()
        return u["watchlist"]


# =============================================================================
# MESIN ORKESTRASI ANALISIS LENGKAP
# =============================================================================
def run_full_analysis(symbol: str, tf: str, force: bool = False) -> Dict[str, Any]:
    """Jalankan pipeline penuh: Data -> SMC Engine -> MTF -> Entry Engine -> Risk & Score."""
    series = fetch_candles(symbol, tf, force=force)
    closed = series["closed"]
    ref_price = series["forming"].c if series["forming"] is not None else closed[-1].c

    # Snapshot awal untuk menghitung ATR, lalu sinkronkan dengan harga tick live
    temp_snap = run_smc_snapshot(series, ref_price)
    live_price, tick_note, market_open = fetch_live_price(symbol, ref_price, temp_snap["atr"])
    final_price = live_price if live_price is not None else ref_price

    snap = run_smc_snapshot(series, final_price)
    if not market_open:
        snap["stale"] = True

    htf_ctx = analyze_htf_context(symbol, tf, snap, force=force)

    buy_entry = detect_scalping_setups("BUY", snap, htf_ctx)
    sell_entry = detect_scalping_setups("SELL", snap, htf_ctx)

    buy_trade, buy_err = (
        build_trade_plan(symbol, "BUY", buy_entry, snap, htf_ctx)
        if buy_entry["valid"]
        else (None, "Setup BUY belum memenuhi konfirmasi zona/struktur.")
    )
    sell_trade, sell_err = (
        build_trade_plan(symbol, "SELL", sell_entry, snap, htf_ctx)
        if sell_entry["valid"]
        else (None, "Setup SELL belum memenuhi konfirmasi zona/struktur.")
    )

    buy_score = compute_deterministic_score("BUY", buy_entry, buy_trade, snap, htf_ctx)
    sell_score = compute_deterministic_score("SELL", sell_entry, sell_trade, snap, htf_ctx)

    candidates: List[Tuple[str, int, Dict[str, Any], Dict[str, Any], Dict[str, Any]]] = []
    if buy_trade is not None:
        candidates.append(("BUY", buy_score["total"], buy_entry, buy_trade, buy_score))
    if sell_trade is not None:
        candidates.append(("SELL", sell_score["total"], sell_entry, sell_trade, sell_score))

    candidates.sort(key=lambda x: -x[1])

    signal = "WAIT"
    chosen_entry: Optional[Dict[str, Any]] = None
    chosen_trade: Optional[Dict[str, Any]] = None
    chosen_score = buy_score if buy_score["total"] >= sell_score["total"] else sell_score
    wait_reason = ""
    wait_invalidation = ""

    if candidates:
        best_dir, best_sc, best_ent, best_tr, best_sc_obj = candidates[0]
        second_sc = candidates[1][1] if len(candidates) > 1 else 0

        if best_sc >= 50 and (best_sc - second_sc >= 4 or best_sc >= 65):
            signal = best_dir
            chosen_entry = best_ent
            chosen_trade = best_tr
            chosen_score = best_sc_obj
        else:
            wait_reason = (
                f"Skor BUY ({buy_score['total']}%) dan SELL ({sell_score['total']}%) masih berimbang "
                f"di bawah ambang eksekusi scalping."
            )
    else:
        if buy_score["total"] >= sell_score["total"]:
            wait_reason = buy_err or "Belum ada konfirmasi entry BUY dengan Risk-Reward valid."
        else:
            wait_reason = sell_err or "Belum ada konfirmasi entry SELL dengan Risk-Reward valid."

    # Tentukan level invalidasi saat WAIT agar user tahu level pemicu berikutnya
    bsl = snap["liquidity"]["bsl"]
    ssl = snap["liquidity"]["ssl"]
    if bsl and ssl:
        wait_invalidation = (
            f"Menunggu candle {tf} close menembus Resistance/BSL {bsl[0]:.2f} atau Support/SSL {ssl[0]:.2f}."
        )
    else:
        wait_invalidation = f"Menunggu terbentuknya BOS/CHOCH terkonfirmasi candle close pada {tf}."

    result = {
        "symbol": symbol,
        "tf": tf,
        "signal": signal,
        "snapshot": snap,
        "htf_ctx": htf_ctx,
        "entry_info": chosen_entry,
        "trade": chosen_trade,
        "score": chosen_score,
        "wait_reason": wait_reason,
        "wait_invalidation": wait_invalidation,
    }
    result["report_html"] = format_analysis_report(result)
    return result


# =============================================================================
# INLINE KEYBOARD & RENDER MENU
# =============================================================================
def build_main_menu_text(selected_symbol: str, watchlist_on: bool = False) -> str:
    wl_status = "AKTIF (alert otomatis cron)" if watchlist_on else "NONAKTIF (/auto)"
    return (
        "🟡 <b>SMC HYBRID SCALPING HUNTER</b>\n\n"
        f"Market aktif: <b>{selected_symbol}</b>\n"
        f"Auto-Alert: <b>{wl_status}</b>\n\n"
        "Pilih market &amp; timeframe untuk analisis:"
    )


def build_main_keyboard(selected_symbol: str) -> InlineKeyboardMarkup:
    xau_label = "✅ XAUUSD" if selected_symbol == "XAUUSD" else "XAUUSD"
    btc_label = "✅ BTCUSD" if selected_symbol == "BTCUSD" else "BTCUSD"

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(xau_label, callback_data="m:XAUUSD"),
                InlineKeyboardButton(btc_label, callback_data="m:BTCUSD"),
            ],
            [
                InlineKeyboardButton("M1", callback_data="t:M1"),
                InlineKeyboardButton("M5", callback_data="t:M5"),
                InlineKeyboardButton("M15", callback_data="t:M15"),
            ],
            [
                InlineKeyboardButton("M30", callback_data="t:M30"),
                InlineKeyboardButton("H1", callback_data="t:H1"),
                InlineKeyboardButton("H4", callback_data="t:H4"),
            ],
        ]
    )


def build_result_keyboard(symbol: str, tf: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 ANALISIS ULANG", callback_data=f"r:{symbol}:{tf}"),
                InlineKeyboardButton("🏠 MENU UTAMA", callback_data="menu"),
            ]
        ]
    )


def _is_user_allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return user is not None and user.id in ALLOWED_USER_IDS


async def _safe_edit_message(message, html_text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        await message.edit_text(
            text=html_text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        logger.warning("Gagal edit HTML (%s), fallback ke plain text.", exc)
        plain = re.sub(r"<[^>]+>", "", html_text)
        try:
            await message.edit_text(text=plain, reply_markup=reply_markup)
        except BadRequest as exc2:
            if "message is not modified" not in str(exc2).lower():
                logger.error("Fallback edit gagal: %s", exc2)


async def _execute_and_render(target_message, symbol: str, tf: str, force: bool = True) -> None:
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(None, run_full_analysis, symbol, tf, force)
        text = res["report_html"]
    except MarketDataError as exc:
        logger.warning("MarketDataError %s %s: %s", symbol, tf, exc)
        text = (
            f"❌ <b>Data Market Tidak Tersedia</b>\n"
            f"Market: <b>{symbol}</b> | Timeframe: <b>{tf}</b>\n"
            f"Alasan: {exc}\n\n"
            f"Silakan tekan <b>ANALISIS ULANG</b> beberapa saat lagi."
        )
    except Exception:
        logger.exception("Kesalahan internal analisis %s %s", symbol, tf)
        text = (
            f"❌ <b>Terjadi Gangguan Analisis</b>\n"
            f"Market: <b>{symbol}</b> | Timeframe: <b>{tf}</b>\n"
            f"Silakan coba kembali."
        )

    await _safe_edit_message(target_message, text, build_result_keyboard(symbol, tf))


# =============================================================================
# HANDLER COMMAND & CALLBACK
# =============================================================================
def create_telegram_app(token: str, state_store: BotStateStore) -> Application:
    app = ApplicationBuilder().token(token).build()

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_user_allowed(update) or update.effective_message is None:
            return
        chat_id = update.effective_chat.id
        user_cfg = state_store.get_user(chat_id)
        await update.effective_message.reply_text(
            text=build_main_menu_text(user_cfg["symbol"], user_cfg.get("watchlist", False)),
            parse_mode=ParseMode.HTML,
            reply_markup=build_main_keyboard(user_cfg["symbol"]),
        )

    async def cmd_auto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_user_allowed(update) or update.effective_message is None:
            return
        chat_id = update.effective_chat.id
        enabled = state_store.toggle_watchlist(chat_id)
        status_txt = (
            "🔔 <b>Auto-Alert Watchlist AKTIF</b>\nBot akan mengirim sinyal otomatis saat GitHub Actions cron mendeteksi setup kuat (Skor ≥ %d%%)."
            % WATCHLIST_MIN_SCORE
            if enabled
            else "🔕 <b>Auto-Alert Watchlist NONAKTIF</b>\nAnalisis tetap bisa dijalankan kapan saja lewat menu."
        )
        await update.effective_message.reply_text(status_txt, parse_mode=ParseMode.HTML)

    async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.message is None:
            return
        if not _is_user_allowed(update):
            await query.answer("Akses ditolak.", show_alert=True)
            return

        chat_id = query.message.chat.id
        data = query.data or ""
        parts = data.split(":")

        try:
            # 1. Tombol MENU UTAMA -> kirim pesan menu baru tanpa menimpa hasil analisis
            if data == "menu":
                await query.answer()
                user_cfg = state_store.get_user(chat_id)
                await query.message.chat.send_message(
                    text=build_main_menu_text(user_cfg["symbol"], user_cfg.get("watchlist", False)),
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_main_keyboard(user_cfg["symbol"]),
                )
                return

            # 2. Pilih Market ([ XAUUSD ] / [ BTCUSD ])
            if len(parts) == 2 and parts[0] == "m" and parts[1] in SYMBOLS:
                await query.answer(f"Market {parts[1]} dipilih")
                state_store.set_user_symbol(chat_id, parts[1])
                user_cfg = state_store.get_user(chat_id)
                await _safe_edit_message(
                    query.message,
                    build_main_menu_text(parts[1], user_cfg.get("watchlist", False)),
                    build_main_keyboard(parts[1]),
                )
                return

            # 3. Pilih Timeframe ([ M1 ] .. [ H4 ]) -> kirim pesan hasil BARU (menu tidak tertimpa)
            if len(parts) == 2 and parts[0] == "t" and parts[1] in TF_CONFIG:
                if chat_id in _busy_chats:
                    await query.answer("Analisis sebelumnya masih diproses…")
                    return
                await query.answer()
                _busy_chats.add(chat_id)
                try:
                    tf = parts[1]
                    state_store.set_user_tf(chat_id, tf)
                    sym = state_store.get_user(chat_id)["symbol"]
                    loading_msg = await query.message.chat.send_message(
                        text=f"⏳ <b>Menganalisis struktur SMC {sym} ({tf})…</b>",
                        parse_mode=ParseMode.HTML,
                    )
                    await _execute_and_render(loading_msg, sym, tf, force=True)
                finally:
                    _busy_chats.discard(chat_id)
                return

            # 4. Analisis Ulang ([ 🔄 ANALISIS ULANG ]) -> perbarui pesan hasil yang sedang dibuka
            if len(parts) == 3 and parts[0] == "r" and parts[1] in SYMBOLS and parts[2] in TF_CONFIG:
                if chat_id in _busy_chats:
                    await query.answer("Analisis sebelumnya masih diproses…")
                    return
                await query.answer("Memperbarui analisis…")
                _busy_chats.add(chat_id)
                try:
                    sym, tf = parts[1], parts[2]
                    await _safe_edit_message(
                        query.message,
                        f"⏳ <b>Memperbarui data &amp; analisis {sym} ({tf})…</b>",
                        None,
                    )
                    await _execute_and_render(query.message, sym, tf, force=True)
                finally:
                    _busy_chats.discard(chat_id)
                return

            await query.answer("Tombol tidak dikenali. Ketik /start.", show_alert=True)
        except Exception:
            logger.exception("Error saat memproses callback: %r", data)

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Telegram handler error: %s", context.error)

    app.add_handler(CommandHandler(["start", "menu"], cmd_start))
    app.add_handler(CommandHandler(["auto", "watchlist"], cmd_auto))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)
    return app


# =============================================================================
# MODE EKSEKUSI GITHUB ACTIONS (BOUNDED POLLING + SCHEDULED WATCHLIST SCAN)
# =============================================================================
async def run_github_actions_cycle(
    token: str,
    state_store: BotStateStore,
    poll_seconds: int = 210,
    run_watchlist: bool = True,
) -> None:
    """
    Siklus eksekusi aman untuk GitHub Actions:
    1. Menjalankan pemindaian watchlist terjadwal (jika diaktifkan user via /auto).
    2. Menangani seluruh command & inline button Telegram selama `poll_seconds` detik
       lalu berhenti dengan bersih sebelum batas runner terlampaui.
    """
    app = create_telegram_app(token, state_store)
    await app.initialize()
    await app.start()

    try:
        # 1. Pemindaian terjadwal untuk user yang mengaktifkan /auto
        if run_watchlist:
            subscribed_chats = [
                int(cid)
                for cid, cfg in state_store.data.get("users", {}).items()
                if cfg.get("watchlist")
            ]
            if subscribed_chats:
                loop = asyncio.get_running_loop()
                for sym in SYMBOLS:
                    for tf in ("M15", "H1"):
                        try:
                            res = await loop.run_in_executor(None, run_full_analysis, sym, tf, True)
                        except Exception as exc:
                            logger.warning("Watchlist scan gagal %s %s: %s", sym, tf, exc)
                            continue

                        if res["signal"] in ("BUY", "SELL") and res["score"]["total"] >= WATCHLIST_MIN_SCORE:
                            t = res["trade"]
                            sig_raw = f"{sym}:{tf}:{res['signal']}:{t['entry_low']:.2f}:{t['sl']:.2f}"
                            sig_hash = hashlib.sha1(sig_raw.encode("utf-8")).hexdigest()[:12]

                            for chat_id in subscribed_chats:
                                alert_key = f"{chat_id}:{sig_hash}"
                                if alert_key in state_store.data.setdefault("sent_alerts", {}):
                                    continue
                                try:
                                    await app.bot.send_message(
                                        chat_id=chat_id,
                                        text=res["report_html"],
                                        parse_mode=ParseMode.HTML,
                                        reply_markup=build_result_keyboard(sym, tf),
                                        disable_web_page_preview=True,
                                    )
                                    state_store.data["sent_alerts"][alert_key] = int(time.time())
                                except TelegramError as tg_err:
                                    logger.warning("Gagal kirim alert ke %s: %s", chat_id, tg_err)
                state_store.save()

        # 2. Bounded update polling loop
        deadline = time.monotonic() + max(10, poll_seconds)
        offset = int(state_store.data.get("last_update_id", 0))

        logger.info("Memulai bounded Telegram polling selama %d detik (offset=%d)...", poll_seconds, offset)
        while time.monotonic() < deadline:
            remaining = max(1, min(15, int(deadline - time.monotonic())))
            try:
                updates = await app.bot.get_updates(
                    offset=offset if offset > 0 else None,
                    timeout=remaining,
                    allowed_updates=["message", "callback_query"],
                )
            except TelegramError as exc:
                logger.warning("get_updates error: %s", exc)
                await asyncio.sleep(2.0)
                continue

            for upd in updates:
                offset = upd.update_id + 1
                state_store.data["last_update_id"] = offset
                await app.process_update(upd)

            if updates:
                state_store.save()

        # Pastikan seluruh background task analisis selesai sebelum shutdown
        while _busy_chats:
            await asyncio.sleep(0.5)

    finally:
        state_store.save()
        await app.stop()
        await app.shutdown()
        logger.info("Siklus GitHub Actions selesai dengan bersih.")
