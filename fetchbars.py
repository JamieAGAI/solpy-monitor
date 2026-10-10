#!/usr/bin/env python3
"""
================================================================================
  FETCHBARS  —  Binance OHLCV downloader for regime / backtest work
================================================================================
Pulls FULL-HISTORY klines for any symbol + interval and writes clean CSVs that
btcregime.py, fibtest.py, or any backtest can load directly.

WHY THIS EXISTS:
  btcregime fetches its own data live on every cycle. That's right for the
  monitor, wrong for research — you can't iterate on a backtest while waiting
  on the API, and you can't reproduce a result if the data changes underneath
  you. This dumps history to disk once, then everything reads local files.

WHAT IT DOES THAT A NAIVE FETCHER DOESN'T:
  - Drops the still-forming bar. The last kline Binance returns is the live,
    incomplete one. Backtesting on it is silent lookahead-adjacent garbage.
  - Validates the symbol against exchangeInfo and suggests near-matches, so a
    renamed ticker (RNDR -> RENDER, MATIC -> POL) fails loudly, not silently.
  - Incremental append: re-run it tomorrow and it only fetches the new bars.
  - Reports gaps (missing bars from exchange halts) instead of hiding them.
  - Reports warmup adequacy for the btcregime indicators, so you know before
    you backtest whether a weekly EMA50 on this symbol means anything.

OUTPUT FORMAT (CSV, one file per symbol+interval):
  ts,open,high,low,close,volume
  ts is the bar OPEN time, UTC, ISO 8601 — same convention as btcregime.

RUN:
  python fetchbars.py                                  # defaults: BTC+SOL, 4h/1d/1w
  python fetchbars.py SOLUSDT RENDERUSDT               # specific symbols
  python fetchbars.py RENDERUSDT -i 4h 1d 1w           # specific intervals
  python fetchbars.py BTCUSDT --start 2017-08-01       # override start date
  python fetchbars.py SOLUSDT --refresh                # ignore cache, refetch all
  python fetchbars.py --list RENDER                    # search available symbols
  python fetchbars.py --selftest                       # offline logic tests

DEPS: pandas, python-binance  (same as btcregime — no new installs)
================================================================================
"""
import os
import sys
import time
import argparse
import datetime as dt

import pandas as pd

# ------------------------------------------------------------------ config ----
DEFAULT_SYMBOLS   = ["BTCUSDT", "SOLUSDT", "RENDERUSDT"]
DEFAULT_INTERVALS = ["4h", "1d", "1w"]
DEFAULT_START     = "2017-01-01"      # before any Binance listing; API clamps it
OUT_DIR           = "data"
RETRIES           = 3
RETRY_SLEEP_S     = 15

# Bar counts btcregime needs before its indicators are trustworthy.
WARMUP = {
    "1d": (250, "daily EMA200 + MIN_DAILY_BARS"),
    "1w": (200, "weekly EMA50 fully converged (FULL_WEEKLY_WARMUP)"),
}

INTERVAL_TD = {
    "1m": pd.Timedelta(minutes=1),   "5m":  pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15), "30m": pd.Timedelta(minutes=30),
    "1h": pd.Timedelta(hours=1),     "2h":  pd.Timedelta(hours=2),
    "4h": pd.Timedelta(hours=4),     "6h":  pd.Timedelta(hours=6),
    "12h": pd.Timedelta(hours=12),   "1d":  pd.Timedelta(days=1),
    "3d": pd.Timedelta(days=3),      "1w":  pd.Timedelta(weeks=1),
}

COLS = ["open", "high", "low", "close", "volume"]

# Binance ticker renames. Substring matching can't catch these — "RNDR" is not
# a substring of "RENDERUSDT" — so the old ticker has to be mapped explicitly
# or it looks identical to a delisting.
RENAMES = {
    "RNDRUSDT":  "RENDERUSDT",
    "MATICUSDT": "POLUSDT",
    "FTMUSDT":   "SUSDT",
}

