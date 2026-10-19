"""Measure the research pipeline on synthetic worlds where the truth is known.

The family under test buys longshots as a taker paying 2026 fees: every contract whose
YES ask is at most θ, for θ in `THRESHOLDS`, one contract each, buying NO (the usual
favorite–longshot trade) or YES (its mirror, where a null built from the mid is too
narrow); a mixed family also has a configuration buying YES favorites. Each world has a
discovery period, where the configuration is picked by Sharpe, four times as long as
the confirmation period that tests it (the real calendar's ratio is about ten); the
periods can differ (`confirmation`), e.g. an edge that vanishes at the seam or a busier
market. A maker family (`depths`, in a sweep world: `synthetic.py`, Resting bids) has
a configuration per depth instead: a bid per contract that far below its side's truth,
re-pegged to the cent grid before every sweep, buying one contract on each sweep through
it (Tier 1's rule), priced by the null at the mid before that sweep. In a snapshot world
(`synthetic.py`, Snapshots) the taker family decides afresh at every snapshot, as H1
does daily, so it buys a contract again whenever its ask is still past the cutoff, each
position priced at its snapshot. `_coupling` measures how far the null's intervals
understate the variance of a cluster's positions, repeated or on several contracts.
`one_world` returns every statistic the candidate decision procedures use, and
`PROCEDURES` turns them into "edge found":

- naive in-sample: the best discovery Sharpe has a one-sided t-stat above z(1 − α);
- naive confirmation: a t-test on the chosen configuration's confirmation daily returns;
- null: the market-calibrated null (`null.py`), without widening;
- bootstrap: the stationary bootstrap of confirmation daily returns (`stats.py`);
- null + bootstrap; the null with DSR > 0.95 and PBO < 0.2 gates;
- Romano–Wolf: every configuration tested on confirmation at once (no discovery
  selection), with jointly simulated null statistics;
- widened null: the null widened for break-even variance and dependence, estimated as
  the runner does: on every configuration's positions in discovery's last 400 days
  (`Gates.dependence_days`; here all of it), each bet once (`gauntlet.pooled`);
- gauntlet: what `gauntlet.evaluate` decides (`Verdict.p_value`).

`rates` reports how often each procedure declares an edge: its size (false positive
rate) where the truth has no edge, its power where it has one. `python -m
kalshibot.research.validation.conformance` runs `GRID`, the worlds behind the gate choice,
and prints the two tables in docs/BACKTESTING.md, then R and R̄ (`_coupling`) of each row
whose positions repeat; append rows only, since each row is seeded by its position.
`... conformance power` checks `gauntlet.power`, the estimate behind an Inconclusive
verdict, against the gauntlet's measured power (`PREDICTION_GRID`).
`--only <prefix>` runs only the rows whose label starts with it (e.g. "makers:");
`--repeated <method>` sets `Gates.repeated` (`gauntlet.REPEATED_METHODS`).
"""

from __future__ import annotations

import math
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

from kalshibot.kalshi.fees import CENT
from kalshibot.kalshi.models import PRICE_SCALE, SIZE_SCALE
from kalshibot.research.backtest.settle import daily_returns
from kalshibot.research.validation import gauntlet, null, stats, synthetic
from kalshibot.strategies.favorite_longshot import FavoriteLongshot

THRESHOLDS = [600, 700, 800, 1000, 1200, 1500, 2000]  # YES ask cutoffs, price units
FAVORITES = 8000  # the mixed family also buys YES at asks of at least this
# fewer draws (slightly less power); a power estimate here bootstraps as the test does
GATES = replace(gauntlet.PREREGISTERED, draws=2_000, power_worlds=200, power_resamples=2_000)
MIN_DSR, MAX_PBO = 0.95, 0.2  # candidate gates, measured
BASE = pd.Timestamp("2020-01-01", tz="UTC")  # day 0 of every world


def _configs(side: str, favorites: bool, depths: tuple[int, ...] | None = None,
             thresholds: tuple[int, ...] | None = None) -> list[tuple[str, str, int]]:
    """(name, side bought, YES-ask cutoff: ≤ cutoff, or ≥ −cutoff when negative), the
    cutoffs `thresholds` (default `THRESHOLDS`), or with `depths` (name, side, how far
    below the side's truth its bid rests), shallowest first."""
    if depths and thresholds:
        raise ValueError("depths or thresholds, not both")
    if depths:
        if not all(d > 0 and d % 100 == 0 for d in depths):  # names count whole cents
            raise ValueError(f"depths must be whole cents, in price units: {depths}")
        return [(f"{side} bid {d // 100}¢ below the truth", side, d) for d in sorted(depths)]
    cuts = sorted(thresholds or THRESHOLDS)
    if not all(0 < t < PRICE_SCALE for t in cuts) or len(set(cuts)) < len(cuts):
        raise ValueError(f"thresholds must be distinct YES asks inside (0, $1): {cuts}")
    out = [(f"{side} ≤ {t}", side, t) for t in cuts]
    return out + [("yes ≥ favorites", "yes", -FAVORITES)] if favorites else out


