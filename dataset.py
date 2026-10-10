"""
dataset.py — turn a folder of daily JSONL into something you can actually work with.

THE PROBLEM, a year out:
    365 files x 3 symbols = 1,095 JSONL files, ~200 MB, ~315,000 records.
    Re-parsing all of that every time you want to test an idea is 30+ seconds
    of JSON decoding for data that has not changed since yesterday.

THE FIX — three layers:

    1. RAW      data/market_SOL_2026-08-29.jsonl     append-only, never edited
    2. CACHE    cache/SOLUSDT_2026-08.csv.gz         one file per symbol-month
    3. MEMORY   load("SOLUSDT")                       a DataFrame

    The cache rebuilds only months whose raw files actually changed, so the
    daily cost is parsing one day, not one year. Today's file is never cached
    (it is still being written) and is always read fresh.

Compression is gzip + CSV: stdlib only, no pyarrow, no DLLs for a security
suite to block. Roughly 8-10x smaller than raw JSONL.

    python dataset.py --build              # build/refresh the cache
    python dataset.py --info               # what do I have
    python dataset.py --archive 30         # gzip raw files older than 30 days

In your own analysis:

    from dataset import load
    df = load("SOLUSDT")                   # everything, 5-min
    df = load("SOLUSDT", freq="1h")        # resampled correctly per column
    df = load("SOLUSDT", start="2026-09")  # a slice
    liq = load_liquidations(freq="1h")     # liquidations aggregated to bars
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

DATA = "data"
CACHE = "cache"

# ── How each column survives a resample ───────────────────────────────
# Levels take the last value in the bucket; flows sum; ranges take min/max.
# Getting this wrong is subtle and silent — summing open interest across an
# hour would produce a number twelve times too large that still looks fine.
LAST = ["last", "mark", "index", "spot", "oi", "oi_usd", "funding", "basis_pct",
        "perp_spot_basis_pct", "best_bid", "best_ask", "global_ls", "top_acct_ls",
        "top_pos_ls", "global_ls_long", "top_acct_ls_long", "top_pos_ls_long",
        "taker_ls", "fng", "next_funding_ms", "chg_24h_pct", "vol_24h",
        "quote_vol_24h", "trades_24h"]
MEAN = ["spread_bps", "bid_qty_top", "ask_qty_top"] + \
       [f"{s}_{d}" for s in ("bid_usd", "ask_usd", "imb")
        for d in ("01", "025", "05", "10", "20")]
SUM = ["taker_buy_vol", "taker_sell_vol"]


# ══════════════════════════════════════════════════════════════════════
#  CACHE BUILD
# ══════════════════════════════════════════════════════════════════════

def _month_of(path: str) -> str:
    return os.path.basename(path).rsplit("_", 1)[1][:7]        # YYYY-MM


def _read_jsonl(path: str) -> list[dict]:
    out = []
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue          # a partial trailing line after a hard stop
    return out


def build_cache(data_dir=DATA, cache_dir=CACHE, verbose=True) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    files = sorted(glob.glob(os.path.join(data_dir, "market_*.jsonl")) +
                   glob.glob(os.path.join(data_dir, "market_*.jsonl.gz")))
    if not files:
        print(f"no market files in {data_dir}/")
        return

    # group by symbol + month
    groups: dict[tuple[str, str], list[str]] = {}
    for f in files:
        base = os.path.basename(f).replace(".gz", "").replace(".jsonl", "")
        _, sym, day = base.split("_", 2)
        if day == today:
            continue          # still being written; always read it fresh
        groups.setdefault((sym, day[:7]), []).append(f)

    built = skipped = 0
    for (sym, month), paths in sorted(groups.items()):
        out = os.path.join(cache_dir, f"{sym}_{month}.csv.gz")
        newest = max(os.path.getmtime(p) for p in paths)
        if os.path.exists(out) and os.path.getmtime(out) >= newest:
            skipped += 1
            continue

        recs = []
        for p in paths:
            recs.extend(_read_jsonl(p))
        if not recs:
            continue
        df = pd.DataFrame(recs)
        df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
        df = df.dropna(subset=["ts"]).set_index("ts").sort_index()
        df = df[~df.index.duplicated(keep="last")]
        df.drop(columns=[c for c in ("symbol", "fields") if c in df], inplace=True)
        df.to_csv(out, compression="gzip")
        built += 1
        if verbose:
            print(f"  {sym} {month}: {len(df):,} records -> "
                  f"{os.path.getsize(out)/1024:.0f} KB")

    if verbose:
        print(f"\ncache: {built} months rebuilt, {skipped} already current")


# ══════════════════════════════════════════════════════════════════════
#  LOADING
# ══════════════════════════════════════════════════════════════════════

def load(symbol: str, start=None, end=None, freq=None,
         data_dir=DATA, cache_dir=CACHE, refresh=True) -> pd.DataFrame:
    """
    Load a symbol's full history. Reads cached months plus today's raw file.

    freq: None keeps native 5-minute. Otherwise any pandas offset ('15min',
    '1h', '4h', '1D') — each column is aggregated according to its type.
    """
    if refresh:
        build_cache(data_dir, cache_dir, verbose=False)

    frames = []
    for f in sorted(glob.glob(os.path.join(cache_dir, f"{symbol}_*.csv.gz"))):
        frames.append(pd.read_csv(f, index_col=0, parse_dates=True))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    live = os.path.join(data_dir, f"market_{symbol}_{today}.jsonl")
    if os.path.exists(live):
        recs = _read_jsonl(live)
        if recs:
            d = pd.DataFrame(recs)
            d["ts"] = pd.to_datetime(d["ts"], utc=True, errors="coerce")
            d = d.dropna(subset=["ts"]).set_index("ts")
            d.drop(columns=[c for c in ("symbol", "fields") if c in d], inplace=True)
            frames.append(d)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames).sort_index()
    df.index = pd.to_datetime(df.index, utc=True)
    df = df[~df.index.duplicated(keep="last")]

    if start:
        df = df[df.index >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df[df.index <= pd.Timestamp(end, tz="UTC")]

    return resample(df, freq) if freq else df


def resample(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Aggregate per column type. Summing a level would silently corrupt it."""
    if df.empty:
        return df
    agg = {}
    for c in df.columns:
        if c in SUM:
            agg[c] = "sum"
        elif c in MEAN:
            agg[c] = "mean"
        elif c in LAST or pd.api.types.is_numeric_dtype(df[c]):
            agg[c] = "last"
    out = df.resample(freq).agg(agg)

    # price extremes within the bucket, useful for barrier tests later
    if "last" in df.columns:
        out["high"] = df["last"].resample(freq).max()
        out["low"] = df["last"].resample(freq).min()
        out["open"] = df["last"].resample(freq).first()
        out["close"] = out["last"]
    out["n_polls"] = df.iloc[:, 0].resample(freq).count()
    return out.dropna(how="all")


