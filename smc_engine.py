#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Python SMC Quant Engine (100% Deterministik, berbasis NumPy & Pandas).
Mendeteksi pada maksimal 20 candle (hanya candle close untuk konfirmasi struktur):
- Market Structure: BOS, CHOCH, MSS (Bullish/Bearish), HH, HL, LH, LL
- Liquidity: BSL, SSL, Equal High, Equal Low, Liquidity Sweep, Liquidity Grab, False Breakout
- Supply & Demand + Support & Resistance: Fresh, Tested, Weak, Strong
- Fair Value Gap (FVG): Bullish/Bearish, Fresh, Mitigated, FVG Rejection
- Order Block (OB): Bullish/Bearish, Fresh, Mitigated
- Price Action: Rejection Wick, Engulfing, Impulsive Candle, Displacement, Breakout, Retest, Momentum, Reversal
- Market Condition: Trending Bullish/Bearish, Sideways, High/Low Volatility
- Multi-Timeframe Context
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config import MAX_CANDLES_PER_TF, TF_CONFIG
from data_provider import Candle, fetch_candles


def clip(val: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return float(max(lo, min(hi, val)))


@dataclass
class SwingPoint:
    i: int
    kind: str       # "H" atau "L"
    price: float
    label: str = "" # "HH", "HL", "LH", "LL"
    broken: bool = False
    swept: bool = False


@dataclass(frozen=True)
class StructureEvent:
    kind: str       # "BOS", "CHOCH", "MSS"
    dirn: str       # "bull" atau "bear"
    i: int
    level: float
    ref_i: int
    displacement: bool


@dataclass(frozen=True)
class LiquidityEvent:
    kind: str       # "Liquidity Sweep", "Liquidity Grab", "False Breakout"
    dirn: str       # "bull" (sapuan low -> potensi naik) atau "bear" (sapuan high -> potensi turun)
    i: int
    level: float


def compute_atr(df: pd.DataFrame, period: int = 10) -> np.ndarray:
    """Hitung ATR dengan NumPy/Pandas yang responsif untuk jendela <= 20 candle."""
    high = df["high"].to_numpy(dtype=np.float64)
    low = df["low"].to_numpy(dtype=np.float64)
    close = df["close"].to_numpy(dtype=np.float64)

    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]

    tr = np.maximum(
        high - low,
        np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)),
    )
    atr = pd.Series(tr).rolling(window=period, min_periods=1).mean().to_numpy(dtype=np.float64)
    return atr


def detect_swings(cs: List[Candle], width: int = 1) -> List[SwingPoint]:
    """Deteksi Swing High & Swing Low serta klasifikasi HH, HL, LH, LL."""
    n = len(cs)
    raw_swings: List[SwingPoint] = []

    for i in range(width, n - width):
        hi = cs[i].h
        lo = cs[i].l
        is_high = all(hi > cs[j].h for j in range(i - width, i)) and all(
            hi >= cs[j].h for j in range(i + 1, i + width + 1)
        )
        is_low = all(lo < cs[j].l for j in range(i - width, i)) and all(
            lo <= cs[j].l for j in range(i + 1, i + width + 1)
        )
        if is_high:
            raw_swings.append(SwingPoint(i=i, kind="H", price=hi))
        if is_low:
            raw_swings.append(SwingPoint(i=i, kind="L", price=lo))

    last_high: Optional[float] = None
    last_low: Optional[float] = None
    for s in raw_swings:
        if s.kind == "H":
            if last_high is None:
                s.label = "HH"
            else:
                s.label = "HH" if s.price > last_high else "LH"
            last_high = s.price
        else:
            if last_low is None:
                s.label = "HL"
            else:
                s.label = "HL" if s.price > last_low else "LL"
            last_low = s.price

    return raw_swings


