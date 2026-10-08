"""Kalshi trading fees: the fee on a fill, given the terms in force (`fee_history.py`).

    model fee ($) = rate × contracts × P × (1 − P)   (+ a flat $/contract for makers in 2025)

The rate is 0.07 × the series' fee_multiplier for takers, and a share of that for makers
(https://help.kalshi.com/en/articles/13823805-fees). Rounding changed in 2026:

- "cent" (until 2026): each fill's model fee is rounded up to the next cent.
- "balance" (https://docs.kalshi.com/getting_started/fee_rounding): the trade fee is
  the model fee rounded up to $0.000001. The balance change (revenue − trade fee) is then
  floored to the member's precision, $0.0001 for direct members or $0.01 for FCM-cleared
  members, so the "rounding fee" also absorbs principal that is off that grid. Rounding
  fees accumulate per order and are rebated in whole precision increments, never pushing
  a fill's net fee below zero (`OrderFees`).

Prices and counts are the fixed-point integers of `kalshi.models`. Fees are returned in
micro-dollars ($0.000001), the finest unit Kalshi uses. Confirm member precision from
`fee_cost` on a real fill.
"""

from __future__ import annotations

from fractions import Fraction
from math import lcm

import numpy as np

from kalshibot.kalshi.fee_history import FeeTerms
from kalshibot.kalshi.models import PRICE_SCALE

DIRECT_MEMBER = 100  # precision in micro-dollars: $0.0001
FCM_MEMBER = 10_000  # $0.01
CENT = 10_000


def _check_precision(precision: int) -> None:
    if not isinstance(precision, int) or precision <= 0:
        raise ValueError(f"precision must be a positive int of micro-dollars, got {precision!r}")


def fee(
    price: int | np.ndarray,
    count: int | np.ndarray,
    *,
    buy: bool | np.ndarray,
    terms: FeeTerms,
    precision: int,
    maker: bool = False,
) -> int | np.ndarray:
    """Net fee in micro-dollars for single-fill orders (vectorized over price/count/buy)."""
    _check_precision(precision)
    p, c = np.asarray(price), np.asarray(count)
    if p.dtype.kind not in "iu" or c.dtype.kind not in "iu":
        raise TypeError("price and count must be integer fixed-point units, parsed from strings")
    if np.any((p < 0) | (p > PRICE_SCALE)) or np.any(c < 0):
        raise ValueError("price must be in [0, 10000] units and count >= 0")
    # micro-dollars: rate × (c/100) × (p/1e4) × ((1e4 − p)/1e4) × 1e6 = rate/1e4 × c × p(1e4 − p)
    #                flat × (c/100) × 1e6 = flat × 1e4 × c
    curve = Fraction(terms.maker if maker else terms.taker) / PRICE_SCALE
    flat = Fraction(terms.maker_flat if maker else 0) * PRICE_SCALE
    den = lcm(curve.denominator, flat.denominator)
    a, b = curve.numerator * (den // curve.denominator), flat.numerator * (den // flat.denominator)
    wide = (a * PRICE_SCALE**2 // 4 + b) * int(c.max(initial=0)) >= 2**62
    c, p = c.astype(object if wide else np.int64), p.astype(object if wide else np.int64)
    num = c * (a * p * (PRICE_SCALE - p) + b)  # model fee × den, in micro-dollars
    if terms.rounding == "cent":
        net = -(-num // (den * CENT)) * CENT
    elif terms.rounding == "balance":
        trade = -(-num // den)
        revenue = np.where(buy, -p * c, p * c)
        net = revenue - (revenue - trade) // precision * precision  # trade fee + rounding fee
    else:
        raise ValueError(f"unknown rounding {terms.rounding!r}")
    if np.ndim(net) == 0:
        return int(net)
    return net if wide else net.astype(np.int64)


class OrderFees:
    """Per-order fee state: Kalshi's 2026 rounding accumulator across an order's fills.
    Under "cent" rounding each fill is independent and nothing accumulates."""

    def __init__(self, *, terms: FeeTerms, precision: int):
        _check_precision(precision)
        self.terms, self._precision = terms, precision
        self.accumulated = 0  # rounding fees not yet rebated, micro-dollars

    def fill(self, price: int, count: int, *, buy: bool, maker: bool) -> int:
        """Record one fill; return its net fee in micro-dollars (after any rebate)."""
        gross = fee(price, count, buy=buy, maker=maker, terms=self.terms,
                    precision=self._precision)
        if self.terms.rounding != "balance":
            return gross
        trade = fee(price, count, buy=buy, maker=maker, terms=self.terms, precision=1)
        self.accumulated += gross - trade  # this fill's rounding fee
        p = self._precision  # rebates come in whole increments, never exceeding this fill's fee
        rebate = min(self.accumulated // p, gross // p) * p
        self.accumulated -= rebate
        return gross - rebate