def _positions(df: pd.DataFrame, config: tuple[str, str, int]) -> pd.DataFrame:
    """One contract bought per quote the configuration takes, or per sweep through its bid
    in a sweep world, in the columns `settle.daily_returns` and
    `gauntlet.Positions.from_frame` read, plus `expected`: its true expected net PnL."""
    _, side, cut = config
    if "bottom" in df:  # a bid on the cent grid `cut` below the side's truth, re-pegged
        truth = df["q"] if side == "yes" else 1 - df["q"]
        bid = (truth * PRICE_SCALE - cut) // 100 * 100
        hit = (bid > 0) & (df["bottom"] < bid)
        pos, bid = df[hit].copy(), bid[hit]
        price = np.maximum(bid - pos["extra"], 1)
        pos["cost"] = pos["paid"] = price * SIZE_SCALE  # no maker fee
        pos["expected"] = (bid - price) * SIZE_SCALE  # E[price after | fill] = bid
    else:
        pos = df[df["ask_yes"] <= cut if cut > 0 else df["ask_yes"] >= -cut].copy()
        ask = pos["ask_yes" if side == "yes" else "ask_no"].to_numpy()
        pos["cost"] = pos["paid"] = synthetic.taker_cost(ask)
        pos["expected"] = synthetic.taker_net(pos["q"] if side == "yes" else 1 - pos["q"], ask)
    pos["priced_at"] = pos["fill_time"] = _priced_at(pos)
    pos["close_time"] = BASE + pd.to_timedelta(pos["day"] + 1, unit="D")  # the day's end
    pos["side"], pos["fill_count"], pos["maker"] = side, SIZE_SCALE, "bottom" in df
    pos["won"] = pos["yes"] if side == "yes" else ~pos["yes"]
    pos["pnl"] = np.where(pos["won"], PRICE_SCALE * SIZE_SCALE, 0) - pos["cost"]
    return pos


def _priced_at(df: pd.DataFrame) -> pd.Series:
    """When the null prices each row: a sweep 30 s into its minute, a snapshot at its
    hour, a contract quoted once at noon."""
    if "minute" in df:
        seconds = df["minute"] * 60 + 30
    else:
        seconds = df["snapshot"] * 3600 if "snapshot" in df else 12 * 3600
    return BASE + pd.to_timedelta(df["day"] * 86400 + seconds, unit="s")


def _true_intervals(quotes: pd.DataFrame) -> pd.DataFrame:
    """`quotes` with each contract's interval at its truth q, not the null's: an exclusive
    event's laid out by position at each snapshot, any other on [0, q)."""
    laid = quotes.sort_values(["position", "ticker"]).groupby(["event", "priced_at"])["q"]
    hi = quotes["q"].where(~quotes["mutually_exclusive"], laid.cumsum())
    return quotes.assign(lo=hi - quotes["q"], hi=hi)


def _coupling(pos: pd.DataFrame, quotes: pd.DataFrame) -> tuple[float, float]:
    """(R, R̄) (docs/BACKTESTING.md, Coupling R) of positions `pos` on the null's clusters.
    `quotes` (`null.inputs`' output, with each contract's truth q) gives every contract's
    interval at every time a position in its cluster is priced: the truths' for R
    (`_true_intervals`), the null's for R̄, which counts two buckets' terms as 0. Its R̄
    half moves to null.py before H1's confirmation test (ROADMAP M3); R, from the world's
    truths, stays here."""
    pos = pos.sort_values("priced_at", kind="stable")
    count = pos["fill_count"].to_numpy(float)
    b = pos["paid"].to_numpy() / (count * PRICE_SCALE)
    sign = np.where(pos["side"] == "yes", 1.0, -1.0) * count
    keys = list(zip(quotes["ticker"], quotes["priced_at"], strict=True))
    at_truth, at_prices = (dict(zip(keys, zip(q["lo"], q["hi"], strict=True), strict=True))
                           for q in (_true_intervals(quotes), quotes))
    tickers, times = pos["ticker"].to_numpy(), pos["priced_at"].to_numpy()
    exclusive = pos["mutually_exclusive"].to_numpy(bool)
    totals = np.full(2, float((count**2 * b * (1 - b)).sum()))  # R's and R̄'s numerators
    for rows in pos.groupby("cluster").indices.values():  # in time order
        if len(rows) < 2:
            continue
        contracts, contract = np.unique(tickers[rows], return_inverse=True)
        moments, moment = np.unique(times[rows], return_inverse=True)
        same = contract[:, None] == contract
        weight = 2 * np.triu(np.outer(sign[rows], sign[rows]), k=1)  # i before j
        totals += float((weight * same * b[rows] * (1 - b[rows])).sum())
        for k, at in enumerate((at_truth, None if exclusive[rows[0]] else at_prices)):
            if at is None or same.all():
                continue
            table = np.array([[at[c, t] for t in moments] for c in contracts])  # c at time t
            lo, hi = table[contract][:, moment, 0], table[contract][:, moment, 1]
            both = np.minimum(hi, np.diagonal(hi)) - np.maximum(lo, np.diagonal(lo))
            # two contracts' YES outcomes covary, at the later position's time, by:
            totals[k] += float((weight * ~same * (np.clip(both, 0, None)
                                                  - (hi - lo) * np.diagonal(hi - lo))).sum())
    null_var = null._clusters(
        (pos["side"] == "yes").to_numpy(), count, pos["p_yes"].to_numpy(),
        pos["lo"].to_numpy(), pos["hi"].to_numpy(), pos["cluster"].to_numpy(),
        pos["day"].to_numpy(), b)["var_b"].sum()
    return tuple(map(float, totals / null_var)) if null_var > 0 else (math.nan, math.nan)


