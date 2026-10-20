"""The pre-registered decision procedure every hypothesis family goes through (the
method and the simulations behind it: docs/BACKTESTING.md).

1. Discovery picks the configuration with the best Sharpe (`select`).
2. Confirmation tests it once against H0: net edge ≤ 0, with the widened market-
   calibrated null (`null.py`) and the stationary bootstrap of daily returns; the
   family's p-value is the larger of the two (`Verdict.p_value`). Its `power` at the
   family's smallest meaningful edge is simulated before any outcome is read.
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
    families: int = 12  # Holm's m, a place per hypothesis; one with no family run is p = 1
    draws: int = 20_000  # null outcome draws and bootstrap resamples
    pbo_blocks: int = 10
    devig: str = "multiplicative"  # how `null.inputs` turns an event's mids into P(YES)
    dependence_days: int = 400  # the dependence estimates use discovery's last decision days
    power_worlds: int = 1_000  # simulated confirmations behind a power estimate
    power_resamples: int = 2_000  # bootstrap draws in each
    jackknife_blocks: int = 10  # a maker sample's widening SE (`_null`)

    @property
    def level(self) -> float:
        """α/m: Holm's first step, and a forward-confirmed family's whole level."""
        return self.alpha / self.families


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
    p_yes: np.ndarray  # null P(YES) when it is priced (`null.inputs`)
    lo: np.ndarray  # null outcome interval on the cluster's uniform draw
    hi: np.ndarray
    cluster: np.ndarray  # integer cluster ids (`null.inputs`)
    day: np.ndarray  # settlement day: integer UTC days, as settle.close_day
    maker: np.ndarray | None = None  # True: a resting bid's fill (`_null`); None: no maker fills
    order: np.ndarray | None = None  # when each is priced, for ordering fills; None: as given
    contract: np.ndarray | None = None  # integer contract codes (from ticker); None: unknown
    exclusive: np.ndarray | None = None  # its cluster is an exclusive event's; None: unknown

    @classmethod
    def from_frame(cls, df: pd.DataFrame) -> Positions:
        """From one row per position: pnl, paid, won, side ('yes' | 'no'), fill_count,
        p_yes, lo, hi, cluster, day, and optionally maker (the fill schema's), ticker and
        priced_at. A family of resting bids trades standalone contracts on one side: the
        Maker null takes a cluster's fills as sharing one outcome, a covariance that
        holds only for one side's fills, so maker fills on several contracts of one cluster (an
        event's buckets, a ladder's strikes) or on both sides of one are refused."""
        if "maker" in df and df["maker"].any():
            makers = df[df["maker"].astype(bool)].groupby("cluster")
            if "ticker" in df and (makers["ticker"].nunique() > 1).any():
                raise ValueError("maker fills on several contracts of one cluster: a family "
                                 "of resting bids trades standalone contracts only")
            if (makers["side"].nunique() > 1).any():
                raise ValueError("maker fills on both sides of one contract: the Maker "
                                 "null's variance is conservative for one side's fills only")
        return cls(net_pnl=df["pnl"].to_numpy(), paid=df["paid"].to_numpy(),
                   won=df["won"].to_numpy(float), buy_yes=(df["side"] == "yes").to_numpy(),
                   count=df["fill_count"].to_numpy(), p_yes=df["p_yes"].to_numpy(),
                   lo=df["lo"].to_numpy(), hi=df["hi"].to_numpy(),
                   cluster=df["cluster"].to_numpy(), day=df["day"].to_numpy(),
                   maker=df["maker"].to_numpy(bool) if "maker" in df else None,
                   order=(pd.to_datetime(df["priced_at"]).astype("int64").to_numpy()
                          if "priced_at" in df else None),
                   contract=(pd.factorize(df["ticker"])[0] if "ticker" in df else None),
                   exclusive=(df["mutually_exclusive"].to_numpy(bool)
                              if "mutually_exclusive" in df else None))

    @property
    def makes(self) -> bool:
        """Whether any position is a maker fill (`_null` then uses the maker null)."""
        return self.maker is not None and bool(self.maker.any())

    @property
    def break_even(self) -> np.ndarray:
        """The probability of winning at which each position's expected net PnL is 0."""
        return self.paid / (self.count * PRICE_SCALE)

    def check(self) -> None:
        won = np.asarray(self.won, dtype=float)
        if not ((0 <= won) & (won <= 1)).all():
            raise ValueError("won is the share of $1 paid per contract, in [0, 1]")
        for extra in (self.maker, self.order):
            if extra is not None and len(extra) != len(self.day):
                raise ValueError("maker and order need one entry per position")
        if not ((0 <= self.lo) & (self.lo < self.hi) & (self.hi <= 1)).all():
            raise ValueError("each outcome interval must be a non-empty part of [0, 1]")
        day = np.asarray(self.day)
        if day.dtype.kind not in "iu" or (len(day) and day.max() - day.min() > 40_000):
            raise ValueError("day must be an integer day number (UTC, as settle.close_day)")


