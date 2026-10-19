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
3. **Tier 1, the verdicts.** `fills.py` fills intents from the tape (Fill models), in
   whole contracts, and `settle.py` prices each fill with the costliest fee regime that
   could have applied, pays settled markets their settlement value, and marks open
   positions at the executable bid at window end (at zero until window-end bids are
   fetched). Tests are net of the fee rate itself: rounding each fill up to a cent
   depends on order size, which execution chooses, and would dwarf a small edge on the
   one-contract fills a tape-based backtest gives. Every trial also records its PnL with
   that rounding. One exception: trade-throughs can't show fills at the touch (queue
   position is invisible, Pitfalls), so a family that quotes at the touch (H11) takes
   its verdict from recorded books instead, filled by queue models calibrated on real
   fills, and must pass under the most pessimistic of them.
4. **Tier 2, model uncertainty.** The same taker intents run through two open-source
   engines that fill them their own way (below). `settle.py` prices their fills too, so
   the gap to Tier 1 is the fill model alone, and each engine's own PnL must equal
   `settle.py`'s on the same fills to the micro-dollar (`tests/test_engines.py`).
   `run.report` runs them on each confirmation after its verdict (in batches of
   markets) and logs the spread, which STRATEGIES.md reports with every verdict. Maker
   strategies get a Tier 2 only from recorded order books.
