"""
backfill.py — pull free history from data.binance.vision into data/hist/

    pip install requests pandas pyarrow
    python backfill.py --symbols BTCUSDT ETHUSDT SOLUSDT --start 2022-01-01
    python backfill.py --symbols SOLUSDT --start 2024-01-01 --end 2024-01-31   # small test run

What it fetches (all free):
  metrics            OI, top-trader ratios, account L/S ratio, taker ratio   (5-min, daily zips)
  klines 5m          price for alignment                                    (monthly + daily zips)
  liquidationSnapshot  liquidations — Binance stopped publishing USDT-M after 2024-03-31
  funding            REST /fapi/v1/fundingRate (full history)
  fear & greed       alternative.me (full history)

Raw zips are cached in data/raw/ so re-runs only fetch what's missing.
Output: data/hist/<SYMBOL>.parquet  (5-min grid, collector column names)
        data/hist/liquidations.parquet
Re-runnable. Safe to Ctrl+C and resume.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone

import pandas as pd

try:                                   # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                      # noqa: BLE001
    pass
import requests

BASE = "https://data.binance.vision/data/futures/um"
FAPI = "https://fapi.binance.com"
FNG = "https://api.alternative.me/fng/"

UM_LIQ = False
S = requests.Session()
S.headers["User-Agent"] = "backfill/1.0"


# ── download helpers ──────────────────────────────────────────────────
def fetch_zip(url: str, cache: str) -> pd.DataFrame | None:
    """Return the CSV inside a Binance Vision zip, or None if it doesn't exist."""
    if os.path.exists(cache):
        if os.path.getsize(cache) == 0:          # cached 404
            return None
        raw = open(cache, "rb").read()
    else:
        for attempt in range(4):
            try:
                r = S.get(url, timeout=60)
                break
            except requests.RequestException:
                time.sleep(2 ** attempt)
        else:
            print(f"    network failure {url}")
            return None
        if r.status_code == 404:
            open(cache, "wb").close()             # remember the miss
            return None
        if r.status_code != 200:
            print(f"    HTTP {r.status_code} {url}")
            return None
        raw = r.content
        with open(cache, "wb") as fh:
            fh.write(raw)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            name = z.namelist()[0]
            text = z.read(name)
    except zipfile.BadZipFile:
        print(f"    bad zip {url}")
        return None
    first = text.split(b"\n", 1)[0]
    has_header = not first.strip()[:1].isdigit()
    return pd.read_csv(io.BytesIO(text), header=0 if has_header else None)


