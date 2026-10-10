#!/usr/bin/env python3
"""
================================================================================
  BTCREGIME v2  —  BTC/USDT Bull Regime Monitor (alert-only)
================================================================================
Same strategy as v1, fixed plumbing.

STRATEGY (unchanged):
  LONG  (BUY alert) when regime is bull OR strong_bull
  FLAT  (SELL alert) when regime exits to bull_chop / bear / chop

REGIME DEFINITIONS (completed bars only):
  strong_bull: weekly_close > weekly_EMA50  AND  weekly_RSI14 > 60
               AND  daily_close > daily_EMA50 > daily_EMA200
  bull:        weekly_close > weekly_EMA50  AND  weekly_RSI14 > 50
               AND  daily_close > daily_EMA50 > daily_EMA200
  bull_chop:   weekly_close > weekly_EMA50  AND  weekly_RSI14 > 50
               AND NOT (daily_close < daily_EMA50 < daily_EMA200)
  bear:        weekly_close < weekly_EMA50  AND  weekly_RSI14 < 50
               AND  daily_close < daily_EMA50 < daily_EMA200
  chop:        everything else

CHANGES FROM v1 (why):
  1. Fetches native 1d and 1w klines with FULL history instead of resampling a
     rolling 430-day 4h window. v1's EMAs/RSI re-seeded every time the window
     slid, so regime inputs jittered with no new bar (visible in the v1 logs:
     weekly EMA50 moved >1% intraday). Now seed weight is negligible and live
     values match TradingView / any full-history backtest.
     NOTE: weekly bars are now standard Mon-Sun (Binance/TradingView convention).
     v1's pandas resample used Tue-Mon weeks. Re-validate with the backtest
     before trusting live alerts.
  2. Evaluates ONCE per day shortly after the 00:00 UTC daily close. The regime
     can only legitimately change on a daily close, so the old 4h cadence could
     only ever produce artifacts. Failed cycles retry every 30 min.
  3. Alerts that fail to send are queued in the state file and retried every
     cycle until delivered. v1 dropped them silently.
  4. Waits for network/DNS at boot (fixes the getaddrinfo crash-loop on
     machine restart). Startup alert is sent only once the network is up.
  5. state key renamed in_strong_bull -> is_long (auto-migrated on load).

RUN:
  python btcregime.py            # live alerts
  python btcregime.py --shadow   # test mode: [SHADOW] prefix, separate state file

SETUP (.env keys needed):
  EMAIL_FROM=you@gmail.com
  EMAIL_TO=you@gmail.com
  EMAIL_PASS=your_16_char_gmail_APP_password
================================================================================
"""
import os
import sys
import json
import time
import socket
import smtplib
import logging
import datetime as dt
from email.mime.text import MIMEText

import pandas as pd
from binance.client import Client
from dotenv import load_dotenv

# ------------------------------------------------------------------ config ----
load_dotenv()

SHADOW_MODE = "--shadow" in sys.argv

EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_TO   = os.getenv("EMAIL_TO")
EMAIL_PASS = os.getenv("EMAIL_PASS")

SYMBOL        = "BTCUSDT"
ASSET         = "BTC"
LISTING_START = "2017-08-01"   # BTCUSDT listed on Binance Aug 2017 — fetch it all
EVAL_UTC_HOUR = 0              # evaluate once daily, shortly after the daily close
EVAL_UTC_MIN  = 5
HEARTBEAT_H   = 20             # < 24 so the daily cycle always triggers it

# Which regimes count as "in a long position":
#   {"strong_bull"}          -> original, most selective (weekly RSI > 60)
#   {"bull", "strong_bull"}  -> relaxed (weekly RSI > 50)
LONG_REGIMES = {"bull", "strong_bull"}

# Re-entry cooldown: after an EXIT, wait this many days before a new long.
# On BTC, 7 days gave the best balance in backtests (drawdown -31% to -25.5%). 0 = off.
REENTRY_COOLDOWN_DAYS = 7

