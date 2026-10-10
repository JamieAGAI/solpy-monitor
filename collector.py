"""
collector.py — log the market data that disappears.

DESIGN RULE: only collect what you cannot get back later.

Klines are retrievable from Binance forever, so we don't log them (a price
snapshot goes in each record purely for alignment). Everything below either
has a short retention window or no history at all:

  series                        retention        value
  ---------------------------------------------------------------------
  order book depth              NONE             highest — never retrievable
  liquidations                  NONE             highest — websocket only
  open interest                 ~30 days         high
  long/short account ratios     ~30 days         high — positioning
  top-trader position ratio     ~30 days         high — "smart money" side
  taker buy/sell volume         ~30 days         medium — aggression
  mark/index basis              none historical  medium
  funding rate                  full history     low, but free to log
  fear & greed                  full history     low, but free to log

No LLM calls. No trading. No decisions. It writes files, nothing else.

    pip install requests
    python collector.py --once                # single pass, verify it works
    python collector.py                       # run forever
    python collector.py --symbols SOLUSDT BTCUSDT ETHUSDT --interval 300
    python collector.py --inspect data/       # summarise what you've gathered

Liquidations need a websocket. If `websocket-client` is installed it runs in a
background thread; if not, everything else still works.

    pip install websocket-client              # optional but recommended
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone

import requests

FAPI = "https://fapi.binance.com"
SPOT = "https://api.binance.com"
FNG = "https://api.alternative.me/fng/"
# Binance split fstream into /public, /market and /private tiers and killed the
# legacy /ws path on 2026-04-23. @forceOrder lives on /market. The old URL still
# connects but never delivers a message — silent, no error.
WS = "wss://fstream.binance.com/market/ws/!forceOrder@arr"

RUNNING = True


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def day_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ══════════════════════════════════════════════════════════════════════
#  FETCHERS
#  Every one returns a dict and never raises. A dead endpoint must not
#  take down a collector that has been running for eight months.
# ══════════════════════════════════════════════════════════════════════

def _get(url, params=None, timeout=10):
    try:
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:                                          # noqa: BLE001
        return None


def grab_open_interest(sym: str) -> dict:
    d = _get(f"{FAPI}/fapi/v1/openInterest", {"symbol": sym})
    return {"oi": float(d["openInterest"])} if d and "openInterest" in d else {}


def grab_premium(sym: str) -> dict:
    d = _get(f"{FAPI}/fapi/v1/premiumIndex", {"symbol": sym})
    if not d:
        return {}
    try:
        mark, index = float(d["markPrice"]), float(d["indexPrice"])
        return {
            "mark": mark,
            "index": index,
            "basis_pct": (mark - index) / index * 100 if index else None,
            "funding": float(d.get("lastFundingRate", 0)),
            "next_funding_ms": d.get("nextFundingTime"),
        }
    except (KeyError, ValueError, TypeError):
        return {}


def grab_book(sym: str, limit: int = 1000) -> dict:
    """
    Order book depth. This is the single most valuable series here because
    there is NO historical endpoint for it anywhere, at any price.

    Storing raw levels every poll would balloon the files, so we keep the
    top of book verbatim and bucket the rest by distance from mid. Imbalance
    at several depths is what you'd actually test on.

    limit=1000 is the endpoint max. On BTC that can still be under 0.2% of
    mid, so the wider buckets are truncated — `book_reach_pct` records how
    far the returned book actually extends so you know which buckets are
    real. Buckets wider than book_reach_pct are identical to the last real
    one and should be ignored.
    """
    d = _get(f"{FAPI}/fapi/v1/depth", {"symbol": sym, "limit": limit})
    if not d or not d.get("bids") or not d.get("asks"):
        return {}
    try:
        bids = [(float(p), float(q)) for p, q in d["bids"]]
        asks = [(float(p), float(q)) for p, q in d["asks"]]
        bb, ba = bids[0][0], asks[0][0]
        mid = (bb + ba) / 2
        out = {
            "best_bid": bb, "best_ask": ba,
            "spread_bps": (ba - bb) / mid * 10000,
            "bid_qty_top": bids[0][1], "ask_qty_top": asks[0][1],
            "book_reach_pct": round(min(mid - bids[-1][0], asks[-1][0] - mid) / mid * 100, 4),
        }
        for pct in (0.1, 0.25, 0.5, 1.0, 2.0):
            lo, hi = mid * (1 - pct / 100), mid * (1 + pct / 100)
            bv = sum(p * q for p, q in bids if p >= lo)
            av = sum(p * q for p, q in asks if p <= hi)
            tag = str(pct).replace(".", "")
            out[f"bid_usd_{tag}"] = round(bv, 2)
            out[f"ask_usd_{tag}"] = round(av, 2)
            out[f"imb_{tag}"] = round((bv - av) / (bv + av), 5) if (bv + av) else None
        return out
    except (ValueError, IndexError, TypeError):
        return {}


def grab_ratios(sym: str, period: str = "5m") -> dict:
    """Positioning. Who is on which side, and how are the big accounts leaning."""
    out = {}
    for ep, tag in (
        ("globalLongShortAccountRatio", "global_ls"),
        ("topLongShortAccountRatio", "top_acct_ls"),
        ("topLongShortPositionRatio", "top_pos_ls"),
    ):
        d = _get(f"{FAPI}/futures/data/{ep}", {"symbol": sym, "period": period, "limit": 1})
        if d and len(d):
            try:
                out[tag] = float(d[0]["longShortRatio"])
                out[f"{tag}_long"] = float(d[0].get("longAccount", d[0].get("longPosition", 0)))
            except (KeyError, ValueError, TypeError):
                pass
    d = _get(f"{FAPI}/futures/data/takerlongshortRatio",
             {"symbol": sym, "period": period, "limit": 1})
    if d and len(d):
        try:
            out["taker_ls"] = float(d[0]["buySellRatio"])
            out["taker_buy_vol"] = float(d[0]["buyVol"])
            out["taker_sell_vol"] = float(d[0]["sellVol"])
        except (KeyError, ValueError, TypeError):
            pass
    return out


def grab_ticker(sym: str) -> dict:
    d = _get(f"{FAPI}/fapi/v1/ticker/24hr", {"symbol": sym})
    if not d:
        return {}
    try:
        return {
            "last": float(d["lastPrice"]),
            "vol_24h": float(d["volume"]),
            "quote_vol_24h": float(d["quoteVolume"]),
            "chg_24h_pct": float(d["priceChangePercent"]),
            "trades_24h": int(d["count"]),
        }
    except (KeyError, ValueError, TypeError):
        return {}


def grab_spot(sym: str) -> dict:
    d = _get(f"{SPOT}/api/v3/ticker/price", {"symbol": sym})
    try:
        return {"spot": float(d["price"])} if d else {}
    except (KeyError, ValueError, TypeError):
        return {}


_fng_cache = {"ts": 0, "val": None}


def grab_fng() -> dict:
    """Updates once a day; cache it rather than hammering the endpoint."""
    if time.time() - _fng_cache["ts"] < 3600 and _fng_cache["val"] is not None:
        return {"fng": _fng_cache["val"]}
    d = _get(FNG, {"limit": 1})
    try:
        v = int(d["data"][0]["value"])
        _fng_cache.update(ts=time.time(), val=v)
        return {"fng": v}
    except (KeyError, ValueError, TypeError, IndexError):
        return {}


# ══════════════════════════════════════════════════════════════════════
#  LIQUIDATIONS — websocket, no REST history exists
# ══════════════════════════════════════════════════════════════════════

class LiquidationStream(threading.Thread):
    """
    Binance broadcasts forced orders on !forceOrder@arr. There is no way to
    request these after the fact, so anything not captured live is gone.
    Runs as a daemon; reconnects with backoff; never kills the main loop.
    """

    def __init__(self, outdir: str, symbols: set[str] | None = None):
        super().__init__(daemon=True)
        self.outdir = outdir
        self.symbols = symbols
        self.count = 0        # written to disk
        self.received = 0     # messages arriving from the socket
        self.filtered = 0     # dropped because symbol not in our list
        self.errors = 0
        self._ws = None

    def run(self):
        try:
            import websocket                                   # noqa: PLC0415
        except ImportError:
            print("  [liq] websocket-client not installed — liquidations NOT collected")
            print("        pip install websocket-client")
            return

        # WebSocketApp rather than a raw socket: liquidations are BURSTY. Quiet
        # stretches of several minutes are normal, and a plain recv() with a
        # socket timeout treats that silence as a dead connection and
        # reconnects in a loop. run_forever handles ping/pong keepalive, so
        # silence stays silence.
        backoff = 1
        while RUNNING:
            connected_at = time.time()
            try:
                self._ws = websocket.WebSocketApp(
                    WS,
                    on_open=lambda _: print("  [liq] connected"),
                    on_message=lambda _, msg: self._handle(msg),
                    on_error=lambda _, e: print(f"  [liq] socket error: {e}"),
                    on_close=lambda _, code, msg: print(f"  [liq] closed ({code}) {msg or ''}"),
                )
                self._ws.run_forever(ping_interval=180, ping_timeout=20)
            except Exception:                                  # noqa: BLE001
                pass

            if not RUNNING:
                break
            # A connection that survived a while was healthy; don't punish it
            # with an escalating backoff after a one-off drop.
            if time.time() - connected_at > 300:
                backoff = 1
            print(f"  [liq] reconnecting in {backoff}s")
            for _ in range(backoff):
                if not RUNNING:
                    break
                time.sleep(1)
            backoff = min(backoff * 2, 60)

    def stop(self):
        try:
            if self._ws:
                self._ws.close()
        except Exception:                                      # noqa: BLE001
            pass

    def _handle(self, raw: str):
        self.received += 1
        try:
            msg = json.loads(raw)

            # First message: log its shape. !forceOrder@arr has been documented
            # as both a single object and an array at different times, and
            # guessing wrong means silently discarding everything.
            if self.received == 1:
                keys = list(msg.keys()) if isinstance(msg, dict) else f"array[{len(msg)}]"
                print(f"  [liq] first message shape: {keys}")

            events = msg if isinstance(msg, list) else [msg]
            for ev in events:
                o = ev.get("o", ev) if isinstance(ev, dict) else {}
                sym = o.get("s")
                if not sym:
                    continue
                if self.symbols and sym not in self.symbols:
                    self.filtered += 1
                    continue
                price = float(o.get("ap") or o.get("p") or 0)
                qty = float(o.get("q") or 0)
                rec = {
                    "ts": utcnow(),
                    "event_ms": ev.get("E"),
                    "symbol": sym,
                    "side": o.get("S"),
                    "price": price,
                    "qty": qty,
                    "usd": price * qty,
                }
                path = os.path.join(self.outdir, f"liquidations_{day_stamp()}.jsonl")
                with open(path, "a") as fh:
                    fh.write(json.dumps(rec) + "\n")
                self.count += 1
        except Exception as e:                                 # noqa: BLE001
            # Never silent. A handler that swallows its own errors cannot be
            # debugged, and you find out months later that the file is empty.
            self.errors += 1
            if self.errors <= 5 or self.errors % 500 == 0:
                print(f"  [liq] handler error #{self.errors}: "
                      f"{type(e).__name__}: {e}")
                print(f"        raw: {str(raw)[:200]}")


# ══════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════════════

def collect_once(sym: str, ratio_period: str = "5m") -> dict:
    rec = {"ts": utcnow(), "symbol": sym}
    for fn in (grab_ticker, grab_open_interest, grab_premium, grab_book, grab_spot):
        rec.update(fn(sym))
    rec.update(grab_ratios(sym, ratio_period))
    rec.update(grab_fng())
    if rec.get("last") and rec.get("spot"):
        rec["perp_spot_basis_pct"] = (rec["last"] - rec["spot"]) / rec["spot"] * 100
    if rec.get("oi") and rec.get("last"):
        rec["oi_usd"] = rec["oi"] * rec["last"]
    rec["fields"] = len(rec)
    return rec


def write(outdir: str, sym: str, rec: dict):
    path = os.path.join(outdir, f"market_{sym}_{day_stamp()}.jsonl")
    with open(path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
        fh.flush()
        os.fsync(fh.fileno())          # survive an unclean shutdown


def inspect(outdir: str):
    import glob
    from collections import defaultdict
    files = sorted(glob.glob(os.path.join(outdir, "*.jsonl")))
    if not files:
        print(f"nothing in {outdir}")
        return
    per = defaultdict(lambda: {"n": 0, "first": None, "last": None})
    for f in files:
        base = os.path.basename(f)
        kind = "liquidations" if base.startswith("liquidations") else base.split("_")[1]
        with open(f) as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                s = per[kind]
                s["n"] += 1
                ts = d.get("ts")
                if ts:
                    s["first"] = min(s["first"] or ts, ts)
                    s["last"] = max(s["last"] or ts, ts)
    print(f"\n{outdir}: {len(files)} files\n")
    print(f"  {'series':<16} {'records':>10}  span")
    print("  " + "-" * 56)
    for k, v in sorted(per.items()):
        span = ""
        if v["first"] and v["last"]:
            d0 = datetime.fromisoformat(v["first"])
            d1 = datetime.fromisoformat(v["last"])
            span = f"{d0.date()} to {d1.date()}  ({(d1-d0).days}d)"
        print(f"  {k:<16} {v['n']:>10,}  {span}")
    total = sum(os.path.getsize(f) for f in files) / 1e6
    print(f"\n  disk: {total:.1f} MB")


def main():
    global RUNNING
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["SOLUSDT"])
    ap.add_argument("--interval", type=int, default=300, help="seconds between polls")
    ap.add_argument("--outdir", default="data")
    ap.add_argument("--ratio-period", default="5m")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-liq", action="store_true")
    ap.add_argument("--inspect", default=None)
    args = ap.parse_args()

    if args.inspect:
        inspect(args.inspect)
        return

    os.makedirs(args.outdir, exist_ok=True)

    # Assigned before the handler is registered: a Ctrl+C arriving in the
    # window between the two would otherwise raise NameError inside stop().
    liq = None

    def stop(*_):
        global RUNNING
        RUNNING = False
        print("\nstopping...")
        if liq:
            liq.stop()
    signal.signal(signal.SIGINT, stop)
    try:
        signal.signal(signal.SIGTERM, stop)
    except (AttributeError, ValueError):
        pass

    if not args.no_liq and not args.once:
        liq = LiquidationStream(args.outdir, set(args.symbols))
        liq.start()

    print(f"collecting {', '.join(args.symbols)} every {args.interval}s -> {args.outdir}/")
    if args.once:
        for s in args.symbols:
            rec = collect_once(s, args.ratio_period)
            write(args.outdir, s, rec)
            got = [k for k in ("oi", "funding", "imb_10", "global_ls",
                               "taker_ls", "basis_pct", "fng") if rec.get(k) is not None]
            print(f"\n{s}: {rec['fields']} fields")
            print(f"  captured: {', '.join(got) if got else 'NOTHING — check network'}")
            missing = [k for k in ("oi", "funding", "imb_10", "global_ls",
                                   "taker_ls", "fng") if rec.get(k) is None]
            if missing:
                print(f"  missing : {', '.join(missing)}")
        return

    n, errors = 0, 0
    while RUNNING:
        t0 = time.time()
        for s in args.symbols:
            try:
                write(args.outdir, s, collect_once(s, args.ratio_period))
                n += 1
            except Exception as e:                             # noqa: BLE001
                errors += 1
                print(f"  [{utcnow()}] {s} write failed: {e}")
        if n % 12 == 0 or n <= len(args.symbols):
            if liq:
                lq = (f"liq: {liq.count} saved / {liq.received} recv "
                      f"/ {liq.filtered} filtered / {liq.errors} err")
                if liq.received == 0 and n >= 12:
                    lq += "  ** no liquidation messages yet — check WS URL/tier **"
            else:
                lq = "liq: off"
            print(f"  [{utcnow()}] {n} records, {lq}, {errors} write errors")
        sleep = max(1, args.interval - (time.time() - t0))
        for _ in range(int(sleep)):
            if not RUNNING:
                break
            time.sleep(1)

    print(f"done: {n} records, {liq.count if liq else 0} liquidations")


if __name__ == "__main__":
    main()