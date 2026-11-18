#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Layer pengambilan data market OHLCV dan harga terkini melalui BiQuote public API.
- Tanpa API Key.
- Dibatasi ketat maksimal 20 candle per timeframe.
- Memisahkan candle close vs candle berjalan (forming) secara akurat.
- Nama penyedia data tidak pernah disertakan pada pesan error untuk user.
"""

import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

from config import (
    BIQUOTE_BASE_URL,
    CACHE_TTL_SECONDS,
    MAX_CANDLES_PER_TF,
    MIN_CLOSED_CANDLES,
    SYMBOLS,
    TF_CONFIG,
    WIB,
)

logger = logging.getLogger("smc.data")


@dataclass(frozen=True)
class Candle:
    t: int      # epoch detik (waktu open candle)
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


class MarketDataError(Exception):
    """Error pengambilan atau validasi data market (tanpa menyebut nama provider)."""

    def __init__(self, user_message: str, status: Optional[int] = None):
        super().__init__(user_message)
        self.status = status


_SESSION = requests.Session()
_SESSION.headers.update(
    {
        "User-Agent": "SMC-Hybrid-Scalping-Hunter/1.0",
        "Accept": "application/json",
    }
)

_cache: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
_cache_lock = threading.Lock()

_KEYS_T = ("time", "t", "timestamp", "ts", "opentime", "open_time", "datetime", "date", "d")
_KEYS_O = ("open", "o")
_KEYS_H = ("high", "h")
_KEYS_L = ("low", "l")
_KEYS_C = ("close", "c")
_KEYS_V = ("volume", "v", "tick_volume", "vol", "tickvolume")


def _http_get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    last_reason = "koneksi tidak stabil"
    for attempt in range(3):
        try:
            resp = _SESSION.get(url, params=params, timeout=(5, 15))
        except requests.Timeout:
            last_reason = "waktu tunggu habis"
            logger.warning("HTTP timeout on %s (attempt %d)", url, attempt + 1)
        except requests.ConnectionError:
            last_reason = "koneksi terputus"
            logger.warning("HTTP connection error on %s (attempt %d)", url, attempt + 1)
        except requests.RequestException as exc:
            last_reason = "gangguan jaringan (%s)" % type(exc).__name__
            logger.warning("HTTP request error on %s: %s", url, exc)
        else:
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    raise MarketDataError("Respons data market tidak valid.")
            if resp.status_code in (400, 404, 422):
                logger.error("HTTP %d from upstream for %s params=%s", resp.status_code, url, params)
                raise MarketDataError(
                    "Parameter market atau timeframe tidak didukung server data.",
                    status=resp.status_code,
                )
            last_reason = "HTTP %d" % resp.status_code
            logger.warning("Upstream HTTP %d on %s (attempt %d)", resp.status_code, url, attempt + 1)
            if resp.status_code not in (429, 500, 502, 503, 504):
                raise MarketDataError("Server data mengembalikan status %d." % resp.status_code, status=resp.status_code)

        if attempt < 2:
            time.sleep(0.8 * (attempt + 1))

    raise MarketDataError("Feed data market sedang tidak dapat dijangkau (%s)." % last_reason)


def _parse_time(raw: Any) -> Optional[float]:
    """Konversi epoch detik/milidetik atau ISO timestamp ke epoch detik (float)."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
    elif isinstance(raw, str):
        s = raw.strip()
        try:
            val = float(s)
        except ValueError:
            iso = s.replace("Z", "+00:00").replace(" ", "T", 1)
            try:
                dt = datetime.fromisoformat(iso)
            except ValueError:
                try:
                    dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
    else:
        return None

    if val > 1e12:
        val /= 1000.0
    if val < 1e9:
        return None
    return val


def _to_float(raw: Any) -> Optional[float]:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) else None


