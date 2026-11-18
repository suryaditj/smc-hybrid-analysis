# SMC HYBRID SCALPING HUNTER V1

Bot Telegram analisis trading **XAUUSD** dan **BTCUSD** berbasis **100% Pure Python + Smart Money Concept (SMC) Quant Engine**, berjalan melalui **GitHub Actions** tanpa VPS dan tanpa AI API.

## Fitur Utama
- **Batas Ketat 20 Candle/Timeframe:** Mengambil maksimal 20 candle per timeframe (`M1`, `M5`, `M15`, `M30`, `H1`, `H4`) dan memisahkan candle *close* vs candle berjalan.
- **Pure Python SMC Quant Engine:** Deteksi BOS, CHOCH, MSS, HH/HL/LH/LL, BSL/SSL, Equal High/Low, Liquidity Sweep/Grab, False Breakout, Supply/Demand, Support/Resistance, Fair Value Gap (FVG), Order Block (OB), dan 10 pola entry scalping.
- **Smart Stop Loss & Pip Conversion:** SL Ideal, SL Aman, Range SL, serta perhitungan pip terpisah untuk `XAUUSD` (`1 pip = $0.10`) dan `BTCUSD` (`1 pip = $1.00`).
- **Arsitektur Ramah GitHub Actions:** Bounded polling window (`RUNNER_POLL_SECONDS`), anti-overlap concurrency, dan persistensi preferensi user via `data/user_state.json`.
