# Strategies

Hypotheses, their pre-registrations, and verdicts. Method:
[BACKTESTING.md](BACKTESTING.md). Venue facts: [RESEARCH.md](RESEARCH.md).

No family has a verdict yet. Verdicts come only from the pre-registered gauntlet (M3).
The notes below are prior evidence, from others and from our own exploration (labelled).
They motivate the hypotheses; they don't settle them.

## Prior notes by family

| H | Family | Prior evidence |
|---|--------|---------------------------|
| H1 | Favorite–longshot bias | Contracts ≤10¢ lose >60% on average, and those above ~70¢ earn small positive returns after fees. This holds in every category but weakened in 2025 ([Bürgi, Deng & Whelan](https://www.karlwhelan.com/Papers/Kalshi.pdf); [VoxEU](https://cepr.org/voxeu/columns/economics-kalshi-prediction-market)) |
| H2 | Single-name maker premium, held to settlement | Over 72M trades (volume-weighted), takers lose ~1.1% and makers gain ~1.1%. The gap is smallest in finance and largest in world events, and it flipped after professional market makers arrived in 2024 ([Becker](https://www.jbecker.dev/research/prediction-market-microstructure)). Makers earn ~1.9¢ a contract in single-name markets and ~0.8¢ in broad ones, all of it in markets that settle NO; price impact is mostly permanent at 5 minutes, and a trailing VPIN near 0.9 or above predicts maker losses ([Bartlett & O'Hara](https://law.stanford.edu/publications/adverse-selection-in-prediction-markets-evidence-from-kalshi/), data to 2026-03-11, fees ignored) |
| H3 | Coherence / ladder arbitrage | Bucket mids can sum above $1 ([RESEARCH §2](RESEARCH.md)). Leg risk and fees decide whether this is exploitable |
| H4 | Crypto/finance: taker lag against external prices | A short-dated crypto contract is a digital option on its settlement index ([RESEARCH §1](RESEARCH.md)). Polymarket added taker fees to its 15-minute crypto markets against latency arbitrage ([The Block](https://www.theblock.co/post/384461/polymarket-adds-taker-fees-to-15-minute-crypto-markets-to-fund-liquidity-rebates)) |
| H5 | Weather | The market beats NBM by ~10% RMSE and an optimal blend of six forecasts by ~3% (7 cities, 7,590 city-days; [Crosier 2026](https://arxiv.org/html/2609.23969)). The edge vanished in Miami. A [pre-registered study](https://github.com/Nemesis-0/weather-prediction-market) found NBM added nothing beyond the 10 AM price |
| H6 | Releases → markets still open (dropped, below) | |
| H7 | Polymarket ↔ Kalshi | Only ~6% of events overlap, rule wording differs, and capital is locked on both venues (unsourced, original notes) |
| H8 | Deep resting orders that catch sweeps | Thin order flow causes temporary price deviations that pay liquidity providers ([Handa & Schwartz 1996](https://ideas.repec.org/a/bla/jfinan/v51y1996i5p1835-61.html)); against it, the permanent impact in the H2 row |
| H9 | Decided outcomes, externally triggered | None found; our exploration only (below) |
| H10 | In-play sports cross-market lag | TV runs ~19 s and streams 48–62 s behind play ([Streaming Media](https://www.streamingmedia.com/Articles/Post/Blog/Why-the-Gap-Between-the-Game-and-Your-Screen-Is-a-Business-Problem-173844.aspx)); professionals have a dedicated sports feed ([RESEARCH §1](RESEARCH.md)) |
| H11 | Crypto market making on recorded books | Designated market makers quote with fee cuts and higher limits, and the Liquidity Incentive Program pays for two-sided depth ([RESEARCH §1](RESEARCH.md)) |
| H12 | Sports vs consensus | About 80% of Kalshi volume, dominated by professional firms (SIG, Jump, DRW) (unsourced, original notes) |
| — | Market accuracy | Kalshi's self-published study reports Brier ≈ 0.01 for $200k+ markets vs ≈ 0.06 for <$10k markets (link pending) |

## Pre-registrations

Each family's design is fixed here before its confirmation test, which then runs once
(`kalshibot research discover | candles | confirm FAMILY`; the method and the gates:
[BACKTESTING.md](BACKTESTING.md)).

### H1 favorite–longshot bias (registered 2026-10-08)

H1's own choices; everything else is the pipeline's
([BACKTESTING.md](BACKTESTING.md), [ROADMAP calendar](ROADMAP.md#research-calendar)).

- **Mechanism.** Takers overpay for longshots, so buying the favorite's side as a taker
  should earn more than its price and fee (Bürgi, Deng & Whelan, Kalshi data 2021 to
  April 2025; Becker, to November 2025).
- **Strategy** (`strategies/favorite_longshot.py`): nine configurations. Buy NO when YES
  prints at or below 3, 5, 10, 15 or 20¢; buy YES when it prints at or above 80, 85, 90
  or 95¢. Each decides at the first print past its threshold in a market's UTC day
  (block trades aside) and buys one contract as a taker, limited to 2¢ past that print.
- **Data:** every category, scalar markets included.
- **Maximum holding period: 15 days.** Kalshi lists most markets' latest expiration a
  week or two after their close (sports about 14 days), so 15 days admits nearly all
  volume, near each market's close. Most Economics, Elections and Politics markets
  still fall outside it (their latest expiration is further out).
- **Confirmed forward,** on [2026-10-15, 2027-02-15) (`calendar.FORWARD_TESTS`; the
  rule: [ROADMAP](ROADMAP.md#research-calendar)).
  [Bartlett & O'Hara 2026](https://law.stanford.edu/publications/adverse-selection-in-prediction-markets-evidence-from-kalshi/)
  (Kalshi data to March 11, 2026) find traders over-bet YES in markets that mostly
  settle NO, part of H1's mechanism, so H1's confirmation would start 2026-03-12, with
  too few days before the lockbox. Its dependence estimates still come from discovery's
  last 400 days, a year and more before the test (what conformance's "shock from the seam on"
  world measures).
- **Fills:** the next print alone, with no candle leg, in both windows: a candle leg on
  discovery would need candles around every decision of all nine configurations.
- **No path null:** positions are held to settlement.
- **Smallest economically meaningful edge:** 0.5¢ net per contract (`min_edge`).
- **Strata:** net PnL and positions by category are reported with the verdict, not
  tested. (The category × hypothesis matrix comes with the other families.)
- **Known at registration and not used** (later data, none informing H1's design):
  Kagan & Baiocchi (2026, calibration; data to mid-2026), Moshrefi
  ([arXiv 2607.14430](https://arxiv.org/abs/2607.14430), sports moneylines),
  Le ([arXiv 2602.19520](https://arxiv.org/abs/2602.19520), calibration), Delvecchio
  (2026 thesis).

Discovery (2026-10-14, `ledger/H1.jsonl`; selection only, not a verdict): every
configuration loses net of fees, from −0.20¢ a position (NO at YES ≤ 20¢, which discovery
selects by Sharpe) to −2.32¢ (YES at YES ≥ 80¢); NO at YES ≤ 10–20¢ are positive before
fees (+0.2¢ to +0.6¢) and lose to the fee. Two investigations found no bug affecting the
result: a rerun of the runner's pieces reproduced two configurations exactly, and an
independent rebuild matched all nine to within 4 positions and about 0.04¢ a position
(`data/exploration/2026-10-h1-discovery/`, local only).

The [positive control](BACKTESTING.md#positive-control) doesn't contradict these losses.
Its longshot loss is the favorite makers' gain: NO makers opposite YES takers at 0–5¢
gained 1.15¢ a contract, of which a NO taker paying the ask (95–100¢) got about a third
over all maturities (+0.43¢) and nothing near expiry. Its favorites' small gain, weighted
by contract, came mostly from far-dated markets, while H1 buys one contract a position
near expiry. The 15-day hold left out 24–37% of the favorite decisions, so the
registration's "nearly all volume" did not hold (36% of discovery's settled-binary
contracts traded more than 15 days before their latest expiration), and near-expiry
favorites lost per trade from 2024 on. No fill variant tried (including the deciding
print's own price, a ±2¢ band, any print size and no limit) makes any configuration
profitable; how far below the deciding print Tier 1 filled:
[BACKTESTING](BACKTESTING.md#fill-models).

Status (2026-10-15): H1's confirmation waits for `gauntlet.power`'s revision and for a
revised method that passes the H1-shaped rows (a logged change to its registered method,
with the user's sign-off), then for its coupling R check ([BACKTESTING](BACKTESTING.md),
Conformance and step 6).

## Hypothesis notes

### H5 weather
Venue and data facts (settlement source, climate day, bucket math, free sources):
[RESEARCH §2](RESEARCH.md).

- **One mechanism: the forecast model** (one per family:
  [BACKTESTING](BACKTESTING.md) step 5). Possible openings for it:
  - distribution shape and tails (the studies above only tested the mean);
  - same-day observations as model features (METAR/ASOS rounding quirks);
  - the first hour after a market opens;
  - thinner cities.
- **Left out:** trading on observations alone (in our exploration, buckets already
  ruled out by the afternoon daily summary kept trading for hours, but the pool is
  tiny), and maker quotes (makers pay nothing on KXHIGHNY, [RESEARCH §1](RESEARCH.md)),
  which need recorded books to judge.
- **Model:** predict a distribution, not a point:
  - [EMOS](https://doi.org/10.1175/MWR2904.1) (Normal with learned mean/variance,
    fit by minimizing CRPS);
  - [NGBoost](https://arxiv.org/abs/1910.03225);
  - quantile LightGBM;
  - distributional nets ([Rasp & Lerch 2018](https://arxiv.org/abs/1805.09091)).

  Map to buckets using the integer rounding rules. Stacking the market price with
  forecast features is legitimate and often the strongest model. Ensemble spread
  (GEFS via `herbie`) is the natural uncertainty feature.
- **Baselines:** climatology, the NWS forecast plus its empirical error distribution,
  and the market itself.
- **Score:** CRPS for the temperature distribution, and Brier/log loss for buckets,
  always relative to the market at the same decision time. Calibrate with
  isotonic/Platt on time-blocked holdouts; bucket probabilities must sum to 1.
- **Data cleaning:** early 2021–22 events had different bucket structures, and some
  events have zero or two YES winners ([RESEARCH §2](RESEARCH.md)).

### Fast, maker and lag families (scoped 2026-10-09)

Families where resting limit orders, trading in and out quickly, or reacting before a
market catches up might pay, scoped from six research sweeps.

Our own numbers here are **exploratory**: proxies rather than fill simulations, cut many
ways, with thresholds picked after looking. They decide what to build first; they are
not evidence. They come from discovery data (before 2025-11-26), with two exposures
declared at registration: one sweep saw the outcome of the October 2025 payrolls release
(KXPAYROLLS-25OCT: any family that trades it excludes it from its confirmation), and
September 2026 crypto volumes were read outside the loader (H4, H11). Each design but
H11's (there are no discovery books) is re-run as `research discover` configurations
before it is registered, and the rest is disclosed then. Scripts and outputs:
`data/exploration/2026-10-fast-strategies/` (local only).

What these families share:
- **Competition and speed.** Professional makers have fee cuts and dedicated feeds
  ([RESEARCH §1](RESEARCH.md)); in our exploration, fast takers reacted 54–146 ms (p10–p25) after BTC spot
  moves. Our own latency is unmeasured (M4).
- **The venue changed since discovery** ([RESEARCH §1](RESEARCH.md) and §2), so 2025 evidence may not carry
  over.
- **The method's limits:** a touch maker takes its verdict from recorded books and
  calibrated queue models ([BACKTESTING](BACKTESTING.md) step 3); early exits need
  round-trip accounting and a path null (the null's scope there); a configuration that
  buys both sides (possible in H3, H4, H8, H9 and H10) needs the mixed-side conformance
  world, and a maker family (H2, H8) a revised method that passes the maker rows and its
  registration checks ([BACKTESTING](BACKTESTING.md), Conformance, step 6 and the
  pre-registration paragraph).
- **One mechanism per family** (BACKTESTING step 5), so m stays 12.
- **H2 and H8:** Bartlett & O'Hara inform both, so S_h is 2026-03-12
  (`calendar.STUDY_END`), likely leaving them to forward tests
  ([ROADMAP](ROADMAP.md#research-calendar)).

#### H8 deep resting orders that catch sweeps
- **Mechanism.** Rest limit orders 5–25¢ behind the touch; takers sweeping thin books
  fill them at prices that revert. Held to settlement. A configuration rests at most one
  bid per contract at a time (`strategies.base`), so a ladder of depths on one contract
  is split across configurations.
- **Exploratory lead.** Maker fills deep in a sweep marked out well at settlement, most
  of it within a minute. Tier 1 credits less: a through-print never fills the deepest
  level a sweep reaches, and "deep" was measured from the sweep's first fill, not from
  the touch when an order would have been placed.
- **Gate:** all its evidence predates the end of market orders
  ([RESEARCH §1](RESEARCH.md)), which may have made sweeps rarer. The check (user
  ruling, 2026-10-14) counted sweeps through its depths on forward days, reading no
  outcomes or PnL; its counts are declared at registration, and none of these days is in
  a window H8 could be tested on (`calendar.py`). The count (2026-10-15;
  `data/exploration/2026-10-h8-gate/`, local only), on 2026-10-01..13 against the same
  days of 2025: game winners' episodes (runs of sweeps on one contract and side,
  reaching 25¢ below the price 2 s earlier; a new run after 10 s without one) were about
  half as many (0.48×; 90% interval 0.30–0.75), from 1.2× in NCAAF to 0.2× in tennis
  (NBA, in preseason both years, aside), and games a day with such a sweep were 0.62×
  without NCAAF. Deep sweeps grew in props, spreads, totals and new leagues, which H8's
  evidence didn't come from. User ruling (2026-10-15): keep H8, with its leagues fixed
  at registration.

#### H9 decided outcomes, externally triggered
- **Mechanism.** After an external trigger that the result is decided (ESPN end of
  game, MLB StatsAPI final out, a total already crossed), buy the decided side at 99¢ or
  less as a taker, held to settlement. The trigger, not the price, separates it from H1.
- **Exploratory lead.** NFL games kept trading a median ~160 s after the final whistle,
  with millions of contracts sold into 99¢ bids; prices below 99¢ were gone within
  ~30 s. Favorites at 97¢ or more did lose late, so "decided" must come from the trigger.
- **Risks:** ties, stat corrections, reviews; one loss erases ~100 wins.
- **Before registration:** check the null's clip and widening, and power, in a
  conformance world at 97–99.9¢.
- **In-calendar.**

#### H10 in-play sports cross-market lag
- **Mechanism.** The moneyline reprices first; spreads and totals follow. Trigger on a
  moneyline print well away from its recent average (Kalshi only) and take stale quotes
  on the related markets, held to settlement (an early-exit version needs a path null).
- **Exploratory lead.** Spreads traded with the move in the first ~2 s marked out above
  the taker fee at one minute, before any exit cost; from 2 to 10 s they lost after
  fees. Touchdown props lag 10–30 s but need the scorer, from a feed whose delay is
  unmeasured.
- **Lower priority:** a small, sub-second pool against firms with official feeds.
- **In-calendar,** unless a study turns out to inform it.

#### H11 crypto market making on recorded books (forward)
- **Mechanism.** Two-sided quotes at the touch, with inventory skew, on crypto hourly
  and 15-minute markets (makers pay no fee there: [RESEARCH §1](RESEARCH.md)), anchored on a fast estimate of the settlement index
  and requoted on spot moves.
- **Exploratory lead.** Takers lost ~0.6–0.7¢ to KXBTCD makers over 1–300 s, but makers
  filled within ~1 s of a spot jump lost 5–8¢: requoting speed decides it.
- **Needs** the recorded books, queue models (its verdict tier), round-trip accounting,
  path null and recorded-stretch selection of [ROADMAP](ROADMAP.md) M3; selected on a
  recorded stretch and confirmed forward ([ROADMAP](ROADMAP.md#research-calendar)).
- **Left out:** touch quoting in sports, and latency-tolerant quotes
  (99¢ bids on decided outcomes, 1¢ asks on dead weather buckets), which need the same
  books.

#### Other families, as now scoped
- **H2:** NO bids at YES 30–70¢ in single-name markets, resting behind the touch (which
  Tier 1 judges), pulled when VPIN runs high, held to settlement. Tail quotes are left
  out: they buy the same favorites as H1.
- **H3** adds the two team markets of a game summing past $1, and KXBTCD against KXBTC
  ranges, each as two taker legs. The cross-series pair needs one shared draw per
  settlement fix (or is judged on leg risk).
- **H4:** taker lag against external prices (Coinbase and Kraken for BRTI), held to
  settlement, with latency a pre-registered parameter ([ROADMAP](ROADMAP.md) M3). It
  likely needs a host near the exchange ([EXECUTION](EXECUTION.md), Hosting).
  In-calendar: Synth's report (Kalshi 15-minute BTC data to August 2026) is known and
  not used; if it were, H4 would go forward.
- **H6: dropped** (user ruling, 2026-10-14). Jobs report → Fed-path markets has ~12
  releases a year, so it was expected to be Inconclusive; its place stays in m
  ([BACKTESTING](BACKTESTING.md) step 5).