def norm(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Give every file the same column names whether or not it shipped with a header."""
    if df.shape[1] == len(cols):
        df.columns = cols
    return df


def to_utc(series: pd.Series) -> pd.Series:
    """Binance epoch columns are ms, except some 2025+ files switched to µs."""
    s = pd.to_numeric(series, errors="coerce")
    unit = "us" if s.dropna().abs().median() > 1e14 else "ms"
    return pd.to_datetime(s, unit=unit, utc=True)


def daterange(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def months(a: date, b: date):
    d = date(a.year, a.month, 1)
    while d <= b:
        yield d
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)


# ── datasets ──────────────────────────────────────────────────────────
METRIC_COLS = ["create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
               "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
               "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]


def get_metrics(sym: str, a: date, b: date, raw: str) -> pd.DataFrame:
    frames, missing = [], 0
    days = list(daterange(a, b))
    for i, d in enumerate(days):
        url = f"{BASE}/daily/metrics/{sym}/{sym}-metrics-{d}.zip"
        df = fetch_zip(url, os.path.join(raw, f"{sym}-metrics-{d}.zip"))
        if df is None:
            missing += 1
            continue
        frames.append(norm(df, METRIC_COLS))
        if i % 100 == 0:
            print(f"    metrics {d}  ({i}/{len(days)})")
    print(f"    metrics: {len(frames)} days, {missing} missing")
    if not frames:
        return pd.DataFrame()
    m = pd.concat(frames, ignore_index=True)
    m["ts"] = pd.to_datetime(m["create_time"], utc=True)
    out = pd.DataFrame({
        "ts": m["ts"],
        "oi": pd.to_numeric(m["sum_open_interest"], errors="coerce"),
        "oi_usd": pd.to_numeric(m["sum_open_interest_value"], errors="coerce"),
        "top_acct_ls": pd.to_numeric(m["count_toptrader_long_short_ratio"], errors="coerce"),
        "top_pos_ls": pd.to_numeric(m["sum_toptrader_long_short_ratio"], errors="coerce"),
        "global_ls": pd.to_numeric(m["count_long_short_ratio"], errors="coerce"),
        "taker_ls": pd.to_numeric(m["sum_taker_long_short_vol_ratio"], errors="coerce"),
    })
    out = out.dropna(subset=["ts"]).set_index("ts").sort_index()
    return out[~out.index.duplicated(keep="last")]


KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
              "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore"]


def get_klines(sym: str, a: date, b: date, raw: str) -> pd.DataFrame:
    frames = []
    today = datetime.now(timezone.utc).date()
    for m in months(a, b):
        # monthly file exists once the month is complete; otherwise fall back to daily
        month_end = date(m.year + (m.month == 12), m.month % 12 + 1, 1) - timedelta(days=1)
        if month_end < today:
            url = f"{BASE}/monthly/klines/{sym}/5m/{sym}-5m-{m:%Y-%m}.zip"
            df = fetch_zip(url, os.path.join(raw, f"{sym}-5m-{m:%Y-%m}.zip"))
            if df is not None:
                frames.append(norm(df, KLINE_COLS))
                continue
        for d in daterange(max(m, a), min(month_end, b)):
            url = f"{BASE}/daily/klines/{sym}/5m/{sym}-5m-{d}.zip"
            df = fetch_zip(url, os.path.join(raw, f"{sym}-5m-{d}.zip"))
            if df is not None:
                frames.append(norm(df, KLINE_COLS))
    print(f"    klines: {len(frames)} files")
    if not frames:
        return pd.DataFrame()
    k = pd.concat(frames, ignore_index=True)
    k["ts"] = to_utc(k["open_time"])
    out = pd.DataFrame({"ts": k["ts"],
                        "last": pd.to_numeric(k["close"], errors="coerce"),
                        "kl_high": pd.to_numeric(k["high"], errors="coerce"),
                        "kl_low": pd.to_numeric(k["low"], errors="coerce"),
                        "kl_vol": pd.to_numeric(k["volume"], errors="coerce")})
    out = out.dropna(subset=["ts"]).set_index("ts").sort_index()
    return out[~out.index.duplicated(keep="last")]


LIQ_COLS = ["time", "symbol", "side", "order_type", "time_in_force", "original_quantity",
            "price", "average_price", "order_status", "last_fill_quantity",
            "accumulated_fill_quantity"]


def get_liquidations(sym: str, a: date, b: date, raw: str) -> pd.DataFrame:
    frames, missing, streak = [], 0, 0
    days = list(daterange(a, b))
    for i, d in enumerate(days):
        if i % 100 == 0:
            print(f"    liquidations {d}  ({i}/{len(days)})")
        url = f"{BASE}/daily/liquidationSnapshot/{sym}/{sym}-liquidationSnapshot-{d}.zip"
        df = fetch_zip(url, os.path.join(raw, f"{sym}-liq-{d}.zip"))
        if df is None:
            missing += 1
            streak += 1
            if streak > 60 and d > date(2024, 4, 1):
                print(f"    liquidations: 60 consecutive missing after {d}, stopping (archive ended)")
                break
            continue
        streak = 0
        frames.append(norm(df, LIQ_COLS))
    print(f"    liquidations: {len(frames)} days, {missing} missing")
    if not frames:
        return pd.DataFrame()
    L = pd.concat(frames, ignore_index=True)
    L["ts"] = to_utc(L["time"])
    qty = pd.to_numeric(L["accumulated_fill_quantity"], errors="coerce")
    price = pd.to_numeric(L["average_price"], errors="coerce")
    return pd.DataFrame({"ts": L["ts"], "symbol": sym, "side": L["side"].astype(str).str.upper(),
                         "price": price, "qty": qty, "usd": price * qty}).dropna()


# ── COIN-M proxies ──────────────────────────────────────────────────────
# Binance deleted the USDT-M liquidation archive, but the COIN-M one survives
# (from 2023-06-25). Coin-margined perps liquidate on the same price moves, so
# BTCUSD_PERP is a usable proxy for BTCUSDT. Contract sizes: BTC 100 USD, others 10.
CM_BASE = "https://data.binance.vision/data/futures/cm"
CM_MAP = {"BTCUSDT": ("BTCUSD_PERP", 100.0), "ETHUSDT": ("ETHUSD_PERP", 10.0), "SOLUSDT": ("SOLUSD_PERP", 10.0)}


def get_cm_liquidations(sym: str, a: date, b: date, raw: str) -> pd.DataFrame:
    if sym not in CM_MAP:
        return pd.DataFrame()
    cm, size = CM_MAP[sym]
    a = max(a, date(2023, 6, 25))
    frames, missing = [], 0
    days = list(daterange(a, b))
    for i, d in enumerate(days):
        if i % 100 == 0:
            print(f"    cm liquidations {d}  ({i}/{len(days)})")
        url = f"{CM_BASE}/daily/liquidationSnapshot/{cm}/{cm}-liquidationSnapshot-{d}.zip"
        df = fetch_zip(url, os.path.join(raw, f"{cm}-liq-{d}.zip"))
        if df is None:
            missing += 1
            continue
        frames.append(norm(df, LIQ_COLS))
    print(f"    cm liquidations: {len(frames)} days, {missing} missing")
    if not frames:
        return pd.DataFrame()
    L = pd.concat(frames, ignore_index=True)
    L["ts"] = to_utc(L["time"])
    qty = pd.to_numeric(L["accumulated_fill_quantity"], errors="coerce")
    price = pd.to_numeric(L["average_price"], errors="coerce")
    return pd.DataFrame({"ts": L["ts"], "symbol": sym, "side": L["side"].astype(str).str.upper(),
                         "price": price, "qty": qty, "usd": qty * size, "venue": "cm"}).dropna()


def get_cm_oi(sym: str, a: date, b: date, raw: str) -> pd.DataFrame:
    """COIN-M open interest in USD (contracts × contract size), to normalise CM liquidations."""
    if sym not in CM_MAP:
        return pd.DataFrame()
    cm, size = CM_MAP[sym]
    a = max(a, date(2023, 6, 25))
    frames = []
    for d in daterange(a, b):
        url = f"{CM_BASE}/daily/metrics/{cm}/{cm}-metrics-{d}.zip"
        df = fetch_zip(url, os.path.join(raw, f"{cm}-metrics-{d}.zip"))
        if df is not None:
            frames.append(norm(df, METRIC_COLS))
    print(f"    cm metrics: {len(frames)} days")
    if not frames:
        return pd.DataFrame()
    m = pd.concat(frames, ignore_index=True)
    out = pd.DataFrame({"ts": pd.to_datetime(m["create_time"], utc=True),
                        "oi_usd_cm": pd.to_numeric(m["sum_open_interest"], errors="coerce") * size})
    out = out.dropna().set_index("ts").sort_index()
    return out[~out.index.duplicated(keep="last")]


def get_funding(sym: str, a: date, b: date) -> pd.DataFrame:
    rows, start = [], int(datetime(a.year, a.month, a.day, tzinfo=timezone.utc).timestamp() * 1000)
    end = int(datetime(b.year, b.month, b.day, 23, 59, tzinfo=timezone.utc).timestamp() * 1000)
    while start < end:
        try:
            r = S.get(f"{FAPI}/fapi/v1/fundingRate",
                      params={"symbol": sym, "startTime": start, "endTime": end, "limit": 1000},
                      timeout=30)
            d = r.json()
        except Exception as e:                                        # noqa: BLE001
            print(f"    funding fetch failed: {e}")
            break
        if not isinstance(d, list) or not d:
            break
        rows += d
        start = int(d[-1]["fundingTime"]) + 1
        if len(d) < 1000:
            break
        time.sleep(0.3)
    print(f"    funding: {len(rows)} rows")
    if not rows:
        return pd.DataFrame()
    f = pd.DataFrame(rows)
    return pd.DataFrame({"ts": pd.to_datetime(f["fundingTime"].astype("int64"), unit="ms", utc=True),
                         "funding": pd.to_numeric(f["fundingRate"], errors="coerce")}
                        ).set_index("ts").sort_index()


def get_fng() -> pd.DataFrame:
    try:
        d = S.get(FNG, params={"limit": 0, "format": "json"}, timeout=30).json()["data"]
    except Exception as e:                                            # noqa: BLE001
        print(f"    fear&greed failed: {e}")
        return pd.DataFrame()
    f = pd.DataFrame(d)
    f["ts"] = pd.to_datetime(f["timestamp"].astype("int64"), unit="s", utc=True)
    f["fng"] = pd.to_numeric(f["value"], errors="coerce")
    print(f"    fear&greed: {len(f)} days")
    return f[["ts", "fng"]].set_index("ts").sort_index()


# ── assemble ──────────────────────────────────────────────────────────
def build(sym: str, a: date, b: date, outdir: str, fng: pd.DataFrame, skip_liq: bool) -> pd.DataFrame:
    raw = os.path.join(outdir, "raw", sym)
    hist = os.path.join(outdir, "hist")
    os.makedirs(raw, exist_ok=True)
    os.makedirs(hist, exist_ok=True)
    print(f"\n{sym}  {a} → {b}")

    metrics = get_metrics(sym, a, b, raw)
    klines = get_klines(sym, a, b, raw)
    funding = get_funding(sym, a, b)
    if klines.empty and metrics.empty:
        print("    nothing fetched — check dates/symbol")
        return pd.DataFrame()

    grid = pd.date_range(pd.Timestamp(a, tz="UTC"), pd.Timestamp(b, tz="UTC") + pd.Timedelta(days=1),
                         freq="5min", inclusive="left")
    df = pd.DataFrame(index=grid)
    if not klines.empty:
        df = df.join(klines.reindex(grid, method="nearest", tolerance=pd.Timedelta("4min")))
    if not metrics.empty:
        df = df.join(metrics.reindex(grid, method="nearest", tolerance=pd.Timedelta("4min")))
    if not funding.empty:
        df["funding"] = funding["funding"].reindex(grid, method="ffill")
    cm_oi = get_cm_oi(sym, a, b, raw)
    if not cm_oi.empty:
        df = df.join(cm_oi.reindex(grid, method="nearest", tolerance=pd.Timedelta("4min")))
    if not fng.empty:
        df["fng"] = fng["fng"].reindex(grid, method="ffill")
    if "oi_usd" not in df or df["oi_usd"].isna().all():
        if "oi" in df and "last" in df:
            df["oi_usd"] = df["oi"] * df["last"]
    df.index.name = "ts"
    df["source"] = "hist"
    df = df.dropna(how="all", subset=[c for c in df.columns if c != "source"])
    path = os.path.join(hist, f"{sym}.parquet")
    df.to_parquet(path)
    print(f"    wrote {path}: {len(df):,} rows, {df.index.min():%Y-%m-%d} → {df.index.max():%Y-%m-%d}")

    if skip_liq:
        return pd.DataFrame()
    um = get_liquidations(sym, a, b, raw) if UM_LIQ else pd.DataFrame()   # UM archive was deleted by Binance
    cm = get_cm_liquidations(sym, a, b, raw)
    if not um.empty:
        um["venue"] = "um"
    return pd.concat([x for x in (um, cm) if not x.empty], ignore_index=True) if (not um.empty or not cm.empty) else pd.DataFrame()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default=(datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat())
    ap.add_argument("--outdir", default="data")
    ap.add_argument("--skip-liq", action="store_true", help="skip liquidationSnapshot (fast)")
    ap.add_argument("--um-liq", action="store_true", help="also try the (deleted) USDT-M liquidation archive")
    a = ap.parse_args()
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    global UM_LIQ
    UM_LIQ = a.um_liq

    print("fear & greed")
    fng = get_fng()
    liqs = []
    for s in a.symbols:
        L = build(s, start, end, a.outdir, fng, a.skip_liq)
        if not L.empty:
            liqs.append(L)
    if liqs:
        path = os.path.join(a.outdir, "hist", "liquidations.parquet")
        L = pd.concat(liqs, ignore_index=True).sort_values("ts")
        L.to_parquet(path, index=False)
        print(f"\nwrote {path}: {len(L):,} liquidations, {L['ts'].min():%Y-%m-%d} → {L['ts'].max():%Y-%m-%d}")
    print("\ndone. next: python merge.py")


if __name__ == "__main__":
    sys.exit(main())