def _pick(mapping: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    for k in keys:
        if k in mapping and mapping[k] is not None:
            return mapping[k]
    return None


def _extract_rows(payload: Any) -> Optional[List[Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("bars", "candles", "data", "ohlc", "result", "items"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                nested = _extract_rows(v)
                if nested is not None:
                    return nested
    return None


def parse_candle_rows(rows: List[Any]) -> List[Candle]:
    """Parse baris mentah JSON menjadi daftar objek Candle tervalidasi."""
    parsed: List[Candle] = []
    bad_count = 0

    for row in rows:
        t = o = h = l = c = None
        v = 0.0
        if isinstance(row, dict):
            low_map = {str(k).lower(): val for k, val in row.items()}
            t = _parse_time(_pick(low_map, _KEYS_T))
            o = _to_float(_pick(low_map, _KEYS_O))
            h = _to_float(_pick(low_map, _KEYS_H))
            l = _to_float(_pick(low_map, _KEYS_L))
            c = _to_float(_pick(low_map, _KEYS_C))
            v_raw = _to_float(_pick(low_map, _KEYS_V))
            v = v_raw if (v_raw is not None and v_raw >= 0.0) else 0.0
        elif isinstance(row, (list, tuple)) and len(row) >= 5:
            t = _parse_time(row[0])
            o, h, l, c = (_to_float(x) for x in row[1:5])
            if len(row) >= 6:
                v_raw = _to_float(row[5])
                v = v_raw if (v_raw is not None and v_raw >= 0.0) else 0.0

        if t is None or None in (o, h, l, c) or min(o, h, l, c) <= 0.0:
            bad_count += 1
            continue

        parsed.append(Candle(int(t), float(o), float(h), float(l), float(c), float(v)))

    if rows and bad_count > 0.3 * len(rows):
        raise MarketDataError("Format data OHLC tidak dapat diproses (%d/%d baris rusak)." % (bad_count, len(rows)))

    return parsed


def clean_and_limit_candles(candles: List[Candle], max_candles: int = MAX_CANDLES_PER_TF) -> List[Candle]:
    """
    Urutkan dari terlama ke terbaru, hapus duplikat timestamp,
    normalisasi High/Low, dan batasi ketat maksimal `max_candles` (20 candle).
    """
    by_time: Dict[int, Candle] = {}
    for c in candles:
        by_time[c.t] = c

    ordered: List[Candle] = []
    for t in sorted(by_time.keys()):
        c = by_time[t]
        hi = max(c.h, c.o, c.c, c.l)
        lo = min(c.l, c.o, c.c, c.h)
        if hi <= lo:
            continue
        ordered.append(Candle(c.t, c.o, hi, lo, c.c, c.v))

    if len(ordered) > max_candles:
        ordered = ordered[-max_candles:]
    return ordered


def candles_to_dataframe(candles: List[Candle]) -> pd.DataFrame:
    """Konversi list Candle menjadi pandas DataFrame dengan kolom standar & waktu WIB."""
    if not candles:
        return pd.DataFrame(columns=["t", "open", "high", "low", "close", "volume", "time_wib"])

    df = pd.DataFrame(
        {
            "t": np.array([c.t for c in candles], dtype=np.int64),
            "open": np.array([c.o for c in candles], dtype=np.float64),
            "high": np.array([c.h for c in candles], dtype=np.float64),
            "low": np.array([c.l for c in candles], dtype=np.float64),
            "close": np.array([c.c for c in candles], dtype=np.float64),
            "volume": np.array([c.v for c in candles], dtype=np.float64),
        }
    )
    df["time_wib"] = [
        datetime.fromtimestamp(int(ts), tz=WIB).strftime("%Y-%m-%d %H:%M")
        for ts in df["t"]
    ]
    return df


def _validate_spacing(candles: List[Candle], expected_sec: int) -> None:
    if len(candles) < 5:
        return
    deltas = [b.t - a.t for a, b in zip(candles, candles[1:])]
    med = median(deltas)
    if not (0.85 * expected_sec <= med <= 1.15 * expected_sec):
        raise MarketDataError("Interval waktu candle tidak sesuai dengan timeframe yang dipilih.")


def _fetch_ohlc_uncached(symbol: str, tf: str, now_ts: Optional[float] = None) -> Dict[str, Any]:
    if symbol not in SYMBOLS:
        raise MarketDataError("Market %s tidak didukung." % symbol)
    if tf not in TF_CONFIG:
        raise MarketDataError("Timeframe %s tidak didukung." % tf)

    cfg = TF_CONFIG[tf]
    sec = cfg["sec"]
    interval_str = cfg["interval"]

    url = "%s/%s/ohlc" % (BIQUOTE_BASE_URL, symbol)
    payload = _http_get_json(url, params={"interval": interval_str, "limit": MAX_CANDLES_PER_TF})
    rows = _extract_rows(payload)
    if rows is None:
        raise MarketDataError("Struktur data OHLC tidak dikenali.")

    raw_candles = parse_candle_rows(rows)
    candles = clean_and_limit_candles(raw_candles, max_candles=MAX_CANDLES_PER_TF)
    if not candles:
        raise MarketDataError("Data candle kosong untuk %s %s." % (symbol, tf))

    _validate_spacing(candles, sec)

    now = time.time() if now_ts is None else float(now_ts)
    warnings: List[str] = []

    # Koreksi otomatis bila timestamp server memakai offset zona waktu di masa depan
    skew = candles[-1].t - now
    if skew > 120:
        offset = int(round(skew / 1800.0)) * 1800
        if 0 < offset <= 14 * 3600:
            candles = [
                Candle(c.t - offset, c.o, c.h, c.l, c.c, c.v)
                for c in candles
            ]
            warnings.append("Penyesuaian offset zona waktu server diterapkan otomatis.")
        else:
            raise MarketDataError("Timestamp candle berada di masa depan dan tidak valid.")

    # Pisahkan candle yang sudah close vs candle berjalan (forming)
    forming: Optional[Candle] = None
    if now < candles[-1].t + sec:
        forming = candles[-1]
        closed = candles[:-1]
    else:
        closed = candles

    if len(closed) < MIN_CLOSED_CANDLES:
        raise MarketDataError(
            "Jumlah candle tertutup hanya %d (minimal %d candle) untuk %s %s."
            % (len(closed), MIN_CLOSED_CANDLES, symbol, tf)
        )

    ref_open = forming.t if forming is not None else closed[-1].t
    age = max(0.0, now - ref_open)
    stale = (forming is None) and (age > 2 * sec + max(120, 0.1 * sec))
    if stale:
        warnings.append("Feed harga tampak tertunda atau market sedang tutup.")

    df_closed = candles_to_dataframe(closed)

    return {
        "symbol": symbol,
        "tf": tf,
        "closed": closed,
        "forming": forming,
        "df_closed": df_closed,
        "total_fetched": len(candles),
        "warnings": warnings,
        "stale": stale,
        "fetched_at": now,
        "age": age,
    }


def fetch_candles(symbol: str, tf: str, force: bool = False) -> Dict[str, Any]:
    """Ambil maksimal 20 candle untuk symbol & timeframe tertentu dengan cache singkat."""
    key = (symbol, tf)
    if not force:
        with _cache_lock:
            cached = _cache.get(key)
        if cached and (time.time() - cached[0] < CACHE_TTL_SECONDS):
            return cached[1]

    result = _fetch_ohlc_uncached(symbol, tf)
    with _cache_lock:
        _cache[key] = (time.time(), result)
    return result


def fetch_live_price(symbol: str, ref_price: float, atr: float) -> Tuple[Optional[float], str, bool]:
    """
    Ambil harga terkini dari endpoint tick.
    Mengembalikan: (harga_atau_None, catatan, market_open).
    """
    try:
        payload = _http_get_json("%s/%s" % (BIQUOTE_BASE_URL, symbol))
    except MarketDataError as exc:
        return None, str(exc), True

    if isinstance(payload, list) and payload:
        payload = payload[0]
    if not isinstance(payload, dict):
        return None, "Format tick tidak dikenali.", True

    low_map = {str(k).lower(): v for k, v in payload.items()}
    mid = _to_float(low_map.get("mid"))
    if mid is None:
        bid = _to_float(low_map.get("bid"))
        ask = _to_float(low_map.get("ask"))
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0

    market_open = low_map.get("marketopen") is not False

    if mid is None or mid <= 0.0:
        return None, "Tick tidak memuat harga valid.", market_open

    max_dev = max(8.0 * atr, 0.03 * ref_price)
    if abs(mid - ref_price) > max_dev:
        return None, "Harga tick menyimpang dari rentang candle terakhir.", market_open

    note = "" if market_open else "Status market sedang tutup."
    return mid, note, market_open
