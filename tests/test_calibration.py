from datetime import UTC, datetime

from kalshibot.research.calibration import taker_calibration


def D(*a):
    return datetime(*a, tzinfo=UTC)


def test_bins_the_price_each_taker_paid(loader):
    out = taker_calibration(loader, D(2025, 9, 29), D(2025, 10, 11), D(2026, 1, 1))
    rows = out.set_index("bin_cents")[["trades", "contracts", "avg_price", "win_rate"]]
    assert rows.to_dict("index") == {
        5: {"trades": 1, "contracts": 2, "avg_price": 0.065, "win_rate": 1.0},  # a1: YES 6.5¢
        10: {"trades": 1, "contracts": 1, "avg_price": 0.1, "win_rate": 0.0},  # a0: NO 10¢, B=yes
        55: {"trades": 1, "contracts": 3, "avg_price": 0.55, "win_rate": 1.0}}  # a2: YES 55¢


def test_outcomes_unknown_at_as_of_leave_nothing_to_calibrate(loader):
    assert taker_calibration(loader, D(2025, 9, 29), D(2025, 10, 11), D(2025, 10, 31)).empty