5. **The gauntlet** (`research/validation/gauntlet.py`), once per hypothesis family:
   - discovery picks the configuration with the best Sharpe, all configurations on one
     zero-filled daily calendar (for a family selected on recordings, its recorded
     stretch stands in for discovery throughout:
     [ROADMAP](ROADMAP.md#research-calendar)). So a family holds one mechanism: one
     bundling several would confirm only the steadiest;
   - confirmation tests that configuration once, against H0: net edge ≤ 0. Two tests
     must both reject: the market-calibrated null (outcomes drawn from de-vigged prices,
     one draw per cluster, recentred to zero edge), widened to each position's
     break-even variance, to the same-day comovement of clusters, and to the persistence
     of shocks across days, both measured on every configuration's discovery positions
     (below; for resting bids' fills, the Maker null); and the stationary bootstrap of
     daily returns;
   - Holm across m = 12 places, one per hypothesis, at family-wise α = 0.05 (a place
     with no family run counts as p = 1), with the families confirmed forward split off
     at α/12 each ([ROADMAP](ROADMAP.md#research-calendar));
   - in-calendar survivors are scored once more in the lockbox with the same test (Holm
     across them), its unlock logged in the ledger before any result is seen.

   DSR (on discovery, deflated for the trials), PBO, and the effective number of trials
   are reported as diagnostics. Why this test and not others: the conformance section.
6. **Verdicts.** Validated (passes the lockbox or a forward confirmation), Promising
   (passes confirmation; the lockbox or L2 data is pending), Rejected, or Inconclusive:
   the family's power at its smallest economically meaningful edge is below 0.8.
   `gauntlet.power` simulates it on the family's own confirmation sample before any
   outcome is read (and the start of the test records it): every position wins with
   its break-even probability plus that edge, under the null's coupling, each day's
   deviation scaled up by the dependence the null is widened for, applied to those
   probabilities but never below its same-day part (the real bootstrap sees each day's
   own variance, whatever the cross-day estimate), and both tests run on each simulated
   world at α/12. Each position stands dated by the last time its cluster is priced
   (Prices, below), which is never after the cluster's last settlement; where
   settlements bunch more than those times do, that spreads the days and overstates
   power, so a family's registration compares the two datings on discovery, where
   outcomes may be read. Both legs see only the positions the null prices; the rest
   (the ledger's `dropped_from_null`) can move the real bootstrap's power either way, by
   how their edge per unit of risk differs. Like the null, power relies on YES and NO
   positions offsetting under one ρ, which understates a day's variance when one
   configuration buys both sides; such a configuration needs a mixed-side world in
   conformance first. A family of resting bids held to settlement (Tier 1's makers)
   needs a maker world first too: its fills come after sweeps against its bids and are
   priced then (Prices, below), a selection no taker world makes, so the null's size and
   power's estimate are checked on simulated price paths whose bids fill that way.
   Conformance checks the estimate against the gauntlet's measured power, over all
   worlds and over those whose cross-day estimate is negative (the third table below).
   It passes if predicted power never exceeds measured by more than 2 Monte Carlo
   standard errors of their per-world difference (bold). A bold taker cell sends
   `gauntlet.power` back for revision before any confirmation runs; a maker row in which
   the gauntlet does not hold its size, or a maker row in bold in the third table, holds
   the confirmation of every family of resting bids held to settlement until a revised
   method passes the maker rows (the Maker null, below, passes them: Conformance,
   Makers). An H1-shaped row in which the gauntlet does not
   hold its size, or in bold in the third table, holds H1's confirmation until a revised
   method passes those rows: a logged change to H1's registered method, with the user's
   sign-off. H1's coupling R check (the pre-registration paragraph) holds it too, until it
   passes.

The null covers contracts held to settlement (step 2 keeps only decisions whose
positions must settle inside their window). A position still open at the window's end
counts as lost in the null, which can only make the test stricter; a scalar settlement
counts as the share of $1 it paid. Families whose positions exit early (market making,
quick round trips) take their exit horizon as their maximum holding period, mark what
inventory is left at the window's end, and need a path null, specified and run through conformance before
their confirmation test. Candidates: block-resampled, sign-randomized increments
within a cluster, which assume a symmetry bounded binary prices lack; and random-entry
or circular-shift permutations, which test timing rather than net edge ≤ 0, so they
run recentred to zero net edge or alongside the bootstrap. Conformance picks the
candidate that holds its size in every world, then the most powerful. Such a family's
power is simulated on round-trip profit, exit costs included.

- **Prices** (`research/quotes.py`): a position is priced at its decision, a maker's at
  its fill (its bid rests while the market moves). A market's mid then is the midpoint
  of the closing YES bid and ask of its last 1-minute candle ending by then, an empty side
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
- **Maker null** (`gauntlet._null`, for a sample holding Tier 1's maker fills), in
  place of the nested intervals' variance. A resting bid's fills on one contract all
  settle on its outcome, so each such cluster's boundary variance is their exact
  covariance: each fill covaries with every earlier one by its own variance, the larger
  of its null and break-even ones (`null._shared_outcome_variance`). The cross-day
  correlations count with equal weights, not Bartlett's, which understate a lasting
  shock. The widening is then raised by its own delete-a-block jackknife standard error,
  over 10 contiguous blocks of the dependence sample's days (`null.jackknife_se`), since
  that estimate is noisy when shocks last. `gauntlet.power` simulates its worlds with
  the point estimate and tests them against the raised null, as the real test is. The
  candidates screened against it and why it was chosen: Conformance, Makers.
- **Dependence sample:** every configuration's discovery positions decided in its last
  400 days (`Gates.dependence_days`), each bet once: contiguous, so clusters up to
  ⌈√D⌉ days apart pair up often (D: the days on which they settle), and nearest the
  confirmation window, since Kalshi's mix of markets keeps changing. Conformance's
  discovery is 400 days, so there the sample is all of it; a 200-day sample proved too
  noisy (the gauntlet's size at Holm's level reached 0.8% when shocks last for days and
  confirmation is busier). Conformance includes a world whose dependence appears only
  after the seam.

Fixed per family at M3 pre-registration, before its confirmation test: the minimum
economically meaningful edge (`Strategy.min_edge`); whether it is confirmed forward; the
maximum holding period (which sets both embargoes and which markets count); the taker
rule's candle leg (the worse of the next 1-minute candle's extreme and the next print,
or the print alone); the window-end bids that mark open positions (until they're wired
in, zero); where needed, the path null; and, for a touch maker, its queue models and
their calibration. H1, before its confirmation test, checks its coupling (Conformance,
Coupling R) against the H1-shaped rows: its largest R̄ among its configurations, each on its own
discovery positions and computed by the runner ([ROADMAP](ROADMAP.md#milestones) M3),
must be at most some passing row's R; a family above every row's R needs a row at its R̄
first. That is conservative where R̄ ≥ R (Coupling R, below), and a row's R is a mean
over the configurations its worlds selected. A family of resting bids held to
settlement needs no such check: on a standalone contract the Maker null's variance is
at least the boundary's (R ≤ 1). It trades standalone contracts only, unless a bucket
or ladder maker row passes first (`Positions.from_frame` refuses maker fills on several
contracts of one cluster).

## Fill models

| | Taker fill | Size | Maker fill |
|---|---|---|---|
| Tier 1 (`fills.py`) | next print by a same-side taker after decision + latency and within `max_wait` (default 5 min); it shows the ask | ≤ 10% of that print (`participation`), in whole contracts | strict trade-through; one fill per sweep, of (1 − haircut) × participation of what it traded through the bid, in whole contracts |
| NautilusTrader (`replay.py`) | the market's last print when the order arrives, however old; a limit fills only within it, the rest canceled | that print's size, the rest one tick worse | not replayed |
| vectorbt (`bars.py`) | last print at the end of the first 1-minute bar after decision + latency, however old | the whole order | not modeled |

On the test tape, a 10-contract YES buy decided at 00:00:30 fills at 43¢ in Tier 1, at
41¢ for 2 and 41.01¢ for 8 in NautilusTrader, and at 42¢ in vectorbt. An order for a
market whose last print was minutes earlier fills at that stale price in both engines and
not at all in Tier 1. NautilusTrader makers aren't replayed: on a book made of prints, a
resting bid at the last price fills completely at the next event of any kind. The engines
do what typical backtests on trade data do; Tier 1 is deliberately harsher about stale
prints.

Tier 1's taker fill is capped by the intent's limit but has no floor, so the next
same-side print, up to `max_wait` later, can come far below the ask at decision time: in
H1's discovery, a third of YES ≥ 80¢ fills came more than 2¢ below the deciding print,
21¢ below on average. Holding it near the decision-time ask is planned
([ROADMAP](ROADMAP.md#milestones) M3).

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
> worlds (a shock from the seam on, the makers and the H1-shaped rows); until then the
> two tables below are M2's, and the third (`python -m
> kalshibot.research.validation.conformance power`) is pending, except the maker rows
> (Makers, below), measured under the Maker null. `--only makers:` (or `--only
> H1-shaped`) runs those rows alone, seeded as in the full grid.

`research/validation/conformance.py` runs the whole pipeline (selection, then the
candidate tests) on synthetic worlds whose truth is known (`synthetic.py`). The family
buys longshots as a taker paying 2026 fees: every contract whose YES ask is at most θ,
for 7 values of θ from 6¢ to 20¢, buying NO (the favorite–longshot trade) or YES (its
mirror); a mixed family adds a configuration buying YES favorites (asks of 80¢ or more);
a maker family rests bids instead (the makers, below).
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
- **edges:** asks lowered by a few cents on contracts with true P(YES) below 15%;
- **makers:** resting NO bids on standalone binaries without fees, one configuration per
  depth below the truth: 5, 10, 15 and 25¢ (H8's), or 1, 2, 3 and 5¢ (shallow, as H2's
  may rest), and single-depth rows at the shallowest of each. Each bid buys one contract
  on every sweep through it, as Tier 1 fills. The sweeps (`synthetic.py`, "Resting
  bids") select adversely, yet leave every depth at zero net edge, the least favorable
  null. The null prices each fill at the mid before its sweep, one sweep a minute (the
  least favorable timing for nested intervals: with staler candles, several fills would
  share one null interval, nearer the one outcome they share in truth). The
  nested-interval null gives a contract's fills nested intervals on one draw, while in
  truth each fill covaries with every earlier one by its own break-even variance
  (Coupling R, below); the Maker null takes that covariance exactly. The maker edge rows
  (the third table) make the same fills a few cents cheaper. The grid's last three rows
  are maker worlds held out from choosing the Maker null, seeded before it was chosen;
- **H1-shaped:** the taker family deciding again at each of 15 snapshots of a contract's
  day, as H1 decides once a day for up to 15 days, so it buys a contract again whenever
  its ask is still past the cutoff. Each truth is a posterior on noisy signals of the
  event's draw (`synthetic.py`, "Snapshots"), for buckets, binaries and strike ladders
  alike, quoted at the boundary every time. The null prices each position at its
  snapshot's mid, so on one draw it nests a binary's or a strike's positions and slides
  a bucket's interval as its event's de-vigged prices move, while in truth they share
  one outcome; Coupling R, below, measures the difference. One row takes H1's NO cutoffs
  (half spread 1¢); the others the 20¢ cutoff alone, at the half spreads their labels
  show: with all five, discovery most often selects 3¢ (about half the worlds), whose
  nearly settled positions vary so little next to the null (its mids carry the half
  spread) that their coupling barely registers. Two rows of the third table lower every
  ask, by 0.75¢ and, with a lasting shock, by 1¢. Limits: NO longshots only, not H1's
  YES favorites configurations; a contract's 15 decisions compressed into one synthetic
  day; and every cutoff decides at the same snapshots, so the pooled dependence sample
  keeps one position per contract and snapshot, where H1's configurations decide at
  their own first prints.

Coupling R, for one configuration's positions: the sum over the null's clusters of the
variance at the boundary (each position winning with its break-even, given the earlier
ones) of a cluster's summed net PnL, over the sum of the null's variance of the same
(`null._clusters`' var_b: the positions' intervals on the cluster's draw, nested or
sliding, scaled up to the break-even variances; the printed R and R̄ use it for the maker
rows too, the coupling the Maker null takes exactly). In the first, positions a before b,
each signed by its side and count, covary by B_b(1 − B_b) on one contract, B the
break-even, and on two contracts by the overlap of their intervals at b's time less
P·P′, their probabilities then (for two buckets of an event, −P·P′). R takes those from
the world's truths: the coupling a row tests. A family knows only prices, so it uses R̄:
the null's de-vigged intervals, every two-bucket term counted as 0. For one side's
positions R̄ ≥ R term by term on buckets (the truth's term, −P·P′, is at most 0), and
R̄ = R on single contracts; on strikes it isn't guaranteed (a strike bought as a longshot
may be a favorite when a further one is bought), and the ladder row's worlds check it
(on three of its worlds in `tests/test_validation_conformance.py`, and on average in the
row's printed R and R̄). Above 1 the null understates the variance of what the test
sums; below 1 it is conservative. Both count repeated positions, so positions per
contract need no separate check. The run prints each row's R and R̄ where positions
repeat, below the tables: means over its worlds of the selected configuration's (in a
single-configuration row, that configuration's own).

A procedure holds its size if, in every world without an edge, it finds one at most
α + 2 Monte Carlo standard errors of the time, at α = 0.05 and at 0.05/12 (Holm's first
step at m = 12). Cells are "% at α = 0.05 / % at 0.05/12"; bold is over the limit. ρ̂,
cross-day and widening are the gauntlet's mean estimates and null widening; Romano–Wolf
tests every configuration of the family, favorites included.

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

### Makers (2026-10)

Under the nested-interval null (4baee4e), the gauntlet's size in the fourth row below
was 6.3 / 0.9, over its limits, and the 1–5¢ power row was in bold where the cross-day
estimate is negative (80.0 / 73.2). Five candidates were screened at 0.25 scale on the
six maker rows and the two maker power rows: one draw and one interval per contract;
the exact covariance; dependence surprises centred at the break-evens; those two
together; and the exact covariance with flat cross-day weights. Each failed the fourth
row. A diagnosis on 400 of its worlds found the cross-day estimate biased low by
Bartlett's weights (about a quarter at φ = 0.8 over 20 lags), and then, with flat
weights, noisy: the widening estimated from 400 days had a standard deviation of 38% of
an oracle's from 4,000. The Maker null adds the jackknife margin for that noise, and is
the one candidate that held every maker row. The three held-out rows were added and
seeded before it was chosen. The screens, the diagnosis (written down before each step
ran) and these runs' outputs: `runs/method-revision/`. Reproduce with
`OMP_NUM_THREADS=1 python -m kalshibot.research.validation.conformance --only makers:`,
then `--only "makers (held out)"` and `power --only makers:` (about 11 hours on 4
cores, Python 3.13), at the commit that adds this section.

| Maker world | n | ρ̂ | cross-day | widening | widened null | gauntlet |
|---|---|---|---|---|---|---|
| makers: NO bids 5–25¢ below the truth, 16 sweeps a contract | 2000 | 0.002 | -0.01 | 1.45 | 2.9 / 0.1 | 2.9 / 0.1 |
| makers: NO bids 5–25¢ below the truth, shock ρ = 0.5 lasting days (φ = 0.8) | 2000 | 0.124 | 0.87 | 11.45 | 2.1 / 0.1 | 2.0 / 0.1 |
| makers: NO bids 1–5¢ below the truth, 64 sweeps a contract and news | 2000 | 0.002 | -0.01 | 1.61 | 3.4 / 0.2 | 3.4 / 0.2 |
| makers: NO bids 1–5¢ below the truth, 64 sweeps and news, shock ρ = 0.5 lasting days (φ = 0.8) | 2000 | 0.118 | 0.84 | 18.76 | 4.2 / 0.3 | 4.1 / 0.3 |
| makers: NO bids 1¢ below the truth only, 64 sweeps a contract and news | 2000 | 0.002 | -0.01 | 1.74 | 4.0 / 0.1 | 3.7 / 0.1 |
| makers: NO bids 5¢ below the truth only, 16 sweeps a contract | 2000 | 0.002 | -0.01 | 1.64 | 3.7 / 0.2 | 3.4 / 0.2 |
| makers (held out): NO bids 1–5¢ below the truth, 64 sweeps and news, shock ρ = 0.5 lasting days (φ = 0.5) | 2000 | 0.120 | 0.19 | 7.37 | 3.3 / 0.4 | 3.1 / 0.4 |
| makers (held out): NO bids 1–5¢ below the truth, 64 sweeps and news, shock ρ = 0.3 lasting days (φ = 0.8) | 2000 | 0.071 | 0.50 | 11.93 | 3.6 / 0.4 | 3.6 / 0.4 |
| makers (held out): NO bids 5–25¢ below the truth, 16 sweeps and news, shock ρ = 0.5 lasting days (φ = 0.8) | 2000 | 0.104 | 0.74 | 10.58 | 2.1 / 0.2 | 2.0 / 0.2 |

Every maker row holds its size at both levels (limits 5.97% / 0.705% at n = 2000), the
held-out ones too. The other procedures' columns are in
`runs/method-revision/final-makers-1.0.md` and `final-held-out-1.0.md`. The maker power
rows, as the third table:

| Edge world | true net edge per position | widening | power predicted / gauntlet's | the same where the cross-day estimate is negative (worlds) |
|---|---|---|---|---|
| makers: fills 4¢ cheaper, NO bids 5–25¢ below the truth | +3.96¢ | 1.65 | 43.3 / 39.0 | 50.4 / 45.4 (194) |
| makers: fills 3.5¢ cheaper, NO bids 1–5¢ below the truth, 64 sweeps and news | +3.45¢ | 1.82 | 54.8 / 56.3 | 63.6 / 68.6 (191) |

Neither is in bold. Measured power is 14–17 points below the nested-interval null's
(56.0 and 70.7 at α/12), which didn't hold its size. The jackknife margin alone costs
about 4–5 points against the same null without it (at 0.25 scale), since it raises the
widening by 10–15% even in maker worlds without a lasting shock.

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
reproducible since the loader moved to the API tape); `pytest -m data` checks it. On the
discovery API tape (69.3M trades from 2021-06-30 to before 2025-11-26, fetched by
2026-10-09) it is met: the 0–5¢ bin wins 1.19% of contracts at an average 2.12¢ (−43.9%
gross), every bin below 20¢ wins less often than its average price, and the bins from
85¢ return +0.25% to +0.75%.

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
