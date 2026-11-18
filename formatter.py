#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Formatter laporan Telegram HTML.
Mematuhi format laporan secara presisi untuk BUY, SELL, dan WAIT,
tanpa pernah menampilkan nama penyedia data.
"""

import html
from typing import Any, Dict, List

from config import PIP_CONFIG


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def _fp(val: float, decimals: int = 2) -> str:
    return f"{val:.{decimals}f}"


def _setup_badge(score: int) -> str:
    if score >= 75:
        return "Setup kuat"
    if score >= 58:
        return "Setup potensial"
    return "Setup spekulatif"


def _build_structure_section(snap: Dict[str, Any], htf_ctx: Dict[str, Any]) -> List[str]:
    cs = snap["cs"]
    n = len(cs)
    price = snap["price"]
    events = snap["structure"]["events"]
    lines: List[str] = []

    # 1. BOS terbaru
    last_bos = next((e for e in reversed(events) if e.kind == "BOS"), None)
    if last_bos is not None:
        age = n - 1 - last_bos.i
        d_txt = "Bullish" if last_bos.dirn == "bull" else "Bearish"
        lines.append(f"• BOS: {d_txt} @ {_fp(last_bos.level)} ({age} candle lalu)")
    else:
        lines.append("• BOS: belum terkonfirmasi")

    # 2. CHOCH / MSS terbaru
    last_choch = next((e for e in reversed(events) if e.kind in ("CHOCH", "MSS")), None)
    if last_choch is not None:
        age = n - 1 - last_choch.i
        d_txt = "Bullish" if last_choch.dirn == "bull" else "Bearish"
        lines.append(f"• CHOCH: {d_txt} @ {_fp(last_choch.level)} ({age} candle lalu)")
    else:
        lines.append("• CHOCH: belum terkonfirmasi")

    # 3. SNR terdekat
    sup = sorted([z for z in snap["snr"] if z["mid"] <= price], key=lambda z: -z["mid"])
    res = sorted([z for z in snap["snr"] if z["mid"] > price], key=lambda z: z["mid"])
    snr_parts: List[str] = []
    if res:
        z = res[0]
        snr_parts.append(f"Resistance {_fp(z['low'])}-{_fp(z['high'])} ({z['label']})")
    if sup:
        z = sup[0]
        snr_parts.append(f"Support {_fp(z['low'])}-{_fp(z['high'])} ({z['label']})")
    lines.append("• SNR: " + (" | ".join(snr_parts) if snr_parts else "tidak ada level dekat"))

    # 4. SND aktif
    snd_parts: List[str] = []
    for kind, label in (("supply", "Supply"), ("demand", "Demand")):
        zs = [z for z in snap["snd"] if z["kind"] == kind]
        zs.sort(key=lambda z: min(abs(price - z["low"]), abs(price - z["high"])))
        if zs:
            z = zs[0]
            snd_parts.append(f"{label} {_fp(z['low'])}-{_fp(z['high'])} ({z['status']})")
    lines.append("• SND: " + (" | ".join(snd_parts) if snd_parts else "tidak ada zona aktif"))

    # 5. FVG aktif
    fvg_parts: List[str] = []
    for kind, label in (("bear", "Bearish FVG"), ("bull", "Bullish FVG")):
        zs = [z for z in snap["fvg"] if z["kind"] == kind]
        zs.sort(key=lambda z: min(abs(price - z["low"]), abs(price - z["high"])))
        if zs:
            z = zs[0]
            fvg_parts.append(f"{label} {_fp(z['low'])}-{_fp(z['high'])} ({z['status']})")
    lines.append("• FVG: " + (" | ".join(fvg_parts) if fvg_parts else "tidak ada FVG terbuka"))

    # 6. Konteks HTF
    contexts = htf_ctx.get("contexts", [])
    if contexts:
        htf_strs = [f"{c['tf']}: {c['summary']}" for c in contexts]
        primary_htf = contexts[-1]
        lines.append(f"• Konteks {primary_htf['tf']}: {primary_htf['summary']}" + (
            f" ({htf_strs[0]})" if len(htf_strs) > 1 else ""
        ))
    else:
        lines.append("• Konteks HTF: tidak tersedia")

    return lines


def _build_entry_reasons(direction: str, entry_info: Dict[str, Any], snap: Dict[str, Any], htf_ctx: Dict[str, Any]) -> str:
    reasons: List[str] = []
    setups = entry_info.get("setups", [])
    if setups:
        reasons.append("Pola terdeteksi: " + ", ".join(setups))

    zone = entry_info.get("zone")
    if zone is not None:
        pos = "harga sudah berada di dalam zona" if zone["dist"] <= 0.1 else f"harga berjarak {zone['dist']:.1f} ATR dari zona"
        reasons.append(f"Konfluensi {' + '.join(zone['sources'])} ({zone['status']}, {pos})")

    # Tambahkan info Order Block & Liquidity bila ada
    want_dir = "bull" if direction == "BUY" else "bear"
    rel_obs = [o for o in snap["obs"] if o["kind"] == want_dir]
    if rel_obs:
        ob = rel_obs[0]
        reasons.append(f"{'Bullish' if want_dir == 'bull' else 'Bearish'} OB {_fp(ob['low'])}-{_fp(ob['high'])} ({ob['status']})")

    recent_liq = entry_info.get("recent_liq", [])
    if recent_liq:
        lq = recent_liq[-1]
        reasons.append(f"{lq.kind} pada level {_fp(lq.level)}")

    pa = snap["pa"][direction]
    pa_tags: List[str] = []
    if pa["rejection"] >= 0.6:
        pa_tags.append("rejection wick")
    if pa["engulfing"] > 0:
        pa_tags.append("engulfing")
    if pa["displacement"] >= 0.5:
        pa_tags.append("displacement")
    if pa_tags:
        reasons.append("Konfirmasi price action: " + ", ".join(pa_tags))

    if entry_info.get("is_countertrend"):
        reasons.append("Setup countertrend valid didukung sweep likuiditas & konfirmasi pembalikan")

    if not reasons:
        return "Struktur dan momentum lokal mendukung arah pergerakan."
    return ". ".join(reasons) + "."


def format_analysis_report(result: Dict[str, Any]) -> str:
    """Format hasil analisis SMC ke HTML Telegram sesuai susunan baku proyek."""
    symbol = result["symbol"]
    tf = result["tf"]
    signal = result["signal"]
    snap = result["snapshot"]
    htf_ctx = result["htf_ctx"]
    score = result["score"]["total"]
    pip_cfg = PIP_CONFIG[symbol]

    condition_label = snap["condition"]["label"]
    structure_lines = _build_structure_section(snap, htf_ctx)

    lines: List[str] = [
        "🟡 <b>SMC HYBRID ANALYST</b>",
        "",
        f"Market: {symbol}   Timeframe: {tf}",
        "",
    ]

    if signal == "BUY":
        lines.append(f"🟢 <b>SINYAL: BUY — {_setup_badge(score)}</b>")
    elif signal == "SELL":
        lines.append(f"🔴 <b>SINYAL: SELL — {_setup_badge(score)}</b>")
    else:
        lines.append("⚪ <b>SINYAL: WAIT — Menunggu Konfirmasi</b>")

    lines.append(f"Kondisi: {_esc(condition_label)}")
    lines.append("")
    lines.append("📊 <b>STRUKTUR MARKET</b>")
    lines.extend(_esc(item) for item in structure_lines)
    lines.append("")
    lines.append("🎯 <b>AREA TRADING</b>")

    if signal in ("BUY", "SELL") and result.get("trade") is not None:
        t = result["trade"]
        lines.append(f"Entry: <code>{_fp(t['entry_low'])} – {_fp(t['entry_high'])}</code>")
        lines.append(
            f"Stop Loss: <code>{_fp(t['sl'])}</code> ({t['sl_pips']:.0f} pip / ${_fp(t['sl_diff'])})"
        )
        lines.append(
            f"• SL Ideal: <code>{_fp(t['sl_ideal'])}</code> | SL Aman: <code>{_fp(t['sl_safe'])}</code>"
        )
        if t.get("sl_range") is not None:
            r_lo, r_hi = t["sl_range"]
            lines.append(f"• Range SL: <code>{_fp(r_lo)} – {_fp(r_hi)}</code>")

        lines.append(
            f"TP1: <code>{_fp(t['tp1'])}</code> (+{t['tp1_pips']:.0f} pip / ${_fp(t['tp1_diff'])} • {_esc(t['tp1_note'])})"
        )
        lines.append(
            f"TP2: <code>{_fp(t['tp2'])}</code> (+{t['tp2_pips']:.0f} pip / ${_fp(t['tp2_diff'])} • {_esc(t['tp2_note'])})"
        )
        if t.get("tp3") is not None:
            lines.append(
                f"TP3: <code>{_fp(t['tp3'])}</code> (+{t['tp3_pips']:.0f} pip / ${_fp(t['tp3_diff'])} • {_esc(t['tp3_note'])})"
            )
        else:
            lines.append("TP3: belum tersedia level struktural lanjutan")
        lines.append(f"<i>Satuan {symbol}: {_esc(pip_cfg['unit_label'])}</i>")
    else:
        lines.append(f"Entry: Menunggu retracement / breakout valid (Harga kini: <code>{_fp(snap['price'])}</code>)")
        lines.append("Stop Loss: - (menunggu struktur invalidasi valid)")
        lines.append("TP1: -")
        lines.append("TP2: -")
        lines.append("TP3: sesuai level struktural jika tersedia")

    lines.append("")
    lines.append(f"🔥 <b>SKOR SETUP: {score}%</b>")
    lines.append("")
    lines.append("🧠 <b>ALASAN ENTRY</b>")

    if signal in ("BUY", "SELL"):
        reason_text = _build_entry_reasons(signal, result["entry_info"], snap, htf_ctx)
        lines.append(_esc(reason_text))
    else:
        lines.append(_esc(result.get("wait_reason", "Struktur dan zona entry belum memenuhi kriteria risiko.")))

    lines.append("")
    lines.append("⚠️ <b>INVALIDASI</b>")

    if signal in ("BUY", "SELL") and result.get("trade") is not None:
        t = result["trade"]
        dir_word = "di bawah" if signal == "BUY" else "di atas"
        lines.append(
            f"Setup batal jika candle {tf} close {dir_word} <code>{_fp(t['invalidation_level'])}</code> "
            f"(SL Ideal <code>{_fp(t['sl_ideal'])}</code> / SL Aman <code>{_fp(t['sl_safe'])}</code>)."
        )
    else:
        lines.append(
            _esc(
                result.get(
                    "wait_invalidation",
                    f"Status WAIT berubah jika terbentuk BOS/CHOCH close yang jelas pada {tf} disertai retest zona valid.",
                )
            )
        )

    if snap.get("stale"):
        lines.append("")
        lines.append("⚠️ <i>Catatan: Data candle terakhir tidak bergerak (market tutup / jeda sesi).</i>")

    output = "\n".join(lines)
    return output if len(output) <= 4000 else output[:3990] + "…"
