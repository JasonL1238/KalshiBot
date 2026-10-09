# Roadmap

The goal is an honest trading system, not a get-rich script. You should be able to
explain every piece: where the data comes from, why a backtest result isn't noise, how a
trade is sized, what stops it from blowing up, and how fast an order reaches the
exchange. Whether it makes money is a result to measure, not an assumption.

| # | Stage | Question | Where |
|---|-------|----------|-----------------|
| 1 | Ingest + store | Can I replay exactly what I knew at time *t*? | `src/kalshibot/data/` |
| 2 | Research | Which strategies have edge after fees, beyond what noise explains? | `src/kalshibot/research/`, [STRATEGIES](STRATEGIES.md) |
| 3 | Validate | Would this result survive the number of things we tried? | `src/kalshibot/research/validation/`, [BACKTESTING](BACKTESTING.md) |
| 4 | Execute | What is the fastest safe path from signal to order ack? | `src/kalshibot/execution/` (M5), [EXECUTION](EXECUTION.md) |

## Milestones

- [x] **Phase 0.** API client, signing, settled-market backfill.
- [x] **M0. Governance.** Agent-doc tree, attribution enforcement, hygiene tests, docs
  with one home per fact.
- [x] **M1. Data and shared types.**
  - Fixed-point units and the dict + price bitmap order book.
  - Canonical series/events/markets/fee-change tables and the trade tape (Kalshi API,
    cross-checked against the Becker dataset).
  - Dated fee schedule (`kalshi/fee_history.py`).
  - Point-in-time loader enforcing the research calendar (`research/load.py`).
  - Trial ledger and lockbox gate.
  - WebSocket recorder with a coverage log.
- [ ] **M2. Backtesting research.**
  - Tier-1 path (fills, settlement) and Tier-2 engines (NautilusTrader replay, vectorbt
    bar closes).
  - Engine capability screen.
  - Validation toolkit: market-calibrated null, stationary bootstrap, PBO/CSCV, DSR,
    Romano–Wolf, Holm.
  - Conformance simulations measuring size and power; the gates chosen from them and
    written down before any confirmation test runs.
  - Positive control: the favorite–longshot pattern on discovery data (criterion set;
    runs when the discovery-period API tape lands).
- [ ] **M3. Strategy research.**
  - Shared strategy types (`strategies/base.py`).
  - The research runner: refuses to run without the ledger, logs every evaluation
    before showing it, and allows one confirmation test per family.
  - Per-family candle fetching (`research/run.py`): the null's quotes, then the candle
    leg of the taker rule and window-end bids.
  - A path null (block-resampled, sign-randomized increments within a cluster) for
    families that exit before settlement, measured in conformance first.
  - Pre-register H1–H12 (m = 12, category strata), each with its minimum economically
    meaningful edge and the power at that edge on its own confirmation sample.
  - Run every confirmation test.
  - Apply Holm once to the in-calendar families (forward ones are decided alone).
  - Open the lockbox once for all in-calendar survivors.
  - Write verdicts.
  - Loaders honor the recorder's coverage log when L2-based hypotheses need it.
- [ ] **M4. Execution research.**
  - Hot-path prototypes (Python, Rust, NautilusTrader) under burst replay.
  - Component benchmarks.
  - Round-trip measurements.
  - The order-safety guard ships with the first order-sending command
    (`bench-orders`): refusal unless `KALSHIBOT_ALLOW_ORDERS=1` plus a TTY confirm,
    with tests.
- [ ] **M5. Execution engine** on the M4 winner:
  - OMS with an UNKNOWN state;
  - risk;
  - rate limiting;
  - reconciliation;
  - mock-exchange chaos tests.
- [ ] **M6. Close-out.** Docs, agent maps, one command for full local verification.

## Research calendar

The windows, their dates, and per-hypothesis confirmation starts are defined in
`src/kalshibot/research/calendar.py`. The rules for using them:

- **Window assignment.** A trade belongs to the window of its decision time. Trades are
  never filtered on actual settlement time.
- **Positions settle inside their window.** Nearly every Kalshi market can close early,
  so its scheduled close says little. A decision counts only if its market's as-listed
  latest expiration (`latest_expiration_time`, fixed at listing) is at most the family's
  maximum holding period after it, and decisions in a window's last maximum holding
  period are embargoed. Markets without one are excluded. A position whose settlement
  still runs past the window's end is marked at the executable bid there (at zero until
  window-end bids are fetched).
- **No look-ahead across seams.** Anything fitted in a window sees only data visible
  before the window's end (the loader's `as_of`), so labels that settle later are
  invisible. Each window's first maximum holding period is also embargoed, so positions
  carried across a seam never enter its tests.
- **Forward confirmation.** A hypothesis whose studies leave it too little confirmation
  window is confirmed on a stretch of the forward window fixed at its registration and
  starting after it (`calendar.FORWARD_TESTS`; H1, and H5, whose study runs into 2026 and
  whose forward window is entirely on the post-2026-08-14 Weather Company settlement
  source). It never opens the lockbox. It is decided alone at α/12 once its window has
  settled, while Holm runs over the other families' places at α·(12 − f)/12, so neither
  waits for the other (`gauntlet.decide`).

## You (the user)

1. Create a **read-only** prod API key (scope `read`), put it in `.env`, and start the
   recorder now (README). Every day without it is forward-window book data lost. Keep
   any key that can trade (demo) out of `.env`.
2. Add `commit_attribution = ""` to `~/.codex/config.toml` so Codex adds no co-author line.
3. When M4 asks, run the order round-trip benchmark on demo, and optionally from an AWS
   us-east-2 host.
4. Decide whether to buy sportsbook odds history (H12).
5. After M5: demo trading, then a small live stake. Compare live fills and PnL to the
   backtest.
