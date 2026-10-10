# solpy-monitor

> **This project places NO trades.** It has no exchange API keys, no order code, and no wallet access. Every script is read-only market data collection, analysis, or alerting.

Personal research tooling for SOL / BTC / ETH on Binance USDⓈ-M perpetuals. It logs market data that disappears (order book depth, liquidations, positioning ratios), watches the BTC regime, builds take-profit ladders, and tests a small set of **pre-registered hypotheses** against that data. Not financial advice.

## What it does

| Script | Purpose |
|---|---|
| `collector.py` | Every 300 s (configurable) polls Binance public endpoints for SOLUSDT/BTCUSDT/ETHUSDT: order-book depth buckets, open interest, global & top-trader long/short ratios, taker buy/sell volume, mark/index basis, funding, plus alternative.me Fear & Greed. Liquidations come from the `@forceOrder` websocket (`/market` tier) in a background thread if `websocket-client` is installed. Only collects what can't be fetched later. |
| `watch.py` | Reads the collector output and prints a per-symbol "what does the tape look like" block plus a verdict, using the hypothesis thresholds. Read-only. |
| `btcregime.py` | BTC bull-regime monitor (alert-only). Daily after the 00:00 UTC close it classifies strong_bull / bull / bull_chop / bear / chop from weekly EMA50 + RSI14 and daily EMA50/200 and emails BUY/SELL *alerts*. `--shadow` for test mode. |
| `levels.py` + `levels_config.example.json` | Take-profit ladders: finds old range floors, swing highs, volume zones and Fib retracements, clusters them into "walls", suggests a 3-rung ladder + runner. Copy `levels_config.example.json` to `levels_config.json` (git-ignored) and enter your own coins/cost/qty. Read-only. |
| `HYPOTHESES.md` | Pre-registered hypotheses H1/H2/H3 (+ H2b, H1-cm, H1-rel variants) with ground rules and a results log. |
| `test_hypotheses.py` | Runs the hypotheses against `data/merged/`: event counts, mean forward return gross/net of 0.6% cost, win rate, t-stat, 1,000-shuffle placebo, verdict. |
| `backfill.py` | Pulls free history from data.binance.vision (metrics, 5m klines, liquidation snapshots where still available), funding via REST, and F&G into `data/hist/*.parquet`. Resumable, caches raw zips in `data/raw/`. |
| `merge.py` | Combines backfill + live jsonl onto one 5-min UTC grid (live wins on overlap) → `data/merged/`. |
| `dataset.py` | Monthly gzip-CSV cache over the raw jsonl (`cache/`), `load("SOLUSDT", freq="1h")` helper, `--archive N` to gzip old raw files. |
| `fetchbars.py` | Full-history OHLCV downloader to CSV (drops the forming bar, reports gaps & warmup adequacy). |
| `fetch_data.py` | Older bulk downloader: spot daily for a 20-coin universe + funding for BTC/ETH/SOL into `data/`. (Note: disables TLS verification — fix before reuse.) |
| `dip_finder.py`, `crypto_dipfinder.py` | Relative-weakness scanners for an example equity and crypto watchlist (yfinance / Binance / CoinGecko). Edit the lists. |
| `install_service.bat` | Registers `collector.py` as the Windows service `SolCollector` via NSSM. |

## Pre-registered hypotheses

`HYPOTHESES.md` is locked: thresholds may not be changed, each hypothesis is tested **once, not before 2027-03-01, and only with ≥ 25 non-overlapping qualifying events** per symbol (must hold on 2 of 3 symbols, net of 0.6% round-trip cost, and beat both buy-and-hold and the 95th percentile of 1,000 shuffled-timestamp placebos). Current status per the log: H3 FAIL, H2/H2b closed, H1 pending live liquidation data.

## Data collected & where it goes

- `data/market_<SYMBOL>_<YYYY-MM-DD>.jsonl` — one record per poll (depth, OI, ratios, taker vol, basis, funding, F&G, price snapshot)
- `data/liquidations_<YYYY-MM-DD>.jsonl` — websocket liquidation events
- `data/hist/`, `data/raw/` — backfill output / cached zips
- `data/merged/` — merged parquet + coverage README
- `cache/` — dataset.py monthly cache
- `logs/` — service stdout/stderr (NSSM)

All of these are git-ignored.

## Setup

```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      REM fill in only if you want btcregime email alerts
```

Run:

```bat
python collector.py --once               REM single pass, verify
python collector.py                      REM forever, 300 s interval
python collector.py --inspect data/
python watch.py
python backfill.py --symbols BTCUSDT ETHUSDT SOLUSDT --start 2022-01-01
python merge.py
python dataset.py --build
python test_hypotheses.py                REM only for the real test after 2027-03-01
python btcregime.py --shadow
python levels.py RENDER --refresh
python fetchbars.py
```

### Run the collector as a Windows service (NSSM)

1. `winget install NSSM` (or put `nssm.exe` in the folder).
2. Edit `PROJDIR` / `PYTHON` / `SERVICE` at the top of `install_service.bat`.
3. From an **Administrator** prompt: `.\install_service.bat`. It uses absolute paths (services don't inherit a working directory) and writes to `data\` and `logs\`.

## Disclaimer

Personal experiments. Not financial advice. Nothing here trades, and nothing here is a recommendation to trade.
