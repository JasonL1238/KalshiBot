"""The pre-registered decision procedure every hypothesis family goes through (the
method and the simulations behind it: docs/BACKTESTING.md).

1. Discovery picks the configuration with the best Sharpe (`select`).
2. Confirmation tests it once against H0: net edge ≤ 0, with the widened market-
   calibrated null (`null.py`) and the stationary bootstrap of daily returns; the
   family's p-value is the larger of the two (`Verdict.p_value`).
3. Holm across all pre-registered families (`decide`).

DSR, PBO, and the effective number of trials are diagnostics of the discovery
selection, reported with every verdict.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from kalshibot.kalshi.models import PRICE_SCALE
from kalshibot.research.validation import null, stats


@dataclass(frozen=True)
class Gates:
    alpha: float = 0.05  # family-wise error rate across all pre-registered families
    families: int = 12  # Holm's m; a family never run counts as p = 1
    draws: int = 20_000  # null outcome draws and bootstrap resamples
    pbo_blocks: int = 10
    devig: str = "multiplicative"  # how `null.inputs` turns an event's mids into P(YES)


PREREGISTERED = Gates()


@dataclass(frozen=True)
class Positions:
    """Binary contracts held to settlement (the null's scope). Money in micro-dollars."""

    net_pnl: np.ndarray  # realized payoff − paid, per position
    paid: np.ndarray  # price × count + fee: the all-in cost
    won: np.ndarray  # the contract held settled in the money
    buy_yes: np.ndarray  # True: holds YES
    count: np.ndarray  # size units
    p_yes: np.ndarray  # null P(YES) at decision time (`null.inputs`)
    lo: np.ndarray  # null outcome interval on the cluster's uniform draw
    hi: np.ndarray
    cluster: np.ndarray  # integer cluster ids (one underlying at one settlement time)
    day: np.ndarray  # settlement day of the cluster: integer UTC days, as settle.close_day

    def check(self) -> None:
        won = np.asarray(self.won)
        if not (won.dtype == bool or np.isin(won, (0, 1)).all()):
            raise ValueError("won must be 0/1: the null covers binary contracts only")
        if not ((0 <= self.lo) & (self.lo < self.hi) & (self.hi <= 1)).all():
            raise ValueError("each outcome interval must be a non-empty part of [0, 1]")
        day = np.asarray(self.day)
        if day.dtype.kind not in "iu" or (len(day) and day.max() - day.min() > 40_000):
            raise ValueError("day must be an integer day number (UTC, as settle.close_day)")


@dataclass(frozen=True)
class Verdict:
    config: str
    p_null: float  # market-calibrated null on confirmation, widened by `widening`
    p_boot: float  # stationary bootstrap of confirmation daily returns
    p_independent: float  # diagnostic: the null without the widening
    rho: float  # same-day YES correlation between clusters, estimated on discovery
    cross_day: float  # Bartlett-weighted cross-day correlations, estimated on discovery
    widening: float  # null variance factor applied (1: none)
    dsr: float  # diagnostic: the selected config's discovery Sharpe, deflated for trials
    pbo: float  # diagnostic: overfitting of the discovery selection
    n_trials: int  # effective number of discovery configurations

    @property
    def p_value(self) -> float:
        """The family's p-value for Holm: both tests must reject."""
        return max(self.p_null, self.p_boot)


def select(discovery: pd.DataFrame) -> str:
    """The configuration (column) with the best discovery Sharpe."""
    return str(discovery.columns[_best(discovery.to_numpy())])


def _best(trials: np.ndarray) -> int:
    return int(np.argmax(stats.sharpe(trials)))


def evaluate(discovery: pd.DataFrame, discovery_positions: Positions,
             confirmation_returns: pd.Series, positions: Positions, rng: np.random.Generator,
             gates: Gates = PREREGISTERED) -> Verdict:
    """`discovery`: days × configs daily returns (one zero-filled calendar).
    `discovery_positions`: every configuration's, all settled before confirmation, for the
    dependence estimates. `confirmation_returns` and `positions` belong to
    `select(discovery)` on confirmation."""
    d, p = discovery_positions, positions
    for both in (d, p):
        both.check()
    if len(d.day) and len(p.day) and d.day.max() >= p.day.min():
        raise ValueError("discovery positions must settle before every confirmation one")
    trials = discovery.to_numpy()
    best = _best(trials)
    enough = trials.shape[1] > 1 and len(trials) >= 2 * gates.pbo_blocks
    pbo = stats.pbo(trials, n_blocks=gates.pbo_blocks) if enough else float("nan")
    n_trials = stats.effective_trials(trials)
    dep = null.dependence(d.won, d.buy_yes, d.count, d.p_yes, d.lo, d.hi, d.cluster, d.day,
                          d.paid / (d.count * PRICE_SCALE))
    widen = null.widening(dep, p.buy_yes, p.count, p.p_yes, p.lo, p.hi, p.cluster, p.day,
                          p.paid / (p.count * PRICE_SCALE))
    totals = null.simulate_totals(p.lo, p.hi, p.cluster, p.buy_yes, p.count, p.p_yes,
                                  gates.draws, rng)
    observed = float(p.net_pnl.sum())
    p_boot = stats.bootstrap_p_value(confirmation_returns.to_numpy(), gates.draws, rng)
    trial_sr_var = float(np.var(stats.sharpe(trials), ddof=1)) if trials.shape[1] > 1 else 0.0
    dsr = stats.dsr(trials[:, best], n_trials, trial_sr_var)
    return Verdict(str(discovery.columns[best]),
                   float(null.p_value(observed, totals * np.sqrt(widen))), p_boot,
                   float(null.p_value(observed, totals)), dep.rho, dep.cross_day, widen,
                   float(dsr), pbo, n_trials)


def decide(p_values: Mapping[str, float], gates: Gates = PREREGISTERED) -> dict[str, bool]:
    """Which families pass: Holm across all `gates.families` pre-registered families."""
    if len(p_values) > gates.families:
        raise ValueError(f"{len(p_values)} families, but {gates.families} were pre-registered")
    names = list(p_values)
    padded = [p_values[n] for n in names] + [1.0] * (gates.families - len(names))
    adjusted = stats.holm(np.array(padded))
    return {n: bool(a <= gates.alpha) for n, a in zip(names, adjusted, strict=False)}