# Warmup floors: below these the EMAs aren't trustworthy, refuse to classify.
MIN_DAILY_BARS  = 250
MIN_WEEKLY_BARS = 60
FULL_WEEKLY_WARMUP = 200       # below this, warn that weekly EMA50 isn't fully converged

STATE_FILE = "btcregime_shadow.json" if SHADOW_MODE else "btcregime_state.json"
LOG_FILE   = "btcregime_shadow.log"  if SHADOW_MODE else "btcregime.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
# Force stdout to handle utf-8 on Windows (avoids cp1252 UnicodeEncodeError)
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
log = logging.getLogger("btcregime")


# ---------------------------------------------------------------- network -----
def wait_for_network(host="api.binance.com", port=443, max_wait_s=900):
    """Block until DNS resolves (machine may boot before the network is up)."""
    start = time.time()
    while True:
        try:
            socket.getaddrinfo(host, port)
            return True
        except OSError:
            waited = time.time() - start
            if waited > max_wait_s:
                log.error("Network still down after %.0f s — proceeding anyway", waited)
                return False
            log.info("Waiting for network/DNS (%.0f s)...", waited)
            time.sleep(15)


# ------------------------------------------------------------------- data -----
def _klines_to_df(klines):
    """Parse Binance kline list, drop the in-progress (live) bar."""
    df = pd.DataFrame(klines, columns=[
        "timestamp","open","high","low","close","volume",
        "close_time","quote_av","trades","tb_base","tb_quote","ignore",
    ])
    df[["open","high","low","close","volume"]] = \
        df[["open","high","low","close","volume"]].astype(float)
    df["ts"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("ts", inplace=True)
    df = df[["open","high","low","close","volume"]]
    df.sort_index(inplace=True)
    # Drop the still-forming bar
    if len(df) >= 2:
        bar_span = df.index[-1] - df.index[-2]
    else:
        bar_span = pd.Timedelta(days=1)
    if df.index[-1] + bar_span > pd.Timestamp.now("UTC"):
        df = df.iloc[:-1]
    return df


def fetch_data():
    """
    Fetch FULL-HISTORY native daily and weekly klines (closed bars only),
    plus the live ticker price for alert context. Retries 3x.
    Full history makes the EMA seed weight negligible: live values are stable
    bar-to-bar and match TradingView / full-history backtests.
    """
    for attempt in range(3):
        try:
            client = Client(requests_params={"timeout": 30})
            daily = _klines_to_df(client.get_historical_klines(
                SYMBOL, Client.KLINE_INTERVAL_1DAY, start_str=LISTING_START))
            weekly = _klines_to_df(client.get_historical_klines(
                SYMBOL, Client.KLINE_INTERVAL_1WEEK, start_str=LISTING_START))
            try:
                live_price = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
            except Exception:
                live_price = float(daily["close"].iloc[-1])
            log.info("Fetched %d daily bars (to %s) and %d weekly bars (to %s)",
                     len(daily), daily.index[-1].date(),
                     len(weekly), weekly.index[-1].date())
            return daily, weekly, live_price
        except Exception as e:
            log.warning("Fetch attempt %d/3 failed: %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(15)
    raise RuntimeError("Data fetch failed after 3 retries")


# ---------------------------------------------------------------- indicators --
def _rsi(close, n=14):
    d  = close.diff()
    g  = d.clip(lower=0).ewm(com=n - 1, min_periods=n).mean()
    l  = (-d).clip(lower=0).ewm(com=n - 1, min_periods=n).mean()
    return 100 - 100 / (1 + g / l)


def compute_regime(daily_c, weekly_c, live_price=None):
    """
    Classify regime from completed daily and weekly bars.
    Returns (regime_str, context_dict) or (None, error_dict) on failure.
    """
    if len(daily_c) < MIN_DAILY_BARS:
        return None, {"error": f"need {MIN_DAILY_BARS}+ daily bars, have {len(daily_c)}"}
    if len(weekly_c) < MIN_WEEKLY_BARS:
        return None, {"error": f"need {MIN_WEEKLY_BARS}+ weekly bars, have {len(weekly_c)}"}
    if len(weekly_c) < FULL_WEEKLY_WARMUP:
        log.warning("Only %d weekly bars — weekly EMA50 not fully converged", len(weekly_c))

    daily_c = daily_c.copy()
    weekly_c = weekly_c.copy()
    daily_c["ema50"]  = daily_c["close"].ewm(span=50,  adjust=False).mean()
    daily_c["ema200"] = daily_c["close"].ewm(span=200, adjust=False).mean()
    weekly_c["ema50"] = weekly_c["close"].ewm(span=50, adjust=False).mean()
    weekly_c["rsi"]   = _rsi(weekly_c["close"])

    d = daily_c.iloc[-1]
    w = weekly_c.iloc[-1]

    if pd.isna(d["ema200"]) or pd.isna(w["ema50"]) or pd.isna(w["rsi"]):
        return None, {"error": "NaN in indicators (not enough warm-up)"}

    dc = float(d["close"]); de50 = float(d["ema50"]); de200 = float(d["ema200"])
    wc = float(w["close"]); we50 = float(w["ema50"]); wrsi  = float(w["rsi"])

    bull_weekly = wc > we50 and wrsi > 50
    bear_weekly = wc < we50 and wrsi < 50
    bull_daily  = dc > de50 > de200
    bear_daily  = dc < de50 < de200

    if   bull_weekly and bull_daily and wrsi > 60: regime = "strong_bull"
    elif bull_weekly and bull_daily:               regime = "bull"
    elif bull_weekly and not bear_daily:           regime = "bull_chop"
    elif bear_weekly and bear_daily:               regime = "bear"
    else:                                          regime = "chop"

    ctx = {
        "regime":      regime,
        "price":       round(dc, 4),                       # last daily close (strategy price)
        "live_price":  round(live_price, 4) if live_price else round(dc, 4),
        "daily_date":  str(daily_c.index[-1].date()),
        "daily_close": round(dc,   2),
        "daily_ema50": round(de50,  2),
        "daily_ema200":round(de200, 2),
        "daily_bull":  bull_daily,
        "weekly_date": str(weekly_c.index[-1].date()),
        "weekly_close":round(wc,   2),
        "weekly_ema50":round(we50,  2),
        "weekly_rsi":  round(wrsi,  1),
        "weekly_bull_weekly": bull_weekly,
        "bar_ts":      str(daily_c.index[-1]),             # regime can only change per daily bar
        # Gap metrics
        "pct_to_weekly_ema50": round((we50 - wc) / wc * 100, 1),
        "daily_ema_gap":       round((de50 - de200) / de200 * 100, 1),
    }
    return regime, ctx


# ------------------------------------------------------------------ state -----
def _empty_state():
    return {
        "is_long":           False,
        "last_regime":       None,
        "entry_price":       None,
        "entry_bar_ts":      None,
        "last_exit_bar_ts":  None,
        "last_bar_seen":     None,
        "last_heartbeat_ts": None,
        "pending_alerts":    [],
    }

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                s = json.load(f)
            # migrate v1 key name
            if "in_strong_bull" in s and "is_long" not in s:
                s["is_long"] = s.pop("in_strong_bull")
            base = _empty_state()
            for k, v in base.items():
                s.setdefault(k, v)
            return s
        except Exception as e:
            log.warning("Failed to load state (%s), starting fresh", e)
    return _empty_state()

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2, default=str)


# ------------------------------------------------------------------ email -----
def _smtp_send(subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as srv:
        srv.login(EMAIL_FROM, EMAIL_PASS)
        srv.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())


def send_alert(subject, body, state=None):
    """
    Send an email. If it fails and state is provided, the alert is queued in
    the state file and retried every cycle until delivered — a missed BUY/SELL
    is the one failure an alert-only system can't afford.
    """
    prefix = "[SHADOW] " if SHADOW_MODE else ""
    subject = prefix + subject
    if not (EMAIL_FROM and EMAIL_TO and EMAIL_PASS):
        log.warning("Email config missing — printing instead:")
        log.info("SUBJECT: %s\n%s", subject, body)
        return True
    try:
        _smtp_send(subject, body)
        log.info("Alert sent: %s", subject)
        return True
    except Exception as e:
        log.warning("Email failed (%s): %s", subject, e)
        if state is not None:
            state.setdefault("pending_alerts", []).append({
                "subject":  subject,
                "body":     body,
                "queued_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            })
            save_state(state)
            log.info("Alert queued for retry (%d pending)", len(state["pending_alerts"]))
        return False


def retry_pending_alerts(state):
    pending = state.get("pending_alerts") or []
    if not pending:
        return
    if not (EMAIL_FROM and EMAIL_TO and EMAIL_PASS):
        return
    still = []
    for a in pending:
        try:
            _smtp_send(a["subject"],
                       a["body"] + f"\n\n[Delivery was delayed — originally queued {a['queued_at'][:16]} UTC]")
            log.info("Queued alert delivered: %s", a["subject"])
        except Exception as e:
            log.warning("Queued alert still failing (%s): %s", a["subject"], e)
            still.append(a)
    state["pending_alerts"] = still
    save_state(state)


# --------------------------------------------------------------- email bodies --
_SEP = "=" * 52

def _regime_block(ctx):
    """Compact summary of all regime inputs — included in every alert."""
    d_align = "BULLISH (close > EMA50 > EMA200)" if ctx["daily_bull"] else "not bullish"
    w_bull  = "YES" if ctx["weekly_bull_weekly"] else "NO"
    sb_gap  = ctx["pct_to_weekly_ema50"]
    arrow   = "above" if sb_gap < 0 else f"+{sb_gap}% below"
    return f"""\
REGIME INPUTS (daily bar {ctx['daily_date']})
  {ASSET} last daily close:  ${ctx['daily_close']:,.2f}
  {ASSET} live price:        ${ctx['live_price']:,.4f}

  WEEKLY (bar: {ctx['weekly_date']}):
    Close:         ${ctx['weekly_close']:,.2f}
    EMA50:         ${ctx['weekly_ema50']:,.2f}  ({arrow} weekly EMA50)
    RSI14:         {ctx['weekly_rsi']}  (>50 bull_weekly, >60 strong_bull)
    Bull weekly:   {w_bull}

  DAILY (bar: {ctx['daily_date']}):
    Close:         ${ctx['daily_close']:,.2f}
    EMA50:         ${ctx['daily_ema50']:,.2f}
    EMA200:        ${ctx['daily_ema200']:,.2f}
    Alignment:     {d_align}

  REGIME:  {ctx['regime'].upper().replace('_',' ')}"""

def buy_body(ctx, prev_regime):
    return f"""\
{ASSET}REGIME — {ASSET} BUY SIGNAL
{_SEP}
Regime changed: {prev_regime.upper()} → {ctx['regime'].upper()}
Daily bar: {ctx['daily_date']}

{_regime_block(ctx)}

ACTION: Go LONG {ASSET}.
  - Trend regime is bullish (weekly close > EMA50, weekly RSI > 50, daily aligned).
  - Hold until SELL alert (regime exits bull / strong_bull).
  - Suggested: execute on your exchange at market or limit near current price.

Strategy: Bull Regime (weekly RSI > 50, daily close > EMA50 > EMA200)
"""

def sell_body(ctx, prev_regime, entry_price):
    if entry_price:
        pnl = (ctx["price"] - entry_price) / entry_price * 100
        pnl_line = f"Approx P&L vs entry (${entry_price:,.4f}): {pnl:+.1f}%"
    else:
        pnl_line = "Entry price not recorded (state missing)."
    return f"""\
{ASSET}REGIME — {ASSET} SELL / EXIT SIGNAL
{_SEP}
Regime changed: {prev_regime.upper()} → {ctx['regime'].upper()}
Daily bar: {ctx['daily_date']}

{_regime_block(ctx)}

{pnl_line}

ACTION: Go FLAT — close your {ASSET} position.
  Regime has exited the bull zone. Risk/reward no longer favourable.
  {"⚠️  BEAR regime confirmed. High-priority exit." if ctx['regime'] == "bear" else ""}
"""

def heartbeat_body(ctx, state):
    pos = f"LONG (in {ASSET})" if state["is_long"] else "FLAT (no position)"
    entry_info = (f"  Entry price:     ${state['entry_price']:,.4f}"
                  if state["entry_price"] else "  Entry price:     —")
    entry_ts   = (f"  Entry bar:       {state['entry_bar_ts']}"
                  if state["entry_bar_ts"] else "")
    pending = len(state.get("pending_alerts") or [])
    pending_line = f"\n⚠️  {pending} undelivered alert(s) queued for retry!" if pending else ""
    return f"""\
{ASSET}REGIME — Daily Heartbeat
{_SEP}
Monitor is running normally.{pending_line}

POSITION: {pos}
{entry_info}
{entry_ts}

{_regime_block(ctx)}

Next LONG entry requires:
  Weekly RSI > 50    (now: {ctx['weekly_rsi']})
  Weekly close > EMA50  (now ${ctx['weekly_close']:,.2f} vs EMA50 ${ctx['weekly_ema50']:,.2f})
  Daily close > EMA50 > EMA200  (now: {ctx['daily_bull']})
"""


# ------------------------------------------------------------- core logic -----
def run_once(state):
    """Fetch, classify, alert on change. Returns True on success."""
    retry_pending_alerts(state)

    try:
        daily, weekly, live_price = fetch_data()
    except RuntimeError as e:
        log.error("Skipping cycle: %s", e)
        return False

    regime, ctx = compute_regime(daily, weekly, live_price)
    if regime is None:
        log.error("Regime compute failed: %s", ctx.get("error"))
        return False

    bar_ts = ctx["bar_ts"]
    if state.get("last_bar_seen") == bar_ts:
        log.info("Daily bar already processed (%s) | regime=%s | %s=$%.4f",
                 ctx["daily_date"], regime, ASSET, ctx["live_price"])
        _maybe_heartbeat(state, ctx)
        return True

    state["last_bar_seen"] = bar_ts
    prev_regime = state.get("last_regime")

    log.info("Daily bar %s | regime=%-12s | %s=$%.4f | wRSI=%.1f | wEMA50=$%.2f | dEMA50=$%.2f dEMA200=$%.2f",
             ctx["daily_date"], regime, ASSET, ctx["price"],
             ctx["weekly_rsi"], ctx["weekly_ema50"],
             ctx["daily_ema50"], ctx["daily_ema200"])

    regime_changed = (prev_regime is not None and prev_regime != regime)
    is_long_regime = regime in LONG_REGIMES

    # Re-entry cooldown: days since the last exit (inf if never exited).
    gap_days = float("inf")
    if state.get("last_exit_bar_ts"):
        gap_days = (pd.Timestamp(bar_ts) - pd.Timestamp(state["last_exit_bar_ts"])
                    ).total_seconds() / 86400.0
    in_cooldown = REENTRY_COOLDOWN_DAYS > 0 and gap_days < REENTRY_COOLDOWN_DAYS

    # ── BUY: entering a long regime (unless still cooling down from last exit)
    if is_long_regime and not state["is_long"] and not in_cooldown:
        state["is_long"]      = True
        state["entry_price"]  = ctx["price"]
        state["entry_bar_ts"] = bar_ts
        log.info("ENTERING %s (LONG) — sending BUY alert", regime.upper())
        send_alert(f"🟢 {ASSET}REGIME: {ASSET} BUY — {regime.upper().replace('_',' ')} confirmed",
                   buy_body(ctx, prev_regime or "unknown"), state)

    # ── HELD BACK: long regime active but still inside the cooldown window
    elif is_long_regime and not state["is_long"] and in_cooldown:
        log.info("Long regime active but within %d-day cooldown (%.1f days since exit) — skipping entry",
                 REENTRY_COOLDOWN_DAYS, gap_days)

    # ── SELL: exiting to a non-long regime
    elif not is_long_regime and state["is_long"]:
        ep = state.get("entry_price")
        state["is_long"]          = False
        state["entry_price"]      = None
        state["entry_bar_ts"]     = None
        state["last_exit_bar_ts"] = bar_ts
        emojis = {"bear":"🔴","chop":"🟡","bull_chop":"🟡"}
        emoji  = emojis.get(regime, "⚪")
        log.info("EXITING LONG to %s — sending SELL alert", regime)
        send_alert(f"{emoji} {ASSET}REGIME: {ASSET} SELL — Exiting to {regime.upper().replace('_',' ')}",
                   sell_body(ctx, prev_regime or "long", ep), state)

    elif regime_changed:
        log.info("Regime: %s -> %s (no trade signal)", prev_regime, regime)

    state["last_regime"] = regime
    save_state(state)
    _maybe_heartbeat(state, ctx)
    return True


def _maybe_heartbeat(state, ctx):
    """Send a heartbeat email every HEARTBEAT_H hours."""
    last_hb = state.get("last_heartbeat_ts")
    now_str = dt.datetime.now(dt.timezone.utc).isoformat()
    if last_hb is None:
        do_hb = True
    else:
        elapsed = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last_hb)
        do_hb = elapsed.total_seconds() > HEARTBEAT_H * 3600
    if do_hb:
        pos = "LONG" if state["is_long"] else "FLAT"
        log.info("Sending heartbeat | position=%s | regime=%s", pos, ctx["regime"])
        send_alert(f"💓 {ASSET}REGIME: Heartbeat | {pos} | {ctx['regime'].upper()}",
                   heartbeat_body(ctx, state), state)
        state["last_heartbeat_ts"] = now_str
        save_state(state)


# ------------------------------------------------------------------- loop -----
def seconds_to_next_eval():
    """Seconds until EVAL_UTC_HOUR:EVAL_UTC_MIN UTC (just after the daily close)."""
    now = dt.datetime.now(dt.timezone.utc)
    target = now.replace(hour=EVAL_UTC_HOUR, minute=EVAL_UTC_MIN,
                         second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return max(60, (target - now).total_seconds())


def main():
    log.info("%sREGIME v2 starting | %s | mode=%s",
             ASSET, SYMBOL, "SHADOW" if SHADOW_MODE else "LIVE")
    wait_for_network()

    state = load_state()
    log.info("State loaded: is_long=%s  last_regime=%s  pending_alerts=%d",
             state["is_long"], state["last_regime"],
             len(state.get("pending_alerts") or []))

    send_alert(
        f"{ASSET}REGIME started | {'SHADOW' if SHADOW_MODE else 'LIVE'} | "
        f"{'LONG' if state['is_long'] else 'FLAT'}",
        f"{ASSET}REGIME v2 monitor is running.\n"
        f"Mode: {'SHADOW (no real trades)' if SHADOW_MODE else 'LIVE'}\n"
        f"Symbol: {SYMBOL} (daily regime check at {EVAL_UTC_HOUR:02d}:{EVAL_UTC_MIN:02d} UTC)\n"
        f"Current state: {'LONG (in ' + ASSET + ')' if state['is_long'] else 'FLAT'}\n"
        f"Last known regime: {state.get('last_regime') or 'unknown'}\n\n"
        f"Alerts: BUY on bull-regime entry, SELL on exit to non-bull.\n"
        f"Heartbeat roughly daily. Failed alerts are queued and retried.\n",
        state,
    )

    # Evaluate immediately on boot (dedup on daily bar prevents double alerts)
    ok = run_once(state)

    while True:
        if ok:
            sleep_s = seconds_to_next_eval()
            log.info("Sleeping %.0f min until next daily close eval.", sleep_s / 60)
        else:
            sleep_s = 1800
            log.info("Last cycle failed — retrying in 30 min.")
        time.sleep(sleep_s)
        try:
            ok = run_once(state)
        except Exception as e:
            log.error("Unhandled error in loop: %s", e, exc_info=True)
            ok = False  # retry in 30 min


if __name__ == "__main__":
    main()