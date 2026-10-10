"""
watch.py — read collector.py output and say what the tape looks like right now.

    python watch.py                # uses ./data
    python watch.py --outdir data --symbols BTCUSDT SOLUSDT

Prints one block per symbol, then a verdict line. Paste the whole thing into chat.
Read-only. Never trades, never writes.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os

import pandas as pd

H1_LIQ_PCT_OI = 0.75   # long liqs (1h) as % of OI  — from HYPOTHESES.md, do not tune
H1_DROP_4H = -3.0      # price change over 4h, %
H2_FUND_PCTL = 0.95    # funding percentile over 30d
H2_OI_NEAR_HIGH = 0.98 # OI within 2% of 30d high
H2_LS_PCTL = 0.90      # global long/short percentile over 30d


def load_market(outdir: str, sym: str) -> pd.DataFrame:
    frames = []
    for f in glob.glob(os.path.join(outdir, f"market_{sym}_*.jsonl")):
        rows = []
        with open(f) as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        if rows:
            frames.append(pd.DataFrame(rows))
    for f in glob.glob(os.path.join(outdir, f"{sym}_*.csv.gz")):     # cache.zip format
        with gzip.open(f, "rt") as fh:
            frames.append(pd.read_csv(fh))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
    df = df.sort_values("ts").drop_duplicates("ts").set_index("ts")
    return df.resample("5min").last().ffill(limit=3)


def load_liqs(outdir: str) -> pd.DataFrame:
    rows = []
    for f in glob.glob(os.path.join(outdir, "liquidations_*.jsonl")):
        with open(f) as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    if not rows:
        return pd.DataFrame(columns=["ts", "symbol", "side", "usd"])
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
    return df


def last_days(series, d=30):
    return series[series.index > series.index[-1] - pd.Timedelta(days=d)]


def pct(a, b):
    return (a - b) / b * 100 if b else float("nan")


def report(sym: str, m: pd.DataFrame, liq: pd.DataFrame) -> list[str]:
    flags = []
    if m.empty:
        print(f"\n{sym}: no market data")
        return flags
    now = m.index[-1]
    last = m.iloc[-1]
    days = (now - m.index[0]).days
    print(f"\n{'='*64}\n{sym}   {now:%Y-%m-%d %H:%M} UTC   ({days}d of data)\n{'='*64}")

    def ago(h):
        t = now - pd.Timedelta(hours=h)
        s = m.loc[:t]
        return s.iloc[-1] if len(s) else None

    p4, p24, p7d = ago(4), ago(24), ago(24 * 7)
    print(f"price      {last['last']:,.2f}   4h {pct(last['last'], p4['last']) if p4 is not None else float('nan'):+.2f}%"
          f"   24h {pct(last['last'], p24['last']) if p24 is not None else float('nan'):+.2f}%"
          f"   7d {pct(last['last'], p7d['last']) if p7d is not None else float('nan'):+.2f}%")

    # funding
    f_ann = last["funding"] * 3 * 365 * 100
    f30 = last_days(m["funding"])
    f_pctl = (f30 < last["funding"]).mean() if len(f30) > 50 else float("nan")
    print(f"funding    {last['funding']*100:+.4f}%/8h  ({f_ann:+.1f}% ann.)   30d pctl {f_pctl:.0%}")

    # open interest
    oi30 = last_days(m["oi_usd"])
    oi_hi = oi30.max()
    print(f"OI         ${last['oi_usd']/1e9:,.2f}B   24h {pct(last['oi_usd'], p24['oi_usd']) if p24 is not None else float('nan'):+.1f}%"
          f"   vs 30d high {last['oi_usd']/oi_hi:.0%}")

    # positioning
    ls30 = last_days(m["global_ls"])
    ls_pctl = (ls30 < last["global_ls"]).mean() if len(ls30) > 50 else float("nan")
    tp24 = pct(last["top_pos_ls"], p24["top_pos_ls"]) if p24 is not None else float("nan")
    gl24 = pct(last["global_ls"], p24["global_ls"]) if p24 is not None else float("nan")
    print(f"long/short retail {last['global_ls']:.2f} (30d pctl {ls_pctl:.0%}, 24h {gl24:+.1f}%)"
          f"   top-pos {last['top_pos_ls']:.2f} (24h {tp24:+.1f}%)")
    print(f"taker b/s  {last['taker_ls']:.2f}   F&G {int(last['fng']) if pd.notna(last.get('fng')) else '?'}"
          f"   perp-spot {last.get('perp_spot_basis_pct', float('nan')):+.3f}%")

    # liquidations
    L = liq[liq["symbol"] == sym] if not liq.empty else liq
    l1 = L[L["ts"] > now - pd.Timedelta(hours=1)]
    l24 = L[L["ts"] > now - pd.Timedelta(hours=24)]
    long1 = l1.loc[l1["side"] == "SELL", "usd"].sum()      # longs liquidated = forced sells
    short1 = l1.loc[l1["side"] == "BUY", "usd"].sum()
    long24 = l24.loc[l24["side"] == "SELL", "usd"].sum()
    short24 = l24.loc[l24["side"] == "BUY", "usd"].sum()
    long1_pct = long1 / last["oi_usd"] * 100 if last["oi_usd"] else 0
    print(f"liqs 1h    longs ${long1/1e6:,.2f}M ({long1_pct:.3f}% of OI)  shorts ${short1/1e6:,.2f}M")
    print(f"liqs 24h   longs ${long24/1e6:,.1f}M   shorts ${short24/1e6:,.1f}M   (events: {len(l24)})")
    if L.empty:
        print("           (no liquidation data for this symbol yet)")

    # ── flags ──────────────────────────────────────────────────────────
    drop4 = pct(last["last"], p4["last"]) if p4 is not None else 0
    if long1_pct >= H1_LIQ_PCT_OI and last["funding"] <= 0 and drop4 <= H1_DROP_4H:
        flags.append(f"{sym}: H1 CAPITULATION — long liqs {long1_pct:.2f}% of OI, funding ≤0, 4h {drop4:+.1f}%")
    elif long1_pct >= H1_LIQ_PCT_OI / 2 and drop4 <= H1_DROP_4H / 2:
        flags.append(f"{sym}: partial flush — liqs {long1_pct:.2f}% OI, 4h {drop4:+.1f}%  (watch, not H1)")
    if (days >= 20 and pd.notna(f_pctl) and f_pctl >= H2_FUND_PCTL
            and last["oi_usd"] >= H2_OI_NEAR_HIGH * oi_hi
            and pd.notna(ls_pctl) and ls_pctl >= H2_LS_PCTL):
        flags.append(f"{sym}: H2 CROWDED LONG — hold off on tranches")
    if pd.notna(gl24) and gl24 >= 10 and tp24 <= -10:
        flags.append(f"{sym}: H3 retail longing / top accounts shorting")
    if pd.notna(last.get("fng")) and last["fng"] <= 15:
        flags.append(f"{sym}: Fear & Greed {int(last['fng'])} — extreme fear")
    return flags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="data")
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    a = ap.parse_args()
    liq = load_liqs(a.outdir)
    flags = []
    for s in a.symbols:
        flags += report(s, load_market(a.outdir, s), liq)
    print(f"\n{'='*64}\nVERDICT\n{'='*64}")
    if flags:
        for f in flags:
            print("  * " + f)
    else:
        print("  nothing firing. normal tape.")
    print("\n(30d percentiles need 30 days of data; blank/nan until then)")


if __name__ == "__main__":
    main()