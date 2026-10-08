"""The market-calibrated null: settle every position at random, as if prices were right.

Under H0 the market's de-vigged probability at decision time is the true one (`inputs`).
Outcomes are drawn jointly per *cluster* (one underlying at one settlement time: a
city-day, a BTC hourly fix, an economic release). Each cluster gets one uniform draw U,
and each contract owns an interval [lo, hi) of U's range, so:

- a strike ladder ("above X" for many X) settles monotonically: intervals [0, q_X);
- mutually exclusive buckets get exactly one winner: consecutive intervals summing to 1;
- every contract's marginal P(YES) equals its implied probability.

Positions in the same standalone or strike market taken at different times get nested
intervals [0, q), so their outcomes are comonotone. A bucket's interval moves with the
whole event's prices, so two positions in one bucket overlap only as their intervals
do; the covariance is exact for the intervals given. The statistic is recentered so
each config sits exactly at E[net PnL] = 0 (`recentered_pnl`), which tests H0: net
edge ≤ 0 rather than "the market is right". Only binary contracts held to settlement
fit.

Three things widen it (`widening`), none using confirmation outcomes:
- at that boundary a position wins with its all-in break-even probability, not the
  de-vigged one, so each position counts with the larger of the two variances;
- clusters settling on the same day move together (one BTC path across hourly fixes,
  one heat wave across cities): `dependence` estimates their correlation ρ;
- clusters a few days apart move together too (a heat wave lasts a week): `dependence`
  estimates the correlation c_h of clusters h days apart.
Both are correlations in the YES direction, estimated on discovery and applied to the
confirmation sample's own clusters, so a family's YES and NO positions offset as they
should and a busier confirmation gets proportionally more variance. Prices are
fixed-point units (kalshi.models).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import brentq

from kalshibot.kalshi.models import PRICE_SCALE


@dataclass(frozen=True)
class Dependence:
    rho: float  # correlation of YES surprises of different clusters on one day, in [0, 1]
    cross: tuple[float, ...]  # c_h, the same for clusters h = 1, 2, … days apart

    @property
    def cross_day(self) -> float:
        """2 Σ_h (1 − h/(H+1)) c_h: the Bartlett-weighted sum `widening` applies."""
        lags = len(self.cross)
        return 2 * sum((1 - h / (lags + 1)) * c for h, c in enumerate(self.cross, start=1))


def inputs(quotes: pd.DataFrame, method: str) -> pd.DataFrame:
    """Null P(YES) and outcome interval per quote. `quotes` has one row per contract
    quote: event, mutually_exclusive (the event's markets are exclusive buckets), mid (the
    YES mid in price units), and optionally decision_time (several snapshots of one
    event; default one). Exclusive events are de-vigged by `method` within each snapshot
    into consecutive intervals; other contracts get [0, p). P is clipped to [0.5%, 99.5%]
    so no interval is empty. Adds p_yes, lo, hi, and an integer cluster per event
    (numbered within the call, so pass every snapshot of a sample at once)."""
    df = quotes.copy()
    df["p_yes"] = (df["mid"] / PRICE_SCALE).clip(0.005, 0.995)
    df["lo"], df["hi"] = 0.0, df["p_yes"]
    snapshot = ["event", "decision_time"] if "decision_time" in df else ["event"]
    buckets = df[df["mutually_exclusive"].astype(bool) & df.duplicated(snapshot, keep=False)]
    for _, event in buckets.groupby(snapshot, sort=False):
        q = devig((event["p_yes"] * PRICE_SCALE).to_numpy(), method)
        lo, hi = bucket_intervals(q)
        df.loc[event.index, ["p_yes", "lo", "hi"]] = np.column_stack([q, lo, hi])
    df["cluster"] = pd.factorize(df["event"])[0]
    return df


def devig(prices: np.ndarray, method: str) -> np.ndarray:
    """Turn one mutually exclusive event's prices (summing above 1) into probabilities.

    multiplicative: q ∝ p.  shin: Shin's (1993) insider-trading model, which takes more
    margin off longshots. Prices that don't sum above 1 have no margin to remove and are
    just normalized."""
    p = np.asarray(prices, dtype=float) / PRICE_SCALE
    if method == "multiplicative" or p.sum() <= 1:  # Shin needs an overround
        return p / p.sum()
    if method == "shin":
        total = p.sum()

        def implied(z):
            return (np.sqrt(z**2 + 4 * (1 - z) * p**2 / total) - z) / (2 * (1 - z))

        return implied(brentq(lambda z: implied(z).sum() - 1, 0, 0.999))
    raise ValueError(f"unknown de-vig method {method!r}")


def bucket_intervals(probabilities: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Consecutive [lo, hi) intervals for mutually exclusive outcomes, in the given order."""
    hi = np.cumsum(probabilities)
    hi[-1] = 1.0  # absorb floating-point drift so the last bucket closes the range
    return np.concatenate([[0.0], hi[:-1]]), hi