def _daily(pos: pd.DataFrame, days: range) -> pd.Series:
    return daily_returns(pos, BASE + pd.Timedelta(days.start, unit="D"),
                         BASE + pd.Timedelta(days.stop, unit="D"))


def _romano_wolf(conf: pd.DataFrame, configs: list, rng: np.random.Generator,
                 draws: int, chunk: int = 100) -> float:
    """Family p-value with every configuration tested on confirmation: the smallest
    Romano–Wolf adjusted p-value of the studentized net PnLs, from one joint null
    simulation of every confirmation contract, `chunk` draws at a time (one call's
    stream, without holding draws × rows)."""
    every = [_positions(conf, config) for config in configs]
    sims = [[] for _ in configs]
    for start in range(0, draws, chunk):
        yes = null.draw_outcomes(conf["lo"].to_numpy(), conf["hi"].to_numpy(),
                                 conf["cluster"].to_numpy(), min(chunk, draws - start), rng)
        for sim, pos in zip(sims, every, strict=True):
            sim.append(null.recentered_pnl(
                yes[:, conf.index.get_indexer(pos.index)], (pos["side"] == "yes").to_numpy(),
                np.full(len(pos), SIZE_SCALE), pos["p_yes"].to_numpy()))
    observed, simulated = [], []
    for pos, sim in zip(every, map(np.concatenate, sims), strict=True):
        sd = sim.std() or 1.0
        observed.append(pos["pnl"].sum() / sd)
        simulated.append(sim / sd)
    return float(stats.romano_wolf(np.array(observed), np.column_stack(simulated)).min())


def _world(rng: np.random.Generator, seam: int, confirmation_days: int, events_per_day: int,
           side: str, quotes: str, depths: tuple[int, ...] | None = None,
           confirmation: dict | None = None, **world) -> pd.DataFrame:
    """`one_world`'s world: `synthetic.world` for discovery, then from day `seam` with
    `confirmation` overriding `world` (and `events_per_day`). With `depths` (price units),
    a sweep world in both periods whose sweeps reach the shallowest."""
    later = {"events_per_day": events_per_day, **world, **(confirmation or {})}
    if any(bool(depths) != bool(settings.get("sweeps")) for settings in (world, later)):
        raise ValueError("depths and sweeps go together: a maker family in a sweep world")
    if depths:
        world["sweep_scale"] = later["sweep_scale"] = min(depths) / PRICE_SCALE
    return pd.concat([synthetic.world(seam, events_per_day, rng, quotes, side=side, **world),
                      synthetic.world(confirmation_days, later.pop("events_per_day"), rng,
                                      quotes, side=side, first_day=seam, **later)],
                     ignore_index=True)