# Rebrands split a coin's history across two tickers. The new pair's klines
# START at the relisting date, so fetching it alone silently returns a stub
# and calls it "full history" — RENDERUSDT begins 2024-07-26, hiding the
# 2021-2024 tape that lives under the delisted RNDRUSDT.
#
# Only listed here when the swap was 1:1, so the two price series join
# without a scaling adjustment. Verify the ratio before adding a pair.
#   RNDR  -> RENDER : 1:1, relisted 2024-07-26
#   MATIC -> POL    : 1:1, relisted 2024-09-04
PREDECESSORS = {
    "RENDERUSDT": "RNDRUSDT",
    "POLUSDT":    "MATICUSDT",
}


def log(msg):
    print(f"{dt.datetime.now(dt.timezone.utc):%H:%M:%S}  {msg}", flush=True)


# ------------------------------------------------------------------- parse ----
def klines_to_df(klines, interval, now=None):
    """
    Binance kline list -> tidy DataFrame indexed on bar OPEN time (UTC),
    with the still-forming bar removed.

    The live bar is the one whose close time is still in the future:
        open_time + interval > now
    Computing the span from the interval (not from the last two rows) means
    this stays correct even when the exchange has a gap right at the end.
    """
    if not klines:
        return pd.DataFrame(columns=COLS,
                            index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    now = now or pd.Timestamp.now("UTC")
    span = INTERVAL_TD[interval]

    df = pd.DataFrame(klines, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_av", "trades", "tb_base", "tb_quote", "ignore",
    ])
    df[COLS] = df[COLS].astype(float)
    df["ts"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("ts")[COLS].sort_index()
    df = df[~df.index.duplicated(keep="last")]

    incomplete = df.index[-1] + span > now
    if incomplete:
        df = df.iloc[:-1]
    return df


def find_gaps(df, interval):
    """Return a list of (after_ts, missing_bar_count) for holes in the series."""
    if len(df) < 2:
        return []
    span = INTERVAL_TD[interval]
    deltas = df.index.to_series().diff().dropna()
    bad = deltas[deltas > span]
    return [(ts, int(d / span) - 1) for ts, d in bad.items()]


# ------------------------------------------------------------------ client ----
def make_client():
    try:
        from binance.client import Client
    except ImportError:
        sys.exit("python-binance not installed.  pip install python-binance")
    return Client(requests_params={"timeout": 30})


_SYMBOL_CACHE = None

def all_symbols(client):
    """Every TRADING spot symbol on Binance, cached for the process."""
    global _SYMBOL_CACHE
    if _SYMBOL_CACHE is None:
        info = client.get_exchange_info()
        _SYMBOL_CACHE = {s["symbol"] for s in info["symbols"]
                         if s["status"] == "TRADING"}
    return _SYMBOL_CACHE


def resolve_symbol(client, symbol):
    """
    Confirm the symbol trades. On a miss, suggest near-matches rather than
    letting the fetch return an empty list that looks like 'no history'.
    Tickers get renamed (RNDR -> RENDER, MATIC -> POL) and that failure mode
    is otherwise indistinguishable from a delisting.
    """
    symbol = symbol.upper()
    syms = all_symbols(client)
    if symbol in syms:
        return symbol

    new = RENAMES.get(symbol)
    if new and new in syms:
        log(f"  note: {symbol} was renamed — using {new}")
        return new

    base = symbol.replace("USDT", "").replace("USDC", "").replace("BUSD", "")
    near = sorted(s for s in syms if base and base in s)[:12]
    hint = f"\n  Did you mean: {', '.join(near)}" if near else ""
    raise ValueError(f"{symbol} is not a TRADING spot symbol on Binance.{hint}")


def fetch_klines(client, symbol, interval, start_str):
    """get_historical_klines with retries. Handles pagination internally."""
    for attempt in range(1, RETRIES + 1):
        try:
            return client.get_historical_klines(symbol, interval, start_str=start_str)
        except Exception as e:
            log(f"  fetch {symbol} {interval} attempt {attempt}/{RETRIES} failed: {e}")
            if attempt < RETRIES:
                time.sleep(RETRY_SLEEP_S)
    raise RuntimeError(f"{symbol} {interval}: fetch failed after {RETRIES} tries")


# --------------------------------------------------------------- disk cache ---
def csv_path(symbol, interval, out_dir):
    return os.path.join(out_dir, f"{symbol}_{interval}.csv")


def load_existing(path):
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path, index_col="ts", parse_dates=["ts"])
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        return df[COLS].sort_index()
    except Exception as e:
        log(f"  existing file unreadable ({e}) — refetching in full")
        return None