def analyze_market_structure(cs: List[Candle], atr: np.ndarray, width: int = 1) -> Dict[str, Any]:
    """
    Deteksi BOS, CHOCH, MSS (hanya dari candle close) serta Liquidity Sweep,
    Liquidity Grab, dan False Breakout.
    """
    swings = detect_swings(cs, width=width)
    confirm_map: Dict[int, List[SwingPoint]] = {}
    for s in swings:
        confirm_map.setdefault(s.i + width, []).append(s)

    events: List[StructureEvent] = []
    liq_events: List[LiquidityEvent] = []
    trend = 0  # 1 = bullish, -1 = bearish, 0 = netral
    last_h: Optional[SwingPoint] = None
    last_l: Optional[SwingPoint] = None

    for i, c in enumerate(cs):
        for s in confirm_map.get(i, []):
            if s.kind == "H":
                last_h = s
            else:
                last_l = s

        a = max(float(atr[i]), 1e-9)
        body = abs(c.c - c.o)
        is_disp = body >= 1.15 * a

        # Cek breakout / sweep terhadap Swing High terakhir
        if last_h is not None and not last_h.broken:
            ext = c.c - last_h.price
            if ext >= 0.08 * a:
                is_choch = trend < 0
                if (not is_choch) or ext >= 0.18 * a or body >= 0.55 * a:
                    ev_kind = ("MSS" if is_disp else "CHOCH") if is_choch else "BOS"
                    events.append(
                        StructureEvent(
                            kind=ev_kind,
                            dirn="bull",
                            i=i,
                            level=last_h.price,
                            ref_i=last_h.i,
                            displacement=is_disp,
                        )
                    )
                    last_h.broken = True
                    trend = 1
                elif i + 1 < len(cs) and cs[i + 1].c < last_h.price:
                    # Breakout tipis yang langsung gagal pada candle close berikutnya
                    liq_events.append(LiquidityEvent("False Breakout", "bear", i + 1, last_h.price))
                    last_h.swept = True
            elif c.h > last_h.price and c.c <= last_h.price and not last_h.swept:
                last_h.swept = True
                grab = (
                    i + 1 < len(cs)
                    and cs[i + 1].c < cs[i + 1].o
                    and abs(cs[i + 1].c - cs[i + 1].o) >= 0.9 * a
                )
                liq_events.append(
                    LiquidityEvent("Liquidity Grab" if grab else "Liquidity Sweep", "bear", i, last_h.price)
                )

        # Cek breakout / sweep terhadap Swing Low terakhir
        if last_l is not None and not last_l.broken:
            ext = last_l.price - c.c
            if ext >= 0.08 * a:
                is_choch = trend > 0
                if (not is_choch) or ext >= 0.18 * a or body >= 0.55 * a:
                    ev_kind = ("MSS" if is_disp else "CHOCH") if is_choch else "BOS"
                    events.append(
                        StructureEvent(
                            kind=ev_kind,
                            dirn="bear",
                            i=i,
                            level=last_l.price,
                            ref_i=last_l.i,
                            displacement=is_disp,
                        )
                    )
                    last_l.broken = True
                    trend = -1
                elif i + 1 < len(cs) and cs[i + 1].c > last_l.price:
                    liq_events.append(LiquidityEvent("False Breakout", "bull", i + 1, last_l.price))
                    last_l.swept = True
            elif c.l < last_l.price and c.c >= last_l.price and not last_l.swept:
                last_l.swept = True
                grab = (
                    i + 1 < len(cs)
                    and cs[i + 1].c > cs[i + 1].o
                    and abs(cs[i + 1].c - cs[i + 1].o) >= 0.9 * a
                )
                liq_events.append(
                    LiquidityEvent("Liquidity Grab" if grab else "Liquidity Sweep", "bull", i, last_l.price)
                )

    return {
        "swings": swings,
        "events": events,
        "liq_events": liq_events,
        "trend": trend,
    }