def one_world(rng: np.random.Generator, side: str = "no", quotes: str = "boundary",
              discovery_days: int = 400, confirmation_days: int = 100,
              events_per_day: int = 20, favorites: bool = False,
              depths: tuple[int, ...] | None = None, thresholds: tuple[int, ...] | None = None,
              confirmation: dict | None = None, gates: gauntlet.Gates = GATES,
              predict: bool = False, **world) -> dict[str, float]:
    """Every decision statistic for one synthetic world (`_world`); with `depths`, of a
    maker family, and with `thresholds`, of takers at those cutoffs alone (`_configs`).
    With `predict`, also `gauntlet.power` on the chosen configuration's confirmation
    sample at the world's true net edge."""
    seam, days = discovery_days, discovery_days + confirmation_days
    df = _world(rng, seam, confirmation_days, events_per_day, side, quotes, depths,
                confirmation, **world)
    df = null.inputs(df.assign(priced_at=_priced_at(df)), gates.devig)  # each snapshot apart
    disc, conf = df[df["day"] < seam], df[df["day"] >= seam]
    configs = _configs(side, favorites, depths, thresholds)
    disc_returns = pd.DataFrame({c[0]: _daily(_positions(disc, c), range(seam))
                                 for c in configs})
    chosen = next(c for c in configs if c[0] == gauntlet.select(disc_returns))
    best = disc_returns[chosen[0]].to_numpy()
    t_in = best.mean() / (best.std(ddof=1) / np.sqrt(len(best))) if best.std() > 0 else 0.0
    pos = _positions(conf, chosen)
    conf_returns = _daily(pos, range(seam, days))
    recent = disc[disc["day"] >= seam - gates.dependence_days]
    every = gauntlet.pooled([_positions(recent, c) for c in configs])
    v = gauntlet.evaluate(disc_returns, gauntlet.Positions.from_frame(every), conf_returns,
                          gauntlet.Positions.from_frame(pos), rng, gates)
    p_t = (scipy_stats.ttest_1samp(conf_returns, 0, alternative="greater").pvalue
           if conf_returns.std() > 0 else 1.0)
    edge = float(pos["expected"].mean()) if len(pos) else 0.0
    out = {**asdict(v), "p_value": v.p_value, "t_in": float(t_in), "p_t": float(p_t),
           "p_rw": _romano_wolf(conf, configs, rng, gates.draws),
           "edge_per_position": edge, "positions_per_day": len(pos) / confirmation_days,
           **dict(zip(("coupling", "r_bar"), _coupling(pos, conf)
                      if depths or "snapshot" in df else (math.nan, math.nan), strict=True))}
    if predict:
        out["power_predicted"] = gauntlet.power(
            gauntlet.Positions.from_frame(every), gauntlet.Positions.from_frame(pos),
            np.arange(seam, days), round(edge / SIZE_SCALE), rng, gates)
    return out


def _dsr_ok(s):
    return np.isfinite(s["dsr"]) and s["dsr"] > MIN_DSR


PROCEDURES = {
    "naive in-sample": lambda s, a: s["t_in"] > scipy_stats.norm.ppf(1 - a),
    "naive confirmation": lambda s, a: s["p_t"] <= a,
    "null": lambda s, a: s["p_independent"] <= a,
    "bootstrap": lambda s, a: s["p_boot"] <= a,
    "null + bootstrap": lambda s, a: max(s["p_independent"], s["p_boot"]) <= a,
    "null + DSR": lambda s, a: s["p_independent"] <= a and _dsr_ok(s),
    "null + DSR + PBO": lambda s, a: (s["p_independent"] <= a and _dsr_ok(s)
                                      and s["pbo"] < MAX_PBO),
    "Romano–Wolf": lambda s, a: s["p_rw"] <= a,
    "widened null": lambda s, a: s["p_null"] <= a,
    "gauntlet": lambda s, a: s["p_value"] <= a,
}
MEANS = ("edge_per_position", "rho", "cross_day", "widening", "positions_per_day", "coupling",
         "r_bar")


def _world_task(args):
    seed, kwargs = args
    return one_world(np.random.default_rng(seed), **kwargs)


def simulate(worlds: int, seed: int = 0, **kwargs) -> list[dict[str, float]]:
    """`one_world` statistics for `worlds` independent worlds, in parallel processes."""
    tasks = [(s, kwargs) for s in np.random.SeedSequence(seed).spawn(worlds)]
    with ProcessPoolExecutor() as pool:
        return list(pool.map(_world_task, tasks, chunksize=4))


def rates(results: list[dict[str, float]], alpha: float = GATES.alpha) -> dict[str, float]:
    """Share of worlds in which each procedure declares an edge at level `alpha`, plus
    the means of `MEANS` (the true net edge per confirmed position in micro-dollars, the
    gauntlet's dependence estimates and widening, confirmed positions per day, and
    `_coupling`'s R and R̄ where positions repeat, else NaN)."""
    out = {name: float(np.mean([bool(rule(s, alpha)) for s in results]))
           for name, rule in PROCEDURES.items()}
    out.update({k: float(np.mean([s[k] for s in results])) for k in MEANS})
    return out


