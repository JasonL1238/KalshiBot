# Backtesting

How the results in [STRATEGIES.md](STRATEGIES.md) are produced, and why they can be
trusted. Everything here was fixed before any confirmation-window data was examined,
except what M3 fixes per family before its own confirmation test (the last list in
"Pipeline"); changing anything later is logged in the trial ledger like any other trial.

## Pipeline

1. **Data.** Research reads only through `research/load.py`: one trade tape (Kalshi's
   API; [RESEARCH §1](RESEARCH.md) explains why not Becker's), markets as they were
   listed at each instant, 1-minute candles (YES bid, YES ask and trade price) for the
   markets a family's null prices, and a lockbox the loader refuses until the ledger
   records its one unlock. Windows: `research/calendar.py`; rules:
   [ROADMAP](ROADMAP.md#research-calendar).
2. **Intents.** A strategy turns point-in-time data into intents: buy this contract, at
   this time, this many, up to this price (`strategies.base.INTENT_COLUMNS`). Families
   are evaluated only by `research/run.py`, which logs every trial in the ledger and
   keeps a decision only if its position must settle inside its window (the calendar
   rules).
3. **Tier 1, the verdicts.** `fills.py` fills intents conservatively from the tape, in
   whole contracts, and `settle.py` prices each fill with the costliest fee regime that
   could have applied, pays settled markets their settlement value, and marks open
   positions at the executable bid at window end (at zero until window-end bids are
   fetched). Tests are net of the fee rate itself: rounding each fill up to a cent
   depends on order size, which execution chooses, and would dwarf a small edge on the
   one-contract fills a tape-based backtest gives. Every trial also records its PnL with
   that rounding.
4. **Tier 2, model uncertainty.** The same taker intents run through two open-source
   engines that fill them their own way (below). `settle.py` prices their fills too, so
   the gap to Tier 1 is the fill model alone, and each engine's own PnL must equal
   `settle.py`'s on the same fills to the micro-dollar (`tests/test_engines.py`).
   STRATEGIES.md reports the spread with every verdict. Maker strategies get a Tier 2
   only from recorded order books.
5. **The gauntlet** (`research/validation/gauntlet.py`), once per hypothesis family:
   - discovery picks the configuration with the best Sharpe, all configurations on one
     zero-filled daily calendar;
   - confirmation tests that configuration once, against H0: net edge ≤ 0. Two tests
     must both reject: the market-calibrated null (outcomes drawn from de-vigged prices,
     one draw per cluster, recentred to zero edge), widened to each position's
     break-even variance, to the same-day comovement of clusters, and to the persistence
     of shocks across days, both measured on every configuration's discovery positions
     (below); and the stationary bootstrap of daily returns;
   - Holm across the 12 pre-registered families at family-wise α = 0.05 (a family not
     run counts as p = 1), with the families confirmed forward split off at α/12 each
     ([ROADMAP](ROADMAP.md#research-calendar));
   - in-calendar survivors are scored once more in the lockbox with the same test (Holm
     across them), its unlock logged in the ledger before any result is seen.

   DSR (on discovery, deflated for the trials), PBO, and the effective number of trials
   are reported as diagnostics. Why this test and not others: the conformance section.
6. **Verdicts.** Validated (passes the lockbox or a forward confirmation), Promising
   (passes confirmation; the lockbox or L2 data is pending), Rejected, or Inconclusive:
   the family's power at its smallest economically meaningful edge, simulated on its own
   confirmation sample with the same machinery, is below 0.8.

The null covers contracts held to settlement (step 2 keeps only decisions whose
positions must settle inside their window). A position still open at the window's end
counts as lost in the null, which can only make the test stricter; a scalar settlement
counts as the share of $1 it paid. Families whose positions exit early (market making,
momentum) need a path null, specified and run through conformance before their
confirmation test.

- **Prices** (`research/quotes.py`): a market's mid at the decision is the midpoint of
  the closing YES bid and ask of its last 1-minute candle ending by then, an empty side
  at its bound (no bid: 0, no ask: $1), searched for up to 7 days back but not before
  the market was listed. A mutually exclusive event is de-vigged multiplicatively over
  every market listed and still open by then (a closed one shows an emptied book, and
  it lost), so an event missing any member's quote leaves the null,
  as does any other contract without its own; the ledger counts them. Probabilities
  are clipped to [0.5%, 99.5%] (`null.inputs`).
- **Draws** (`quotes.py` reads them off Kalshi's tickers and strikes; `null.inputs`):
  an exclusive event's buckets share one draw, laid out in strike order at every
  snapshot. So does a strike ladder on one underlying in any other event,
  whatever Kalshi calls its strike type: markets whose ticker ends in a strike, grouped
  by the ticker less that strike (BTC at one fix; one player's points; one team's
  margin in a spread event), "below X" on [0, q) and "above X" on [1 − q, 1), so they
  settle monotonically. Every other contract, such as two players' goal-scorer markets,
  has its own draw, and the widening measures how draws move together. A cluster
  counts on the day its last position settles.
- **Dependence sample:** every configuration's discovery positions decided in its last
  400 days (`Gates.dependence_days`), each bet once: contiguous, so clusters up to
  ⌈√D⌉ days apart pair up often (D: the days on which they settle), and nearest the
  confirmation window, since Kalshi's mix of markets keeps changing. Conformance's
  discovery is 400 days, so there the sample is all of it; a 200-day sample proved too
  noisy (the gauntlet's size at Holm's level reached 0.8% when shocks last for days and
  confirmation is busier). Conformance includes a world whose dependence appears only
  after the seam.

Fixed per family at M3 pre-registration, before its confirmation test: the minimum
economically meaningful edge and the power there; whether it is confirmed forward; the
maximum holding period (which sets both embargoes and which markets count); the taker
rule's candle leg (the worse of the next 1-minute candle's extreme and the next print,
or the print alone); the window-end bids that mark open positions (until they're wired
in, zero); and, where needed, the path null.

## Fill models

| | Taker fill | Size | Maker fill |
|---|---|---|---|
| Tier 1 (`fills.py`) | next print by a same-side taker after decision + latency and within `max_wait` (default 5 min); it shows the ask | ≤ 10% of that print (`participation`), in whole contracts | strict trade-through; (1 − haircut) × participation of each print through the bid, in whole contracts |
| NautilusTrader (`replay.py`) | the market's last print when the order arrives, however old; a limit fills only within it, the rest canceled | that print's size, the rest one tick worse | not replayed |
| vectorbt (`bars.py`) | last print at the end of the first 1-minute bar after decision + latency, however old | the whole order | not modeled |

On the test tape, a 10-contract YES buy decided at 00:00:30 fills at 43¢ in Tier 1, at
41¢ for 2 and 41.01¢ for 8 in NautilusTrader, and at 42¢ in vectorbt. An order for a
market whose last print was minutes earlier fills at that stale price in both engines and
not at all in Tier 1. NautilusTrader makers aren't replayed: on a book made of prints, a
resting bid at the last price fills completely at the next event of any kind. The engines
do what typical backtests on trade data do; Tier 1 is deliberately harsher.

## Engine screen (2026-10-08)

Each engine was installed and run on the same two-market weekend tape: a signal, a
sub-penny fill at the next print, a per-fill Kalshi fee, binary settlement, and 1M events
for speed (Apple M4 Pro, Python 3.13; LEAN runs its own 3.11). Qlib was reviewed from
source only. The probes, with how to run each, are in `benchmarks/engine_screen/` at
commit 754e472.

✓ native, ◐ with a workaround, ✗ no.

| Engine | Version | Many markets | Binary settlement | Per-fill fee | 24/7 | Trades only | L2 / queue | Latency | Py 3.13, maintained | 1M events | License | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | 1.230.0 | ✓ | ◐ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 2.7 s | LGPL-3.0 | Keep: Tier-2 replay; L2 later |
| [hftbacktest](https://github.com/nkaz001/hftbacktest) | 2.4.4 | ✓ | ✗ | ✗ | ✓ | ✗ | ✓ | ✓ | ✓ | 0.07 s | MIT | Candidate for market making on recorded books (not installed) |
| [vectorbt](https://github.com/polakowo/vectorbt) | 1.0.0 | ✓ | ◐ | ✓ | ✓ | ◐ | ✗ | ✗ | ✓ | 0.9 s | Apache-2.0 + Commons Clause | Keep: Tier-2 bar closes |
| [backtrader](https://github.com/mementum/backtrader) | 1.9.78.123 | ✓ | ◐ | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | 44 s | GPL-3.0 | Reject |
| [Backtesting.py](https://github.com/kernc/backtesting.py) | 0.6.6 | ✗ | ◐ | ✓ | ✓ | ✗ | ✗ | ✗ | ✓ | 5.5 s | AGPL-3.0 | Reject |
| [zipline-reloaded](https://github.com/stefan-jansen/zipline-reloaded) | 3.1.1 | ✓ | ✗ | ✓ | ◐ | ✗ | ✗ | ✗ | ✓ | 3.9 s | Apache-2.0 | Reject |
| [LEAN](https://github.com/QuantConnect/Lean) | 2.5 (Docker) | ✓ | ✗ | ◐ | ✓ | ✓ | ✗ | ✗ | ✗ | 23 s | Apache-2.0 | Reject |
| [Qlib](https://github.com/microsoft/qlib) | 0.9.7 | ✓ | ✗ | ✗ | ◐ | ✗ | ✗ | ✗ | ✗ | — | MIT | Reject |

- **NautilusTrader:** BinaryOption doesn't expire, so settlement is an injected
  InstrumentClose; several engines per process need logging bypassed. The fastest
  event-driven engine with a Python strategy.
- **hftbacktest:** the best queue models (probabilistic L2, L3 FIFO), but linear fees
  only and no settlement, and on trades-only data a market buy "filled" at 9.2e14 with no
  error.
- **vectorbt:** settlement is a final row (an exit at $0 is rejected, so positions are
  valued there instead); a trade tape must be pivoted into a price matrix.
- **backtrader, Backtesting.py:** each print becomes a fake OHLC bar; Backtesting.py
  runs one instrument at a time, whole units only, and needs two synthetic bars to settle.
  backtrader's last release was 2023.
- **zipline-reloaded:** minute bars only, prices truncated to 1/1000 by default, a $0
  price reads back as NaN, fractional orders silently rounded.
- **LEAN:** a $0 settlement silently fills at the stale last price; fees see the
  security's price, not the fill's; a 19 GB image running its own Python 3.11.
- **Qlib:** a bar-based cross-sectional stock platform with linear costs; no Python 3.13
  wheels.

Every engine needed a settlement workaround, and three (zipline, LEAN, hftbacktest on
trades-only data) gave silently wrong numbers without one. That is why `settle.py` is
the reference, and every engine's own PnL is reconciled against it.

## Conformance: how the confirmation test was chosen

> Being re-measured for M3's dependence sample (discovery's last 400 days) and its new
> world (a shock from the seam on); until then the tables below are M2's.

`research/validation/conformance.py` runs the whole pipeline (selection, then the
candidate tests) on synthetic worlds whose truth is known (`synthetic.py`). The family
buys longshots as a taker paying 2026 fees: every contract whose YES ask is at most θ,
for 7 values of θ from 6¢ to 20¢, buying NO (the favorite–longshot trade) or YES (its
mirror); a mixed family adds a configuration buying YES favorites (asks of 80¢ or more).
A world has 20 events a day (or 100), half standalone binaries and half ladders of 3–7
buckets; discovery is 400 days and confirmation the next 100 (the real calendar's ratio
is about ten), and the dependence sample is the runner's (above). The worlds:

- **market:** prices are right, so the family loses the spread and fee;
- **boundary:** every bought ask is the highest price that still breaks even after the
  fee, so every configuration has zero edge (the least favorable null);
- **shocks:** events on a day share a factor of strength ρ ("binaries only" makes the
  dependence starkest), in some worlds lasting for days (AR(1) with persistence φ);
- **YES and mixed families:** for a longshot buyer the mid understates the variance, and
  a shock moves YES and NO holders apart;
- **strike ladders:** nested strike contracts settled by one draw;
- **seams:** an edge that exists only in discovery (the post-publication trap) or halves
  at the seam; a confirmation twice as busy as discovery; a shock that starts at the
  seam, so discovery can't see the dependence;
- **edges:** asks lowered by a few cents on contracts with true P(YES) below 15%.

A procedure holds its size if, in every world without an edge, it finds one at most
α + 2 Monte Carlo standard errors of the time, at α = 0.05 and at 0.05/12 (Holm's first
step with 12 families). Cells are "% at α = 0.05 / % at 0.05/12"; bold is over the
limit. ρ̂, cross-day and widening are the gauntlet's mean estimates and null widening;
Romano–Wolf tests every configuration of the family, favorites included.

| No-edge world | n | ρ̂ | cross-day | widening | naive in-sample | naive confirmation | null | bootstrap | null + bootstrap | null + DSR | null + DSR + PBO | Romano–Wolf | widened null | gauntlet |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| market prices: buying NO loses spread + fee | 400 | 0.002 | -0.00 | 1.03 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 | 0.0 / 0.0 |
| boundary | 2000 | 0.003 | -0.00 | 1.04 | **22.0** / **3.6** | **7.1** / **1.2** | 4.2 / 0.2 | **8.6** / **2.6** | 4.1 / 0.2 | 0.5 / 0.1 | 0.0 / 0.0 | 3.5 / 0.2 | 3.7 / 0.1 | 3.7 / 0.1 |
| boundary, 100 events/day | 1000 | 0.000 | -0.00 | 1.05 | **20.4** / **3.4** | 5.6 / 0.5 | 4.1 / 0.2 | **7.0** / 0.6 | 4.1 / 0.2 | 0.4 / 0.0 | 0.0 / 0.0 | 2.9 / 0.0 | 3.8 / 0.2 | 3.8 / 0.2 |
| boundary, Shin de-vig | 1000 | 0.003 | -0.01 | 1.05 | **24.0** / **4.1** | **7.5** / **1.7** | 5.3 / 0.3 | **7.8** / **2.8** | 5.1 / 0.3 | 0.4 / 0.0 | 0.2 / 0.0 | 5.5 / 0.2 | 5.1 / 0.1 | 4.9 / 0.1 |
| boundary, same-day shock ρ = 0.5 | 2000 | 0.024 | -0.01 | 1.12 | **23.9** / **5.0** | **7.1** / **1.6** | 4.3 / 0.3 | **8.2** / **2.5** | 4.3 / 0.3 | 0.4 / 0.1 | 0.1 / 0.1 | 4.8 / 0.5 | 3.7 / 0.1 | 3.7 / 0.1 |
| boundary, binaries only, shock ρ = 0.5 | 2000 | 0.190 | -0.02 | 1.34 | **24.1** / **7.0** | **12.2** / **5.9** | **6.6** / 0.4 | **16.9** / **8.9** | **6.3** / 0.4 | 0.4 / 0.0 | 0.0 / 0.0 | **7.2** / 0.7 | 2.6 / 0.1 | 2.6 / 0.1 |
| boundary, binaries only, shock ρ = 0.8, half spread 0.5¢ | 2000 | 0.424 | -0.03 | 1.93 | **18.2** / **5.8** | **12.3** / **5.5** | **10.2** / **2.0** | **14.4** / **8.4** | **8.9** / **1.8** | 0.4 / 0.1 | 0.1 / 0.0 | **13.2** / **4.0** | 3.4 / 0.2 | 3.4 / 0.2 |
| boundary, binaries only, shock ρ = 0.3, half spread 0.5¢, 100 events/day | 1000 | 0.107 | -0.01 | 2.27 | **14.8** / **2.6** | **9.5** / **2.4** | **13.0** / **3.3** | **11.6** / **3.9** | **9.5** / **1.9** | 0.4 / 0.1 | 0.0 / 0.0 | **16.1** / **5.0** | 3.8 / 0.1 | 3.8 / 0.1 |
| boundary, binaries only, shock ρ = 0.5, confirmation twice as busy | 2000 | 0.193 | -0.02 | 1.74 | **21.9** / **7.0** | **11.3** / **3.8** | **8.5** / **1.1** | **12.8** / **7.2** | **7.3** / **0.9** | 0.5 / 0.1 | 0.1 / 0.0 | **11.6** / **3.1** | 3.1 / 0.1 | 3.1 / 0.1 |
| YES family: boundary | 2000 | 0.002 | -0.00 | 1.22 | **15.5** / **1.5** | 4.6 / 0.3 | **6.6** / 0.7 | 5.2 / 0.5 | 4.8 / 0.4 | 0.5 / 0.1 | 0.1 / 0.0 | **7.8** / **0.8** | 4.7 / 0.2 | 4.2 / 0.1 |
| YES family: boundary, binaries only, shock ρ = 0.5 | 2000 | 0.237 | -0.02 | 2.11 | **7.5** / 0.1 | 2.1 / 0.0 | **12.7** / **3.2** | 2.0 / 0.0 | 2.0 / 0.0 | 0.9 / 0.3 | 0.2 / 0.1 | **15.4** / **4.5** | 5.9 / **0.8** | 1.9 / 0.0 |
| edge in discovery only (2¢, gone at the seam) | 2000 | 0.002 | -0.00 | 1.07 | **100.0** / **100.0** | 5.7 / 0.5 | 5.0 / 0.4 | **6.6** / **1.1** | 4.9 / 0.3 | 5.0 / 0.4 | 3.6 / 0.2 | 4.4 / 0.2 | 4.4 / 0.1 | 4.3 / 0.1 |
| mixed family (adds YES favorites): binaries only, shock ρ = 0.3, half spread 0.5¢, 100 events/day | 1000 | 0.104 | 0.03 | 2.71 | **16.3** / **2.7** | **9.8** / **3.2** | **12.1** / **3.1** | **11.2** / **4.7** | **9.6** / **2.2** | 0.1 / 0.0 | 0.1 / 0.0 | **14.0** / **5.6** | 3.8 / 0.1 | 3.8 / 0.1 |
| boundary, binaries only, shock ρ = 0.5 lasting days (φ = 0.5) | 2000 | 0.192 | 0.24 | 1.86 | **25.6** / **10.2** | **15.8** / **8.6** | **10.4** / **1.8** | **20.1** / **11.5** | **10.2** / **1.7** | 1.2 / 0.3 | 0.2 / 0.1 | **13.7** / **4.3** | 2.7 / 0.0 | 2.7 / 0.0 |
| boundary, binaries only, shock ρ = 0.5 lasting days (φ = 0.8) | 2000 | 0.192 | 0.92 | 3.07 | **35.3** / **20.8** | **25.9** / **17.2** | **18.5** / **5.7** | **28.9** / **20.7** | **17.9** / **5.5** | 4.7 / **2.1** | 0.5 / 0.4 | **25.4** / **14.4** | 2.9 / 0.2 | 2.9 / 0.2 |
| mixed family, binaries only, shock ρ = 0.5 lasting days (φ = 0.8) | 2000 | 0.179 | 1.11 | 4.08 | **36.9** / **21.4** | **23.0** / **14.5** | **16.2** / **5.1** | **26.2** / **17.6** | **14.4** / **4.9** | 2.3 / **0.9** | 0.5 / 0.3 | **30.1** / **15.3** | 1.0 / 0.0 | 1.0 / 0.0 |
| boundary, binaries only, shock ρ = 0.5 lasting days (φ = 0.8), confirmation twice as busy | 2000 | 0.191 | 0.90 | 5.07 | **36.6** / **21.9** | **30.0** / **17.8** | **24.2** / **10.7** | **28.6** / **20.8** | **20.5** / **8.5** | **6.2** / **3.5** | 0.9 / 0.6 | **33.1** / **22.3** | 3.1 / 0.2 | 3.0 / 0.2 |
| boundary, ladders all strike ladders, same-day shock ρ = 0.5 | 2000 | 0.215 | -0.02 | 1.53 | **23.2** / **6.3** | **11.2** / **5.1** | **7.3** / 0.7 | **15.2** / **8.2** | **6.6** / 0.5 | 0.2 / 0.0 | 0.0 / 0.0 | **10.1** / **2.5** | 2.6 / 0.1 | 2.6 / 0.1 |

- Selecting on the same data that is then tested (naive in-sample) finds an edge
  7.5–37% of the time where none exists, and every time when it existed only before the seam.
- A t-test or bootstrap on confirmation alone, Romano–Wolf, and the plain null break
  under dependence (same-day, or lasting days) or for YES buyers; the null with a DSR
  gate breaks when shocks last.
- Two procedures hold their size in every world: the gauntlet, and the null gated by
  both DSR and PBO, which does it by rarely rejecting (below). The widened null alone
  breaks at Holm's level for YES buyers under a shock (0.8% against 0.71%), where the
  bootstrap's daily view rescues it, so the gauntlet requires both, for at most about 5
  points of power.

Power, as the share of worlds where an edge is found (same cells):

| Edge world | true net edge per position | positions/day | naive in-sample | null | null + DSR | null + DSR + PBO | Romano–Wolf | widened null | gauntlet |
|---|---|---|---|---|---|---|---|---|---|
| 0.5¢ lower NO asks on q < 0.15 | +0.46¢ | 17.2 | 85.2 / 47.8 | 14.2 / 2.2 | 10.2 / 2.0 | 2.0 / 0.5 | 15.5 / 2.5 | 14.0 / 1.8 | 14.0 / 1.5 |
| 1¢ lower NO asks on q < 0.15 | +0.91¢ | 20.8 | 99.5 / 96.2 | 51.0 / 12.5 | 50.0 / 12.0 | 15.5 / 3.0 | 49.8 / 15.5 | 48.5 / 11.2 | 48.5 / 11.2 |
| 2¢ lower NO asks on q < 0.15 | +1.77¢ | 25.3 | 100.0 / 100.0 | 97.8 / 81.2 | 97.8 / 81.2 | 74.0 / 61.3 | 98.8 / 86.2 | 97.2 / 77.0 | 97.0 / 75.8 |
| 3¢ lower NO asks on q < 0.15 | +2.62¢ | 25.3 | 100.0 / 100.0 | 100.0 / 97.2 | 100.0 / 97.2 | 95.5 / 93.2 | 100.0 / 99.0 | 100.0 / 97.0 | 100.0 / 97.0 |
| 0.25¢ lower NO asks on q < 0.15, 100 events/day | +0.23¢ | 96.9 | 88.2 / 51.5 | 26.0 / 4.8 | 21.0 / 3.5 | 3.8 / 0.8 | 23.2 / 4.0 | 25.0 / 3.8 | 24.5 / 3.8 |
| 0.5¢ lower NO asks on q < 0.15, 100 events/day | +0.46¢ | 111.2 | 100.0 / 99.5 | 59.0 / 19.5 | 59.0 / 19.5 | 20.2 / 6.2 | 62.0 / 23.8 | 56.5 / 17.5 | 56.2 / 17.2 |
| 1¢ lower NO asks on q < 0.15, 100 events/day | +0.90¢ | 125.5 | 100.0 / 100.0 | 98.2 / 87.2 | 98.2 / 87.2 | 75.8 / 69.0 | 98.8 / 89.5 | 97.8 / 84.8 | 97.8 / 83.5 |
| 2¢ lower NO asks on q < 0.15, halving to 1¢ at the seam | +0.89¢ | 25.8 | 100.0 / 100.0 | 57.2 / 16.5 | 57.2 / 16.5 | 43.0 / 12.5 | 52.2 / 18.0 | 54.0 / 15.8 | 53.5 / 15.5 |
| YES family: 1¢ lower YES asks on q < 0.15 | +1.02¢ | 24.5 | 100.0 / 99.2 | 71.5 / 37.2 | 71.5 / 37.2 | 10.8 / 5.8 | 77.5 / 42.5 | 67.2 / 27.5 | 65.8 / 22.2 |
| YES family: 2¢ lower YES asks on q < 0.15 | +2.09¢ | 26.1 | 100.0 / 100.0 | 99.8 / 98.5 | 99.8 / 98.5 | 35.0 / 34.2 | 100.0 / 99.5 | 99.5 / 97.2 | 99.5 / 96.0 |
| YES family: 3¢ lower YES asks on q < 0.15 | +3.16¢ | 26.6 | 100.0 / 100.0 | 100.0 / 100.0 | 100.0 / 100.0 | 52.0 / 52.0 | 100.0 / 100.0 | 100.0 / 100.0 | 100.0 / 100.0 |

A 1¢ edge on about 20 positions a day for 100 days is found only 11% of the time at
Holm's level, and 84% of the time with five times as many events a day. Each family's own sample
sets its power, which is why a verdict can be Inconclusive. Reproduce with
`OMP_NUM_THREADS=1 python -m kalshibot.research.validation.conformance`, which prints both
tables (about 2 hours on 14 cores, Python 3.13; each world's seed is fixed) at the commit
that adds this section. It uses 2,000 draws per test (`conformance.GATES`); the
pre-registered gauntlet uses 20,000 (`gauntlet.PREREGISTERED`). The p-value is valid at
any count, and fewer draws only lose power.

## Positive control

The pipeline should reproduce what is already known before it is trusted on anything
new: on discovery data, takers who buy longshots win less often than the price paid,
and favorites are roughly fair (the favorite–longshot pattern, STRATEGIES H1).
`research/calibration.py` tabulates it through the loader; it checks the data layer
(tape, results, side conventions), while the backtest path is checked by known-answer
tests and the engine reconciliation. The criterion, in contract-weighted 5¢ bins of the
taker's price over discovery (trades before the calendar's seam, on markets settled by
then): every bin below 20¢ wins less often than its average price, the 0–5¢ bin's gross
return is below −20%, and every bin from 85¢ up is within ±3%. It was set after a
preview on Becker's subset of the same period through the M1 loader, which met it (not
reproducible since the loader moved to the API tape); `pytest -m data` checks it. Pending the discovery-period API tape (backfilling).

## Pitfalls every backtest here must handle

1. **Information time ≠ nominal time.** Research sees a market only after it was listed,
   and its post-hoc columns (result, volume, an early close) only after it settled
   (`research/load.py`). External series (forecast runs, macro releases, index closes)
   are keyed by publication time, not observation time, when they arrive in M3.
2. **Executable prices.** Trade at the ask or bid that existed after the decision,
   never at the last trade or the print that triggered the signal.
3. **Dated fees.** Fees vary by series and change over time. Price every fill with
   the terms in force then (`kalshibot.kalshi.fee_history`), and bracket windows where
   the switch date is unknown (`FeeTerms.uncertain`).
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
