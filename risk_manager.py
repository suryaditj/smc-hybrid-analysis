#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Smart Stop Loss, Structural Take Profit, Konversi Pip XAUUSD/BTCUSD,
dan Deterministic Setup Scoring Engine (0-100).
"""

from typing import Any, Dict, List, Optional, Tuple

from config import PIP_CONFIG
from smc_engine import clip


def price_to_pips(symbol: str, price_diff: float) -> float:
    """Konversi selisih harga ke satuan pip sesuai konfigurasi market."""
    cfg = PIP_CONFIG[symbol]
    return abs(price_diff) / cfg["pip_size"]


def pips_to_price(symbol: str, pips: float) -> float:
    """Konversi jumlah pip ke selisih harga sesuai konfigurasi market."""
    cfg = PIP_CONFIG[symbol]
    return abs(pips) * cfg["pip_size"]


def calculate_smart_sl(direction: str, zone: Dict[str, Any], snap: Dict[str, Any]) -> Dict[str, Any]:
    """
    Hitung Smart Stop Loss berdasarkan invalidasi struktur SMC:
    - SL Ideal: tepat di luar batas struktur/zona invalidasi + buffer volatilitas mikro.
    - SL Aman: di luar ekstrem sweep likuiditas / swing terluar.
    - Range SL: rentang valid antara SL Ideal dan SL Aman bila terdapat 2 level invalidasi valid.
    """
    atr = snap["atr"]
    swings = snap["structure"]["swings"]
    liq_events = snap["structure"]["liq_events"]
    origin = zone["origin"]

    if direction == "BUY":
        sl_ideal = origin - 0.20 * atr

        # Cari ekstrem bawah tambahan (sweep low atau swing low terdekat di bawah origin)
        outer_lows = [origin]
        for lq in liq_events:
            if lq.dirn == "bull" and lq.level <= origin + 0.2 * atr:
                outer_lows.append(lq.level)
        for sw in swings:
            if sw.kind == "L" and sw.price < origin and (origin - sw.price) <= 1.5 * atr:
                outer_lows.append(sw.price)

        outer_anchor = min(outer_lows)
        sl_safe = min(sl_ideal - 0.25 * atr, outer_anchor - 0.25 * atr)
        sl_range = (sl_safe, sl_ideal) if abs(sl_ideal - sl_safe) >= 0.15 * atr else None
    else:
        sl_ideal = origin + 0.20 * atr

        outer_highs = [origin]
        for lq in liq_events:
            if lq.dirn == "bear" and lq.level >= origin - 0.2 * atr:
                outer_highs.append(lq.level)
        for sw in swings:
            if sw.kind == "H" and sw.price > origin and (sw.price - origin) <= 1.5 * atr:
                outer_highs.append(sw.price)

        outer_anchor = max(outer_highs)
        sl_safe = max(sl_ideal + 0.25 * atr, outer_anchor + 0.25 * atr)
        sl_range = (sl_ideal, sl_safe) if abs(sl_safe - sl_ideal) >= 0.15 * atr else None

    return {
        "sl_primary": sl_ideal,
        "sl_ideal": sl_ideal,
        "sl_safe": sl_safe,
        "sl_range": sl_range,
        "invalidation_level": origin,
    }


def _collect_structural_targets(direction: str, entry_ref: float, snap: Dict[str, Any], htf_ctx: Dict[str, Any]) -> List[Tuple[float, str]]:
    """Kumpulkan level target struktural (Liquidity, Swing, SND, OB, FVG, SNR, HTF) sebelum area reversal."""
    atr = snap["atr"]
    buffer = 0.08 * atr
    raw: List[Tuple[float, str]] = []

    if direction == "BUY":
        for p in snap["liquidity"]["bsl"]:
            if p > entry_ref:
                raw.append((p - buffer, "Buy Side Liquidity"))
        for p in snap["liquidity"]["eqh"]:
            if p > entry_ref:
                raw.append((p - buffer, "Equal High Liquidity"))
        for sw in snap["structure"]["swings"]:
            if sw.kind == "H" and not sw.broken and sw.price > entry_ref:
                raw.append((sw.price - buffer, "Swing High"))
        for z in snap["snd"]:
            if z["kind"] == "supply" and z["low"] > entry_ref:
                raw.append((z["low"] - buffer, "Supply Zone"))
        for z in snap["obs"]:
            if z["kind"] == "bear" and z["low"] > entry_ref:
                raw.append((z["low"] - buffer, "Bearish OB"))
        for z in snap["snr"]:
            if z["role"] == "resistance" and z["low"] > entry_ref:
                raw.append((z["low"] - buffer, "Resistance"))
        for lvl, kind, htf in htf_ctx.get("levels", []):
            if kind == "H" and lvl > entry_ref:
                raw.append((lvl - buffer, "Swing High %s" % htf))

        raw.sort(key=lambda x: x[0])
    else:
        for p in snap["liquidity"]["ssl"]:
            if p < entry_ref:
                raw.append((p + buffer, "Sell Side Liquidity"))
        for p in snap["liquidity"]["eql"]:
            if p < entry_ref:
                raw.append((p + buffer, "Equal Low Liquidity"))
        for sw in snap["structure"]["swings"]:
            if sw.kind == "L" and not sw.broken and sw.price < entry_ref:
                raw.append((sw.price + buffer, "Swing Low"))
        for z in snap["snd"]:
            if z["kind"] == "demand" and z["high"] < entry_ref:
                raw.append((z["high"] + buffer, "Demand Zone"))
        for z in snap["obs"]:
            if z["kind"] == "bull" and z["high"] < entry_ref:
                raw.append((z["high"] + buffer, "Bullish OB"))
        for z in snap["snr"]:
            if z["role"] == "support" and z["high"] < entry_ref:
                raw.append((z["high"] + buffer, "Support"))
        for lvl, kind, htf in htf_ctx.get("levels", []):
            if kind == "L" and lvl < entry_ref:
                raw.append((lvl + buffer, "Swing Low %s" % htf))

        raw.sort(key=lambda x: -x[0])

    # Gabungkan level target yang terlalu berdekatan (< 0.25 ATR)
    filtered: List[Tuple[float, str]] = []
    for price_lvl, label in raw:
        if not filtered or abs(price_lvl - filtered[-1][0]) >= 0.25 * atr:
            filtered.append((price_lvl, label))
    return filtered


def build_trade_plan(
    symbol: str,
    direction: str,
    entry_info: Dict[str, Any],
    snap: Dict[str, Any],
    htf_ctx: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Susun rencana Entry, SL (Ideal & Aman), serta TP1/TP2/TP3 berdasarkan
    struktur SMC dan panduan pip referensi (30-50, 50-100, 100-120 pip).
    """
    zone = entry_info["zone"]
    if zone is None:
        return None, "Zona entry struktural belum terbentuk di sekitar harga."

    price = snap["price"]
    atr = snap["atr"]
    pip_cfg = PIP_CONFIG[symbol]

    if direction == "BUY":
        entry_ref = price if zone["low"] <= price <= zone["high"] else (zone["low"] + zone["high"]) / 2.0
        sign = 1.0
    else:
        entry_ref = price if zone["low"] <= price <= zone["high"] else (zone["low"] + zone["high"]) / 2.0
        sign = -1.0

    sl_data = calculate_smart_sl(direction, zone, snap)
    sl_primary = sl_data["sl_primary"]
    risk = abs(entry_ref - sl_primary)

    # Pastikan SL minimal 0.45 ATR agar tidak tersapu noise mikro
    if risk < 0.45 * atr:
        sl_primary = entry_ref - sign * 0.45 * atr
        sl_data["sl_ideal"] = sl_primary
        sl_data["sl_primary"] = sl_primary
        if direction == "BUY":
            sl_data["sl_safe"] = min(sl_data["sl_safe"], sl_primary - 0.25 * atr)
            sl_data["sl_range"] = (sl_data["sl_safe"], sl_data["sl_ideal"])
        else:
            sl_data["sl_safe"] = max(sl_data["sl_safe"], sl_primary + 0.25 * atr)
            sl_data["sl_range"] = (sl_data["sl_ideal"], sl_data["sl_safe"])
        risk = abs(entry_ref - sl_primary)

    if risk > 4.2 * atr:
        return None, "Jarak Stop Loss struktural terlalu lebar (%.1f ATR) sehingga Risk-Reward tidak sehat." % (risk / atr)

    targets = _collect_structural_targets(direction, entry_ref, snap, htf_ctx)

    # Referensi jarak pip TP1 (30-50), TP2 (50-100), TP3 (100-120)
    tp1_ref_mid = pips_to_price(symbol, sum(pip_cfg["tp1_pips"]) / 2.0)
    tp2_ref_mid = pips_to_price(symbol, sum(pip_cfg["tp2_pips"]) / 2.0)
    tp3_ref_mid = pips_to_price(symbol, sum(pip_cfg["tp3_pips"]) / 2.0)

    # Pilih TP1: prioritaskan level struktural dengan RR >= 1.0
    valid_tp1 = [t for t in targets if abs(t[0] - entry_ref) >= 1.0 * risk]
    if valid_tp1:
        # Pilih target struktural yang paling dekat dengan area profit ideal (sekitar 1.2R - 2.0R atau referensi TP1)
        valid_tp1.sort(key=lambda t: abs(abs(t[0] - entry_ref) - max(1.3 * risk, tp1_ref_mid)))
        tp1, tp1_note = valid_tp1[0]
    elif not targets:
        # Tidak ada hambatan struktural di depan -> pakai proyeksi RR 1:1.5
        tp1 = entry_ref + sign * max(1.5 * risk, min(tp1_ref_mid, 2.2 * atr))
        tp1_note = "Proyeksi RR 1:1.5 (ruang terbuka)"
    else:
        return None, "Ruang profit menuju area pembalikan terdekat kurang dari 1R."

    # Pilih TP2: level struktural lebih jauh dengan RR >= 1.8
    valid_tp2 = [
        t for t in targets
        if sign * (t[0] - tp1) >= 0.4 * atr and abs(t[0] - entry_ref) >= 1.8 * risk
    ]
    if valid_tp2:
        valid_tp2.sort(key=lambda t: abs(abs(t[0] - entry_ref) - max(2.2 * risk, tp2_ref_mid)))
        tp2, tp2_note = valid_tp2[0]
    else:
        tp2 = entry_ref + sign * max(2.5 * risk, abs(tp1 - entry_ref) + 1.0 * risk, min(tp2_ref_mid, 3.8 * atr))
        tp2_note = "Proyeksi ekstensi momentum"

    # Pilih TP3: HANYA jika tersedia level struktural lanjutan setelah TP2 (jangan mengarang)
    tp3: Optional[float] = None
    tp3_note: Optional[str] = None
    valid_tp3 = [
        t for t in targets
        if sign * (t[0] - tp2) >= 0.4 * atr and abs(t[0] - entry_ref) >= 2.6 * risk
    ]
    if valid_tp3:
        valid_tp3.sort(key=lambda t: abs(abs(t[0] - entry_ref) - max(3.0 * risk, tp3_ref_mid)))
        tp3, tp3_note = valid_tp3[0]

    rr1 = abs(tp1 - entry_ref) / risk
    rr2 = abs(tp2 - entry_ref) / risk

    return {
        "entry_low": zone["low"],
        "entry_high": zone["high"],
        "entry_ref": entry_ref,
        "sl": sl_primary,
        "sl_ideal": sl_data["sl_ideal"],
        "sl_safe": sl_data["sl_safe"],
        "sl_range": sl_data["sl_range"],
        "invalidation_level": sl_data["invalidation_level"],
        "tp1": tp1,
        "tp1_note": tp1_note,
        "tp1_pips": price_to_pips(symbol, tp1 - entry_ref),
        "tp1_diff": abs(tp1 - entry_ref),
        "tp2": tp2,
        "tp2_note": tp2_note,
        "tp2_pips": price_to_pips(symbol, tp2 - entry_ref),
        "tp2_diff": abs(tp2 - entry_ref),
        "tp3": tp3,
        "tp3_note": tp3_note,
        "tp3_pips": price_to_pips(symbol, tp3 - entry_ref) if tp3 is not None else None,
        "tp3_diff": abs(tp3 - entry_ref) if tp3 is not None else None,
        "sl_pips": price_to_pips(symbol, sl_primary - entry_ref),
        "sl_diff": abs(sl_primary - entry_ref),
        "risk": risk,
        "rr1": rr1,
        "rr2": rr2,
        "room_targets_count": len(targets),
    }, None