def detect_liquidity_pools(swings: List[SwingPoint], atr_now: float, price: float) -> Dict[str, Any]:
    """Deteksi Buy Side Liquidity (BSL), Sell Side Liquidity (SSL), Equal High, dan Equal Low."""
    tol = 0.25 * atr_now
    unmitigated_highs = [s for s in swings if s.kind == "H" and not s.broken and not s.swept and s.price > price]
    unmitigated_lows = [s for s in swings if s.kind == "L" and not s.broken and not s.swept and s.price < price]

    bsl = sorted([s.price for s in unmitigated_highs])
    ssl = sorted([s.price for s in unmitigated_lows], reverse=True)

    eqh: List[float] = []
    all_highs = sorted([s.price for s in swings if s.kind == "H" and not s.broken])
    for idx in range(len(all_highs) - 1):
        if abs(all_highs[idx + 1] - all_highs[idx]) <= tol:
            eqh.append((all_highs[idx] + all_highs[idx + 1]) / 2.0)

    eql: List[float] = []
    all_lows = sorted([s.price for s in swings if s.kind == "L" and not s.broken])
    for idx in range(len(all_lows) - 1):
        if abs(all_lows[idx + 1] - all_lows[idx]) <= tol:
            eql.append((all_lows[idx] + all_lows[idx + 1]) / 2.0)

    return {
        "bsl": bsl,
        "ssl": ssl,
        "eqh": eqh,
        "eql": eql,
    }


def _zone_visits(cs: List[Candle], start: int, low: float, high: float) -> List[Tuple[int, int]]:
    groups: List[Tuple[int, int]] = []
    cur: Optional[List[int]] = None
    for i in range(start, len(cs)):
        if cs[i].l <= high and cs[i].h >= low:
            if cur is None:
                cur = [i, i]
            else:
                cur[1] = i
        elif cur is not None:
            groups.append((cur[0], cur[1]))
            cur = None
    if cur is not None:
        groups.append((cur[0], cur[1]))
    return groups


def build_snr_zones(cs: List[Candle], swings: List[SwingPoint], atr_now: float, price: float) -> List[Dict[str, Any]]:
    """Bangun zona Support & Resistance dari klaster swing dengan klasifikasi Lemah/Sedang/Kuat."""
    if not swings:
        return []
    tol = 0.35 * atr_now
    pts = sorted((s.price, s.i) for s in swings)
    clusters: List[List[Tuple[float, int]]] = []
    cur = [pts[0]]
    for pt in pts[1:]:
        if pt[0] - cur[0][0] <= tol:
            cur.append(pt)
        else:
            clusters.append(cur)
            cur = [pt]
    clusters.append(cur)

    zones: List[Dict[str, Any]] = []
    n = len(cs)
    for cl in clusters:
        lo = min(p for p, _ in cl)
        hi = max(p for p, _ in cl)
        half = 0.12 * atr_now
        if hi - lo < 2 * half:
            mid0 = (lo + hi) / 2.0
            lo, hi = mid0 - half, mid0 + half
        mid = (lo + hi) / 2.0
        first_i = min(i for _, i in cl)
        touches = rejections = breaks = 0
        for a, b in _zone_visits(cs, first_i, lo, hi):
            touches += 1
            before = cs[a - 1].c if a > 0 else None
            after = cs[b + 1].c if b + 1 < n else None
            if before is None or after is None:
                continue
            if (before > mid) == (after > mid):
                rejections += 1
            else:
                breaks += 1
        touches = max(touches, len(cl))
        strength = clip(0.55 * min(rejections, 3) / 3.0 + 0.30 * min(touches, 4) / 4.0 + 0.15)
        if breaks >= 2:
            strength *= 0.6
        label = "Kuat" if strength >= 0.65 else ("Sedang" if strength >= 0.42 else "Lemah")
        zones.append(
            {
                "low": lo,
                "high": hi,
                "mid": mid,
                "touches": touches,
                "rejections": rejections,
                "breaks": breaks,
                "strength": clip(strength),
                "label": label,
                "role": "support" if mid <= price else "resistance",
            }
        )
    return zones


