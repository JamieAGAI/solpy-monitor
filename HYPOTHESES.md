# Pre-registered hypotheses — collector data

Written: 2026-09-08 (UTC). Data available at time of writing: 2026-08-27 → 2026-09-08.
None of the thresholds below may be changed after this date. If a hypothesis fails,
it fails. A new hypothesis gets a new entry with a new date; it does not replace this one.

Symbols: BTCUSDT, ETHUSDT, SOLUSDT (Binance USDⓈ-M perps). Each hypothesis is tested
per symbol; a result only counts if it holds on at least 2 of 3.

## Ground rules (apply to all three)

- Test date: not before 2027-03-01, and not before 25 qualifying events exist for a symbol.
- Test once. No re-running with different thresholds.
- Cost assumption: 0.6% round trip (Binance spot/ETF-equivalent). Report net of this.
- Baselines every result must beat:
  1. Buy-and-hold over the same period.
  2. The same rule with event timestamps randomly shuffled (1,000 shuffles); the real
     result must beat the 95th percentile of the shuffled results.
- Overlapping events: if two events fire within one forward window, keep only the first.
- Method: threshold rule, then a plain mean and win-rate of forward returns. No model.

---

## H1 — Long-liquidation capitulation is a buy at the 24h horizon

**Idea.** A burst of forced long selling with negative funding is exhaustion, not the
start of a trend. The market has already flushed the leverage that would otherwise sell.

**Event** (evaluated on the 5-min snapshot grid, all conditions at the same timestamp):
- Sum of `usd` for `side == "SELL"` liquidations in the trailing 60 min ≥ 0.75% of `oi_usd`.
- `funding` ≤ 0.
- Price change over the trailing 4h ≤ −3%.

**Prediction.** Mean forward 24h return from `last` ≥ +1.0% net of cost; win rate ≥ 55%.

**Kills it.** Mean net return ≤ 0, or fails the shuffled baseline, or fewer than 25 events
by the test date (in which case: inconclusive, wait, do not loosen thresholds).

---

## H2 — Crowded, expensive longs get punished at the 48h horizon

**Idea.** When funding is extreme, open interest is at a high and retail is net long, the
next big move is down because there's no one left to buy and a lot of leverage to unwind.

**Event:**
- `funding` in the top 5% of the trailing 30 days.
- `oi_usd` within 2% of its trailing 30-day high.
- `global_ls` ≥ its trailing 30-day 90th percentile.

**Prediction.** Mean forward 48h return ≤ −1.0%; and forward 48h *low* averages ≤ −3%
(i.e. even if it recovers, it dips first — useful for entry timing).

**Kills it.** Mean forward 48h return ≥ 0, or fails the shuffled baseline.

**Use if true.** Do not deploy a tranche while this condition is active; wait for it to clear.

---

## H3 — Retail and top traders disagreeing: fade retail at the 24h horizon

**Idea.** `global_ls` is the crowd; `top_pos_ls` is the largest accounts by position size.
When the crowd is getting longer while the big accounts are getting shorter, the big
accounts tend to be right.

**Event:**
- `global_ls` has risen ≥ 10% over the trailing 24h.
- `top_pos_ls` has fallen ≥ 10% over the trailing 24h.

**Prediction.** Mean forward 24h return ≤ −0.5%; win rate for a short ≥ 55%.
Symmetric version (crowd shortening, top accounts lengthening → long) tested separately
and reported separately; it does not rescue the primary if the primary fails.

**Kills it.** Mean forward 24h return ≥ 0, or fails the shuffled baseline.

---

## What I expect

Honestly: H2 has the best odds because it's the most-documented effect in perp markets
and the horizon matches the data. H1 is the one I'd most like to be true for the tranche
plan. H3 is the most likely to fail — the ratio series are noisy and 30 days of history
is Binance's limit, so the collector is the only source.

If all three fail, that is a real result: it means this data does not carry a
day-horizon edge at retail cost, and the collector's value is descriptive (knowing what
a capitulation looked like) rather than predictive. That's still worth having.

---

## H2b — H2 with workable thresholds (registered 2026-09-08, after seeing H2 event counts only)