def stitch_predecessor(client, symbol, interval, start_str, df):
    """
    Prepend the delisted predecessor ticker's history to a rebranded pair.
    Only bars strictly older than the successor's first bar are used, so the
    join can't duplicate or overwrite live-era data. Returns (df, note).
    """
    old = PREDECESSORS.get(symbol)
    if not old or df.empty:
        return df, None

    try:
        raw = fetch_klines(client, old, interval, start_str)
    except Exception as e:
        return df, f"predecessor {old} unavailable ({e}) — history starts at relisting"

    prior = klines_to_df(raw, interval)
    if prior.empty:
        return df, f"predecessor {old} returned no klines (delisted pairs are often purged)"

    prior = prior[prior.index < df.index[0]]
    if prior.empty:
        return df, f"predecessor {old} had no bars older than {df.index[0]:%Y-%m-%d}"

    joined = pd.concat([prior, df]).sort_index()
    joined = joined[~joined.index.duplicated(keep="last")]
    return joined, (f"stitched {len(prior)} bars from {old} "
                    f"({prior.index[0]:%Y-%m-%d} -> {prior.index[-1]:%Y-%m-%d}) at 1:1")


def fetch_one(client, symbol, interval, start_str, out_dir, refresh):
    """Fetch (or top up) one symbol+interval, write CSV, print a summary."""
    path = csv_path(symbol, interval, out_dir)
    existing = None if refresh else load_existing(path)

    if existing is not None and len(existing):
        # Refetch from the last stored bar so a bar that was still forming at
        # the previous run gets overwritten with its final values.
        resume = existing.index[-1]
        start = resume.strftime("%d %b %Y %H:%M:%S")
        log(f"  {symbol} {interval}: {len(existing)} cached bars, topping up from {resume:%Y-%m-%d %H:%M}")
    else:
        start = start_str
        log(f"  {symbol} {interval}: full history from {start_str}")

    new = klines_to_df(fetch_klines(client, symbol, interval, start), interval)

    # Only stitch on a full fetch — a top-up already sits on stitched history.
    if (existing is None or not len(existing)) and not new.empty:
        new, note = stitch_predecessor(client, symbol, interval, start_str, new)
        if note:
            log(f"    {note}")

    if existing is not None and len(existing):
        df = pd.concat([existing, new])
        df = df[~df.index.duplicated(keep="last")].sort_index()
        added = len(df) - len(existing)
    else:
        df = new
        added = len(df)

    if not len(df):
        log(f"  {symbol} {interval}: NO COMPLETED BARS — skipped")
        return None

    os.makedirs(out_dir, exist_ok=True)
    out = df.copy()
    out.index = out.index.strftime("%Y-%m-%dT%H:%M:%S%z")
    out.to_csv(path, index_label="ts")

    span_days = (df.index[-1] - df.index[0]).days
    log(f"  {symbol} {interval}: {len(df)} bars  {df.index[0]:%Y-%m-%d} -> "
        f"{df.index[-1]:%Y-%m-%d}  ({span_days/365.25:.1f}y, +{added} new)  -> {path}")

    gaps = find_gaps(df, interval)
    if gaps:
        shown = ", ".join(f"{ts:%Y-%m-%d}(+{n})" for ts, n in gaps[:5])
        log(f"    ⚠ {len(gaps)} gap(s), {sum(n for _, n in gaps)} missing bars: {shown}")

    need, why = WARMUP.get(interval, (0, ""))
    if need and len(df) < need:
        log(f"    ⚠ {len(df)}/{need} bars — not enough for {why}")

    return df


# ------------------------------------------------------------------ verdict ---
def regime_readiness(results, symbol):
    """One-line verdict on whether btcregime's indicators can be trusted here."""
    d = results.get((symbol, "1d"))
    w = results.get((symbol, "1w"))
    if d is None or w is None:
        return
    nd, nw = len(d), len(w)
    if nd >= 250 and nw >= 200:
        v = "READY — daily and weekly indicators fully warmed up."
    elif nd >= 250 and nw >= 60:
        v = (f"PARTIAL — only {nw} weekly bars ({nw/52:.1f}y), want 200. The weekly "
             f"EMA50 still carries seed weight, so regime flips near the EMA are "
             f"unreliable and the first ~2 years of any backtest are suspect.")
    else:
        v = f"NOT READY — {nd} daily / {nw} weekly bars is too little history."
    log(f"  {symbol} regime readiness: {v}")