LONGSHOTS = (0.0, 0.15)  # true P(YES) below 15%
BINARIES_SHOCK = {"shock": 0.5, "bucket_share": 0.0}
MAKERS = {"bucket_share": 0.0, "depths": (500, 1000, 1500, 2500), "sweeps": 16}  # H8's
MAKERS_STRESS = {"bucket_share": 0.0, "depths": (100, 200, 300, 500), "sweeps": 64,
                 "news": 0.1}  # shallow, as H2's may rest
H1_SHAPED = {"snapshots": 15, "signal": 0.5,  # a decision a day for H1's 15-day hold
             "thresholds": tuple(sorted(c["yes_price"] for c in FavoriteLongshot().configs()
                                        .values() if c["buy"] == "no"))}  # its NO cutoffs
H1_COUPLED = {**H1_SHAPED, "thresholds": (2000,), "half_spread_cents": 0.5}  # R at its most
GRID = [  # (label, worlds, one_world settings); no edge unless the label says so
    ("market prices: buying NO loses spread + fee", 400, {"quotes": "market"}),
    ("boundary", 2000, {}),
    ("boundary, 100 events/day", 1000, {"events_per_day": 100}),
    ("boundary, Shin de-vig", 1000, {"gates": replace(GATES, devig="shin")}),
    ("boundary, same-day shock ρ = 0.5", 2000, {"shock": 0.5}),
    ("boundary, binaries only, shock ρ = 0.5", 2000, BINARIES_SHOCK),
    ("boundary, binaries only, shock ρ = 0.8, half spread 0.5¢", 2000,
     {"shock": 0.8, "bucket_share": 0.0, "half_spread_cents": 0.5}),
    ("boundary, binaries only, shock ρ = 0.3, half spread 0.5¢, 100 events/day", 1000,
     {"shock": 0.3, "bucket_share": 0.0, "half_spread_cents": 0.5, "events_per_day": 100}),
    ("boundary, binaries only, shock ρ = 0.5, confirmation twice as busy", 2000,
     {**BINARIES_SHOCK, "confirmation": {"events_per_day": 40}}),
    ("YES family: boundary", 2000, {"side": "yes"}),
    ("YES family: boundary, binaries only, shock ρ = 0.5", 2000,
     {"side": "yes", **BINARIES_SHOCK}),
    ("edge in discovery only (2¢, gone at the seam)", 2000,
     {"edge": synthetic.Edge(*LONGSHOTS, 2), "confirmation": {"edge": synthetic.NO_EDGE}}),
    ("mixed family (adds YES favorites): binaries only, shock ρ = 0.3, half spread 0.5¢, "
     "100 events/day", 1000, {"favorites": True, "shock": 0.3, "bucket_share": 0.0,
                             "half_spread_cents": 0.5, "events_per_day": 100}),
    ("boundary, binaries only, shock ρ = 0.5 lasting days (φ = 0.5)", 2000,
     {**BINARIES_SHOCK, "persistence": 0.5}),
    ("boundary, binaries only, shock ρ = 0.5 lasting days (φ = 0.8)", 2000,
     {**BINARIES_SHOCK, "persistence": 0.8}),
    ("mixed family, binaries only, shock ρ = 0.5 lasting days (φ = 0.8)", 2000,
     {"favorites": True, **BINARIES_SHOCK, "persistence": 0.8}),
    ("boundary, binaries only, shock ρ = 0.5 lasting days (φ = 0.8), confirmation twice "
     "as busy", 2000, {**BINARIES_SHOCK, "persistence": 0.8,
                       "confirmation": {"events_per_day": 40}}),
    ("boundary, ladders all strike ladders, same-day shock ρ = 0.5", 2000,
     {"strike_share": 1.0, "shock": 0.5}),
    ("boundary, binaries only, shock ρ = 0.5 from the seam on (none in discovery)", 2000,
     {"bucket_share": 0.0, "confirmation": {"shock": 0.5}}),
    *[(f"{c}¢ lower NO asks on q < 0.15", 400, {"edge": synthetic.Edge(*LONGSHOTS, c)})
      for c in (0.5, 1, 2, 3)],
    *[(f"{c}¢ lower NO asks on q < 0.15, 100 events/day", 400,
       {"edge": synthetic.Edge(*LONGSHOTS, c), "events_per_day": 100}) for c in (0.25, 0.5, 1)],
    ("2¢ lower NO asks on q < 0.15, halving to 1¢ at the seam", 400,
     {"edge": synthetic.Edge(*LONGSHOTS, 2),
      "confirmation": {"edge": synthetic.Edge(*LONGSHOTS, 1)}}),
    *[(f"YES family: {c}¢ lower YES asks on q < 0.15", 400,
       {"side": "yes", "edge": synthetic.Edge(*LONGSHOTS, c)}) for c in (1, 2, 3)],
    ("makers: NO bids 5–25¢ below the truth, 16 sweeps a contract", 2000, MAKERS),
    ("makers: NO bids 5–25¢ below the truth, shock ρ = 0.5 lasting days (φ = 0.8)", 2000,
     {**MAKERS, "shock": 0.5, "persistence": 0.8}),
    ("makers: NO bids 1–5¢ below the truth, 64 sweeps a contract and news", 2000,
     MAKERS_STRESS),
    ("makers: NO bids 1–5¢ below the truth, 64 sweeps and news, shock ρ = 0.5 lasting days "
     "(φ = 0.8)", 2000, {**MAKERS_STRESS, "shock": 0.5, "persistence": 0.8}),
    ("makers: NO bids 1¢ below the truth only, 64 sweeps a contract and news", 2000,
     {**MAKERS_STRESS, "depths": (100,)}),
    ("makers: NO bids 5¢ below the truth only, 16 sweeps a contract", 2000,
     {**MAKERS, "depths": (500,)}),
    ("H1-shaped: boundary, 15 daily decisions a contract", 2000, H1_SHAPED),
    ("H1-shaped: NO at YES ≤ 20¢ only, half spread 0.5¢, binaries only", 2000,
     {**H1_COUPLED, "bucket_share": 0.0}),
    ("H1-shaped: NO at YES ≤ 20¢ only, half spread 0.5¢, shock ρ = 0.5 lasting days "
     "(φ = 0.8)", 2000, {**H1_COUPLED, "shock": 0.5, "persistence": 0.8}),
    ("H1-shaped: NO at YES ≤ 20¢ only, half spread 1¢", 2000,
     {**H1_SHAPED, "thresholds": (2000,)}),
    ("H1-shaped: NO at YES ≤ 20¢ only, half spread 0.5¢", 2000, H1_COUPLED),
    ("H1-shaped: NO at YES ≤ 20¢ only, half spread 0.5¢, ladders all strike ladders", 2000,
     {**H1_COUPLED, "strike_share": 1.0}),
    # held out from the maker method's choice (`--only makers:` skips them), seeds fixed first
    ("makers (held out): NO bids 1–5¢ below the truth, 64 sweeps and news, shock ρ = 0.5 "
     "lasting days (φ = 0.5)", 2000, {**MAKERS_STRESS, "shock": 0.5, "persistence": 0.5}),
    ("makers (held out): NO bids 1–5¢ below the truth, 64 sweeps and news, shock ρ = 0.3 "
     "lasting days (φ = 0.8)", 2000, {**MAKERS_STRESS, "shock": 0.3, "persistence": 0.8}),
    ("makers (held out): NO bids 5–25¢ below the truth, 16 sweeps and news, shock ρ = 0.5 "
     "lasting days (φ = 0.8)", 2000, {**MAKERS, "news": 0.1, "shock": 0.5, "persistence": 0.8}),
    # held out from the H1 method's choice (`--only "H1-shaped:"` skips them), seeds fixed first
    ("H1-shaped (held out): H1's NO cutoffs, half spread 1¢, shock ρ = 0.5 lasting days "
     "(φ = 0.8)", 2000, {**H1_SHAPED, "shock": 0.5, "persistence": 0.8}),
    ("H1-shaped (held out): NO at YES ≤ 20¢ only, half spread 0.5¢, binaries only, shock "
     "ρ = 0.3 lasting days (φ = 0.5)", 2000,
     {**H1_COUPLED, "bucket_share": 0.0, "shock": 0.3, "persistence": 0.5}),
    ("H1-shaped (held out): NO at YES ≤ 20¢ only, half spread 0.5¢, ladders all strike "
     "ladders, shock ρ = 0.5 lasting days (φ = 0.8)", 2000,
     {**H1_COUPLED, "strike_share": 1.0, "shock": 0.5, "persistence": 0.8}),
    ("H1-shaped (held out): NO at YES ≤ 20¢ only, half spread 0.5¢, noisier signals (σ = 1), "
     "shock ρ = 0.5 lasting days (φ = 0.8)", 2000,
     {**H1_COUPLED, "signal": 1.0, "shock": 0.5, "persistence": 0.8}),
]
POWER_COLUMNS = ["naive in-sample", "null", "null + DSR", "null + DSR + PBO", "Romano–Wolf",
                 "widened null", "gauntlet"]


