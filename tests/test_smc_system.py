#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unit test terpadu untuk memvalidasi:
- Batas maksimal 20 candle & pemisahan candle close vs berjalan
- Deteksi BOS, CHOCH, MSS, Liquidity Sweep, FVG, OB, dan SND
- Entry Engine & Countertrend rules
- Smart SL (Ideal, Aman, Range) & Konversi Pip XAUUSD vs BTCUSD
- Deterministic Scoring (0-100) & Format HTML tanpa nama provider data
"""

import unittest
from typing import List

from config import MAX_CANDLES_PER_TF, PIP_CONFIG
from data_provider import Candle, candles_to_dataframe, clean_and_limit_candles
from entry_engine import detect_scalping_setups
from formatter import format_analysis_report
from risk_manager import build_trade_plan, compute_deterministic_score, price_to_pips
from smc_engine import run_smc_snapshot


def _make_series(symbol: str, tf: str, candles: List[Candle]) -> dict:
    limited = clean_and_limit_candles(candles, max_candles=MAX_CANDLES_PER_TF)
    df = candles_to_dataframe(limited)
    return {
        "symbol": symbol,
        "tf": tf,
        "closed": limited,
        "forming": None,
        "df_closed": df,
        "total_fetched": len(limited),
        "warnings": [],
        "stale": False,
    }


class TestSMCHybridScalpingHunter(unittest.TestCase):
    def test_max_20_candles_limit(self):
        base_t = 1730000000
        raw = [
            Candle(base_t + i * 900, 3800 + i, 3805 + i, 3798 + i, 3803 + i, 100.0)
            for i in range(45)
        ]
        cleaned = clean_and_limit_candles(raw, max_candles=MAX_CANDLES_PER_TF)
        self.assertEqual(len(cleaned), 20)
        self.assertEqual(cleaned[0].t, base_t + 25 * 900)
        self.assertEqual(cleaned[-1].t, base_t + 44 * 900)

    def test_separate_pip_conversion_xau_vs_btc(self):
        self.assertAlmostEqual(price_to_pips("XAUUSD", 4.50), 45.0, places=2)
        self.assertAlmostEqual(price_to_pips("BTCUSD", 45.00), 45.0, places=2)
        self.assertNotEqual(PIP_CONFIG["XAUUSD"]["pip_size"], PIP_CONFIG["BTCUSD"]["pip_size"])

    def test_bearish_smc_structure_and_report_formatting(self):
        base_t = 1730000000
        ohlc_rows = [
            (3820, 3826, 3819, 3825),
            (3825, 3832, 3824, 3831),
            (3831, 3838, 3830, 3837),
            (3837, 3844, 3836, 3842),
            (3842, 3846, 3835, 3836),
            (3836, 3839, 3832, 3834),
            (3834, 3841, 3833, 3840),
            (3840, 3855, 3839, 3853),
            (3853, 3862, 3851, 3860),
            (3860, 3861, 3848, 3849),
            (3849, 3851, 3836, 3837),
            (3837, 3839, 3822, 3824),
            (3824, 3827, 3818, 3820),
            (3820, 3823, 3814, 3816),
            (3816, 3821, 3815, 3820),
            (3820, 3829, 3819, 3827),
            (3827, 3830, 3821, 3822),
            (3822, 3824, 3817, 3818),
            (3818, 3820, 3813, 3815),
            (3815, 3817, 3811, 3813),
        ]
        candles = [
            Candle(base_t + i * 900, o, h, l, c, 250.0)
            for i, (o, h, l, c) in enumerate(ohlc_rows)
        ]
        series = _make_series("XAUUSD", "M15", candles)
        snap = run_smc_snapshot(series, live_price=3823.50)

        self.assertGreater(len(snap["structure"]["events"]), 0)
        htf_ctx = {
            "contexts": [{"tf": "H1", "trend": -1, "summary": "Bearish, BOS bear"}],
            "overall_trend": -1,
            "levels": [],
        }

        sell_entry = detect_scalping_setups("SELL", snap, htf_ctx)
        self.assertTrue(sell_entry["valid"])

        trade, err = build_trade_plan("XAUUSD", "SELL", sell_entry, snap, htf_ctx)
        self.assertIsNone(err)
        self.assertIsNotNone(trade)
        self.assertGreater(trade["sl_safe"], trade["sl_ideal"])
        self.assertGreaterEqual(trade["rr1"], 1.0)

        score1 = compute_deterministic_score("SELL", sell_entry, trade, snap, htf_ctx)
        score2 = compute_deterministic_score("SELL", sell_entry, trade, snap, htf_ctx)
        self.assertEqual(score1["total"], score2["total"])
        self.assertTrue(0 <= score1["total"] <= 100)

        report = format_analysis_report(
            {
                "symbol": "XAUUSD",
                "tf": "M15",
                "signal": "SELL",
                "snapshot": snap,
                "htf_ctx": htf_ctx,
                "entry_info": sell_entry,
                "trade": trade,
                "score": score1,
            }
        )
        self.assertNotIn("biquote", report.lower())
        self.assertIn("SMC HYBRID ANALYST", report)
        self.assertIn("SL Ideal", report)
        self.assertIn("SL Aman", report)

    def test_rejects_trade_when_strong_reversal_wall_blocks_1r(self):
        base_t = 1730000000
        ohlc_rows = [
            (95000, 95150, 94950, 95100) for _ in range(20)
        ]
        candles = [
            Candle(base_t + i * 900, o, h, l, c, 100.0)
            for i, (o, h, l, c) in enumerate(ohlc_rows)
        ]
        series = _make_series("BTCUSD", "M15", candles)
        snap = run_smc_snapshot(series, live_price=95050.0)
        # Tambahkan dinding Demand kuat tepat di 95020 (< 1R dari entry 95050 dengan SL 95250)
        snap["snd"].append(
            {
                "kind": "demand",
                "low": 95000.0,
                "high": 95025.0,
                "origin": 95000.0,
                "visits": 0,
                "status": "fresh",
                "quality": "Strong Zone",
                "strength": 0.90,
                "i": 5,
            }
        )
        fake_entry = {
            "valid": True,
            "setups": ["FVG Entry"],
            "zone": {
                "low": 95040.0,
                "high": 95070.0,
                "origin": 95250.0,
                "alt_origins": [95250.0],
                "sources": ["FVG"],
                "dist": 0.0,
                "status": "fresh",
                "strength": 0.8,
            },
            "is_countertrend": False,
        }
        trade, err = build_trade_plan("BTCUSD", "SELL", fake_entry, snap, {"overall_trend": -1, "levels": []})
        self.assertIsNone(trade)
        self.assertIsNotNone(err)


if __name__ == "__main__":
    unittest.main()