def load_liquidations(freq="1h", data_dir=DATA) -> pd.DataFrame:
    """Liquidation events aggregated into bars: count and USD by side."""
    files = sorted(glob.glob(os.path.join(data_dir, "liquidations_*.jsonl")) +
                   glob.glob(os.path.join(data_dir, "liquidations_*.jsonl.gz")))
    recs = []
    for f in files:
        recs.extend(_read_jsonl(f))
    if not recs:
        return pd.DataFrame()

    df = pd.DataFrame(recs)
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df.dropna(subset=["ts"]).set_index("ts").sort_index()

    out = []
    for sym, g in df.groupby("symbol"):
        longs = g[g["side"] == "SELL"]      # a long liquidation sells
        shorts = g[g["side"] == "BUY"]
        r = pd.DataFrame({
            f"{sym}_liq_count": g["usd"].resample(freq).count(),
            f"{sym}_liq_usd": g["usd"].resample(freq).sum(),
            f"{sym}_liq_long_usd": longs["usd"].resample(freq).sum(),
            f"{sym}_liq_short_usd": shorts["usd"].resample(freq).sum(),
        })
        out.append(r)
    return pd.concat(out, axis=1).fillna(0) if out else pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════
#  MAINTENANCE
# ══════════════════════════════════════════════════════════════════════

def archive(days=30, data_dir=DATA) -> None:
    """
    gzip raw files older than `days`. Roughly 85% smaller, still readable by
    everything here. The raw files stay the source of truth — the cache can
    always be rebuilt from them, so never delete these.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    n = saved = 0
    for f in sorted(glob.glob(os.path.join(data_dir, "*.jsonl"))):
        day = os.path.basename(f).replace(".jsonl", "").rsplit("_", 1)[1]
        if day >= cutoff:
            continue
        before = os.path.getsize(f)
        with open(f, "rb") as src, gzip.open(f + ".gz", "wb", compresslevel=6) as dst:
            dst.writelines(src)
        after = os.path.getsize(f + ".gz")
        os.remove(f)
        n += 1
        saved += before - after
    print(f"archived {n} files, saved {saved/1e6:.1f} MB" if n else "nothing to archive")


def info(data_dir=DATA, cache_dir=CACHE) -> None:
    # Build first. Without this, load(refresh=False) sees only the cache plus
    # today's file — so on a cold cache it reports one day and looks broken.
    build_cache(data_dir, cache_dir, verbose=False)

    raw = glob.glob(os.path.join(data_dir, "*.jsonl*"))
    cached = glob.glob(os.path.join(cache_dir, "*.csv.gz"))
    rmb = sum(os.path.getsize(f) for f in raw) / 1e6
    cmb = sum(os.path.getsize(f) for f in cached) / 1e6
    print(f"\nraw   : {len(raw):4d} files  {rmb:7.1f} MB   {data_dir}/")
    print(f"cache : {len(cached):4d} files  {cmb:7.1f} MB   {cache_dir}/")
    if rmb and cmb:
        print(f"        cache is {cmb/rmb*100:.0f}% the size of raw")

    syms = sorted({os.path.basename(f).split("_")[1]
                   for f in raw if os.path.basename(f).startswith("market_")})
    print()
    for s in syms:
        df = load(s, refresh=False)
        if df.empty:
            continue
        span_min = (df.index[-1] - df.index[0]).total_seconds() / 60
        expect = max(1, span_min / 5)          # a poll every 5 minutes
        cov = len(df) / expect * 100
        gaps = np.diff(df.index.values).astype("timedelta64[m]").astype(float)
        big = int((gaps > 15).sum())           # more than two missed polls
        note = f", {big} gaps >15min" if big else ""
        print(f"  {s:10} {len(df):7,} records  {df.index[0].date()} to "
              f"{df.index[-1].date()}  ({cov:.0f}% coverage{note})")

    liq = load_liquidations()
    if not liq.empty:
        print(f"\n  liquidations: {len(liq):,} bars")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--info", action="store_true")
    ap.add_argument("--archive", type=int, metavar="DAYS")
    ap.add_argument("--data", default=DATA)
    ap.add_argument("--cache", default=CACHE)
    args = ap.parse_args()

    if args.archive is not None:
        archive(args.archive, args.data)
    elif args.build:
        build_cache(args.data, args.cache)
    else:
        info(args.data, args.cache)


if __name__ == "__main__":
    main()