"""
Crypto Dip Finder -- relative-weakness scanner for the a crypto watchlist
=============================================================================
Companion to dip_finder.py (equities). Same idea, crypto-calibrated:
  - Pulls daily closes from BINANCE public API (no key needed -- same data
    source as btcregime.py / solregime.py).
  - Anything not on Binance (GEOD, NOS) falls back to CoinGecko's free API.
  - Thresholds are harsher than the equity version: a -25% drawdown is a
    Tuesday for TAO, so DEEP DIP here means -40%+ with washed-out RSI.
  - RSI uses Wilder's smoothing (the equity version's simple average runs
    hot on fast moves; matters more on crypto vol).
  - Prints a BTC regime banner (price vs 200d MA) -- see btcregime.py for the real regime filter.

HONEST LIMITS (same as before, more so):
  * Finds RELATIVE WEAKNESS, not bottoms. Crypto "oversold" can halve again.
  * Buy tranches on red weeks only -- this tells you WHICH name, not WHEN safe.
  * Small-cap liquidity is thin -- never size beyond what one day's volume exits.
"""

import json
import random
import urllib.request
from datetime import datetime

# ===================== EDIT THIS =====================
# (label, binance_symbol or None, coingecko_id, tier, note)
WATCHLIST = [
    # Example list only -- replace with your own. Notes are generic reminders.
    ("BTC",  "BTCUSDT",  "bitcoin",   "CORE", "example note"),
    ("ETH",  "ETHUSDT",  "ethereum",  "CORE", "example note"),
    ("SOL",  "SOLUSDT",  "solana",    "CORE", "example note"),
    ("LINK", "LINKUSDT", "chainlink", "ALT",  "example note"),
    ("GEOD", None,       "geodnet",   "SMALL","not on Binance -> CoinGecko fallback; check liquidity"),
]

HIGH_LOOKBACK = 90    # calendar days for the recent high (crypto trades 7d/wk)
MA_WINDOW     = 50
REGIME_MA     = 200   # quick-look regime check on BTC
RSI_PERIOD    = 14
# =====================================================


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "dip-finder/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def closes_binance(symbol, days):
    # Public endpoint, no API key. Close price is field [4] of each kline.
    url = (f"https://api.binance.com/api/v3/klines"
           f"?symbol={symbol}&interval=1d&limit={days}")
    return [float(k[4]) for k in http_json(url)]


def closes_coingecko(coin_id, days):
    url = (f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
           f"?vs_currency=usd&days={days}&interval=daily")
    return [float(p[1]) for p in http_json(url)["prices"]]


def closes_demo(label, days):
    # Offline fallback so the script always runs (same trick as the equity version).
    random.seed(hash(label) % 9999)
    drift = random.uniform(-0.006, 0.004)          # crypto bear: drift skews down
    price = random.uniform(0.1, 70000)
    out = []
    for _ in range(days):
        price *= (1 + drift + random.gauss(0, 0.05))
        out.append(max(price, 0.0001))
    return out


def get_closes(label, binance_sym, gecko_id, days=240):
    """Binance first (matches your regime systems), CoinGecko fallback, demo last."""
    if binance_sym:
        try:
            return closes_binance(binance_sym, days), "binance"
        except Exception:
            pass
    try:
        return closes_coingecko(gecko_id, days), "coingecko"
    except Exception:
        return closes_demo(label, days), "DEMO"


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else sum(vals) / len(vals)


def rsi_wilder(vals, n=RSI_PERIOD):
    """Wilder's smoothed RSI -- seeds with a simple average, then smooths."""
    if len(vals) < n + 1:
        return 50.0
    deltas = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def signal(drawdown, r):
    # Crypto-calibrated: everything here is roughly 1.6x the equity thresholds.
    if drawdown <= -40 and r < 35:
        return "DEEP DIP"
    if drawdown <= -25 and r < 45:
        return "DIP"
    if drawdown <= -12:
        return "soft pullback"
    if drawdown > -5 and r > 70:
        return "(running hot)"
    return "-"


def main():
    rows, sources = [], set()
    btc_regime = None

    for label, bsym, gid, tier, note in WATCHLIST:
        closes, src = get_closes(label, bsym, gid)
        sources.add(src)
        price = closes[-1]
        recent_high = max(closes[-HIGH_LOOKBACK:])
        drawdown = (price / recent_high - 1) * 100
        vs_ma = (price / sma(closes, MA_WINDOW) - 1) * 100
        r = rsi_wilder(closes)
        rows.append((label, tier, price, drawdown, vs_ma, r, signal(drawdown, r), note))
        if label == "BTC":
            btc_regime = "BULL-TREND" if price > sma(closes, REGIME_MA) else "BEAR / CHOP"

    rows.sort(key=lambda x: x[3])   # most below its 90d high first

    print(f"\n=== Crypto Dip Finder ({datetime.now():%Y-%m-%d}) ===")
    print("Data: " + " + ".join(sorted(sources)) +
          ("   <-- DEMO means offline; run at home for real numbers" if "DEMO" in sources else ""))
    if btc_regime:
        print(f"BTC quick-look regime (close vs {REGIME_MA}d MA): {btc_regime}"
              + ("   -> dry powder STAYS PARKED" if btc_regime != "BULL-TREND"
                 else "   -> confirm with btcregime.py before deploying reserve"))
    print("-" * 88)
    print(f"{'Ticker':<9}{'Tier':<6}{'Price':>12}{'vs 90d high':>13}{'vs 50d':>9}{'RSI':>6}   Signal")
    for label, tier, p, dd, vm, r, sig, note in rows:
        px = f"{p:,.2f}" if p >= 1 else f"{p:.4f}"
        print(f"{label:<9}{tier:<6}{px:>12}{dd:>12.1f}%{vm:>8.1f}%{r:>6.0f}   {sig}")
    print("-" * 88)
    print("Per-coin standing orders:")
    for label, tier, p, dd, vm, r, sig, note in rows:
        print(f"  {label:<8} {note}")
    print("-" * 88)
    print("Most 'on sale' is at the top. DIP/DEEP DIP = tranche candidates -- on RED weeks only.")
    print("This ranks names; it does not call bottoms. Size positions to your own risk limits.")


if __name__ == "__main__":
    main()