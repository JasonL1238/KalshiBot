from fractions import Fraction
from math import ceil, floor

import numpy as np
import pytest

from kalshibot.kalshi.fees import DIRECT_MEMBER, FCM_MEMBER, OrderFees, fee

TAKER = {"fee_type": "quadratic", "multiplier": 1}
MAKER_SERIES = {"fee_type": "quadratic_with_maker_fees", "multiplier": 1}


def reference(price, count, *, buy, precision, rate=Fraction(7, 100), mult=1):
    """Kalshi's fee_rounding algorithm written out independently, in exact fractions."""
    p, c = Fraction(price, 10_000), Fraction(count, 100)
    trade = ceil(Fraction(rate) * Fraction(mult) * c * p * (1 - p) * 10**6)  # micro-dollars
    revenue = (-1 if buy else 1) * price * count  # micro-dollars
    return revenue - floor(Fraction(revenue - trade, precision)) * precision


def test_kalshi_worked_example_1():
    # fee_rounding doc: buy 1 contract at $0.055, model fee $0.00363825, $0.01 precision.
    # Balance changes by -$0.06, so trade fee + rounding fee = $0.005.
    assert fee(550, 100, buy=True, **TAKER, precision=FCM_MEMBER) == 5_000


def test_kalshi_worked_example_2_accumulator():
    # Doc: three fills each add a $0.004 rounding fee to the order's accumulator; the third
    # brings it to $0.012, so $0.01 is rebated and $0.002 carries forward.
    def rounding(p, c):
        return gross(p, c) - fee(p, c, buy=True, **TAKER, precision=1)

    def gross(p, c):
        return fee(p, c, buy=True, **TAKER, precision=FCM_MEMBER)

    p, c = next((p, c) for c in range(100, 2_000, 100) for p in range(1, 10_000)
                if rounding(p, c) == 4_000 and gross(p, c) >= 10_000)
    order = OrderFees(**TAKER, precision=FCM_MEMBER)
    nets, carried = [], []
    for _ in range(3):
        nets.append(order.fill(p, c, buy=True, maker=False))
        carried.append(order.accumulated)
    assert carried == [4_000, 8_000, 2_000]
    assert nets == [gross(p, c), gross(p, c), gross(p, c) - 10_000]


def test_rebate_cap_keeps_balance_on_the_precision_grid():
    order = OrderFees(**TAKER, precision=FCM_MEMBER)
    nets = [order.fill(1, 100, buy=True, maker=False), order.fill(550, 100, buy=True, maker=False)]
    assert nets == [9_900, 5_000] and order.accumulated == 11_254


def test_random_fill_sequences_stay_on_grid_and_never_go_negative():
    rng = np.random.default_rng(1)
    for precision in (DIRECT_MEMBER, FCM_MEMBER):
        for _ in range(300):
            order = OrderFees(fee_type="quadratic_with_maker_fees", multiplier=1,
                              precision=precision)
            for _ in range(rng.integers(1, 8)):
                p, c = int(rng.integers(1, 10_000)), int(rng.integers(1, 5_000))
                buy, maker = bool(rng.integers(2)), bool(rng.integers(2))
                net = order.fill(p, c, buy=buy, maker=maker)
                revenue = -p * c if buy else p * c
                assert net >= 0 and (revenue - net) % precision == 0
                assert 0 <= order.accumulated


def test_table_values_and_member_precision():
    # 100 contracts at 50¢ → $1.75; 1 contract at 50¢ → $0.0175, which FCM rounding makes 2¢.
    assert fee(5000, 10_000, buy=True, **TAKER, precision=FCM_MEMBER) == 1_750_000
    assert fee(5000, 100, buy=True, **TAKER, precision=FCM_MEMBER) == 20_000
    assert fee(5000, 100, buy=True, **TAKER, precision=DIRECT_MEMBER) == 17_500
    assert fee(5000, 100, buy=False, **TAKER, precision=FCM_MEMBER) == 20_000


@pytest.mark.parametrize("price, count, precision, expected", [
    (5500, 170, FCM_MEMBER, 35_000),  # fractional size: $0.035, not ceil(model fee) = $0.03
    (95, 100, FCM_MEMBER, 10_500),  # sub-penny price: principal misalignment is charged
    (5555, 1, DIRECT_MEMBER, 245),  # 0.01 contract, direct member
])
def test_rounding_includes_principal_misalignment(price, count, precision, expected):
    assert fee(price, count, buy=True, **TAKER, precision=precision) == expected


def test_maker_fees_depend_on_series_fee_type():
    assert fee(5000, 10_000, buy=True, **TAKER, maker=True, precision=FCM_MEMBER) == 0
    assert fee(5000, 10_000, buy=True, **MAKER_SERIES, maker=True,
               precision=FCM_MEMBER) == 440_000  # $0.4375 → $0.44
    assert fee(5000, 10_000, buy=True, fee_type="quadratic_with_combo_maker_fees",
               multiplier=1, maker=True, precision=FCM_MEMBER) == 880_000
    # Exact $20.56000000000275 must round up; floats would give $20.56.
    assert fee(1369, 994_307, buy=False, **MAKER_SERIES, maker=True,
               precision=FCM_MEMBER) == reference(1369, 994_307, buy=False,
                                                  precision=FCM_MEMBER, rate=Fraction(7, 400))


def test_vectorized_matches_reference():
    rng = np.random.default_rng(0)
    prices, counts = rng.integers(0, 10_001, 2_000), rng.integers(0, 10**7, 2_000)
    buys = rng.integers(0, 2, 2_000).astype(bool)
    for precision in (DIRECT_MEMBER, FCM_MEMBER):
        out = fee(prices, counts, buy=buys, fee_type="quadratic", multiplier="1.5",
                  precision=precision)
        for p, c, b, f in zip(prices, counts, buys, out, strict=True):
            assert f == reference(int(p), int(c), buy=bool(b), precision=precision, mult="1.5")


def test_huge_counts_stay_exact():
    count = 10**14
    assert fee(5000, count, buy=True, **TAKER, precision=FCM_MEMBER) == reference(
        5000, count, buy=True, precision=FCM_MEMBER)


@pytest.mark.parametrize("price, count", [(5600.5, 100), (10_001, 100), (-1, 100), (5000, -100)])
def test_bad_inputs_rejected(price, count):
    with pytest.raises((ValueError, TypeError)):
        fee(price, count, buy=True, **TAKER, precision=FCM_MEMBER)


def test_bad_series_parameters_rejected():
    with pytest.raises(ValueError):
        fee(5000, 100, buy=True, fee_type="flat", multiplier=1, precision=FCM_MEMBER)
    with pytest.raises(ValueError):
        fee(5000, 100, buy=True, fee_type="quadratic", multiplier=0, precision=FCM_MEMBER)
    with pytest.raises(TypeError):
        fee(5000, 100, buy=True)  # fee_type, multiplier, and precision are required
    for bad in (0, -100, 0.01):
        with pytest.raises(ValueError):
            fee(550, 100, buy=True, **TAKER, precision=bad)
        with pytest.raises(ValueError):
            OrderFees(**TAKER, precision=bad)