def _has_edge(kwargs: dict) -> bool:
    edge = (kwargs.get("confirmation") or {}).get("edge", kwargs.get("edge"))
    return edge is not None and edge.cents > 0


def _cell(rates_by_alpha: dict, column: str, worlds: int | None) -> str:
    """'% at α / % at α/m', bold where a no-edge rate is over α + 2 SE."""
    out = []
    for alpha, r in rates_by_alpha.items():
        text = f"{100 * r[column]:.1f}"
        if worlds and r[column] > alpha + 2 * math.sqrt(alpha * (1 - alpha) / worlds):
            text = f"**{text}**"
        out.append(text)
    return " / ".join(out)


SIZE_HEADER = ("| No-edge world | n | ρ̂ | cross-day | widening | " + " | ".join(PROCEDURES)
               + " |")
POWER_HEADER = ("| Edge world | true net edge per position | positions/day | "
                + " | ".join(POWER_COLUMNS) + " |")


def _rows(grid: list, scale: float, seed: int = 0, only: str = "", repeated: str = "",
          **extra):
    """(label, worlds, settings, `one_world` results) for each row of a grid whose label
    starts with `only`, in order, each row seeded by its position in the grid; with
    `repeated`, under that `Gates.repeated`."""
    for i, (label, worlds, kwargs) in enumerate(grid):
        if not label.startswith(only):
            continue
        if repeated:
            kwargs = {**kwargs, "gates": replace(kwargs.get("gates", GATES), repeated=repeated)}
        n = max(20, int(worlds * scale))
        yield label, n, kwargs, simulate(n, seed=seed + i, **extra, **kwargs)
        print(f"{label}: done", file=sys.stderr, flush=True)


