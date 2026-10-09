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


@dataclass(frozen=True)
class Edge:
    """The bought side's ask is `cents` too low for true P(YES) in [lo, hi)."""

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


def world(days: int, events_per_day: int, rng: np.random.Generator, quotes: str = "market",
          edge: Edge = NO_EDGE, bucket_share: float = 0.5, half_spread_cents: float = 1.0,
          shock: float = 0.0, persistence: float = 0.0, strike_share: float = 0.0,
          side: str = "no", first_day: int = 0) -> pd.DataFrame:
    """One row per contract, in `null.inputs`' terms (event, ticker, mutually_exclusive,
    position, ladder, below), plus day, q, yes, ask_yes / ask_no (price units). Days are
    numbered from `first_day`."""
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
            "q": q.ravel(), "yes": ((lo <= u[ids, None]) & (u[ids, None] < hi)).ravel()}))
    df = pd.concat(parts).sort_values(["event_id", "bucket"], ignore_index=True)
    df["day"] = first_day + df["event_id"] // events_per_day
    df["event"] = f"E{first_day}-" + df["event_id"].astype(str)
    df["ticker"] = df["event"] + "-" + df["bucket"].astype(str)
    df["position"], df["below"] = df["bucket"], True  # ladders settle YES when U < q: [0, q)
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
    return df[["event", "ticker", "mutually_exclusive", "position", "ladder", "below", "day",
               "q", "yes", "ask_yes", "ask_no"]]
