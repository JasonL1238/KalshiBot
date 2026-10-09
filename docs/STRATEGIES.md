# Strategies

Hypotheses, their pre-registrations, and verdicts. Method:
[BACKTESTING.md](BACKTESTING.md). Venue facts: [RESEARCH.md](RESEARCH.md).

No family has a verdict yet. Verdicts come only from the pre-registered gauntlet (M3).
The notes below are prior evidence from others. They motivate the hypotheses; they
don't settle them.

## Prior notes by family

| H | Family | Prior evidence (not ours) |
|---|--------|---------------------------|
| H1 | Favorite–longshot bias | Contracts ≤10¢ lose >60% on average, and those above ~70¢ earn small positive returns after fees. This holds in every category but weakened in 2025 ([Bürgi, Deng & Whelan](https://www.karlwhelan.com/Papers/Kalshi.pdf); [VoxEU](https://cepr.org/voxeu/columns/economics-kalshi-prediction-market)) |
| H2 | Maker premium / market making | Over 72M trades (volume-weighted), takers lose ~1.1% and makers gain ~1.1%. The gap is smallest in finance and largest in world events, and it flipped after professional market makers arrived in late 2024 ([Becker](https://www.jbecker.dev/research/prediction-market-microstructure)). Risks: adverse selection and inventory |
| H3 | Coherence / ladder arbitrage | Bucket mids can sum above $1 ([RESEARCH §2](RESEARCH.md)). Leg risk and fees decide whether this is exploitable |
| H4 | Crypto/finance fair value | Pricing a short-dated crypto contract is a digital-option problem on the settlement index. It is latency-sensitive (unsourced, original notes) |
| H5 | Weather | The market beats NBM by ~10% RMSE and an optimal blend of six forecasts by ~3% (7 cities, 7,590 city-days; [Crosier 2026](https://arxiv.org/html/2609.23969)). The edge vanished in Miami. A [pre-registered study](https://github.com/Nemesis-0/weather-prediction-market) found NBM added nothing beyond the 10 AM price |
| H6 | Economic releases | Fed markets look nearly efficient and employment markets are the least calibrated. There are only ~12 events per year, so power is low (unsourced, original notes) |
| H7 | Polymarket ↔ Kalshi | Only ~6% of events overlap, rule wording differs, and capital is locked on both venues (unsourced, original notes) |
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
  tested. (The category × hypothesis matrix comes with H2–H12.)
- **Known at registration and not used** (later data, none informing H1's design):
  Kagan & Baiocchi (2026, calibration; data to mid-2026), Moshrefi
  ([arXiv 2607.14430](https://arxiv.org/abs/2607.14430), sports moneylines),
  Le ([arXiv 2602.19520](https://arxiv.org/abs/2602.19520), calibration), Delvecchio
  (2026 thesis).

## Hypothesis notes

### H5 weather
Venue and data facts (settlement source, climate day, bucket math, free sources):
[RESEARCH §2](RESEARCH.md).

- **Possible openings:**
  - distribution shape and tails (the studies above only tested the mean);
  - same-day observations (METAR/ASOS rounding quirks);
  - the first hour after a market opens;
  - thinner cities;
  - maker execution where the series' `fee_type` is plain `quadratic` (e.g. KXHIGHNY,
    [RESEARCH §1](RESEARCH.md)).
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
