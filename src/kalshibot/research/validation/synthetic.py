"""Synthetic Kalshi worlds with a known truth, for measuring the research pipeline itself.

Each world is a set of events over a span of days. An event is a standalone binary, a
ladder of mutually exclusive buckets, or (a `strike_share` of the ladders) a strike
ladder of nested contracts, and every contract has a true probability q of
YES. Outcomes come from one uniform draw U per event, exactly as in the null model
(`null.py`): a standalone or strike contract settles YES when U < q, a bucket when U
falls in its interval. With `shock` = ρ > 0, U = Φ(√ρ·Z_day + √(1−ρ)·ε_event), so
events on the same day move together (a heat wave, a crypto selloff) while every
marginal stays q; with `persistence` = φ, Z_day is AR(1) (Z_d = φ·Z_{d−1} + √(1−φ²)·η_d),
so the shocks last for days.

Quotes are set for a family that buys `side` (YES or NO) as a taker:

- "market": the mid is the truth and the asks are a half spread either side, so buying
  loses the spread and the fee.
- "boundary": each `side` ask is the highest price on the $0.0001 grid at which buying it
  still breaks even after the 2026 taker fee (E[net] in [0, $0.0002) per contract; fee
  rounding moves in $0.0001 steps). This is the least favorable null: every
  configuration has zero edge, so only luck separates them.

An `Edge` lowers the `side` ask by `cents` for true probabilities q in [lo, hi); the net
edge is a little less, because the fee rises as the price falls (conformance reports the
measured edge). The other side's ask is 1 − (this ask − spread).

Resting bids (`sweeps` > 0, boundary binaries only): each contract's truth q takes
Poisson(`sweeps`) steps over its day, the k-th in minute k + 1. A step brings news first,
if `news` = β > 0: up with probability (1 + β(2q − 1))/2, taking q to
q(1 + β)/(1 + β(2q − 1)), else down to q(1 − β)/(1 − β(2q − 1)) (Bayes' rule). Then, if
s ≤ q ≤ 1 − s (s = `sweep_scale`), a sweep sells YES or NO, by a fair coin, from its
price p down to x = max(p − s·V^(−NOISE), 0), V uniform: P(p − x > t) = (s/t)² for
s ≤ t < p, and an atom (s/p)² at x = 0, the book emptied. A NOISE share of the sweeps
that leave a book are noise, and the price returns to p; after the rest it stays at x.
So a bid b ≤ p − s that a sweep trades through is worth exactly b afterwards on
average, and E[price after] = p − s: q is a martingale. The contract settles YES if
U < q at its day's end (U its event's draw above), so P(YES | its path so far) = q. Each
sweep through `side` is a row, with q after the step's news and before the sweep, and
the mid, q rounded to a unit. An `Edge` makes a fill on q in [lo, hi) `cents` cheaper
(`extra`), on the same paths.

Snapshots (`snapshots` = N > 1): each contract is quoted N times over its day, the
k-th (k = 0, …, N − 1) after k signals of its event's draw, U + σ·ε each (σ = `signal`).
Its truth then is the probability of its outcome given those signals, U uniform a
priori: the mass, over the range of U on which it settles YES, of the N(the signals'
mean, σ²/k) density truncated to [0, 1] (`_posterior`; at k = 0 the prior, q). So every
truth is a martingale, an exclusive event's sum to 1 at each snapshot, and every contract
still settles on U. Each snapshot is quoted on its truths as above.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.special import ndtr

from kalshibot.kalshi.fee_history import FeeTerms
from kalshibot.kalshi.fees import DIRECT_MEMBER, fee
from kalshibot.kalshi.models import PRICE_SCALE, SIZE_SCALE
from kalshibot.research.validation.null import bucket_intervals

TAKER = FeeTerms.from_series("quadratic", "1", "balance")
NOISE = 0.5  # the share of sweeps leaving a book that revert, and 1/tail of their depths


@dataclass(frozen=True)
class Edge:
    """The bought side's ask is `cents` too low for true P(YES) in [lo, hi), or, in a sweep
    world, a fill on q in [lo, hi) costs `cents` less."""

    lo: float
    hi: float
    cents: float

    def units(self, q: np.ndarray) -> np.ndarray:
        return np.where((q >= self.lo) & (q < self.hi), round(self.cents * 100), 0)


NO_EDGE = Edge(0.0, 0.0, 0.0)


def taker_cost(ask: np.ndarray) -> np.ndarray:
    """Micro-dollars paid, fee included, to buy one contract at `ask` as a taker."""
    ask = np.asarray(ask)
    return ask * SIZE_SCALE + fee(ask, np.full(len(ask), SIZE_SCALE), buy=True, terms=TAKER,
                                  precision=DIRECT_MEMBER)


def taker_net(p_win: np.ndarray, ask: np.ndarray) -> np.ndarray:
    """Expected net PnL in micro-dollars of buying one contract that wins with `p_win`."""
    return p_win * PRICE_SCALE * SIZE_SCALE - taker_cost(ask)


def _break_even_ask(p_win: np.ndarray) -> np.ndarray:
    a, rate = p_win, float(TAKER.taker)
    for _ in range(6):  # a = p − rate·a(1 − a): fixed point of the fee curve
        a = p_win - rate * a * (1 - a)
    near = np.floor(a * PRICE_SCALE).astype(np.int64)
    best = np.ones_like(near)
    for step in range(-3, 4):  # fee rounding moves the answer by a unit or two
        ask = np.clip(near + step, 1, PRICE_SCALE - 1)
        best = np.where((taker_net(p_win, ask) >= 0) & (ask > best), ask, best)
    return best


def _news(q: np.ndarray, beta: float, u: np.ndarray) -> np.ndarray:
    """q after news of strength `beta`, from a uniform u: up with probability
    (1 + β(2q − 1))/2, each way by Bayes' rule."""
    tilt = beta * (2 * q - 1)
    return q * np.where(u < (1 + tilt) / 2, (1 + beta) / (1 + tilt), (1 - beta) / (1 - tilt))