def pooled(positions: list[pd.DataFrame]) -> pd.DataFrame:
    """Every configuration's positions for the dependence estimates, each bet once: one
    per contract, side and `priced_at` (`research.run._fill`)."""
    return pd.concat(positions, ignore_index=True).drop_duplicates(
        ["ticker", "side", "priced_at"])


@dataclass(frozen=True)
class Verdict:
    config: str
    p_null: float  # market-calibrated null on confirmation, widened by `widening`
    p_boot: float  # stationary bootstrap of confirmation daily returns
    p_independent: float  # diagnostic: the null without the widening
    rho: float  # same-day YES correlation between clusters, estimated on discovery
    cross_day: float  # weighted cross-day correlations (`null.Dependence.weight`), discovery
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
    dep, widen, totals = _null(d, p, rng, gates)
    observed = float(p.net_pnl.sum())
    p_boot = stats.bootstrap_p_value(confirmation_returns.to_numpy(), gates.draws, rng)
    trial_sr_var = float(np.var(stats.sharpe(trials), ddof=1)) if trials.shape[1] > 1 else 0.0
    dsr = stats.dsr(trials[:, best], n_trials, trial_sr_var)
    return Verdict(str(discovery.columns[best]),
                   float(null.p_value(observed, totals * np.sqrt(widen))), p_boot,
                   float(null.p_value(observed, totals)), dep.rho, dep.cross_day, widen,
                   float(dsr), pbo, n_trials)


def _null(d: Positions, p: Positions, rng: np.random.Generator, gates: Gates
          ) -> tuple[null.Dependence, float, np.ndarray]:
    """The dependence estimated on discovery positions `d`, the widening it implies for
    confirmation positions `p`, and `p`'s null totals (not yet widened). A sample with
    maker fills (docs/BACKTESTING.md, Maker null) gets their covariance on one outcome
    (`_null_args`), every cross-day correlation weighted alike
    (`null.Dependence.flat`), and a widening raised by its delete-a-block jackknife SE
    over blocks of the dependence sample's days (`null.jackknife`, `gates.jackknife_blocks`).
    A taker's repeated decisions on one contract get their shared outcome's covariance
    (`_repeated`; runs/h1-revision/candidates.md)."""
    sample = (d.won, d.buy_yes, d.count, d.p_yes, d.lo, d.hi, d.cluster, d.day, d.break_even)
    scored = (p.buy_yes, p.count, p.p_yes, p.lo, p.hi, p.cluster, p.day, p.break_even)
    flat = {"flat": True} if d.makes else {}
    dep = null.dependence(*sample, **_null_args(d), **flat)
    widen = null.widening(dep, *scored, **_null_args(p))
    if d.makes:
        replicates = null.jackknife(*sample, gates.jackknife_blocks, **_null_args(d), **flat)
        widen += null.jackknife_se(replicates, *scored, **_null_args(p))
    return dep, widen, null.simulate_totals(p.lo, p.hi, p.cluster, p.buy_yes, p.count,
                                            p.p_yes, gates.draws, rng)


def _null_args(p: Positions) -> dict:
    """The null functions' keyword arguments for `p`: `null._clusters`' exact for maker
    fills, whose fills on one contract all settle on its outcome (docs/BACKTESTING.md,
    Maker null), and repeated for a taker's repeated decisions (`_repeated`)."""
    maker = {"exact": p.maker, "order": p.order} if p.makes else {}
    return {**maker, **_repeated(p)}


def _repeated(p: Positions) -> dict:
    """`null._clusters`' repeated and order for `p`: the clusters where a taker buys one
    contract at more than one time (H1's decisions, or any family's), and no maker fills
    share them; or none. Every position of such a cluster is revised, including a
    one-time buy of another of its contracts."""
    if p.contract is None or p.order is None or p.exclusive is None:
        return {}  # without them, the nested null
    taker = np.ones(len(p.day), bool) if p.maker is None else ~p.maker
    key = pd.DataFrame({"cluster": p.cluster, "contract": p.contract, "order": p.order})[taker]
    times = key.groupby(["cluster", "contract"])["order"].nunique()
    hit = times[times > 1].index.get_level_values("cluster").unique()
    if p.maker is not None:
        hit = hit.difference(np.unique(p.cluster[p.maker]))
    rows = np.isin(p.cluster, hit)
    if not rows.any():
        return {}
    return {"repeated": null.Repeated(rows, p.contract, p.exclusive), "order": p.order}