def compute_deterministic_score(
    direction: str,
    entry_info: Dict[str, Any],
    trade: Optional[Dict[str, Any]],
    snap: Dict[str, Any],
    htf_ctx: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Hitung Skor Setup (0-100) secara 100% deterministik dari 9 komponen:
    1. Market Structure (18)
    2. Liquidity (14)
    3. FVG (12)
    4. Order Block (12)
    5. Price Action (12)
    6. Momentum (10)
    7. HTF Context (10)
    8. Risk Reward (6)
    9. Ruang menuju target (6)
    """
    want_dir = "bull" if direction == "BUY" else "bear"
    n = len(snap["cs"])
    price = snap["price"]
    atr = snap["atr"]

    # 1. Market Structure (max 18)
    events = snap["structure"]["events"]
    last_ev = events[-1] if events else None
    if last_ev is not None and last_ev.dirn == want_dir:
        age = n - 1 - last_ev.i
        recency = max(0.45, 1.0 - age / 20.0)
        base_struct = 1.0 if last_ev.kind in ("BOS", "MSS") else 0.90
        struct_pts = 18.0 * base_struct * recency
    elif any(e.dirn == want_dir for e in events):
        struct_pts = 9.0
    else:
        struct_pts = 2.5

    # 2. Liquidity (max 14)
    liq_events = snap["structure"]["liq_events"]
    has_sweep = any(lq.dirn == want_dir and (n - 1 - lq.i) <= 6 for lq in liq_events)
    has_grab = any(lq.kind == "Liquidity Grab" and lq.dirn == want_dir and (n - 1 - lq.i) <= 6 for lq in liq_events)
    has_pool = (
        len(snap["liquidity"]["bsl"]) > 0 if direction == "BUY" else len(snap["liquidity"]["ssl"]) > 0
    )
    liq_pts = (14.0 if has_grab else (11.5 if has_sweep else 0.0)) + (2.5 if (has_pool and not has_grab) else 0.0)
    liq_pts = min(14.0, liq_pts)

    # 3. FVG (max 12)
    rel_fvgs = [f for f in snap["fvg"] if f["kind"] == want_dir]
    if rel_fvgs:
        best_fvg = max(rel_fvgs, key=lambda f: f["strength"])
        dist_fvg = 0.0 if best_fvg["low"] <= price <= best_fvg["high"] else min(
            abs(price - best_fvg["low"]), abs(price - best_fvg["high"])
        ) / atr
        prox_f = max(0.35, 1.0 - min(dist_fvg, 2.5) / 2.5)
        fvg_pts = 12.0 * best_fvg["strength"] * prox_f
    else:
        fvg_pts = 0.0

    # 4. Order Block (max 12)
    rel_obs = [o for o in snap["obs"] if o["kind"] == want_dir]
    if rel_obs:
        best_ob = max(rel_obs, key=lambda o: o["strength"])
        dist_ob = 0.0 if best_ob["low"] <= price <= best_ob["high"] else min(
            abs(price - best_ob["low"]), abs(price - best_ob["high"])
        ) / atr
        prox_o = max(0.35, 1.0 - min(dist_ob, 2.5) / 2.5)
        ob_pts = 12.0 * best_ob["strength"] * prox_o
    else:
        ob_pts = 0.0

    # 5. Price Action (max 12)
    pa = snap["pa"][direction]
    pa_pts = 12.0 * pa["composite"]

    # 6. Momentum (max 10)
    mom_pts = 10.0 * max(pa["momentum"], 0.65 * pa["impulsive"])

    # 7. HTF Context (max 10)
    htf_trend = htf_ctx.get("overall_trend", 0)
    if htf_trend == 0:
        htf_pts = 6.0
    elif (htf_trend > 0) == (direction == "BUY"):
        htf_pts = 10.0
    else:
        # Countertrend tetap mendapat poin bila memiliki konfirmasi sweep/reversal
        htf_pts = 4.5 if entry_info.get("valid") else 1.5

    # 8. Risk Reward (max 6) & 9. Ruang menuju target (max 6)
    if trade is not None:
        rr_pts = 6.0 * clip((trade["rr1"] - 0.8) / 1.2)
        room_pts = 6.0 if trade["room_targets_count"] >= 2 else (4.5 if trade["room_targets_count"] == 1 else 3.0)
    else:
        rr_pts = 0.0
        room_pts = 1.5

    breakdown = {
        "market_structure": round(struct_pts, 1),
        "liquidity": round(liq_pts, 1),
        "fvg": round(fvg_pts, 1),
        "order_block": round(ob_pts, 1),
        "price_action": round(pa_pts, 1),
        "momentum": round(mom_pts, 1),
        "htf_context": round(htf_pts, 1),
        "risk_reward": round(rr_pts, 1),
        "room_to_target": round(room_pts, 1),
    }
    total = int(round(clip(sum(breakdown.values()) / 100.0) * 100))

    return {
        "total": total,
        "breakdown": breakdown,
    }
