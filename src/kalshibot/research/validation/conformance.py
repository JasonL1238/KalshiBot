"""Measure the research pipeline on synthetic worlds where the truth is known.

The family under test buys longshots as a taker paying 2026 fees: every contract whose
YES ask is at most θ, for θ in `THRESHOLDS`, one contract each, buying NO (the usual
favorite–longshot trade) or YES (its mirror, where a null built from the mid is too
narrow); a mixed family also has a configuration buying YES favorites. Each world has a
discovery period, where the configuration is picked by Sharpe, four times as long as
the confirmation period that tests it (the real calendar's ratio is about ten); the
periods can differ (`confirmation`), e.g. an edge that vanishes at the seam or a busier
market. `one_world` returns every statistic the candidate
decision procedures use, and `PROCEDURES` turns them into "edge found":

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
and prints the two tables in docs/BACKTESTING.md; append rows only, since each row is
seeded by its position.
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

THRESHOLDS = [600, 700, 800, 1000, 1200, 1500, 2000]  # YES ask cutoffs, price units
FAVORITES = 8000  # the mixed family also buys YES at asks of at least this
GATES = replace(gauntlet.PREREGISTERED, draws=2_000)  # fewer draws: slightly less power
MIN_DSR, MAX_PBO = 0.95, 0.2  # candidate gates, measured
BASE = pd.Timestamp("2020-01-01", tz="UTC")  # day 0 of every world


def _configs(side: str, favorites: bool) -> list[tuple[str, str, int]]:
    """(name, side bought, YES-ask cutoff: ≤ cutoff, or ≥ −cutoff when negative)."""
    out = [(f"{side} ≤ {t}", side, t) for t in THRESHOLDS]
    return out + [("yes ≥ favorites", "yes", -FAVORITES)] if favorites else out


def _positions(df: pd.DataFrame, config: tuple[str, str, int]) -> pd.DataFrame:
    """One contract bought per quote the configuration takes, in the columns
    `settle.daily_returns` and `gauntlet.Positions.from_frame` read."""
    _, side, cut = config
    pos = df[df["ask_yes"] <= cut if cut > 0 else df["ask_yes"] >= -cut].copy()
    pos["side"], pos["fill_count"] = side, SIZE_SCALE
    pos["won"] = pos["yes"] if side == "yes" else ~pos["yes"]
    pos["cost"] = pos["paid"] = synthetic.taker_cost(
        pos["ask_yes" if side == "yes" else "ask_no"].to_numpy())
    pos["pnl"] = np.where(pos["won"], PRICE_SCALE * SIZE_SCALE, 0) - pos["cost"]
    pos["decision_time"] = pos["fill_time"] = pos["close_time"] = (
        BASE + pd.to_timedelta(pos["day"], unit="D") + pd.Timedelta(hours=12))  # mid-day
    return pos


def _daily(pos: pd.DataFrame, days: range) -> pd.Series:
    return daily_returns(pos, BASE + pd.Timedelta(days=days.start),
                         BASE + pd.Timedelta(days=days.stop))


def _romano_wolf(conf: pd.DataFrame, configs: list, rng: np.random.Generator,
                 draws: int) -> float:
    """Family p-value with every configuration tested on confirmation: the smallest
    Romano–Wolf adjusted p-value of the studentized net PnLs, from one joint null
    simulation of every confirmation contract."""
    sims = null.draw_outcomes(conf["lo"].to_numpy(), conf["hi"].to_numpy(),
                              conf["cluster"].to_numpy(), draws, rng)
    observed, simulated = [], []
    for config in configs:
        pos = _positions(conf, config)
        at = conf.index.get_indexer(pos.index)
        sim = null.recentered_pnl(sims[:, at], (pos["side"] == "yes").to_numpy(),
                                  np.full(len(pos), SIZE_SCALE), pos["p_yes"].to_numpy())
        sd = sim.std() or 1.0
        observed.append(pos["pnl"].sum() / sd)
        simulated.append(sim / sd)
    return float(stats.romano_wolf(np.array(observed), np.column_stack(simulated)).min())


def one_world(rng: np.random.Generator, side: str = "no", quotes: str = "boundary",
              discovery_days: int = 400, confirmation_days: int = 100,
              events_per_day: int = 20, favorites: bool = False,
              confirmation: dict | None = None, gates: gauntlet.Gates = GATES,
              **world) -> dict[str, float]:
    """Every decision statistic for one synthetic world. `world` goes to `synthetic.world`
    for both periods; `confirmation` overrides it (and `events_per_day`) for the second."""
    seam, days = discovery_days, discovery_days + confirmation_days
    later = {"events_per_day": events_per_day, **world, **(confirmation or {})}
    df = pd.concat([synthetic.world(seam, events_per_day, rng, quotes, side=side, **world),
                    synthetic.world(confirmation_days, later.pop("events_per_day"), rng,
                                    quotes, side=side, first_day=seam, **later)],
                   ignore_index=True)
    df = null.inputs(df.assign(mid=(df["ask_yes"] + PRICE_SCALE - df["ask_no"]) / 2),
                     gates.devig)  # one snapshot: every contract quoted once
    disc, conf = df[df["day"] < seam], df[df["day"] >= seam]
    configs = _configs(side, favorites)
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
    p_win = np.where(pos["side"] == "yes", pos["q"], 1 - pos["q"])
    ask = np.where(pos["side"] == "yes", pos["ask_yes"], pos["ask_no"])
    return {**asdict(v), "p_value": v.p_value, "t_in": float(t_in), "p_t": float(p_t),
            "p_rw": _romano_wolf(conf, configs, rng, gates.draws),
            "edge_per_position": float(synthetic.taker_net(p_win, ask).mean()) if len(pos)
            else 0.0, "positions_per_day": len(pos) / confirmation_days}


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
MEANS = ("edge_per_position", "rho", "cross_day", "widening", "positions_per_day")


def _world_task(args):
    seed, kwargs = args
    return one_world(np.random.default_rng(seed), **kwargs)


def simulate(worlds: int, seed: int = 0, **kwargs) -> list[dict[str, float]]:
    """`one_world` statistics for `worlds` independent worlds, in parallel processes."""
    tasks = [(s, kwargs) for s in np.random.SeedSequence(seed).spawn(worlds)]
    with ProcessPoolExecutor() as pool:
        return list(pool.map(_world_task, tasks, chunksize=4))


def rates(results: list[dict[str, float]], alpha: float = 0.05) -> dict[str, float]:
    """Share of worlds in which each procedure declares an edge at level `alpha`, plus
    the means of `MEANS` (the true net edge per confirmed position in micro-dollars, the
    gauntlet's dependence estimates and widening, confirmed positions per day)."""
    out = {name: float(np.mean([bool(rule(s, alpha)) for s in results]))
           for name, rule in PROCEDURES.items()}
    out.update({k: float(np.mean([s[k] for s in results])) for k in MEANS})
    return out


LONGSHOTS = (0.0, 0.15)  # true P(YES) below 15%
BINARIES_SHOCK = {"shock": 0.5, "bucket_share": 0.0}
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
]
POWER_COLUMNS = ["naive in-sample", "null", "null + DSR", "null + DSR + PBO", "Romano–Wolf",
                 "widened null", "gauntlet"]