def report(scale: float = 1.0, only: str = "", repeated: str = "") -> str:
    """The size and power tables in docs/BACKTESTING.md, from `GRID` (`scale` shrinks it;
    `only`, see `_rows`), then the mean `_coupling` of each row whose positions repeat
    (makers, H1-shaped)."""
    size = [SIZE_HEADER, "|---|---|---|---|---|" + "---|" * len(PROCEDURES)]
    power = [POWER_HEADER, "|---|---|---|" + "---|" * len(POWER_COLUMNS)]
    coupling = []
    for label, n, kwargs, results in _rows(GRID, scale, only=only, repeated=repeated):
        by_alpha = {a: rates(results, a) for a in (GATES.alpha, GATES.level)}
        r = by_alpha[GATES.alpha]
        if not math.isnan(r["coupling"]):  # positions repeat (NaN if a world has none)
            coupling.append(f"- {label}: R = {r['coupling']:.2f} "
                            f"(R̄ from prices: {r['r_bar']:.2f})")
        if _has_edge(kwargs):
            power.append(f"| {label} | {r['edge_per_position'] / CENT:+.2f}¢ | "
                         f"{r['positions_per_day']:.1f} | "
                         + " | ".join(_cell(by_alpha, c, None) for c in POWER_COLUMNS) + " |")
        else:
            size.append(f"| {label} | {n} | {r['rho']:.3f} | {r['cross_day']:.2f} | "
                        f"{r['widening']:.2f} | "
                        + " | ".join(_cell(by_alpha, c, n) for c in PROCEDURES) + " |")
    return "\n\n".join("\n".join(block) for block in (size, power, coupling) if block)


PREDICTION_GRID = [  # (label, worlds, one_world settings): edges on every contract bought
    ("0.5¢ lower asks", 300, {"edge": synthetic.Edge(0.0, 1.0, 0.5)}),
    ("1¢ lower asks", 300, {"edge": synthetic.Edge(0.0, 1.0, 1)}),
    ("1¢ lower asks, binaries only, shock ρ = 0.5", 300,
     {"edge": synthetic.Edge(0.0, 1.0, 1), **BINARIES_SHOCK}),
    ("0.5¢ lower asks, binaries only, shock ρ = 0.3, 100 events/day", 300,
     {"edge": synthetic.Edge(0.0, 1.0, 0.5), "shock": 0.3, "bucket_share": 0.0,
      "events_per_day": 100}),
    ("1¢ lower asks, binaries only, shock ρ = 0.5 lasting days (φ = 0.8)", 300,
     {"edge": synthetic.Edge(0.0, 1.0, 1), **BINARIES_SHOCK, "persistence": 0.8}),
    ("0.5¢ lower asks, 100 events/day", 300,
     {"edge": synthetic.Edge(0.0, 1.0, 0.5), "events_per_day": 100}),
    ("YES family: 1¢ lower asks", 300, {"side": "yes", "edge": synthetic.Edge(0.0, 1.0, 1)}),
    ("mixed family (adds YES favorites): 1¢ lower NO asks, binaries only, shock ρ = 0.3, "
     "100 events/day", 300, {"favorites": True, "edge": synthetic.Edge(0.0, 1.0, 1),
                            "shock": 0.3, "bucket_share": 0.0, "events_per_day": 100}),
    ("makers: fills 4¢ cheaper, NO bids 5–25¢ below the truth", 300,
     {**MAKERS, "edge": synthetic.Edge(0.0, 1.0, 4)}),
    ("makers: fills 3.5¢ cheaper, NO bids 1–5¢ below the truth, 64 sweeps and news", 300,
     {**MAKERS_STRESS, "edge": synthetic.Edge(0.0, 1.0, 3.5)}),
    ("H1-shaped: 0.75¢ lower asks, NO at YES ≤ 20¢ only, half spread 0.5¢", 300,
     {**H1_COUPLED, "edge": synthetic.Edge(0.0, 1.0, 0.75)}),
    ("H1-shaped: 1¢ lower asks, NO at YES ≤ 20¢ only, half spread 0.5¢, shock ρ = 0.5 "
     "lasting days (φ = 0.8)", 300, {**H1_COUPLED, "edge": synthetic.Edge(0.0, 1.0, 1),
                                    "shock": 0.5, "persistence": 0.8}),
    ("H1-shaped (held out): 0.75¢ lower asks, NO at YES ≤ 20¢ only, half spread 0.5¢, shock "
     "ρ = 0.3 lasting days (φ = 0.5)", 300, {**H1_COUPLED, "edge": synthetic.Edge(0.0, 1.0, 0.75),
                                            "shock": 0.3, "persistence": 0.5}),
]
PREDICTION_HEADER = ("| Edge world | true net edge per position | widening | power "
                     "predicted / gauntlet's | the same where the cross-day estimate is "
                     "negative (worlds) |")