def draw_outcomes(lo: np.ndarray, hi: np.ndarray, cluster: np.ndarray, n_draws: int,
                  rng: np.random.Generator) -> np.ndarray:
    """n_draws × n_positions booleans: did each position's contract settle YES?
    `cluster` holds integer ids; positions in one cluster share a uniform draw."""
    ids, index = np.unique(cluster, return_inverse=True)
    u = rng.random((n_draws, len(ids)))[:, index]
    return (u >= lo) & (u < hi)


def recentered_pnl(yes: np.ndarray, buy_yes: np.ndarray, count: np.ndarray,
                   p_yes: np.ndarray) -> np.ndarray:
    """Null distribution of a config's total net PnL at the boundary E[net PnL] = 0, in
    micro-dollars per draw: simulated payoff minus its null expectation. Costs and fees
    are the same in every draw, so compare it with the observed net PnL.

    yes: draws × positions outcomes; buy_yes: the position holds YES (else NO); count in
    size units; p_yes: the null P(YES) of each position's market at decision time."""
    weight = count * float(PRICE_SCALE)
    won = np.where(buy_yes, yes, ~yes)
    expected = np.where(buy_yes, p_yes, 1 - p_yes)
    with np.errstate(all="ignore"):  # macOS Accelerate raises spurious flags in matmul
        return won @ weight - expected @ weight


def simulate_totals(lo: np.ndarray, hi: np.ndarray, cluster: np.ndarray, buy_yes: np.ndarray,
                    count: np.ndarray, p_yes: np.ndarray, n_draws: int,
                    rng: np.random.Generator, chunk: int = 1_000) -> np.ndarray:
    """`recentered_pnl` of `draw_outcomes`, `chunk` draws at a time (the same random
    stream as one call, without holding draws × positions in memory)."""
    return np.concatenate([
        recentered_pnl(draw_outcomes(lo, hi, cluster, min(chunk, n_draws - start), rng),
                       buy_yes, count, p_yes) for start in range(0, n_draws, chunk)])


def p_value(observed_net: float, null_net: np.ndarray) -> float:
    """One-sided P(null ≥ observed), with the +1 correction so it is never exactly 0."""
    return (1 + np.sum(null_net >= observed_net)) / (1 + len(null_net))


def _clusters(buy_yes, count, p_yes, lo, hi, cluster, day, break_even) -> pd.DataFrame:
    """Per cluster: its day, the null variance of its summed won × count (exact, from the
    shared uniform draw), that variance at the break-even boundary (scaled by the
    positions' larger variances), and its direction: +1 if net long YES, else −1."""
    sign = np.where(buy_yes, 1.0, -1.0) * count  # won × count = constant + sign × YES
    win = np.where(buy_yes, p_yes, 1 - p_yes)
    df = pd.DataFrame({"cluster": cluster, "day": day, "lo": lo, "hi": hi, "sign": sign,
                       "own": win * (1 - win) * count**2,
                       "boundary": np.maximum(win * (1 - win),
                                              break_even * (1 - break_even)) * count**2})
    if (df.groupby("cluster")["day"].nunique() > 1).any():
        raise ValueError("a cluster settles at one time, so on one day")
    p = df["hi"] - df["lo"]
    df["var"] = df["sign"] ** 2 * p * (1 - p)  # singleton clusters; shared ones below
    for _, g in df[df.duplicated("cluster", keep=False)].groupby("cluster"):
        a, b, w = g["lo"].to_numpy(), g["hi"].to_numpy(), g["sign"].to_numpy()
        overlap = np.clip(np.minimum.outer(b, b) - np.maximum.outer(a, a), 0, None)
        df.loc[g.index, "var"] = 0.0
        df.loc[g.index[0], "var"] = w @ (overlap - np.outer(b - a, b - a)) @ w
    out = df.groupby("cluster").agg(day=("day", "first"), var=("var", "sum"),
                                    own=("own", "sum"), boundary=("boundary", "sum"),
                                    sign=("sign", "sum"))
    ratio = (out["boundary"] / out["own"]).where(out["own"] > 0, 1.0)
    out["var_b"] = out["var"].clip(lower=0) * ratio
    out["direction"] = np.where(out["sign"] >= 0, 1.0, -1.0)
    return out