def _alternative(p: Positions, edge: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(win, lo, hi): each position wins with its break-even probability plus `edge`
    (price units per contract), its interval resized to that YES probability from the
    end it is anchored at; a bucket's start moves down if it must, so the marginal stays
    exact."""
    win = np.clip(p.break_even + edge / PRICE_SCALE, 0.0, 1.0)
    q = np.where(p.buy_yes, win, 1 - win)
    lo = np.where(p.lo == 0, 0.0, np.where(p.hi >= 1, 1 - q, np.minimum(p.lo, 1 - q)))
    return win, lo, lo + q


def power(discovery_positions: Positions, positions: Positions, days: np.ndarray,
          edge: int, rng: np.random.Generator, gates: Gates = PREREGISTERED) -> float:
    """The share of `gates.power_worlds` simulated confirmations in which `evaluate`'s two
    tests both reject at `gates.level` if every position truly won with its break-even
    probability plus `edge` (price units per contract). `positions` are the
    confirmation's before any outcome is read (net_pnl and won unused; `day` stands in
    for when each settles), `days` the window's day numbers.

    Each world draws outcomes with the null's coupling (`_alternative`), then gets the
    dependence the widening describes, on the alternative's own probabilities: every
    day's deviation from its expectation is scaled by the square root of that factor, so
    the world's total varies as the null's model says it would under the alternative;
    but never by less than the factor's same-day part, since the real bootstrap sees each
    day's own variance whatever the cross-day estimate (a negative one would otherwise
    shrink the days and overstate power). With maker fills the worlds take the point
    dependence (the best guess of the truth) while the null keeps `_null`'s jackknife
    margin, which the real test pays too. The bootstrap uses `gates.power_resamples`
    draws per world, fewer than the test's: a slightly stricter cutoff, and a little
    more noise."""
    d, p = discovery_positions, positions
    dep, widen, totals = _null(d, p, rng, gates)
    null_net = totals * np.sqrt(widen)
    win, lo, hi = _alternative(p, edge)
    scale = np.sqrt(max(null.widening(dependence, p.buy_yes, p.count, hi - lo, lo, hi,
                                      p.cluster, p.day, win, **_null_args(p))
                        for dependence in (dep, null.Dependence(dep.rho, ()))))
    stake = p.count * float(PRICE_SCALE)
    day = np.searchsorted(days, p.day)
    expected = np.bincount(day, win * stake - p.paid, len(days))
    rejected, chunk = [], 100  # worlds simulated at once
    for start in range(0, gates.power_worlds, chunk):
        yes = null.draw_outcomes(lo, hi, p.cluster, min(chunk, gates.power_worlds - start), rng)
        net = np.where(p.buy_yes, yes, ~yes) * stake - p.paid
        daily = np.zeros((len(net), len(days)))
        np.add.at(daily.T, day, net.T)
        daily = expected + scale * (daily - expected)
        p_boot = [stats.bootstrap_p_value(r, gates.power_resamples, rng) for r in daily]
        rejected.append(np.maximum(null.p_value(daily.sum(axis=1), null_net), p_boot)
                        <= gates.level)
    return float(np.concatenate(rejected).mean())


def decide(p_values: Mapping[str, float], gates: Gates = PREREGISTERED) -> dict[str, bool]:
    """Which families pass, with family-wise error ≤ `gates.alpha` over the
    `gates.families` (m) places, one per hypothesis. A family confirmed forward
    (`calendar.FORWARD`, f of them) is decided alone at α/m once its window has settled;
    the others by Holm over the remaining m − f places at α(m − f)/m, a place with no
    family run counting as p = 1. The union bound adds the two, so neither waits for the
    other."""
    m, forward = gates.families, calendar.FORWARD
    rest = [n for n in p_values if n not in forward]
    places = m - len(forward)
    if len(rest) > places:
        raise ValueError(f"{len(rest)} in-calendar families, but only {places} places")
    out = {n: bool(p <= gates.level) for n, p in p_values.items() if n in forward}
    adjusted = stats.holm(np.array([p_values[n] for n in rest] + [1.0] * (places - len(rest))))
    return out | {n: bool(a <= gates.alpha * places / m)
                  for n, a in zip(rest, adjusted, strict=False)}
