"""
Dip Finder -- a relative-weakness scanner for your watchlist
============================================================
What it does: for each ticker, it measures how "on sale" the stock is right now
using three classic signals, then ranks them so you can see what's most beaten
down on any given day. It answers "what's on sale right now?" for a watchlist.

HONEST LIMITS (read these):
  * This finds RELATIVE WEAKNESS, not bottoms. "Oversold" things can keep falling.
  * Use it to buy in TRANCHES on weakness, not to nail the exact low.
  * A freshly-IPO'd stock has no history, so RSI/averages are
    meaningless for ~the first few months. Early on, the real "dips" are events:
    the lock-up expiry (~90-180 days post-IPO) and the first earnings reports.

CHANGELOG (Jul 2026 fix-up):
  * Per-ticker DEMO flag -- a failed download no longer hides behind one
    "LIVE" banner. Any row marked DEMO is fake data: ignore it, fix the ticker.
  * RSI now uses Wilder's smoothing so it matches TradingView's RSI.
  * Tickers with too little history (new IPOs) are shown but not ranked or
    signalled -- they get "(too new -- watch events, not signals)".
"""

from datetime import datetime
import random

# ===================== EDIT THIS =====================
WATCHLIST = [
    # Example list only -- replace with your own tickers.
    'SPY', 'QQQ', 'VFV.TO', 'XIU.TO', 'NVDA', 'MSFT',
]

HIGH_LOOKBACK = 63    # trading days (~3 months) to measure the recent high
MA_WINDOW     = 50    # moving-average window
RSI_PERIOD    = 14
MIN_HISTORY   = 70    # bars needed before signals mean anything (~3.5 months)
# =====================================================


def get_closes(ticker, days=160):
    """Live prices via yfinance; if offline, synthetic DEMO data so it still runs.
    Returns (closes, live) -- live=False means THIS ticker's data is fake."""
    try:
        import yfinance as yf, pandas as pd
        df = yf.download(ticker, period="9mo", interval="1d", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):      # single-ticker downloads come back MultiIndexed
            df.columns = df.columns.get_level_values(0)
        s = df['Close'].dropna()
        closes = [float(x) for x in s.values[-days:]]
        if not closes:
            raise ValueError("no data returned")
        return closes, True
    except Exception:
        # ---- demo fallback: random walk with a per-ticker drift, so output varies ----
        random.seed(hash(ticker) % 9999)
        drift = random.uniform(-0.004, 0.004)
        price = random.uniform(20, 300)
        out = []
        for _ in range(days):
            price *= (1 + drift + random.gauss(0, 0.03))
            out.append(max(price, 0.05))
        return out, False


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else sum(vals) / len(vals)


def rsi_wilder(vals, n):
    """Wilder-smoothed RSI -- matches TradingView's built-in RSI."""
    if len(vals) < n + 1:
        return None
    # seed with a simple average of the first n changes...
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = vals[i] - vals[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    # ...then Wilder-smooth across the rest of the series
    for i in range(n + 1, len(vals)):
        d = vals[i] - vals[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def signal(drawdown, r):
    if drawdown <= -25 and r < 35:
        return "DEEP DIP"
    if drawdown <= -15 and r < 45:
        return "DIP"
    if drawdown <= -8:
        return "soft pullback"
    if drawdown > -3 and r > 70:
        return "(near highs)"
    return "-"


rows = []
too_new = []
demo_count = 0
for t in WATCHLIST:
    closes, live = get_closes(t)
    if not live:
        demo_count += 1
    price = closes[-1]

    # not enough history -> show it, but don't pretend the signals mean anything
    if len(closes) < MIN_HISTORY:
        too_new.append((t, price, len(closes), live))
        continue

    recent_high = max(closes[-HIGH_LOOKBACK:])
    drawdown = (price / recent_high - 1) * 100          # % below 3-month high
    vs_ma = (price / sma(closes, MA_WINDOW) - 1) * 100   # % vs 50-day average
    r = rsi_wilder(closes, RSI_PERIOD)
    rows.append((t, price, drawdown, vs_ma, r, signal(drawdown, r), live))

rows.sort(key=lambda x: x[2])   # most below its high first = most "on sale"

print(f"\n=== Dip Finder ({datetime.now():%Y-%m-%d}) ===")
if demo_count == 0:
    print("Prices: LIVE (Yahoo)")
elif demo_count == len(WATCHLIST):
    print("Prices: ALL DEMO DATA (offline -- run at home for real numbers)")
else:
    print(f"Prices: MIXED -- {demo_count} ticker(s) failed to download and are "
          f"marked DEMO below. DO NOT act on DEMO rows; fix the ticker instead.")
print("-" * 78)
print(f"{'Ticker':<9}{'Price':>9}{'vs 3mo high':>13}{'vs 50d avg':>12}{'RSI':>6}   Signal")
for t, p, dd, vm, r, sig, live in rows:
    tag = "" if live else "  [DEMO -- fake data]"
    print(f"{t:<9}{p:>9.2f}{dd:>12.1f}%{vm:>11.1f}%{r:>6.0f}   {sig}{tag}")
for t, p, nbars, live in too_new:
    tag = "" if live else "  [DEMO -- fake data]"
    print(f"{t:<9}{p:>9.2f}{'--':>12}{'--':>11}{'--':>6}   (too new: {nbars} bars -- watch events, not signals){tag}")
print("-" * 78)
print("Most 'on sale' is at the top. DIP/DEEP DIP = candidates to buy a tranche of.")
print("Reminder: oversold can get more oversold -- buy in pieces, don't chase the bottom.")
print("Note: for low-vol index funds even a 'soft pullback' is a meaningful discount.")