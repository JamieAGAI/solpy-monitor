"""
merge.py — combine data/hist/ (backfill) with the live collector's jsonl into data/merged/

    python merge.py
    python merge.py --outdir data --symbols BTCUSDT SOLUSDT

Rules:
  - both sources are put on the same 5-min UTC grid
  - where they overlap, the live collector wins (it has funding/basis/book columns
    the archive doesn't)
  - liquidations: archive rows + live jsonl rows, de-duplicated
Output:
  data/merged/<SYMBOL>.parquet
  data/merged/liquidations.parquet
  data/merged/README.txt   (coverage report — read this)
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys

import pandas as pd

try:                                   # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                      # noqa: BLE001
    pass


def read_jsonl(files):
    rows = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return pd.DataFrame(rows)


def load_live(outdir: str, sym: str) -> pd.DataFrame:
    frames = []
    j = read_jsonl(glob.glob(os.path.join(outdir, f"market_{sym}_*.jsonl")))
    if not j.empty:
        frames.append(j)
    for f in glob.glob(os.path.join(outdir, f"{sym}_*.csv.gz")):      # older cache format
        with gzip.open(f, "rt") as fh:
            frames.append(pd.read_csv(fh))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
    df = df.sort_values("ts").drop_duplicates("ts").set_index("ts")
    df = df.select_dtypes(exclude="object")            # drop text cols like symbol
    df = df.resample("5min").last()
    df["source"] = "live"
    return df


def coverage(df: pd.DataFrame, col: str) -> str:
    s = df[col].dropna()
    if s.empty:
        return f"  {col:<16} none"
    return f"  {col:<16} {s.index.min():%Y-%m-%d} → {s.index.max():%Y-%m-%d}   {len(s):>9,} rows"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="data")
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    a = ap.parse_args()
    hist_dir, merged = os.path.join(a.outdir, "hist"), os.path.join(a.outdir, "merged")
    os.makedirs(merged, exist_ok=True)
    report = []

    for sym in a.symbols:
        hp = os.path.join(hist_dir, f"{sym}.parquet")
        hist = pd.read_parquet(hp) if os.path.exists(hp) else pd.DataFrame()
        live = load_live(a.outdir, sym)
        if hist.empty and live.empty:
            print(f"{sym}: nothing to merge")
            continue
        # live wins on overlap: combine_first fills live's NaNs from hist
        if not hist.empty and not live.empty:
            df = live.combine_first(hist)
            df.loc[live.index, "source"] = "live"
        else:
            df = live if hist.empty else hist
        df = df.sort_index()
        df = df[~df.index.duplicated()]
        if "oi_usd" in df and "oi" in df and "last" in df:
            df["oi_usd"] = df["oi_usd"].fillna(df["oi"] * df["last"])
        df.to_parquet(os.path.join(merged, f"{sym}.parquet"))

        lines = [f"{sym}: {len(df):,} rows  {df.index.min():%Y-%m-%d} → {df.index.max():%Y-%m-%d}",
                 f"  hist rows {int((df['source']=='hist').sum()):,}   live rows {int((df['source']=='live').sum()):,}"]
        for c in ("last", "oi_usd", "global_ls", "top_pos_ls", "taker_ls", "funding", "fng", "imb_01"):
            if c in df:
                lines.append(coverage(df, c))
        report += lines + [""]
        print("\n".join(lines) + "\n")

    # liquidations
    parts = []
    lp = os.path.join(hist_dir, "liquidations.parquet")
    if os.path.exists(lp):
        parts.append(pd.read_parquet(lp))
    live_liq = read_jsonl(glob.glob(os.path.join(a.outdir, "liquidations_*.jsonl")))
    if not live_liq.empty:
        live_liq["ts"] = pd.to_datetime(live_liq["ts"], utc=True, format="ISO8601")
        live_liq["venue"] = "um"
        parts.append(live_liq[["ts", "symbol", "side", "price", "qty", "usd", "venue"]])
    if parts:
        L = pd.concat(parts, ignore_index=True)
        L = L[L["symbol"].isin(a.symbols)].sort_values("ts")
        if "venue" not in L:
            L["venue"] = "um"
        L["venue"] = L["venue"].fillna("um")
        L = L.drop_duplicates(["ts", "symbol", "side", "price", "qty", "venue"])
        L.to_parquet(os.path.join(merged, "liquidations.parquet"), index=False)
        per = L.groupby(["symbol", "venue"])["ts"].agg(["min", "max", "count"])
        lines = ["liquidations:"] + [f"  {s:<10} {v:<3} {r['min']:%Y-%m-%d} → {r['max']:%Y-%m-%d}   {int(r['count']):>9,} events"
                                     for (s, v), r in per.iterrows()]
        # find the gap (archive ended 2024-03-31, live began when the collector started)
        for s in a.symbols:
            t = L.loc[L["symbol"] == s, "ts"].sort_values()
            if len(t) > 1:
                gaps = t.diff()
                g = gaps.idxmax()
                if gaps.max() > pd.Timedelta(days=7):
                    lines.append(f"  {s:<10} GAP {t.shift().loc[g]:%Y-%m-%d} → {t.loc[g]:%Y-%m-%d}  "
                                 f"({gaps.max().days}d, no liquidation data — H1 cannot be tested here)")
        report += lines
        print("\n".join(lines))

    with open(os.path.join(merged, "README.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(report))
    print(f"\ncoverage written to {merged}/README.txt\nnext: python test_hypotheses.py")


if __name__ == "__main__":
    main()