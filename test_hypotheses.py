"""
test_hypotheses.py — run H1/H2/H3 from HYPOTHESES.md against data/merged/

    python test_hypotheses.py
    python test_hypotheses.py --symbols SOLUSDT --shuffles 200

Thresholds are the ones written on 2026-09-08. Do not edit them here.
Each hypothesis prints: events, mean forward return (gross & net of 0.6%),
win rate, t-stat, the 95th percentile of 1,000 shuffled placebo runs, the
unconditional drift over the same horizon, and a verdict.

PASS         = prediction met, beats shuffle 95th pctl, n >= 25
FAIL         = n >= 25 and prediction not met (or loses to shuffle)
INCONCLUSIVE = fewer than 25 non-overlapping events
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

try:                                   # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                      # noqa: BLE001
    pass

COST = 0.6          # round-trip cost, %
MIN_N = 25
H1 = dict(liq_pct_oi=0.75, drop_4h=-3.0, horizon_h=24, target=+1.0, win=0.55)
H1R = dict(pctl=0.99, window_d=90, drop_4h=-3.0, horizon_h=24, target=+1.0, win=0.55)   # H1-rel, registered 2026-09-08
H2 = dict(fund_pctl=0.95, oi_near=0.98, ls_pctl=0.90, horizon_h=48, target=-1.0, low_target=-3.0)
H2B = dict(fund_pctl=0.90, oi_near=0.95, ls_pctl=0.75, horizon_h=48, target=-1.0, low_target=-3.0)   # registered 2026-09-08
H3 = dict(gl_up=10.0, tp_dn=-10.0, horizon_h=24, target=-0.5, win=0.55)


def _gap_ok(idx: pd.DatetimeIndex, h: int, step: pd.Timedelta) -> pd.Series:
    """True where the bar h steps ahead really is h*step later (no data gap in between)."""
    ahead = pd.Series(idx, index=idx).shift(-h)
    return (ahead - idx) == h * step


def fwd_ret(px: pd.Series, h: int) -> pd.Series:
    step = pd.Series(px.index).diff().mode()[0]
    r = (px.shift(-h) / px - 1) * 100
    return r.where(_gap_ok(px.index, h, step))


def fwd_low(px: pd.Series, h: int) -> pd.Series:
    step = pd.Series(px.index).diff().mode()[0]
    lo = (px[::-1].rolling(h, min_periods=1).min()[::-1].shift(-1) / px - 1) * 100
    return lo.where(_gap_ok(px.index, h, step))


def dedupe(idx: pd.DatetimeIndex, horizon: pd.Timedelta) -> pd.DatetimeIndex:
    keep, last = [], None
    for t in idx:
        if last is None or t - last >= horizon:
            keep.append(t)
            last = t
    return pd.DatetimeIndex(keep)


def shuffle_baseline(ret: pd.Series, n_events: int, shuffles: int, rng) -> float:
    valid = ret.dropna().values
    if len(valid) < n_events * 2:
        return float("nan")
    means = [rng.choice(valid, n_events, replace=False).mean() for _ in range(shuffles)]
    return float(np.percentile(means, 95))


def evaluate(name, sym, events, ret, direction, target, win_target, shuffles, rng, extra=""):
    """direction +1 = long prediction, -1 = short prediction."""
    r = ret.reindex(events).dropna()
    n = len(r)
    uncond = ret.dropna().mean()
    if n == 0:
        print(f"  {name} {sym}: 0 events  → INCONCLUSIVE")
        return
    gross = r.mean() * direction
    net = gross - COST
    wins = ((r * direction) > 0).mean()
    t = r.mean() / (r.std(ddof=1) / np.sqrt(n)) if n > 1 and r.std() > 0 else float("nan")
    p95 = shuffle_baseline(ret * direction, n, shuffles, rng)
    met = (net >= abs(target) if direction == 1 else gross >= abs(target)) and (wins >= win_target if win_target else True)
    beats = np.isnan(p95) or gross > p95
    if n < MIN_N:
        verdict = "INCONCLUSIVE"
    elif met and beats:
        verdict = "PASS"
    else:
        verdict = "FAIL"
    side = "long" if direction == 1 else "short"
    print(f"  {name} {sym}: n={n}  {side} mean gross {gross:+.2f}%  net {net:+.2f}%  win {wins:.0%}  "
          f"t={t:.2f}  shuffle95 {p95:+.2f}%  drift {uncond*direction:+.2f}%{extra}  → {verdict}")


def load(outdir, sym):
    p = os.path.join(outdir, "merged", f"{sym}.parquet")
    if not os.path.exists(p):
        return None
    df = pd.read_parquet(p)
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def hourly(df):
    h = df.resample("1h").last()
    if "kl_low" in df:
        h["low_1h"] = df["kl_low"].resample("1h").min()
    return h


def run_h1(sym, df, liq, shuffles, rng, venue="um"):
    name = "H1" if venue == "um" else "H1-cm"
    oi_col = "oi_usd" if venue == "um" else "oi_usd_cm"
    if liq is None or liq.empty or oi_col not in df or df[oi_col].isna().all():
        print(f"  {name} {sym}: no {venue} liquidation/OI data → INCONCLUSIVE")
        return
    A = liq[(liq["symbol"] == sym) & (liq.get("venue", "um") == venue)]
    L = A[A["side"] == "SELL"]
    if L.empty:
        print(f"  {name} {sym}: no {venue} long-liquidation rows → INCONCLUSIVE")
        return
    g = df.copy()
    liq5 = L.set_index("ts")["usd"].resample("5min").sum().reindex(g.index, fill_value=0.0)
    # only bins inside days that actually have liquidation coverage (either side)
    covered_days = set(A["ts"].dt.floor("D"))
    covered = pd.Series([d in covered_days for d in g.index.floor("D")], index=g.index)
    g["liq_long_1h"] = liq5.rolling(12, min_periods=1).sum()
    g["liq_pct_oi"] = g["liq_long_1h"] / g[oi_col] * 100
    g["ret_4h"] = (g["last"] / g["last"].shift(48) - 1) * 100
    H = H1["horizon_h"] * 12
    ret = fwd_ret(g["last"], H)
    cond = covered & (g["liq_pct_oi"] >= H1["liq_pct_oi"]) & (g["funding"] <= 0) & (g["ret_4h"] <= H1["drop_4h"])
    ev = dedupe(g.index[cond.fillna(False)], pd.Timedelta(hours=H1["horizon_h"]))
    evaluate(name, sym, ev, ret[covered], +1, H1["target"], H1["win"], shuffles, rng,
             extra=f"  (covered {covered.sum()/288:.0f}d)")


def run_h1_rel(sym, df, liq, shuffles, rng, venue="cm"):
    """H1 with the liquidation burst measured against the feed's own trailing distribution
    (Binance's feed is a 1/sec-per-symbol subsample, so absolute %-of-OI thresholds are untestable)."""
    name = f"H1-rel({venue})"
    if liq is None or liq.empty:
        print(f"  {name} {sym}: no liquidation data → INCONCLUSIVE")
        return
    A = liq[(liq["symbol"] == sym) & (liq.get("venue", "um") == venue)]
    L = A[A["side"] == "SELL"]
    if L.empty:
        print(f"  {name} {sym}: no {venue} long-liquidation rows → INCONCLUSIVE")
        return
    g = df.copy()
    liq5 = L.set_index("ts")["usd"].resample("5min").sum().reindex(g.index, fill_value=0.0)
    covered_days = set(A["ts"].dt.floor("D"))
    covered = pd.Series([d in covered_days for d in g.index.floor("D")], index=g.index)
    g["liq_long_1h"] = liq5.rolling(12, min_periods=1).sum()
    W = H1R["window_d"] * 288
    thr = g["liq_long_1h"].where(covered).rolling(W, min_periods=W // 3).quantile(H1R["pctl"])
    g["ret_4h"] = (g["last"] / g["last"].shift(48) - 1) * 100
    H = H1R["horizon_h"] * 12
    ret = fwd_ret(g["last"], H)
    cond = covered & (g["liq_long_1h"] >= thr) & (g["liq_long_1h"] > 0) & (g["funding"] <= 0) & (g["ret_4h"] <= H1R["drop_4h"])
    ev = dedupe(g.index[cond.fillna(False)], pd.Timedelta(hours=H1R["horizon_h"]))
    evaluate(name, sym, ev, ret[covered], +1, H1R["target"], H1R["win"], shuffles, rng,
             extra=f"  (covered {covered.sum()/288:.0f}d)")


def run_h2(sym, df, shuffles, rng, P=H2, name="H2"):
    h = hourly(df)
    W = 24 * 30
    h["f_pctl"] = h["funding"].rolling(W, min_periods=W // 2).rank(pct=True)
    h["oi_hi"] = h["oi_usd"].rolling(W, min_periods=W // 2).max()
    h["ls_q"] = h["global_ls"].rolling(W, min_periods=W // 2).quantile(P["ls_pctl"])
    H = P["horizon_h"]
    ret = fwd_ret(h["last"], H)
    low_src = h["low_1h"] if "low_1h" in h else h["last"]
    low = fwd_low(low_src, H)
    cond = (h["f_pctl"] >= P["fund_pctl"]) & (h["oi_usd"] >= P["oi_near"] * h["oi_hi"]) & (h["global_ls"] >= h["ls_q"])
    ev = dedupe(h.index[cond.fillna(False)], pd.Timedelta(hours=H))
    lo = low.reindex(ev).dropna().mean()
    evaluate(name, sym, ev, ret, -1, P["target"], None, shuffles, rng,
             extra=f"  fwd-low mean {lo:+.2f}% (target ≤ {P['low_target']})")


def run_h3(sym, df, shuffles, rng):
    h = hourly(df)
    h["gl_24"] = (h["global_ls"] / h["global_ls"].shift(24) - 1) * 100
    h["tp_24"] = (h["top_pos_ls"] / h["top_pos_ls"].shift(24) - 1) * 100
    H = H3["horizon_h"]
    ret = fwd_ret(h["last"], H)
    cond = (h["gl_24"] >= H3["gl_up"]) & (h["tp_24"] <= H3["tp_dn"])
    ev = dedupe(h.index[cond.fillna(False)], pd.Timedelta(hours=H))
    evaluate("H3", sym, ev, ret, -1, H3["target"], H3["win"], shuffles, rng)
    # symmetric, reported separately, does not rescue the primary
    cond_s = (h["gl_24"] <= -H3["gl_up"]) & (h["tp_24"] >= -H3["tp_dn"])
    ev_s = dedupe(h.index[cond_s.fillna(False)], pd.Timedelta(hours=H))
    evaluate("H3-sym", sym, ev_s, ret, +1, H3["target"], H3["win"], shuffles, rng)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="data")
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    ap.add_argument("--shuffles", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=20260908)
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    lp = os.path.join(a.outdir, "merged", "liquidations.parquet")
    liq = pd.read_parquet(lp) if os.path.exists(lp) else None
    if liq is not None:
        liq["ts"] = pd.to_datetime(liq["ts"], utc=True)
        if "venue" not in liq:
            liq["venue"] = "um"

    print(f"cost {COST}%  min events {MIN_N}  shuffles {a.shuffles}\n")
    for sym in a.symbols:
        df = load(a.outdir, sym)
        if df is None:
            print(f"{sym}: no merged data (run merge.py)")
            continue
        need = [c for c in ("last", "oi_usd", "funding", "global_ls", "top_pos_ls") if c not in df]
        if need:
            print(f"{sym}: missing columns {need}")
            continue
        bh = (df["last"].dropna().iloc[-1] / df["last"].dropna().iloc[0] - 1) * 100
        print(f"{sym}  {df.index.min():%Y-%m-%d} → {df.index.max():%Y-%m-%d}   buy&hold {bh:+.0f}%")
        run_h1(sym, df, liq, a.shuffles, rng, "um")
        run_h1(sym, df, liq, a.shuffles, rng, "cm")
        run_h1_rel(sym, df, liq, a.shuffles, rng, "cm")
        run_h2(sym, df, a.shuffles, rng)
        run_h2(sym, df, a.shuffles, rng, H2B, "H2b")
        run_h3(sym, df, a.shuffles, rng)
        print()
    print("A hypothesis counts only if it PASSES on 2 of 3 symbols. See HYPOTHESES.md.")


if __name__ == "__main__":
    main()