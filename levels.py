"""
levels.py - take-profit ladders from price history.

    python levels.py                 # every coin in levels_config.json
    python levels.py RENDER HNT      # just these
    python levels.py RENDER --refresh   # ignore the 12h cache

Finds where sellers showed up before (old range floors, swing highs, heavy-volume
zones, Fibonacci retracements of the last big fall), clusters them into "walls",
and suggests a 3-rung ladder that sells just UNDER each wall, plus a runner.

History tells you where a rally is likely to stall. It does NOT tell you whether
price gets there. Read-only. Never trades.

First run creates levels_config.json next to this file - put your cost and
token count in it for each coin you hold.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

try:                                    # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                       # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "levels_config.json")
CACHE = os.path.join(HERE, "data", "levels_cache")
S = requests.Session()
S.headers["User-Agent"] = "levels/1.0"

DEFAULT_CONFIG = {
    "usd_cad": None,           # null = fetch live rate; or set e.g. 1.38
    "ladder": {
        "rungs": 3,            # rungs with a target; the last quarter is the runner
        "first_min_x_cost": 1.75,   # first rung at least this multiple of your cost
        "spacing": 1.35,       # each rung at least this multiple of the previous
        "under_wall_pct": 3,   # sell this % below the bottom of each wall
    },
    "coins": {
        "RENDER": {"coingecko": "render-token",
                   "binance": ["RENDERUSDT", "RNDRUSDT"], "bybit": ["RENDERUSDT", "RNDRUSDT"],
                   "coinbase": ["RENDER-USD", "RNDR-USD"],
                   "cost_cad": None, "qty": None, "supply": None, "note": ""},
        "HNT":    {"coingecko": "helium",
                   "binance": ["HNTUSDT"], "bybit": ["HNTUSDT"], "coinbase": ["HNT-USD"],
                   "cost_cad": None, "qty": None, "supply": None, "note": ""},
        "GEOD":   {"coingecko": "geodnet",
                   "binance": ["GEODUSDT"], "bybit": ["GEODUSDT"], "coinbase": ["GEOD-USD"],
                   "cost_cad": None, "qty": None, "supply": None, "note": ""},
        "GRASS":  {"coingecko": "grass",
                   "binance": ["GRASSUSDT"], "bybit": ["GRASSUSDT"], "coinbase": ["GRASS-USD"],
                   "cost_cad": None, "qty": None, "supply": None,
                   "note": "Monthly unlocks continue to 2028 (~3% of supply each). Market caps use CoinGecko's circulating supply."},
        "JUP":    {"coingecko": "jupiter-exchange-solana",
                   "binance": ["JUPUSDT"], "bybit": ["JUPUSDT"], "coinbase": ["JUP-USD"],
                   "cost_cad": None, "qty": None, "supply": None, "note": ""},
    },
}


# ── network ────────────────────────────────────────────────────────────
def _get(url, params=None):
    for i in range(3):
        try:
            r = S.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (400, 404):
                return None
        except (requests.RequestException, ValueError):
            pass
        time.sleep(1 + i)
    return None


def _frame(rows, cols, unit):
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=cols)
    for c in ("open", "high", "low", "close", "qvol"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = pd.to_datetime(pd.to_numeric(df["t"]), unit=unit, utc=True).dt.normalize()
    return df[["date", "open", "high", "low", "close", "qvol"]].dropna(subset=["high", "low", "close"])


def fetch_binance(sym):
    rows, start = [], int(datetime(2017, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    while True:
        d = _get("https://data-api.binance.vision/api/v3/klines",
                 {"symbol": sym, "interval": "1d", "startTime": start, "limit": 1000})
        if not d or not isinstance(d, list):
            break
        rows += [r[:8] for r in d]
        if len(d) < 1000:
            break
        start = int(d[-1][0]) + 86_400_000
        time.sleep(0.2)
    return _frame(rows, ["t", "open", "high", "low", "close", "vol", "ct", "qvol"], "ms")


def fetch_bybit(sym):
    rows, end = [], int(time.time() * 1000)
    while True:
        d = _get("https://api.bybit.com/v5/market/kline",
                 {"category": "spot", "symbol": sym, "interval": "D", "end": end, "limit": 1000})
        lst = ((d or {}).get("result") or {}).get("list") or []
        if not lst:
            break
        rows += lst
        if len(lst) < 1000:
            break
        end = int(lst[-1][0]) - 1
        time.sleep(0.2)
    return _frame(rows, ["t", "open", "high", "low", "close", "vol", "qvol"], "ms")


def fetch_coinbase(product):
    rows, end = [], datetime.now(timezone.utc)
    while end > datetime(2017, 1, 1, tzinfo=timezone.utc):
        start = end - timedelta(days=299)
        d = _get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                 {"granularity": 86400, "start": start.isoformat(), "end": end.isoformat()})
        if not d or not isinstance(d, list):
            break
        rows += d
        end = start
        time.sleep(0.2)
    # coinbase rows are [time, low, high, open, close, base_volume]; quote volume = base * close
    rows = [list(r[:6]) + [float(r[5]) * float(r[4])] for r in rows]
    return _frame(rows, ["t", "low", "high", "open", "close", "vol", "qvol"], "s")


def fetch_mexc(sym):
    rows, start = [], int(datetime(2018, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    while True:
        d = _get("https://api.mexc.com/api/v3/klines",
                 {"symbol": sym, "interval": "1d", "startTime": start, "limit": 1000})
        if not d or not isinstance(d, list):
            break
        rows += [r[:8] for r in d]
        if len(d) < 1000:
            break
        start = int(d[-1][0]) + 86_400_000
        time.sleep(0.2)
    return _frame(rows, ["t", "open", "high", "low", "close", "vol", "ct", "qvol"], "ms")


def fetch_gate(pair):
    # gate rows: [time_s, quote_volume, close, high, low, open, base_volume, closed]; max 1000 per call
    rows, t0 = [], int(datetime(2018, 1, 1, tzinfo=timezone.utc).timestamp())
    now = int(time.time())
    while t0 < now:
        t1 = min(t0 + 900 * 86400, now)
        d = _get("https://api.gateio.ws/api/v4/spot/candlesticks",
                 {"currency_pair": pair, "interval": "1d", "from": t0, "to": t1})
        if d is None:
            break                       # unknown pair
        if isinstance(d, list):
            rows += [[r[0], r[5], r[3], r[4], r[2], r[6], r[1]] for r in d]
        t0 = t1 + 1
        time.sleep(0.2)
    return _frame(rows, ["t", "open", "high", "low", "close", "vol", "qvol"], "s")


def fetch_coingecko(ids):
    d = _get("https://api.coingecko.com/api/v3/coins/markets",
             {"vs_currency": "usd", "ids": ",".join(ids)})
    return {x["id"]: x for x in d} if isinstance(d, list) else {}


def fetch_usd_cad():
    for url, params, path in (
        ("https://api.frankfurter.dev/v1/latest", {"base": "USD", "symbols": "CAD"}, ("rates", "CAD")),
        ("https://api.frankfurter.app/latest", {"from": "USD", "to": "CAD"}, ("rates", "CAD")),
    ):
        d = _get(url, params)
        try:
            return float(d[path[0]][path[1]])
        except (TypeError, KeyError, ValueError):
            continue
    return None


FETCHERS = {"binance": fetch_binance, "bybit": fetch_bybit, "coinbase": fetch_coinbase,
            "mexc": fetch_mexc, "gate": fetch_gate}


def symbols_for(c, src):
    """MEXC and Gate default to the Binance tickers, so older config files work unchanged."""
    if c.get(src) is not None:
        return c[src]
    base = c.get("binance") or c.get("bybit") or []
    if src == "mexc":
        return base
    if src == "gate":
        return [b[:-4] + "_USDT" for b in base if b.endswith("USDT")]
    return []


# ── history ────────────────────────────────────────────────────────────
def stitch(frames):
    """Join renamed tickers (RNDR -> RENDER). On overlapping days keep the busier one."""
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True).sort_values(["date", "qvol"])
    return df.drop_duplicates("date", keep="last").set_index("date").sort_index()


def load_history(coin, c, refresh):
    os.makedirs(CACHE, exist_ok=True)
    got = {}
    for src, fn in FETCHERS.items():
        syms = symbols_for(c, src)
        if not syms:
            continue
        path = os.path.join(CACHE, f"{coin}_{src}.csv")
        fresh = os.path.exists(path) and time.time() - os.path.getmtime(path) < 12 * 3600
        if fresh and not refresh:
            df = pd.read_csv(path)
            if df.empty or "close" not in df:
                df = pd.DataFrame()
            else:
                df["date"] = pd.to_datetime(df["date"], utc=True)
                df = df.set_index("date").sort_index()
        else:
            df = stitch([fn(s) for s in syms])
            (df.reset_index() if not df.empty else pd.DataFrame(columns=["date"])).to_csv(path, index=False)
        if not df.empty:
            got[src] = df
    if not got:
        return pd.DataFrame(), None
    now = pd.Timestamp.now(tz="UTC").normalize()
    recent = {k: v for k, v in got.items() if (now - v.index.max()).days <= 4} or got
    best = min(recent, key=lambda k: (recent[k].index.min(), -len(recent[k])))
    return recent[best], best


# ── analysis ───────────────────────────────────────────────────────────
def volume_profile(df, bins=120):
    lo = np.log(df["low"].clip(lower=1e-12).values)
    hi = np.log(df["high"].clip(lower=1e-12).values)
    hi = np.maximum(hi, lo + 1e-9)
    edges = np.linspace(lo.min(), hi.max(), bins + 1)
    vol = df["qvol"].fillna(0).values
    prof = np.zeros(bins)
    for l, h, v in zip(lo, hi, vol):
        ov = np.clip(np.minimum(edges[1:], h) - np.maximum(edges[:-1], l), 0, None)
        prof += v * ov / (h - l)
    mids = np.exp((edges[:-1] + edges[1:]) / 2)
    return mids, prof


def candidates(df, price, top, top_date):
    after = df.loc[df.index >= top_date] if top_date is not None else df
    if after.empty:
        after = df
    bottom = after["close"].min()          # closes, not wicks: a 5-minute flash crash isn't a price you could buy
    bottom_date = after["close"].idxmin()
    out = []
    for r in (0.236, 0.382, 0.5, 0.618, 0.786):
        out.append((bottom + (top - bottom) * r, 1.0, "fib", f"fib {r*100:.1f}%"))
    out.append((top, 1.0, "ath", f"cycle high {pd.Timestamp(top_date):%b %Y}" if top_date is not None else "cycle high"))

    w, span = 10, 30
    hmax = df["high"].rolling(2 * w + 1, center=True, min_periods=w).max()
    lmin = df["low"].rolling(2 * w + 1, center=True, min_periods=w).min()
    around_lo = df["low"].rolling(2 * span + 1, center=True, min_periods=span).min()
    around_hi = df["high"].rolling(2 * span + 1, center=True, min_periods=span).max()
    for d, row in df[(df["high"] == hmax) & (df["high"] >= 1.2 * around_lo)].iterrows():
        out.append((row["high"], 1.0, "pivot", f"swing high {d:%b %Y}"))
    for d, row in df[(df["low"] == lmin) & (around_hi >= 1.2 * df["low"])].iterrows():
        out.append((row["low"], 1.2, "floor", f"old floor {d:%b %Y}"))   # broken support -> resistance

    mids, prof = volume_profile(df)
    thr = np.percentile(prof, 70)
    for i in range(1, len(prof) - 1):
        if prof[i] >= prof[i - 1] and prof[i] >= prof[i + 1] and prof[i] >= thr:
            out.append((mids[i], 1.0 + prof[i] / prof.max(), "volume", "heavy trading"))

    out = [x for x in out if price * 1.02 < x[0] <= top * 1.05]
    return sorted(out), bottom, bottom_date


def cluster(cands, gap=0.045, max_width=0.10):
    zones = []
    for p, w, kind, label in cands:
        if zones and np.log(p / zones[-1]["hi"]) <= gap and np.log(p / zones[-1]["lo"]) <= max_width:
            z = zones[-1]
            z["hi"] = p
        else:
            z = {"lo": p, "hi": p, "kinds": {}, "labels": []}
            zones.append(z)
        z["kinds"][kind] = z["kinds"].get(kind, 0) + w
        if label not in z["labels"]:
            z["labels"].append(label)
    for z in zones:     # cap each method's say so ten swing highs don't outvote everything
        z["score"] = sum(min(v, 2.0) for v in z["kinds"].values())
    return zones


def ladder(zones, price, cost_usd, L):
    under = L["under_wall_pct"] / 100
    floor = max(price * 1.25, cost_usd * L["first_min_x_cost"]) if cost_usd else price * 1.5
    rungs = []
    for _ in range(L["rungs"]):
        pool = [z for z in zones if z["lo"] * (1 - under) >= floor]
        if not pool:
            break
        window = [z for z in pool if z["lo"] <= floor * L["spacing"]]
        z = max(window, key=lambda x: x["score"]) if window else pool[0]
        sell = z["lo"] * (1 - under)
        rungs.append((sell, z))
        floor = sell * L["spacing"]
    # price is near or above everything it has traded at: no walls to sell under, so fall back
    # to plain multiples (of your cost if known, else of today's price) and let the mcap column judge them
    base = cost_usd or price
    for m in L.get("fallback_multiples", [2.0, 3.0, 4.5, 6.0]):
        if len(rungs) >= L["rungs"]:
            break
        target = base * m
        if target >= floor:
            rungs.append((target, None))
            floor = target * L["spacing"]
    return rungs


# ── report ─────────────────────────────────────────────────────────────
def stars(s):
    return "*" * max(1, min(5, int(round(s))))


def money(x):
    if x >= 1e9:
        return f"${x/1e9:.2f}B"
    if x >= 1e6:
        return f"${x/1e6:.0f}M"
    return f"${x:,.0f}"


def px(x):
    return f"{x:,.4f}" if x < 1 else f"{x:,.2f}"


def report(coin, c, df, src, fx, cg, L):
    price = float(df["close"].iloc[-1])
    hist_top, hist_top_date = float(df["high"].max()), df["high"].idxmax()
    top, top_date, note = hist_top, hist_top_date, None
    if cg.get("ath") and cg["ath"] > hist_top * 1.15:
        top = float(cg["ath"])
        top_date = pd.Timestamp(cg["ath_date"]).tz_convert("UTC") if cg.get("ath_date") else hist_top_date
        note = (f"exchange history starts {df.index.min():%Y-%m-%d} and misses the real high "
                f"(${px(top)}, {top_date:%b %Y}); fibs use the real high")
        if top_date < df.index.min():
            top_date = df.index.min()

    cands, bottom, bottom_date = candidates(df, price, top, top_date)
    zones = cluster(cands)
    cost_usd = (c["cost_cad"] / c["qty"]) / fx if c.get("cost_cad") and c.get("qty") else None
    rungs = ladder(zones, price, cost_usd, L)
    supply = c.get("supply") or cg.get("circulating_supply")
    full = cg.get("max_supply") or cg.get("total_supply")

    print(f"\n{'=' * 78}\n{coin}   ${px(price)} USD  (CA${px(price * fx)})   "
          f"history {df.index.min():%Y-%m-%d} -> {df.index.max():%Y-%m-%d} via {src}\n{'=' * 78}")
    print(f"cycle high ${px(top)} ({pd.Timestamp(top_date):%b %Y})   low since ${px(bottom)} "
          f"({bottom_date:%b %Y})   now {price / bottom:.1f}x off the low, {1 - price / top:.0%} below the high")
    if note:
        print(f"note: {note}")
    if c.get("note"):
        print(f"note: {c['note']}")
    if cost_usd:
        val = c["qty"] * price * fx
        print(f"position {c['qty']:,.2f} @ CA${px(c['cost_cad'] / c['qty'])}   cost CA${c['cost_cad']:,.2f}   "
              f"now CA${val:,.2f} ({val / c['cost_cad'] - 1:+.0%})")

    print(f"\nWALLS ABOVE PRICE{'':<26}strength  why")
    for z in zones[:10]:
        rng = f"${px(z['lo'])}" if z["hi"] / z["lo"] < 1.005 else f"${px(z['lo'])}-{px(z['hi'])}"
        cad = f"CA${px(z['lo'] * fx)}"
        why = ", ".join(z["labels"][:3]) + ("..." if len(z["labels"]) > 3 else "")
        mult = f"{z['lo'] / price:.1f}x"
        print(f"  {rng:<20} {cad:<14}  {mult:<6} {stars(z['score']):<8} {why}")
    if not zones:
        print("  (no history above current price)")

    print(f"\nSUGGESTED LADDER - sell {L['under_wall_pct']}% under each wall")
    hdr = f"  {'rung':<7}{'USD':>10}{'CAD':>11}{'x now':>8}"
    if cost_usd:
        hdr += f"{'x cost':>8}{'tokens':>10}{'proceeds':>12}"
    if supply:
        hdr += f"{'mcap':>10}"
    if full:
        hdr += f"{'FDV':>10}"
    print(hdr)
    part = (c["qty"] / (L["rungs"] + 1)) if cost_usd else None
    for i, (sell, z) in enumerate(rungs, 1):
        tag = "" if z is not None else ("   <- no history here: " + ("multiple of your cost" if cost_usd else "multiple of today's price"))
        line = f"  {i:<7}{'$' + px(sell):>10}{'CA$' + px(sell * fx):>11}{sell / price:>7.1f}x"
        if cost_usd:
            line += f"{sell / cost_usd:>7.1f}x{part:>10,.2f}{'CA$' + format(part * sell * fx, ',.0f'):>12}"
        if supply:
            line += f"{money(sell * supply):>10}"
        if full:
            line += f"{money(sell * full):>10}"
        print(line + tag)
    rest = [z for z in zones if not rungs or z["lo"] > rungs[-1][0] * 1.1]
    if any(z is None for _, z in rungs):
        print("  history runs out below these targets - judge them by the mcap column, not the chart")
    walls = ", ".join(f"${px(z['lo'])}" for z in sorted(rest, key=lambda z: -z["score"])[:3])
    print(f"  runner  last {100 // (L['rungs'] + 1)}% - no target. next walls: {walls or 'none in history'}")
    if supply and cg.get("market_cap") and rungs:
        print(f"  market cap today {money(cg['market_cap'])}; top rung needs {money(rungs[-1][0] * supply)} at current supply")


def main():
    ap = argparse.ArgumentParser(description="Take-profit ladders from price history")
    ap.add_argument("coins", nargs="*", help="coins from levels_config.json (default: all)")
    ap.add_argument("--refresh", action="store_true", help="ignore the 12h cache")
    a = ap.parse_args()

    if not os.path.exists(CONFIG):
        with open(CONFIG, "w", encoding="utf-8") as fh:
            json.dump(DEFAULT_CONFIG, fh, indent=2)
        print(f"created {CONFIG} - add cost_cad and qty for each coin you hold.")
    with open(CONFIG, encoding="utf-8") as fh:
        cfg = json.load(fh)
    L = {**DEFAULT_CONFIG["ladder"], **cfg.get("ladder", {})}
    coins = [x.upper() for x in a.coins] or list(cfg["coins"])

    fx = cfg.get("usd_cad") or fetch_usd_cad()
    if not fx:
        fx = 1.38
        print("could not fetch USD/CAD - using 1.38. Set usd_cad in levels_config.json to fix.")
    cg_all = fetch_coingecko([cfg["coins"][k]["coingecko"] for k in coins
                              if k in cfg["coins"] and cfg["coins"][k].get("coingecko")])
    print(f"USD/CAD {fx:.4f}" + ("" if cg_all else "   (CoinGecko unavailable - no market caps)"))

    for coin in coins:
        c = cfg["coins"].get(coin)
        if not c:
            print(f"\n{coin}: not in levels_config.json")
            continue
        df, src = load_history(coin, c, a.refresh)
        if df.empty or len(df) < 60:
            print(f"\n{coin}: couldn't get enough price history from Binance, Bybit or Coinbase")
            continue
        report(coin, c, df, src, fx, cg_all.get(c.get("coingecko"), {}), L)
    print("\nWalls are where sellers showed up before. They say where a rally may stall, not whether it gets there.")


if __name__ == "__main__":
    main()