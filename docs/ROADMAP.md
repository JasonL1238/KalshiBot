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
  - Positive control: the favorite–longshot pattern on discovery data (met:
    [BACKTESTING](BACKTESTING.md#positive-control)).
- [ ] **M3. Strategy research.**
  - Shared strategy types (`strategies/base.py`).
  - The research runner: refuses to run without the ledger, logs every evaluation
    before showing it, and allows one confirmation test per family.
  - Per-family candle fetching (`research/run.py`): the null's quotes, then the candle
    leg of the taker rule and window-end bids.
  - Each confirmation records its power at the family's smallest meaningful edge
    (checked in conformance) and its results by category, and gets a Tier-2 report
    after its verdict.
  - Round-trip accounting and a path null for families that exit before settlement,
    chosen in conformance first (candidates: [BACKTESTING](BACKTESTING.md)).
  - A recorded-stretch selection window in `calendar.py` and `run.py` for families
    selected on recordings, with its dependence sample, and size and power checked in
    conformance for a stretch of its length.
  - For the fast, maker and lag families ([STRATEGIES](STRATEGIES.md)), in order:
    - the discovery tape days their explorations used, then those explorations re-run
      as `research discover` configurations;
    - keyless capture on an always-on host, until the keyed recorder runs: REST book
      snapshots of the markets they trade (built: `kalshibot record --rest`), the
      incentive programs (built: a `backfill reference` table), and logs of when external
      data arrives (ESPN plays, NWS daily summaries and METARs, Coinbase and Kraken
      trades);
    - Tier-1 maker fills per print, capped at the sweep, with the null priced at fill
      time;
    - external data keyed by when it became available;
    - latency as a pre-registered parameter: PnL against latency, a negative-latency
      canary, a time-shift placebo;
    - after weeks of recorded books, queue-model fills for makers (hftbacktest,
      NautilusTrader), calibrated on the user's own fills (You, item 7): a Tier 2 for
      makers, and the verdict tier for touch makers (BACKTESTING step 3).
  - Pre-register H1–H12 (m = 12, category strata), each with its minimum economically
    meaningful edge.
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
  window (counted in decision days left before the lockbox, less the embargoes at its
  maximum holding period), or whose data exists only from recordings, is confirmed on a stretch of the forward window fixed at its registration and
  starting after it (`calendar.FORWARD_TESTS`): H1; H5, whose study runs into 2026 and
  whose forward window is entirely on the post-2026-08-14 Weather Company settlement
  source; and H11, whose data (order books) exists only from the recorder's start, so
  it selects on a recorded stretch that ends before its registration. A forward family
  never opens the lockbox. It is decided alone at α/12 once its window has settled,
  while Holm runs over the other families' places at α·(12 − f)/12, so neither waits for
  the other (`gauntlet.decide`). Which families are forward (`calendar.FORWARD`) is
  final before Holm runs.

## You (the user)

1. Create a **read-only** prod API key (scope `read`), put it in `.env`, and start the
   recorder now (README; until the key exists, the keyless `record --rest`) on an
   always-on host, not a laptop (where: [EXECUTION](EXECUTION.md), Hosting). Every day
   without it is forward-window book data lost, and the market-making families need
   weeks of it. Keep any key that can trade (demo) out of `.env`.
2. Add `commit_attribution = ""` to `~/.codex/config.toml` so Codex adds no co-author line.
3. When M4 asks, run the order round-trip benchmark on demo, and optionally from an AWS
   us-east-2 host.
4. Decide whether to buy sportsbook odds history (H12).
5. Rule on H8's gate: may sweep counts from forward days before H8's registration (no
   outcomes) show whether sweeps survived the end of market orders?
6. Keep H6 ([STRATEGIES](STRATEGIES.md#fast-maker-and-lag-families-scoped-2026-10-09)),
   or drop it?
7. Optionally, before M5, place a few tiny orders by hand to calibrate the makers'
   queue model (M3).
8. After M5: demo trading, then a small live stake. Compare live fills and PnL to the
   backtest.