def _has_edge(kwargs: dict) -> bool:
    edge = (kwargs.get("confirmation") or {}).get("edge", kwargs.get("edge"))
    return edge is not None and edge.cents > 0


def _cell(rates_by_alpha: dict, column: str, worlds: int | None) -> str:
    """'% at α = 0.05 / % at 0.05/12', bold where a no-edge rate is over α + 2 SE."""
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


def report(scale: float = 1.0) -> str:
    """The two tables in docs/BACKTESTING.md, from `GRID` (`scale` shrinks it)."""
    size = [SIZE_HEADER, "|---|---|---|---|---|" + "---|" * len(PROCEDURES)]
    power = [POWER_HEADER, "|---|---|---|" + "---|" * len(POWER_COLUMNS)]
    for i, (label, worlds, kwargs) in enumerate(GRID):
        n = max(20, int(worlds * scale))
        results = simulate(n, seed=i, **kwargs)
        by_alpha = {a: rates(results, a) for a in (0.05, 0.05 / 12)}
        r = by_alpha[0.05]
        if _has_edge(kwargs):
            power.append(f"| {label} | {r['edge_per_position'] / CENT:+.2f}¢ | "
                         f"{r['positions_per_day']:.1f} | "
                         + " | ".join(_cell(by_alpha, c, None) for c in POWER_COLUMNS) + " |")
        else:
            size.append(f"| {label} | {n} | {r['rho']:.3f} | {r['cross_day']:.2f} | "
                        f"{r['widening']:.2f} | "
                        + " | ".join(_cell(by_alpha, c, n) for c in PROCEDURES) + " |")
        print(f"{label}: done", file=sys.stderr, flush=True)
    return "\n".join(size) + "\n\n" + "\n".join(power)


if __name__ == "__main__":
    print(report(float(sys.argv[1]) if len(sys.argv) > 1 else 1.0))