def _days(clusters: pd.DataFrame) -> pd.DataFrame:
    """Per day: V0 and V, the null and boundary variances of its sum with independent
    clusters; W, the extra variance per unit of same-day YES correlation,
    Σ_{k≠l} d_k d_l √(v_k v_l) with d the cluster's direction; and A = Σ d_k √v_k, whose
    products across days carry the cross-day correlation."""
    sd = np.sqrt(clusters["var_b"])
    by_day = clusters["day"]
    own = (sd**2).groupby(by_day).sum()
    a = (clusters["direction"] * sd).groupby(by_day).sum()
    return pd.DataFrame({"V0": clusters["var"].clip(lower=0).groupby(by_day).sum(), "V": own,
                         "W": a**2 - own, "A": a})


def _calendar(series: pd.Series) -> np.ndarray:
    """A per-day series on every calendar day from its first to its last, 0 elsewhere."""
    return series.reindex(np.arange(series.index.min(), series.index.max() + 1),
                          fill_value=0.0).to_numpy()


def dependence(won: np.ndarray, buy_yes: np.ndarray, count: np.ndarray, p_yes: np.ndarray,
               lo: np.ndarray, hi: np.ndarray, cluster: np.ndarray, day: np.ndarray,
               break_even: np.ndarray) -> Dependence:
    """Estimate the YES-direction correlations from realized outcomes, by the method of
    moments. Each cluster's surprise x, the sum of (won − P_null(won)) × count over its
    positions less k times its variance (k = Σx / Σv removes any edge in proportion), is
    turned to the YES direction, y = d·x, with sd s = √v. Then
    ρ = Σ_{k≠l, same day} y_k y_l / Σ_{k≠l} s_k s_l, and for h = 1 … H (H = ⌈√D⌉ over the
    D calendar days) c_h = Σ_d Y_d Y_{d+h} / Σ_d S_d S_{d+h}, with Y_d and S_d a day's
    sums of y and s; all clipped to [−1, 1] (ρ to [0, 1]). Estimate on positions the test
    doesn't score, over a long discovery, and broadly (every configuration's)."""
    clusters = _clusters(buy_yes, count, p_yes, lo, hi, cluster, day, break_even)
    surprise = (np.asarray(won, dtype=float) - np.where(buy_yes, p_yes, 1 - p_yes)) * count
    x = pd.Series(surprise).groupby(np.asarray(cluster)).sum().reindex(clusters.index)
    total = clusters["var_b"].sum()
    if total <= 0 or clusters["day"].nunique() < 2:
        return Dependence(0.0, ())
    y = clusters["direction"] * (x - clusters["var_b"] * x.sum() / total)
    s = np.sqrt(clusters["var_b"])
    by_day = clusters["day"]
    big_y, big_s = y.groupby(by_day).sum(), s.groupby(by_day).sum()
    pairs = (big_s**2 - (s**2).groupby(by_day).sum()).sum()
    same = (big_y**2 - (y**2).groupby(by_day).sum()).sum()
    rho = float(np.clip(same / pairs, 0, 1)) if pairs > 0 else 0.0
    ys, ss = _calendar(big_y), _calendar(big_s)
    lags = min(int(np.ceil(np.sqrt(len(ys)))), len(ys) - 1)
    cross = tuple(float(np.clip(ys[:-h] @ ys[h:] / den, -1, 1)) if (den := ss[:-h] @ ss[h:])
                  > 0 else 0.0 for h in range(1, lags + 1))
    return Dependence(rho, cross)


def widening(dep: Dependence, buy_yes: np.ndarray, count: np.ndarray, p_yes: np.ndarray,
             lo: np.ndarray, hi: np.ndarray, cluster: np.ndarray, day: np.ndarray,
             break_even: np.ndarray) -> float:
    """The factor (≥ 1) by which the null's variance must grow for these positions:
    (ΣV + ρ·ΣW + 2 Σ_h (1 − h/(H+1)) c_h Σ_d A_d A_{d+h}) / ΣV0 over this sample's own
    days (`_days`), never below the break-even variance ΣV, even where YES and NO
    positions offset."""
    days = _days(_clusters(buy_yes, count, p_yes, lo, hi, cluster, day, break_even))
    null_var, boundary = days["V0"].sum(), days["V"].sum()
    if null_var <= 0:
        return 1.0
    a, lags = _calendar(days["A"]), len(dep.cross)
    cross = sum((1 - h / (lags + 1)) * c * float(a[:-h] @ a[h:])
                for h, c in enumerate(dep.cross, start=1) if h < len(a))
    total = boundary + dep.rho * days["W"].sum() + 2 * cross
    return float(max(1.0, max(boundary, total) / null_var))