def _calibration(results: list[dict[str, float]]) -> str:
    """'predicted / measured' power at α/m, in %, over `results`; bold where predicted
    exceeds measured by more than 2 Monte Carlo standard errors of their per-world
    difference (the pass rule, docs/BACKTESTING.md step 6) and as few rejections as
    measured have less than a one-sided 2σ chance under the per-world predictions
    (`_at_most`). ‡ marks a cell only the first would put in bold."""
    if len(results) < 2:
        return "–"
    predicted = np.array([s["power_predicted"] for s in results])
    found = np.array([PROCEDURES["gauntlet"](s, GATES.level) for s in results], dtype=float)
    gap = predicted - found
    text = f"{100 * predicted.mean():.1f} / {100 * found.mean():.1f}"
    if gap.mean() <= 2 * gap.std(ddof=1) / np.sqrt(len(gap)):
        return text
    # and so few rejections are unlikely (one-sided 2σ) if each world's prediction is right
    unlikely = _at_most(predicted, int(found.sum())) < CALIBRATION_TAIL
    return f"**{text}**" if unlikely else f"{text} ‡"


CALIBRATION_TAIL = scipy_stats.norm.cdf(-2)  # one-sided 2σ


def _at_most(p: np.ndarray, k: int) -> float:
    """P(K ≤ k) for K a sum of independent Bernoulli(p_i) (Poisson binomial), exactly."""
    dist = np.zeros(k + 2)
    dist[0] = 1.0
    for q in p:
        dist[1:] = dist[1:] * (1 - q) + dist[:-1] * q
        dist[0] *= 1 - q
    return float(dist[:k + 1].sum())


def prediction_report(scale: float = 1.0, only: str = "", repeated: str = "") -> str:
    """The predicted-vs-measured power table in docs/BACKTESTING.md: `gauntlet.power`'s
    estimate, made as `confirm` makes it with conformance's draws, against how often the
    gauntlet found the edge, both at α/m; again over the worlds whose cross-day estimate
    is negative, where a per-day error would show (`gauntlet.power`). Synthetic positions
    settle on the day they are priced, so this doesn't measure the stand-in `confirm` uses for
    later settlement. `only`: see `_rows`."""
    rows = [PREDICTION_HEADER, "|---|---|---|---|---|"]
    for label, _, _, results in _rows(PREDICTION_GRID, scale, seed=1000, only=only,
                                      repeated=repeated, predict=True):
        r, tilted = rates(results), [s for s in results if s["cross_day"] < 0]
        rows.append(f"| {label} | {r['edge_per_position'] / CENT:+.2f}¢ | "
                    f"{r['widening']:.2f} | {_calibration(results)} | "
                    f"{_calibration(tilted)} ({len(tilted)}) |")
    return "\n".join(rows)


if __name__ == "__main__":
    args, options = sys.argv[1:], {"--only": "", "--repeated": ""}
    for flag in options:
        if flag in args:
            at = args.index(flag)
            options[flag], args = args[at + 1], args[:at] + args[at + 2:]
    only, repeated = options["--only"], options["--repeated"]
    if args[:1] == ["power"]:
        print(prediction_report(float(args[1]) if len(args) > 1 else 1.0, only, repeated))
    else:
        print(report(float(args[0]) if args else 1.0, only, repeated))
