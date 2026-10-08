# Roadmap

The goal is an honest trading system, not a get-rich script. You should be able to
explain every piece: where the data comes from, why a backtest result isn't noise, how a
trade is sized, what stops it from blowing up, and how fast an order reaches the
exchange. Whether it makes money is a result to measure, not an assumption.

| # | Stage | Question | Where (planned) |
|---|-------|----------|-----------------|
| 1 | Ingest + store | Can I replay exactly what I knew at time *t*? | `src/kalshibot/data/` |
| 2 | Research | Which strategies have edge after fees, beyond what noise explains? | `src/kalshibot/research/`, [STRATEGIES](STRATEGIES.md) |
| 3 | Validate | Would this result survive the number of things we tried? | `src/kalshibot/research/validation/`, [BACKTESTING](BACKTESTING.md) |
| 4 | Execute | What is the fastest safe path from signal to order ack? | `src/kalshibot/execution/`, [EXECUTION](EXECUTION.md) |

## Milestones

- [x] **Phase 0.** API client, signing, settled-market backfill.
- [x] **M0. Governance.** Agent-doc tree, attribution enforcement, hygiene tests, docs
  with one home per fact.
- [ ] **M1. Data and shared types.**
  - Fixed-point units and the dict + price bitmap order book.
  - Canonical series/events/markets/fee-change tables and the trade tape (Becker
    dataset + Kalshi API).
  - Dated fee schedule (`kalshi/fee_history.py`).
  - Point-in-time loader enforcing the research calendar (`research/load.py`).
  - Trial ledger and lockbox gate.
  - WebSocket recorder with a coverage log.
- [ ] **M2. Backtesting research.**
  - Shared strategy types (`strategies/base.py`) and the Tier-1 path (fills, settlement).
  - Engine capability screen.
  - Validation toolkit: null generator, Romano–Wolf, PBO/CSCV, DSR.
  - Conformance simulations measuring false-positive rate and power.
  - Pass rules written down before any confirmation test runs.
- [ ] **M3. Strategy research.**
  - Pre-register H1–H12 (m = 12, category strata).
  - Run every confirmation test.
  - Apply Holm once.
  - Open the lockbox once for all survivors.
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

- **Window assignment.** A trade belongs to the window of its decision time. The primary
  PnL marks positions still open at a window's end at the executable bid then.
  Settlement-date PnL is secondary and counts only markets whose *as-listed* scheduled
  close is inside the window. Trades are never filtered on actual settlement time.
- **No look-ahead across seams.** Anything fitted in a window sees only data visible
  before the window's end (the loader's `as_of`), so labels that settle later are
  invisible. Each window's first maximum holding period is also embargoed, so positions
  carried across a seam never enter its tests.
- **H5 (weather).** Crosier's sample runs into 2026, so H5 is tested only in the forward
  window, which is entirely on the post-2026-08-14 Weather Company settlement source.

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