def build_snd_zones(cs: List[Candle], atr: np.ndarray, events: List[StructureEvent]) -> List[Dict[str, Any]]:
    """Bangun Supply & Demand Zone (Fresh, Tested, Strong, Weak)."""
    zones: List[Dict[str, Any]] = []
    n = len(cs)

    for i in range(1, n):
        c = cs[i]
        a = max(float(atr[i - 1]), 1e-9)
        rng = c.h - c.l
        body = abs(c.c - c.o)
        if rng <= 0 or body < 1.15 * a or (body / rng) < 0.52:
            continue

        dirn = 1 if c.c > c.o else -1
        base: List[int] = []
        j = i - 1
        while j >= max(0, i - 2) and (cs[j].h - cs[j].l) <= 0.95 * a:
            base.insert(0, j)
            j -= 1

        if not base:
            for k in range(i - 1, max(-1, i - 3), -1):
                if (cs[k].c - cs[k].o) * dirn < 0:
                    base = [k]
                    break
        if not base:
            continue

        b_low = min(cs[k].l for k in base)
        b_high = max(cs[k].h for k in base)
        if (dirn > 0 and c.c <= b_high) or (dirn < 0 and c.c >= b_low):
            continue

        low, high = b_low, b_high
        if high - low > 1.2 * a:
            if dirn > 0:
                high = max(max(cs[k].o, cs[k].c) for k in base)
            else:
                low = min(min(cs[k].o, cs[k].c) for k in base)
        if high <= low:
            continue

        broken = False
        for m in range(i + 1, n):
            if dirn > 0 and cs[m].c < low - 0.08 * a:
                broken = True
                break
            if dirn < 0 and cs[m].c > high + 0.08 * a:
                broken = True
                break
        if broken:
            continue

        visits = len(_zone_visits(cs, i + 1, low, high))
        if visits > 2:
            continue

        fresh_status = "fresh" if visits == 0 else ("tested 1x" if visits == 1 else "tested 2x")
        disp_score = clip((body / a) / 2.2)
        tied_bos = any(e.dirn == ("bull" if dirn > 0 else "bear") and i <= e.i <= i + 2 for e in events)
        strength = clip(0.5 * disp_score + 0.35 * (1.0 if visits == 0 else 0.6) + (0.15 if tied_bos else 0.0))
        quality = "Strong Zone" if strength >= 0.62 else "Weak Zone"

        zones.append(
            {
                "kind": "demand" if dirn > 0 else "supply",
                "low": low,
                "high": high,
                "origin": low if dirn > 0 else high,
                "visits": visits,
                "status": fresh_status,
                "quality": quality,
                "strength": strength,
                "i": i,
            }
        )

    zones.sort(key=lambda z: -z["strength"])
    deduped: List[Dict[str, Any]] = []
    for z in zones:
        if any(
            k["kind"] == z["kind"] and z["low"] <= k["high"] and k["low"] <= z["high"]
            for k in deduped
        ):
            continue
        deduped.append(z)
    return deduped


def build_fvg_zones(cs: List[Candle], atr: np.ndarray) -> List[Dict[str, Any]]:
    """Bangun Fair Value Gap (Fresh, Mitigated, FVG Rejection)."""
    zones: List[Dict[str, Any]] = []
    n = len(cs)

    for k in range(2, n):
        a = max(float(atr[k]), 1e-9)
        c0, c1, c2 = cs[k - 2], cs[k - 1], cs[k]
        if c2.l > c0.h:
            bull, lo, hi, origin = True, c0.h, c2.l, min(c0.l, c1.l)
        elif c2.h < c0.l:
            bull, lo, hi, origin = False, c2.h, c0.l, max(c0.h, c1.h)
        else:
            continue

        orig_size = hi - lo
        if orig_size < 0.10 * a:
            continue

        status = "fresh"
        rejected = False
        filled = False
        for m in range(k + 1, n):
            cm = cs[m]
            if bull:
                if cm.l <= lo:
                    filled = True
                    break
                if cm.l < hi:
                    hi = cm.l
                    status = "mitigated"
                    if cm.c > hi:
                        rejected = True
            else:
                if cm.h >= hi:
                    filled = True
                    break
                if cm.h > lo:
                    lo = cm.h
                    status = "mitigated"
                    if cm.c < lo:
                        rejected = True

        if filled or (hi - lo) < 0.04 * a:
            continue

        disp = clip(abs(c1.c - c1.o) / (1.4 * a))
        strength = clip(
            0.45 * disp
            + 0.30 * (1.0 if status == "fresh" else 0.7)
            + 0.15 * (1.0 if rejected else 0.0)
            + 0.10 * clip(orig_size / (0.8 * a))
        )
        zones.append(
            {
                "kind": "bull" if bull else "bear",
                "low": lo,
                "high": hi,
                "origin": origin,
                "status": "FVG rejection" if rejected else status,
                "rejected": rejected,
                "strength": strength,
                "i": k,
            }
        )

    zones.sort(key=lambda z: -z["i"])
    return zones


