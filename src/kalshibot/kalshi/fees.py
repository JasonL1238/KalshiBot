"""Kalshi trading fees: the single home for the fee model.

General Trading Fees Table (https://help.kalshi.com/en/articles/13823805-fees):

    model fee ($) = rate × fee_multiplier × contracts × P × (1 − P)

- Takers pay rate 0.07 on every series.
- Makers pay 0.25 × that (0.0175) on `quadratic_with_maker_fees` series and 0.5 × that
  (0.035) on `quadratic_with_combo_maker_fees` series. Makers pay nothing on plain
  `quadratic` series.
- `flat` series use a separate table that isn't modeled here, so they are rejected.

Rounding, per fill (https://docs.kalshi.com/getting_started/fee_rounding):
1. trade fee = model fee rounded up to $0.000001.
2. The balance change (revenue − trade fee) is floored to the member's precision:
   $0.0001 for direct members, $0.01 for FCM-cleared members. The difference is a
   "rounding fee". It depends on the trade's principal, not just its fee.
3. Rounding fees accumulate per order and are rebated in whole precision increments,
   capped so that no fill's net fee goes negative.

`OrderFees` runs these steps across an order's fills. `fee()` is the vectorized
single-fill case, where the accumulator never rebates.

Regime: this is the 2026 rounding method (effective date between Feb and Jul 2026, to be
pinned in M1). Earlier fills rounded each order's fee up to the cent, and earlier fee
rates and maker-fee lists differ by date. The dated history lands here in M1; until then
don't apply `fee()` to pre-2026 fills. Read `fee_type` and
`fee_multiplier` from `GET /series/{ticker}`, and confirm member precision from
`fee_cost` on a real fill. All three are required, so a forgotten lookup can't silently
mean "no fee".

Units are fixed-point integers parsed from Kalshi's strings, never through float:
price in $0.0001 (0..10_000), count in 0.01 contracts. Fees are returned in micro-dollars
($0.000001), the finest unit Kalshi uses.
"""

from __future__ import annotations

from fractions import Fraction

import numpy as np

PRICE_SCALE = 10_000  # price units per dollar (moves to models.py when the book goes fixed-point)
MAKER_SHARE = {
    "quadratic": Fraction(0),
    "quadratic_with_maker_fees": Fraction(1, 4),
    "quadratic_with_combo_maker_fees": Fraction(1, 2),
}
DIRECT_MEMBER = 100  # precision in micro-dollars: $0.0001
FCM_MEMBER = 10_000  # $0.01


def _check_precision(precision: int) -> None:
    if not isinstance(precision, int) or precision <= 0:
        raise ValueError(f"precision must be a positive int of micro-dollars, got {precision!r}")


def _rate(fee_type: str, multiplier: float | str, maker: bool) -> Fraction:
    if fee_type not in MAKER_SHARE:
        raise ValueError(f"unsupported fee_type {fee_type!r}")
    mult = Fraction(str(multiplier))
    if mult <= 0:
        raise ValueError(f"fee_multiplier must be positive, got {multiplier!r}")
    return Fraction(7, 100) * (MAKER_SHARE[fee_type] if maker else 1) * mult


def fee(
    price: int | np.ndarray,
    count: int | np.ndarray,
    *,
    buy: bool | np.ndarray,
    fee_type: str,
    multiplier: float | str,
    precision: int,
    maker: bool = False,
) -> int | np.ndarray:
    """Net fee in micro-dollars for single-fill orders (vectorized over price/count/buy)."""
    _check_precision(precision)
    rate = _rate(fee_type, multiplier, maker)
    p, c = np.asarray(price), np.asarray(count)
    if p.dtype.kind not in "iu" or c.dtype.kind not in "iu":
        raise TypeError("price and count must be integer fixed-point units, parsed from strings")
    if np.any((p < 0) | (p > PRICE_SCALE)) or np.any(c < 0):
        raise ValueError("price must be in [0, 10000] units and count >= 0")
    # micro-dollars: model fee = rate × (c/100) × (p/1e4) × ((1e4 − p)/1e4) × 1e6
    #                          = rate × c × p × (1e4 − p) / 1e4;  revenue = ∓ p × c exactly.
    wide = rate.numerator * int(c.max(initial=0)) * PRICE_SCALE**2 // 4 >= 2**62
    c, p = c.astype(object if wide else np.int64), p.astype(object if wide else np.int64)
    trade = -(-rate.numerator * c * p * (PRICE_SCALE - p) // (rate.denominator * PRICE_SCALE))
    revenue = np.where(buy, -p * c, p * c)
    net = revenue - (revenue - trade) // precision * precision  # trade fee + rounding fee
    if np.ndim(net) == 0:
        return int(net)
    return net if wide else net.astype(np.int64)


class OrderFees:
    """Per-order fee state: Kalshi's rounding accumulator across fills."""

    def __init__(self, *, fee_type: str, multiplier: float | str, precision: int):
        _check_precision(precision)
        self._series = {"fee_type": fee_type, "multiplier": multiplier}
        self._precision = precision
        self.accumulated = 0  # rounding fees not yet rebated, micro-dollars

    def fill(self, price: int, count: int, *, buy: bool, maker: bool) -> int:
        """Record one fill; return its net fee in micro-dollars (after any rebate)."""
        gross = fee(price, count, buy=buy, maker=maker, precision=self._precision, **self._series)
        trade = fee(price, count, buy=buy, maker=maker, precision=1, **self._series)
        self.accumulated += gross - trade  # this fill's rounding fee
        p = self._precision  # rebates come in whole increments, never exceeding this fill's fee
        rebate = min(self.accumulated // p, gross // p) * p
        self.accumulated -= rebate
        return gross - rebate
