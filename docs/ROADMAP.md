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

- [x] **Phase 0.** API client, signing, settled-market backfill, REST book snapshots.
- [ ] **M0. Governance.** Agent-doc tree, attribution enforcement, hygiene tests, docs
  with one home per fact.
- [ ] **M1. Data and shared types.**
  - Fixed-point units and the bitmap-ladder order book.
  - Canonical markets/trades/candles tables (Becker dataset + Kalshi API), with each
    column classified as-listed or post-hoc.
  - Loaders that enforce the research calendar.
  - Book recorder and coverage table.
  - Dated fee history in `kalshi/fees.py`: rates, maker-fee series lists, multipliers,
    waivers, and pre-2026 cent rounding, each with its source.
  - Order-safety guard.
- [ ] **M2. Backtesting research.** Engine capability screen; validation toolkit (null
  generator, Romano–Wolf, PBO/CSCV, DSR); conformance simulations measuring false-positive
  rate and power. Pass rules are written down before any confirmation test runs.
- [ ] **M3. Strategy research.** In order:
  1. Pre-register H1–H12 (m = 12, category strata).
  2. Run every confirmation test.
  3. Apply Holm once.
  4. Open the lockbox once for all survivors.
  5. Write verdicts.
- [ ] **M4. Execution research.** Hot-path prototypes (Python, Rust, NautilusTrader) under
  burst replay; component benchmarks; round-trip measurements.
- [ ] **M5. Execution engine** on the M4 winner: OMS with an UNKNOWN state, risk,
  rate limiting, reconciliation, mock-exchange chaos tests.
- [ ] **M6. Close-out.** Docs, agent maps, one command for full local verification.

## Research calendar

Fixed before any confirmation test runs. In M1 the authoritative definition moves into
`src/kalshibot/research/calendar.py`, along with the verified Becker end date. After
that, this section links there instead of restating it.

Definitions (all instants UTC):
- **B** = 00:00 on the day after the last trade date in the Becker dataset.
- **S_h** = 00:00 on the day after the latest data date of any study that informed
  hypothesis h. S_h = B if no study did.

| Window | Interval (half-open) | Use |
|--------|----------------------|-----|
| Discovery | [start, B) | Exploration and parameter selection only. Published papers already mined it |
| Unused | [B, S_h) for hypothesis h | Neither explored nor tested for that hypothesis |
| Confirmation | [max(B, S_h), 2026-05-01) | One primary test per hypothesis |
| Lockbox | [2026-05-01, 2026-10-01) | Opened once, for all survivors together, and logged |
| Forward | [2026-10-01, …) | Paper and live-book tests with a pre-registered horizon |

- **Window assignment.** A trade belongs to the window of its decision time. The primary
  PnL marks positions still open at a window's end at the executable bid then.
  Settlement-date PnL is secondary and counts only markets whose *as-listed* scheduled
  close is inside the window. Trades are never filtered on actual settlement time.
- **No look-ahead across seams.** Anything fitted in a window sees only rows with
  `available_at` before the window's end (loader `as_of`), so labels that settle later are
  invisible. Each window's first maximum holding period is also embargoed, so positions
  carried across a seam never enter its tests.
- **H5 (weather).** Crosier's sample runs into 2026, so H5 is tested only in the forward
  window, which is entirely on the post-2026-08-14 Weather Company settlement source.

## You (the user)

1. Create a **read-only** prod API key (scope `read`) for book recording. Keep any key
   that can trade (demo) out of `.env`.
2. Add `commit_attribution = ""` to `~/.codex/config.toml` so Codex adds no co-author line.
3. When M4 asks, run the order round-trip benchmark on demo, and optionally from an AWS
   us-east-2 host.
4. Decide whether to buy sportsbook odds history (H12).
5. After M5: demo trading, then a small live stake. Compare live fills and PnL to the
   backtest.