def build_order_blocks(cs: List[Candle], atr: np.ndarray, events: List[StructureEvent]) -> List[Dict[str, Any]]:
    """Bangun Bullish OB & Bearish OB (Fresh OB vs Mitigated OB)."""
    obs: List[Dict[str, Any]] = []
    n = len(cs)

    for i in range(1, n - 1):
        a = max(float(atr[i]), 1e-9)
        ob_c = cs[i]
        imp_c = cs[i + 1]
        imp_body = abs(imp_c.c - imp_c.o)
        if imp_body < 1.1 * a:
            continue

        # Bullish OB: candle bearish terakhir sebelum dorongan naik kuat
        if ob_c.c < ob_c.o and imp_c.c > imp_c.o and imp_c.c > ob_c.h:
            kind = "bull"
            lo, hi, origin = ob_c.l, ob_c.h, ob_c.l
        # Bearish OB: candle bullish terakhir sebelum dorongan turun kuat
        elif ob_c.c > ob_c.o and imp_c.c < imp_c.o and imp_c.c < ob_c.l:
            kind = "bear"
            lo, hi, origin = ob_c.l, ob_c.h, ob_c.h
        else:
            continue

        broken = False
        visits = 0
        for m in range(i + 2, n):
            cm = cs[m]
            if kind == "bull":
                if cm.c < lo - 0.05 * a:
                    broken = True
                    break
                if cm.l <= hi and cm.h >= lo:
                    visits += 1
            else:
                if cm.c > hi + 0.05 * a:
                    broken = True
                    break
                if cm.h >= lo and cm.l <= hi:
                    visits += 1

        if broken or visits > 2:
            continue

        tied_struct = any(e.dirn == kind and (i + 1) <= e.i <= (i + 3) for e in events)
        status = "Fresh OB" if visits == 0 else "Mitigated OB"
        strength = clip(
            0.45 * clip(imp_body / (2.0 * a))
            + 0.35 * (1.0 if visits == 0 else 0.65)
            + (0.20 if tied_struct else 0.0)
        )
        obs.append(
            {
                "kind": kind,
                "low": lo,
                "high": hi,
                "origin": origin,
                "status": status,
                "visits": visits,
                "strength": strength,
                "i": i,
            }
        )

    obs.sort(key=lambda x: -x["i"])
    return obs