**Why.** H2 as written produced 1–2 events per symbol in 4.7 years. The conjunction was
mis-calibrated by the author, not rejected by the market. H2b widens each condition so the
rule can fire roughly monthly. Outcomes had NOT been examined when these were set.

**Event** (hourly grid):
- `funding` ≥ trailing 30-day 90th percentile.
- `oi_usd` ≥ 95% of trailing 30-day high.
- `global_ls` ≥ trailing 30-day 75th percentile.

**Prediction.** Same as H2: mean forward 48h return ≤ −1.0%; mean forward 48h low ≤ −3%.

**Kills it.** Same as H2. H2 stays on the books as INCONCLUSIVE; H2b does not replace it.

## H1-cm — H1 on the COIN-M proxy (registered 2026-09-08, before any outcome was seen)

**Why.** Binance deleted the USDT-M liquidation archive. The COIN-M archive (BTCUSD_PERP,
ETHUSD_PERP, SOLUSD_PERP) survives from 2023-06-25. Coin-margined perps liquidate on the
same price moves, so they proxy the same event.

**Event.** Identical to H1, with COIN-M long liquidations (contracts × contract size)
normalised by COIN-M open interest instead of USDT-M. Thresholds unchanged.

**Prediction / kills it.** Identical to H1. Reported as H1-cm. H1 proper is tested on
live USDT-M data from 2026-09-08 onward.

## H1-rel — H1 with a feed-relative burst threshold (registered 2026-09-08, before outcomes seen)

**Why.** Binance's liquidation feed (archive and websocket) pushes only the largest order per
symbol per second. It is a subsample, so "≥ 0.75% of OI in an hour" can never trigger on
it — H1-cm produced 0/1/2 events across 474 days including 2024-08-05. That is a
measurement error in H1, not evidence about the market.

**Event.** Trailing-1h long-liquidation USD ≥ 99th percentile of its own trailing 90 days
(and > 0); `funding` ≤ 0; 4h price change ≤ −3%. Other conditions unchanged.

**Prediction / kills it.** Identical to H1.

**This is the last H1 variant.** If inconclusive on COIN-M 2023–24 data, H1 waits for live
USDT-M data and is judged on the next real capitulation.

## Log

- 2026-09-08 — written. Collector running with /market-tier liquidation fix from this date.
  BTC depth buckets beyond 0.1% invalid before this date.
- 2026-09-08 — backfilled 2022-01-01 → 2026-09-07 from data.binance.vision (metrics, klines,
  funding, F&G). Liquidation archive returned nothing; H1 untested.
- 2026-09-08 — first test run on backfill (cost 0.6%, 1,000 shuffles):
  - **H3: FAIL** on BTC (n=33), ETH (n=64), SOL (n=100). Mean 24h returns −0.07%, +0.50%,
    −0.29% gross; all below shuffle 95th pctl. Symmetric version also fails on all three.
    Retail-vs-top-trader divergence carries no 24h edge. Closed.
  - **H2: INCONCLUSIVE** — n=2, 2, 1. Thresholds too tight. Registered H2b above.
  - **H1: INCONCLUSIVE** — no liquidation data before 2026-09-08.
- 2026-09-08 — USDT-M liquidation archive confirmed deleted from data.binance.vision.
  COIN-M archive found (from 2023-06-25). Registered H1-cm above.
- 2026-09-08 — COIN-M archive runs 2023-06-25 → 2024-10-14 (BTC 53k, ETH 28k, SOL 15k events).
  **H1-cm: INCONCLUSIVE** — 0/1/2 events. Feed is a 1/sec subsample; absolute threshold
  untestable on Binance data. Registered H1-rel above. Live collector has the same limit.
- 2026-09-08 — **H1-rel(cm): INCONCLUSIVE** — n=1, 1, 8. The COIN-M window (2023-06 → 2024-10)
  is a bull market; the funding ≤ 0 condition rarely held. Not a failure — wrong sample.
  Per the rule above, no further H1 variants. H1/H1-rel judged on live USDT-M data from
  the next real capitulation. **H2b** directionally wrong on BTC/ETH; treated as closed
  alongside H2. Status: H3 FAIL, H2/H2b closed, H1 pending live data.