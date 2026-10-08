# Backtesting

How the results in [STRATEGIES.md](STRATEGIES.md) are produced, and why they can be
trusted. The full method (engines, validation statistics, conformance simulations,
pass thresholds) is written and pre-registered in M2, before any confirmation test runs.

## Pitfalls every backtest here must handle

1. **Information time ≠ nominal time.** A row is usable only after its `available_at`.
   That covers forecast runs, macro releases, index closes, and market metadata that
   is only known at settlement.
2. **Executable prices.** Trade at the ask or bid that existed after the decision,
   never at the last trade or the print that triggered the signal.
3. **Dated fees.** Fees vary by series and change over time
   (`kalshibot.kalshi.fees`; the dated history arrives in M1).
4. **Fills.** Resting orders get adversely selected, and queue position is invisible
   without L3 data. Maker fills require the price to trade through.
5. **Settlement rules change.** For example, weather settlement sources changed in Aug
   2026 ([RESEARCH §2](RESEARCH.md)), and crypto averaging windows changed too. Read
   each market's own rules.
6. **Multiple testing.** Categories × strategies × parameters produce false positives.
   Every trial is logged and counted.
7. **Regimes.** Volume grows about 2× a year, and market structure shifts (see
   [STRATEGIES H2](STRATEGIES.md)). Old data is not today's market.

## Sizing

For a binary contract with calibrated probability p and all-in cost c per contract
(executable price plus fee, from `kalshibot.kalshi.fees`), Kelly is f* = (p − c) / (1 − c).
Use ¼–½ Kelly because p is estimated with noise. Treat mutually exclusive outcomes of
one event as a single position.