def _sweep(p: np.ndarray, scale: float, v: np.ndarray, w: np.ndarray
           ) -> tuple[np.ndarray, np.ndarray]:
    """(bottom, price after) of sweeps selling a contract priced `p`, from uniforms v
    (depth) and w (noise if below NOISE, unless the book emptied)."""
    bottom = np.maximum(p - scale * v**-NOISE, 0.0)
    return bottom, np.where((w < NOISE) & (bottom > 0), p, bottom)


def _paths(df: pd.DataFrame, u: np.ndarray, sweeps: float, scale: float, news: float,
           side: str, edge: Edge, rng: np.random.Generator) -> pd.DataFrame:
    """The sweeps through `side` of binaries `df` (row r is event r) whose draws are `u`."""
    q = df["q"].to_numpy().copy()
    count = rng.poisson(sweeps, len(q))
    parts = []
    for k in range(count.max(initial=0)):
        i = np.flatnonzero(k < count)
        if news:
            q[i] = _news(q[i], news, rng.random(len(i)))
        i = i[(q[i] >= scale) & (q[i] <= 1 - scale)]
        sells_yes = rng.random(len(i)) < 0.5
        p = np.where(sells_yes, q[i], 1 - q[i])
        bottom, after = _sweep(p, scale, rng.random(len(i)), rng.random(len(i)))
        mine = sells_yes == (side == "yes")
        parts.append(pd.DataFrame({"row": i[mine], "minute": k + 1, "q": q[i[mine]],
                                   "bottom": bottom[mine] * PRICE_SCALE}))
        q[i] = np.where(sells_yes, after, 1 - after)
    out = pd.concat(parts, ignore_index=True)
    row = out.pop("row").to_numpy()
    contract = df.iloc[row][["event", "ticker", "mutually_exclusive", "position", "ladder",
                             "below", "day"]].reset_index(drop=True)
    return pd.concat([contract, out], axis=1).assign(
        mid=np.round(out["q"] * PRICE_SCALE), extra=edge.units(out["q"].to_numpy()),
        yes=u[row] < q[row])  # q is the truth at the day's end now


def _posterior(lo: np.ndarray, hi: np.ndarray, mean: np.ndarray, sd: np.ndarray
               ) -> np.ndarray:
    """P(lo ≤ U < hi | signals) for U uniform a priori: the N(mean, sd²) density truncated
    to [0, 1], each mass taken from the tail that keeps its precision."""
    def mass(a, b):
        a, b = (a - mean) / sd, (b - mean) / sd
        return np.where(a > 0, ndtr(-a) - ndtr(-b), ndtr(b) - ndtr(a))

    return mass(lo, hi) / mass(0.0, 1.0)


def _snapshots(df: pd.DataFrame, u: np.ndarray, snapshots: int, signal: float,
               rng: np.random.Generator) -> pd.DataFrame:
    """Each contract of `df` (event r's draw `u[r]`) at every snapshot, its truth then."""
    k = np.arange(1, snapshots)
    signals = u[:, None] + signal * rng.standard_normal((len(u), snapshots - 1))
    mean = (np.cumsum(signals, axis=1) / k)[df["event_id"].to_numpy()]
    lo, hi = df["lo"].to_numpy()[:, None], df["hi"].to_numpy()[:, None]
    q = np.column_stack([hi - lo, _posterior(lo, hi, mean, signal / np.sqrt(k))])
    out = df.loc[df.index.repeat(snapshots)].reset_index(drop=True)
    out["snapshot"], out["q"] = np.tile(np.arange(snapshots), len(df)), q.ravel()
    return out