# ----------------------------------------------------------------- selftest ---
def selftest():
    """Offline checks on the parsing logic — no network."""
    now = pd.Timestamp("2026-01-10 12:00", tz="UTC")

    def mk(ts_list):
        return [[int(pd.Timestamp(t).timestamp() * 1000), "1", "2", "0.5", "1.5",
                 "100", 0, 0, 0, 0, 0, 0] for t in ts_list]

    # 1. the still-forming bar is dropped
    ks = mk(["2026-01-08", "2026-01-09", "2026-01-10"])
    df = klines_to_df(ks, "1d", now=now)
    assert len(df) == 2, df
    assert df.index[-1] == pd.Timestamp("2026-01-09", tz="UTC")

    # 2. a fully closed final bar is kept
    ks = mk(["2026-01-08", "2026-01-09"])
    assert len(klines_to_df(ks, "1d", now=now)) == 2

    # 3. drop decision uses the interval, not the spacing of the last two rows,
    #    so a gap right before the live bar can't fool it
    ks = mk(["2026-01-01", "2026-01-09", "2026-01-10"])
    assert len(klines_to_df(ks, "1d", now=now)) == 2

    # 4. 4h bars: 08:00 closed, 12:00 still forming at 12:00
    ks = mk(["2026-01-10 04:00", "2026-01-10 08:00", "2026-01-10 12:00"])
    assert len(klines_to_df(ks, "4h", now=now)) == 2

    # 5. gap detection counts missing bars
    ks = mk(["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06"])
    g = find_gaps(klines_to_df(ks, "1d", now=now), "1d")
    assert g and g[0][1] == 2, g

    # 6. empty input doesn't explode
    assert len(klines_to_df([], "1d", now=now)) == 0

    # 7. duplicate timestamps collapse
    ks = mk(["2026-01-08", "2026-01-08", "2026-01-09"])
    assert len(klines_to_df(ks, "1d", now=now)) == 2

    print("selftest: all 7 checks passed")


# --------------------------------------------------------------------- main ---
def main():
    p = argparse.ArgumentParser(description="Fetch Binance OHLCV history to CSV.")
    p.add_argument("symbols", nargs="*", default=None,
                   help=f"symbols, e.g. BTCUSDT SOLUSDT (default: {' '.join(DEFAULT_SYMBOLS)})")
    p.add_argument("-i", "--intervals", nargs="+", default=DEFAULT_INTERVALS,
                   choices=list(INTERVAL_TD), help="klines intervals")
    p.add_argument("--start", default=DEFAULT_START, help="start date YYYY-MM-DD")
    p.add_argument("-o", "--out", default=OUT_DIR, help="output directory")
    p.add_argument("--refresh", action="store_true", help="ignore cache, refetch everything")
    p.add_argument("--list", metavar="SUBSTR", help="list matching Binance symbols and exit")
    p.add_argument("--selftest", action="store_true", help="run offline logic tests and exit")
    a = p.parse_args()

    if a.selftest:
        selftest()
        return

    client = make_client()

    if a.list:
        hits = sorted(s for s in all_symbols(client) if a.list.upper() in s)
        print(f"{len(hits)} symbols matching {a.list.upper()!r}:")
        for s in hits:
            print("  " + s)
        return

    symbols = [s.upper() for s in (a.symbols or DEFAULT_SYMBOLS)]

    resolved = []
    for s in symbols:
        try:
            resolved.append(resolve_symbol(client, s))
        except ValueError as e:
            log(f"✗ {e}")
    if not resolved:
        sys.exit("No valid symbols — nothing to do.")

    results = {}
    for sym in resolved:
        log(f"{sym}")
        for iv in a.intervals:
            try:
                df = fetch_one(client, sym, iv, a.start, a.out, a.refresh)
                if df is not None:
                    results[(sym, iv)] = df
            except Exception as e:
                log(f"  ✗ {sym} {iv}: {e}")
        regime_readiness(results, sym)

    log(f"Done. {len(results)} file(s) in {os.path.abspath(a.out)}/")


if __name__ == "__main__":
    main()