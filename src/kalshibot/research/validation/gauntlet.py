"""The pre-registered decision procedure every hypothesis family goes through (the
method and the simulations behind it: docs/BACKTESTING.md).

1. Discovery picks the configuration with the best Sharpe (`select`).
2. Confirmation tests it once against H0: net edge ≤ 0, with the widened market-
   calibrated null (`null.py`) and the stationary bootstrap of daily returns; the
   family's p-value is the larger of the two (`Verdict.p_value`).
3. Holm across the pre-registered families (`decide`).

DSR, PBO, and the effective number of trials are diagnostics of the discovery
selection, reported with every verdict.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from kalshibot.kalshi.models import PRICE_SCALE
from kalshibot.research import calendar
from kalshibot.research.validation import null, stats


@dataclass(frozen=True)
class Gates:
    alpha: float = 0.05  # family-wise error rate across all pre-registered families
    families: int = 12  # Holm's m; a family never run counts as p = 1
    draws: int = 20_000  # null outcome draws and bootstrap resamples
    pbo_blocks: int = 10
    devig: str = "multiplicative"  # how `null.inputs` turns an event's mids into P(YES)
    dependence_days: int = 400  # the dependence estimates use discovery's last decision days


PREREGISTERED = Gates()


@dataclass(frozen=True)
class Positions:
    """Contracts held to settlement, or still open at the window's end and counted as
    lost (the null's scope). Money in micro-dollars."""

    net_pnl: np.ndarray  # realized payoff − paid, per position
    paid: np.ndarray  # price × count + fee: the all-in cost
    won: np.ndarray  # share of $1 per contract paid at settlement: 0 or 1 unless scalar
    buy_yes: np.ndarray  # True: holds YES
    count: np.ndarray  # size units
    p_yes: np.ndarray  # null P(YES) at decision time (`null.inputs`)
    lo: np.ndarray  # null outcome interval on the cluster's uniform draw
    hi: np.ndarray
    cluster: np.ndarray  # integer cluster ids (`null.inputs`)
    day: np.ndarray  # settlement day: integer UTC days, as settle.close_day

    @classmethod
    def from_frame(cls, df: pd.DataFrame) -> Positions:
        """From one row per position: pnl, paid, won, side ('yes' | 'no'), fill_count,
        p_yes, lo, hi, cluster, day."""
        return cls(net_pnl=df["pnl"].to_numpy(), paid=df["paid"].to_numpy(),
                   won=df["won"].to_numpy(float), buy_yes=(df["side"] == "yes").to_numpy(),
                   count=df["fill_count"].to_numpy(), p_yes=df["p_yes"].to_numpy(),
                   lo=df["lo"].to_numpy(), hi=df["hi"].to_numpy(),
                   cluster=df["cluster"].to_numpy(), day=df["day"].to_numpy())

    def check(self) -> None:
        won = np.asarray(self.won, dtype=float)
        if not ((0 <= won) & (won <= 1)).all():
            raise ValueError("won is the share of $1 paid per contract, in [0, 1]")
        if not ((0 <= self.lo) & (self.lo < self.hi) & (self.hi <= 1)).all():
            raise ValueError("each outcome interval must be a non-empty part of [0, 1]")
        day = np.asarray(self.day)
        if day.dtype.kind not in "iu" or (len(day) and day.max() - day.min() > 40_000):
            raise ValueError("day must be an integer day number (UTC, as settle.close_day)")


def pooled(positions: list[pd.DataFrame]) -> pd.DataFrame:
    """Every configuration's positions for the dependence estimates, each bet once: one
    per contract, side and decision time."""
    return pd.concat(positions, ignore_index=True).drop_duplicates(
        ["ticker", "side", "decision_time"])


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
    `discovery_positions`: every configuration's decided in discovery's last
    `gates.dependence_days` (`pooled`), all settled before confirmation, for the
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
    """Which families pass, with family-wise error ≤ `gates.alpha` over all
    `gates.families` (m) pre-registered families. A family confirmed forward
    (`calendar.FORWARD`, f of them) is decided alone at α/m once its window has settled;
    the others by Holm over the remaining m − f places at α(m − f)/m, a family not run
    counting as p = 1. The union bound adds the two, so neither waits for the other."""
    m, forward = gates.families, calendar.FORWARD
    rest = [n for n in p_values if n not in forward]
    places = m - len(forward)
    if len(rest) > places:
        raise ValueError(f"{len(rest)} in-calendar families, but only {places} places")
    out = {n: bool(p <= gates.alpha / m) for n, p in p_values.items() if n in forward}
    adjusted = stats.holm(np.array([p_values[n] for n in rest] + [1.0] * (places - len(rest))))
    return out | {n: bool(a <= gates.alpha * places / m)
                  for n, a in zip(rest, adjusted, strict=False)}
