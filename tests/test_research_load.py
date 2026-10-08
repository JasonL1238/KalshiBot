from datetime import UTC, datetime

import pandas as pd
import pytest

from kalshibot.data import backfill as bf
from kalshibot.research import calendar, ledger, load
from kalshibot.research.load import Loader, LockboxError, MissingData

T = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
D = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731


def test_every_market_column_is_classified():
    columns = load.AS_LISTED + load.POST_HOC + load.META
    assert sorted(columns) == sorted(bf.MARKET_SCHEMA.names)
    assert len(set(columns)) == len(columns)


def test_one_tape_in_fixed_point_units(loader):
    df = loader.trades(D(2025, 9, 29), D(2025, 10, 11)).df().set_index("trade_id")
    assert sorted(df.index) == ["a0", "a1", "a2"]
    assert (df.loc["a2", "yes_price"], df.loc["a2", "count"]) == (5500, 300)
    assert loader.trades(D(2025, 10, 1), D(2025, 10, 1)).df().empty  # an empty range


def test_missing_api_days_raise(loader):
    with pytest.raises(MissingData):
        loader.trades(D(2025, 10, 1), D(2025, 10, 13))


def test_lockbox_is_closed_but_the_forward_window_is_not(loader):
    with pytest.raises(LockboxError):
        loader.trades(D(2026, 4, 1), D(2026, 5, 2))
    with pytest.raises(MissingData):  # past the lockbox gate; the forward days aren't on disk
        loader.trades(D(2026, 10, 1), D(2026, 10, 3))
    forward = loader.markets(D(2026, 10, 5)).df().set_index("ticker")
    assert pd.isna(forward.loc["D", "result"])  # a lockbox-era outcome stays hidden
    assert forward.loc["A", "result"] == "yes"
    with pytest.raises(ValueError):
        loader.markets(datetime(2026, 1, 1))  # naive times are ambiguous


def test_only_survivors_see_the_lockbox(loader, tmp_path):
    ledger.unlock_lockbox(["H1"], "market-calibrated null p-value")
    survivor = Loader(tmp_path, family="H1").markets(D(2026, 10, 5)).df().set_index("ticker")
    assert survivor.loc["D", "result"] == "no"
    other = Loader(tmp_path, family="H2").markets(D(2026, 10, 5)).df().set_index("ticker")
    assert pd.isna(other.loc["D", "result"])
    with pytest.raises(LockboxError):
        Loader(tmp_path, family="H2").trades(D(2026, 4, 1), D(2026, 5, 2))


def test_markets_are_point_in_time(loader):
    before = loader.markets(D(2025, 10, 15)).df().set_index("ticker")
    assert "C" not in before.index  # listed later
    assert "C" not in loader.markets(D(2025, 11, 15)).df().ticker.tolist()  # half-open
    assert before.result.isna().all() and before.volume.isna().all()
    assert before.loc["A", "scheduled_close"] == T("2025-12-01")  # can't close early: listed
    assert before.loc["A", "series_ticker"] == "SA" and before.loc["B", "category"] == "Crypto"
    assert before.loc["A", "rules_primary"] == "If ..."
    after = loader.markets(D(2025, 12, 2)).df().set_index("ticker")
    assert after.loc["A", "result"] == "yes" and after.loc["B", "volume"] == 900
    assert pd.isna(after.loc["B", "scheduled_close"])  # an early close is never "listed"


def test_fee_schedule_from_tables(loader):
    schedule = loader.fee_schedule()
    assert schedule.terms("SA", D(2025, 10, 1)).taker > 0  # before the logged waiver
    assert schedule.terms("SA", D(2025, 12, 1)).taker == 0  # SA → multiplier 0 on Nov 1


def test_windows():
    assert calendar.window(datetime(2025, 11, 25, 23, tzinfo=UTC)) == "discovery"
    assert calendar.window(calendar.BECKER_END) == "confirmation"
    assert calendar.window(calendar.LOCKBOX_START) == "lockbox"
    assert calendar.window(calendar.FORWARD_START) == "forward"
    calendar.STUDY_END["HX"] = D(2026, 1, 1)
    try:
        assert calendar.window(D(2025, 12, 15), "HX") == "unused"
        assert calendar.confirmation_start("HX") == D(2026, 1, 1)
    finally:
        del calendar.STUDY_END["HX"]
