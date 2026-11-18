#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flexible Scalping Entry Engine.
Mengevaluasi 10 pola scalping SMC:
1. BOS + Retest
2. CHOCH + Confirmation
3. Liquidity Sweep + Rejection
4. FVG Entry
5. Order Block Retest
6. Supply/Demand Rejection
7. Momentum Breakout
8. Breakout + Retest
9. Engulfing + Displacement
10. Reversal dari Liquidity
"""

from typing import Any, Dict, List, Optional


def zone_distance_atr(direction: str, zone: Dict[str, Any], price: float, atr: float) -> Optional[float]:
    """
    Hitung jarak harga ke zona entry dalam kelipatan ATR.
    Mengembalikan 0.0 jika harga berada di dalam zona, > 0 jika menunggu retracement,
    dan None jika zona sudah ditembus atau tidak relevan.
    """
    if direction == "BUY":
        if zone["low"] > price + 0.15 * atr or price < zone["low"] - 0.35 * atr:
            return None
        return max(0.0, price - zone["high"]) / atr
    else:
        if zone["high"] < price - 0.15 * atr or price > zone["high"] + 0.35 * atr:
            return None
        return max(0.0, zone["low"] - price) / atr


def detect_scalping_setups(direction: str, snap: Dict[str, Any], htf_ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Identifikasi setup scalping valid untuk arah BUY atau SELL
    serta bangun zona entry terbaik berdasarkan konfluensi zona SMC.
    """
    price = snap["price"]
    atr = snap["atr"]
    cs = snap["cs"]
    n = len(cs)
    want_dir = "bull" if direction == "BUY" else "bear"

    events = snap["structure"]["events"]
    liq_events = snap["structure"]["liq_events"]
    pa = snap["pa"][direction]

    matched_setups: List[str] = []
    candidate_zones: List[Dict[str, Any]] = []

    for ob in snap["obs"]:
        if ob["kind"] == want_dir:
            dist = zone_distance_atr(direction, ob, price, atr)
            if dist is not None and dist <= 3.0:
                candidate_zones.append(
                    {
                        "src": "Order Block",
                        "low": ob["low"],
                        "high": ob["high"],
                        "origin": ob["origin"],
                        "strength": ob["strength"],
                        "status": ob["status"],
                        "dist": dist,
                    }
                )

    for fvg in snap["fvg"]:
        if fvg["kind"] == want_dir:
            dist = zone_distance_atr(direction, fvg, price, atr)
            if dist is not None and dist <= 3.0:
                candidate_zones.append(
                    {
                        "src": "FVG",
                        "low": fvg["low"],
                        "high": fvg["high"],
                        "origin": fvg["origin"],
                        "strength": fvg["strength"],
                        "status": fvg["status"],
                        "dist": dist,
                    }
                )

    for snd in snap["snd"]:
        if snd["kind"] == ("demand" if direction == "BUY" else "supply"):
            dist = zone_distance_atr(direction, snd, price, atr)
            if dist is not None and dist <= 3.0:
                candidate_zones.append(
                    {
                        "src": "Supply/Demand",
                        "low": snd["low"],
                        "high": snd["high"],
                        "origin": snd["origin"],
                        "strength": snd["strength"],
                        "status": "%s (%s)" % (snd["status"], snd["quality"]),
                        "dist": dist,
                    }
                )

    for snr in snap["snr"]:
        expected_role = "support" if direction == "BUY" else "resistance"
        dist = zone_distance_atr(direction, snr, price, atr)
        if dist is not None and dist <= 2.5 and (snr["role"] == expected_role or dist == 0.0):
            candidate_zones.append(
                {
                    "src": "SNR",
                    "low": snr["low"],
                    "high": snr["high"],
                    "origin": snr["low"] if direction == "BUY" else snr["high"],
                    "strength": snr["strength"],
                    "status": "%s (%dx reaksi)" % (snr["label"], snr["rejections"]),
                    "dist": dist,
                }
            )

    recent_events = [e for e in events if e.dirn == want_dir and (n - 1 - e.i) <= 8]
    recent_liq = [lq for lq in liq_events if lq.dirn == want_dir and (n - 1 - lq.i) <= 5]

    has_bos = any(e.kind == "BOS" for e in recent_events)
    has_choch = any(e.kind in ("CHOCH", "MSS") for e in recent_events)
    has_sweep = len(recent_liq) > 0
    has_retest = any(z["dist"] <= 0.35 for z in candidate_zones)

    if has_bos and (has_retest or len(candidate_zones) > 0):
        matched_setups.append("BOS + Retest")
    if has_choch and (pa["composite"] >= 0.35 or has_retest):
        matched_setups.append("CHOCH + Confirmation")
    if has_sweep and (pa["rejection"] >= 0.6 or pa["engulfing"] > 0 or pa["reversal"] > 0):
        matched_setups.append("Liquidity Sweep + Rejection")
    if any(z["src"] == "FVG" and z["dist"] <= 1.2 for z in candidate_zones):
        matched_setups.append("FVG Entry")
    if any(z["src"] == "Order Block" and z["dist"] <= 1.2 for z in candidate_zones):
        matched_setups.append("Order Block Retest")
    if any(z["src"] == "Supply/Demand" and z["dist"] <= 1.2 for z in candidate_zones):
        matched_setups.append("Supply/Demand Rejection")
    if pa["breakout"] > 0 and pa["momentum"] >= 0.45 and pa["impulsive"] > 0:
        matched_setups.append("Momentum Breakout")
    if has_bos and any(z["src"] == "SNR" and z["dist"] <= 0.6 for z in candidate_zones):
        matched_setups.append("Breakout + Retest")
    if pa["engulfing"] > 0 and pa["displacement"] >= 0.5:
        matched_setups.append("Engulfing + Displacement")
    if has_sweep and has_choch:
        matched_setups.append("Reversal dari Liquidity")

    if not candidate_zones and matched_setups:
        c_last = cs[-1]
        c_prev = cs[-2]
        if direction == "BUY":
            z_lo = min(c_last.l, c_prev.l)
            z_hi = min(price, max(c_last.o, c_last.c))
            if z_hi <= z_lo:
                z_hi = price
            candidate_zones.append(
                {
                    "src": "Price Action",
                    "low": z_lo,
                    "high": z_hi,
                    "origin": z_lo,
                    "strength": 0.60,
                    "status": "konfirmasi candle close",
                    "dist": 0.0,
                }
            )
        else:
            z_hi = max(c_last.h, c_prev.h)
            z_lo = max(price, min(c_last.o, c_last.c))
            if z_lo >= z_hi:
                z_lo = price
            candidate_zones.append(
                {
                    "src": "Price Action",
                    "low": z_lo,
                    "high": z_hi,
                    "origin": z_hi,
                    "strength": 0.60,
                    "status": "konfirmasi candle close",
                    "dist": 0.0,
                }
            )

    if not candidate_zones:
        return {
            "valid": False,
            "setups": matched_setups,
            "zone": None,
            "is_countertrend": False,
        }

    # Prioritaskan zona yang kuat, dekat harga, dan memiliki invalidasi SL yang proporsional
    candidate_zones.sort(
        key=lambda z: -(
            z["strength"]
            * (1.0 - 0.25 * min(z["dist"], 3.0) / 3.0)
            * (1.0 / (1.0 + 0.15 * abs(z["origin"] - price) / atr))
        )
    )
    primary = candidate_zones[0]
    lo, hi = primary["low"], primary["high"]
    origin = primary["origin"]
    alt_origins: List[float] = [origin]
    sources = [primary["src"]]

    for other in candidate_zones[1:]:
        if other["low"] <= hi and lo <= other["high"]:
            overlap_lo = max(lo, other["low"])
            overlap_hi = min(hi, other["high"])
            if (overlap_hi - overlap_lo) >= 0.10 * atr:
                lo, hi = overlap_lo, overlap_hi
            if other["src"] not in sources:
                sources.append(other["src"])
            alt_origins.append(other["origin"])
            # Hanya perlebar origin utama bila selisihnya masih dalam batas scalping (<= 1.0 ATR)
            if abs(other["origin"] - origin) <= 1.0 * atr:
                origin = min(origin, other["origin"]) if direction == "BUY" else max(origin, other["origin"])

    htf_trend = htf_ctx.get("overall_trend", 0)
    is_countertrend = (htf_trend != 0) and ((htf_trend > 0) != (direction == "BUY"))

    countertrend_ok = True
    if is_countertrend:
        has_rev_conf = has_sweep or has_choch or pa["rejection"] >= 0.6 or pa["engulfing"] > 0
        has_mom = pa["momentum"] >= 0.30 or pa["impulsive"] > 0
        countertrend_ok = has_rev_conf and has_mom

    zone_bundle = {
        "low": lo,
        "high": hi,
        "origin": origin,
        "alt_origins": alt_origins,
        "sources": sources,
        "dist": primary["dist"],
        "status": primary["status"],
        "strength": primary["strength"],
    }

    return {
        "valid": countertrend_ok and (len(matched_setups) > 0 or primary["dist"] <= 1.2),
        "setups": matched_setups,
        "zone": zone_bundle,
        "is_countertrend": is_countertrend,
        "recent_events": recent_events,
        "recent_liq": recent_liq,
    }