def analyze_price_action(cs: List[Candle], atr: np.ndarray, events: List[StructureEvent], liq_events: List[LiquidityEvent]) -> Dict[str, Any]:
    """Deteksi elemen Price Action untuk arah BUY dan SELL."""
    a = max(float(atr[-1]), 1e-9)
    c1 = cs[-1]
    c2 = cs[-2]
    c3 = cs[-3] if len(cs) >= 3 else c2

    def lower_wick_ratio(c: Candle) -> float:
        r = c.h - c.l
        return (min(c.o, c.c) - c.l) / r if r > 0 else 0.0

    def upper_wick_ratio(c: Candle) -> float:
        r = c.h - c.l
        return (c.h - max(c.o, c.c)) / r if r > 0 else 0.0

    # Momentum 3 candle terakhir
    net_move = sum(c.c - c.o for c in (c1, c2, c3)) / (2.5 * a)
    mom_bull = clip(net_move)
    mom_bear = clip(-net_move)

    # Rejection Wick
    rej_bull = (
        1.0
        if (lower_wick_ratio(c1) >= 0.45 and c1.c >= c1.l + 0.5 * (c1.h - c1.l))
        else (0.6 if lower_wick_ratio(c1) >= 0.35 and c1.c > c1.o else 0.0)
    )
    rej_bear = (
        1.0
        if (upper_wick_ratio(c1) >= 0.45 and c1.c <= c1.l + 0.5 * (c1.h - c1.l))
        else (0.6 if upper_wick_ratio(c1) >= 0.35 and c1.c < c1.o else 0.0)
    )

    # Engulfing
    eng_bull = 1.0 if (c2.c < c2.o and c1.c > c1.o and c1.c >= c2.o and c1.o <= c2.c) else 0.0
    eng_bear = 1.0 if (c2.c > c2.o and c1.c < c1.o and c1.c <= c2.o and c1.o >= c2.c) else 0.0

    # Impulsive Candle & Displacement
    imp_bull = 1.0 if any((c.c - c.o) >= 1.15 * a for c in (c1, c2)) else 0.0
    imp_bear = 1.0 if any((c.o - c.c) >= 1.15 * a for c in (c1, c2)) else 0.0

    disp_bull = 1.0 if (imp_bull > 0 and c1.c > c2.h) else (0.5 if imp_bull > 0 else 0.0)
    disp_bear = 1.0 if (imp_bear > 0 and c1.c < c2.l) else (0.5 if imp_bear > 0 else 0.0)

    n = len(cs)
    brk_bull = 1.0 if any(e.dirn == "bull" and (n - 1 - e.i) <= 3 for e in events) else 0.0
    brk_bear = 1.0 if any(e.dirn == "bear" and (n - 1 - e.i) <= 3 for e in events) else 0.0

    rev_bull = 1.0 if (
        any(e.kind in ("CHOCH", "MSS") and e.dirn == "bull" and (n - 1 - e.i) <= 4 for e in events)
        or any(lq.dirn == "bull" and (n - 1 - lq.i) <= 3 for lq in liq_events)
    ) else 0.0
    rev_bear = 1.0 if (
        any(e.kind in ("CHOCH", "MSS") and e.dirn == "bear" and (n - 1 - e.i) <= 4 for e in events)
        or any(lq.dirn == "bear" and (n - 1 - lq.i) <= 3 for lq in liq_events)
    ) else 0.0

    return {
        "BUY": {
            "momentum": mom_bull,
            "rejection": rej_bull,
            "engulfing": eng_bull,
            "impulsive": imp_bull,
            "displacement": disp_bull,
            "breakout": brk_bull,
            "reversal": rev_bull,
            "composite": clip(
                0.25 * mom_bull
                + 0.20 * rej_bull
                + 0.15 * eng_bull
                + 0.15 * imp_bull
                + 0.10 * disp_bull
                + 0.15 * max(brk_bull, rev_bull)
            ),
        },
        "SELL": {
            "momentum": mom_bear,
            "rejection": rej_bear,
            "engulfing": eng_bear,
            "impulsive": imp_bear,
            "displacement": disp_bear,
            "breakout": brk_bear,
            "reversal": rev_bear,
            "composite": clip(
                0.25 * mom_bear
                + 0.20 * rej_bear
                + 0.15 * eng_bear
                + 0.15 * imp_bear
                + 0.10 * disp_bear
                + 0.15 * max(brk_bear, rev_bear)
            ),
        },
    }


def classify_market_condition(cs: List[Candle], atr: np.ndarray, structure: Dict[str, Any]) -> Dict[str, str]:
    """Klasifikasi kondisi market: Trending Bullish/Bearish, Sideways, High/Low Volatility."""
    closes = np.array([c.c for c in cs], dtype=np.float64)
    diffs = np.abs(np.diff(closes))
    path = float(np.sum(diffs))
    net = abs(float(closes[-1] - closes[0]))
    efficiency = (net / path) if path > 0 else 0.0

    trend = structure["trend"]
    events = structure["events"]
    if trend == 0 and events:
        trend = 1 if events[-1].dirn == "bull" else -1
    if trend == 0:
        trend = 1 if closes[-1] > closes[0] else (-1 if closes[-1] < closes[0] else 0)

    if efficiency < 0.20 and len(events) == 0:
        direction_state = "Sideways"
    elif efficiency < 0.18:
        direction_state = "Sideways"
    elif trend > 0:
        direction_state = "Trending Bullish"
    elif trend < 0:
        direction_state = "Trending Bearish"
    else:
        direction_state = "Sideways"

    atr_med = float(np.median(atr)) if len(atr) > 0 else float(atr[-1])
    atr_now = float(atr[-1])
    ratio = (atr_now / atr_med) if atr_med > 0 else 1.0

    if ratio >= 1.22:
        vol_state = "High Volatility"
    elif ratio <= 0.78:
        vol_state = "Low Volatility"
    else:
        vol_state = "Normal Volatility"

    label = (
        "%s (%s)" % (direction_state, vol_state)
        if vol_state != "Normal Volatility"
        else direction_state
    )

    return {
        "direction": direction_state,
        "volatility": vol_state,
        "label": label,
        "efficiency": efficiency,
    }


