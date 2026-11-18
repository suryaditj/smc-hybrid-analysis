#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Konfigurasi pusat SMC HYBRID SCALPING HUNTER V1.
Mengatur market, batas 20 candle per timeframe, pemetaan HTF,
konversi pip terpisah untuk XAUUSD & BTCUSD, serta parameter runner.
"""

import os
from datetime import timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# =============================================================================
# IDENTITAS & KEAMANAN
# =============================================================================
APP_NAME = "SMC HYBRID SCALPING HUNTER V1"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

_raw_allowed = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = {
    int(uid.strip())
    for uid in _raw_allowed.split(",")
    if uid.strip().isdigit()
}

# =============================================================================
# KONFIGURASI SUMBER DATA & BATAS CANDLE
# =============================================================================
BIQUOTE_BASE_URL = "https://biquote.io/api"
SYMBOLS = ["XAUUSD", "BTCUSD"]
DEFAULT_SYMBOL = "XAUUSD"
DEFAULT_TIMEFRAME = "M15"

# WAJIB maksimal 20 candle per timeframe
MAX_CANDLES_PER_TF = 20
MIN_CLOSED_CANDLES = 10
CACHE_TTL_SECONDS = 8

WIB = timezone(timedelta(hours=7))

# Pemetaan timeframe & konteks Multi-Timeframe (MTF) sesuai spesifikasi
TF_ORDER = ["M1", "M5", "M15", "M30", "H1", "H4"]

TF_CONFIG = {
    "M1": {
        "sec": 60,
        "interval": "1m",
        "htf_list": ["M5", "M15"],
        "swing_micro": 1,
        "swing_major": 2,
    },
    "M5": {
        "sec": 300,
        "interval": "5m",
        "htf_list": ["M15", "M30"],
        "swing_micro": 1,
        "swing_major": 2,
    },
    "M15": {
        "sec": 900,
        "interval": "15m",
        "htf_list": ["M30", "H1"],
        "swing_micro": 1,
        "swing_major": 2,
    },
    "M30": {
        "sec": 1800,
        "interval": "30m",
        "htf_list": ["H1", "H4"],
        "swing_micro": 1,
        "swing_major": 2,
    },
    "H1": {
        "sec": 3600,
        "interval": "1h",
        "htf_list": ["H4"],
        "swing_micro": 1,
        "swing_major": 2,
    },
    "H4": {
        "sec": 14400,
        "interval": "4h",
        "htf_list": ["H4"],
        "swing_micro": 1,
        "swing_major": 2,
    },
}

# =============================================================================
# KONFIGURASI KONVERSI PIP TERPISAH (XAUUSD vs BTCUSD)
# =============================================================================
# Catatan:
# - XAUUSD: 1 pip standar = $0.10 pergerakan harga (10 poin @ $0.01).
#   Sehingga 30-50 pip = $3.00-$5.00; 50-100 pip = $5.00-$10.00; 100-120 pip = $10.00-$12.00.
# - BTCUSD: 1 pip standar = $1.00 pergerakan harga (100 poin @ $0.01).
#   Sehingga 30-50 pip = $30-$50; 50-100 pip = $50-$100; 100-120 pip = $100-$120.
PIP_CONFIG = {
    "XAUUSD": {
        "pip_size": 0.10,
        "point_size": 0.01,
        "decimals": 2,
        "unit_label": "1 pip = $0.10 (10 poin)",
        "tp1_pips": (30.0, 50.0),
        "tp2_pips": (50.0, 100.0),
        "tp3_pips": (100.0, 120.0),
    },
    "BTCUSD": {
        "pip_size": 1.00,
        "point_size": 0.01,
        "decimals": 2,
        "unit_label": "1 pip = $1.00 (100 poin)",
        "tp1_pips": (30.0, 50.0),
        "tp2_pips": (50.0, 100.0),
        "tp3_pips": (100.0, 120.0),
    },
}

# =============================================================================
# PERSISTENSI & GITHUB ACTIONS RUNNER
# =============================================================================
STATE_FILE_PATH = Path(os.getenv("STATE_FILE_PATH", "data/user_state.json"))
RUNNER_POLL_SECONDS = int(os.getenv("RUNNER_POLL_SECONDS", "210"))
WATCHLIST_MIN_SCORE = int(os.getenv("WATCHLIST_MIN_SCORE", "65"))