def world(days: int, events_per_day: int, rng: np.random.Generator, quotes: str = "market",
          edge: Edge = NO_EDGE, bucket_share: float = 0.5, half_spread_cents: float = 1.0,
          shock: float = 0.0, persistence: float = 0.0, strike_share: float = 0.0,
          side: str = "no", first_day: int = 0, sweeps: float = 0.0, sweep_scale: float = 0.05,
          news: float = 0.0, snapshots: int = 1, signal: float = 1.0) -> pd.DataFrame:
    """One row per contract, in `null.inputs`' terms (event, ticker, mutually_exclusive,
    position, ladder, below, mid), plus day, q, yes, ask_yes / ask_no (price units). Days
    are numbered from `first_day`. With `sweeps`, one row per sweep through `side` instead
    (`_paths`): the contract's columns, minute, q and mid before the sweep, its bottom in
    `side`'s price units, the fill's discount `extra` (price units) and yes. With
    `snapshots` > 1, one row per contract and snapshot (`snapshot`), quoted on its truth
    then."""
    if sweeps and (bucket_share or quotes != "boundary" or snapshots > 1):
        raise ValueError("a sweep world is boundary binaries (bucket_share=0, quotes='boundary'), "
                         "without snapshots")
    n = days * events_per_day
    size = np.where(rng.random(n) < bucket_share, rng.integers(3, 8, n), 1)
    z = rng.standard_normal(days)
    for d in range(1, days):  # AR(1) across days; φ = 0 leaves them independent
        z[d] = persistence * z[d - 1] + np.sqrt(1 - persistence**2) * z[d]
    z = z[np.arange(n) // events_per_day]
    u = ndtr(np.sqrt(shock) * z + np.sqrt(1 - shock) * rng.standard_normal(n))
    parts = []
    for k in np.unique(size):
        ids = np.flatnonzero(size == k)
        if k == 1:
            q = (0.03 + 0.94 * rng.beta(0.6, 0.6, len(ids)))[:, None]
            lo, hi = np.zeros_like(q), q
        else:  # every bucket >= 2.8%
            q = 0.8 * rng.dirichlet(np.full(k, 0.8), len(ids)) + 0.2 / k
            lo, hi = map(np.array, zip(*(bucket_intervals(row) for row in q), strict=True))
        exclusive = np.full(len(ids), k > 1)
        if k > 1 and strike_share > 0:  # strike ladders: nested contracts
            strike = rng.random(len(ids)) < strike_share
            q[strike] = -np.sort(-(0.03 + 0.94 * rng.random((strike.sum(), k))), axis=1)
            lo[strike], hi[strike] = 0.0, q[strike]
            exclusive &= ~strike
        ladder = ~exclusive & (k > 1)
        parts.append(pd.DataFrame({
            "event_id": np.repeat(ids, k), "bucket": np.tile(np.arange(k), len(ids)),
            "mutually_exclusive": np.repeat(exclusive, k),
            "ladder": np.where(np.repeat(ladder, k), "", None),
            "q": q.ravel(), "lo": lo.ravel(), "hi": hi.ravel(),
            "yes": ((lo <= u[ids, None]) & (u[ids, None] < hi)).ravel()}))
    df = pd.concat(parts).sort_values(["event_id", "bucket"], ignore_index=True)
    df["day"] = first_day + df["event_id"] // events_per_day
    df["event"] = f"E{first_day}-" + df["event_id"].astype(str)
    df["ticker"] = df["event"] + "-" + df["bucket"].astype(str)
    df["position"], df["below"] = df["bucket"], True  # ladders settle YES when U < q: [0, q)
    if sweeps:
        return _paths(df, u, sweeps, sweep_scale, news, side, edge, rng)
    if snapshots > 1:
        df = _snapshots(df, u, snapshots, signal, rng)
    q = df["q"].to_numpy()
    p_win = q if side == "yes" else 1 - q
    half = round(half_spread_cents * 100)
    if quotes == "market":
        ask = np.round(p_win * PRICE_SCALE).astype(np.int64) + half
    elif quotes == "boundary":
        ask = _break_even_ask(p_win)
    else:
        raise ValueError(f"unknown quotes {quotes!r}")
    ask = np.clip(ask - edge.units(q), 1, PRICE_SCALE - 1)
    other = np.clip(PRICE_SCALE - ask + 2 * half, 1, PRICE_SCALE - 1)
    df["ask_yes"], df["ask_no"] = (ask, other) if side == "yes" else (other, ask)
    df["mid"] = (df["ask_yes"] + PRICE_SCALE - df["ask_no"]) / 2
    return df[["event", "ticker", "mutually_exclusive", "position", "ladder", "below", "mid",
               "day", "q", "yes", "ask_yes", "ask_no"] + (["snapshot"] if snapshots > 1 else [])]