def run_smc_snapshot(series: Dict[str, Any], live_price: float) -> Dict[str, Any]:
    """Jalankan mesin kuantitatif SMC lengkap pada 1 series timeframe (maks 20 candle)."""
    cs: List[Candle] = series["closed"][-MAX_CANDLES_PER_TF:]
    df: pd.DataFrame = series["df_closed"].iloc[-MAX_CANDLES_PER_TF:].reset_index(drop=True)
    tf: str = series["tf"]
    cfg = TF_CONFIG[tf]

    atr = compute_atr(df, period=10)
    atr_now = max(float(atr[-1]), 1e-9)

    structure = analyze_market_structure(cs, atr, width=cfg["swing_micro"])
    liquidity = detect_liquidity_pools(structure["swings"], atr_now, live_price)
    snr = build_snr_zones(cs, structure["swings"], atr_now, live_price)
    snd = build_snd_zones(cs, atr, structure["events"])
    fvg = build_fvg_zones(cs, atr)
    obs = build_order_blocks(cs, atr, structure["events"])
    pa = analyze_price_action(cs, atr, structure["events"], structure["liq_events"])
    condition = classify_market_condition(cs, atr, structure)

    return {
        "symbol": series["symbol"],
        "tf": tf,
        "cs": cs,
        "forming": series.get("forming"),
        "price": live_price,
        "atr": atr_now,
        "structure": structure,
        "liquidity": liquidity,
        "snr": snr,
        "snd": snd,
        "fvg": fvg,
        "obs": obs,
        "pa": pa,
        "condition": condition,
        "stale": series.get("stale", False),
        "warnings": series.get("warnings", []),
    }


def analyze_htf_context(symbol: str, primary_tf: str, primary_snapshot: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
    """
    Analisis konteks multi-timeframe sesuai pemetaan:
    M1 -> M5/M15, M5 -> M15/M30, M15 -> M30/H1, M30 -> H1/H4, H1 -> H4, H4 -> H4.
    """
    htf_list = TF_CONFIG[primary_tf]["htf_list"]
    contexts: List[Dict[str, Any]] = []
    unmitigated_levels: List[Tuple[float, str, str]] = []

    for htf in htf_list:
        if htf == primary_tf:
            snap = primary_snapshot
        else:
            try:
                htf_series = fetch_candles(symbol, htf, force=force)
                snap = run_smc_snapshot(htf_series, primary_snapshot["price"])
            except Exception:
                continue

        st = snap["structure"]
        last_ev = st["events"][-1] if st["events"] else None
        trend = st["trend"]
        if trend == 0 and last_ev is not None:
            trend = 1 if last_ev.dirn == "bull" else -1

        trend_str = "Bullish" if trend > 0 else ("Bearish" if trend < 0 else "Sideways")
        ev_str = ""
        if last_ev is not None:
            ev_str = ", %s %s" % (last_ev.kind, "bull" if last_ev.dirn == "bull" else "bear")

        contexts.append(
            {
                "tf": htf,
                "trend": trend,
                "summary": "%s%s" % (trend_str, ev_str),
                "event": last_ev,
            }
        )

        for sw in st["swings"]:
            if not sw.broken and not sw.swept:
                unmitigated_levels.append((sw.price, sw.kind, htf))

    # Gabungkan arah dominan HTF (HTF kedua berbobot lebih besar karena lebih tinggi)
    net_trend = 0
    for idx, c in enumerate(contexts):
        weight = idx + 1
        net_trend += weight * c["trend"]

    overall_dir = 1 if net_trend > 0 else (-1 if net_trend < 0 else 0)
    primary_htf = contexts[-1] if contexts else None

    return {
        "contexts": contexts,
        "primary_htf": primary_htf,
        "overall_trend": overall_dir,
        "levels": unmitigated_levels,
    }
