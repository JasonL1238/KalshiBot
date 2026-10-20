from datetime import UTC, datetime, timedelta

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


def test_relations_stay_lazy(loader):
    """A materialized relation (DuckDB's for any query with parameters) scans a column
    buffer; a lazy one scans the files, so callers' filters reach them."""
    relations = (loader.markets(D(2025, 10, 15)), loader.trades(D(2025, 9, 29), D(2025, 10, 11)))
    assert all("COLUMN_DATA_SCAN" not in r.filter("ticker = 'A'").explain() for r in relations)


def test_missing_and_unfinished_days_raise(loader):
    with pytest.raises(MissingData):
        loader.trades(D(2025, 10, 1), D(2025, 10, 13))
    with pytest.raises(MissingData, match="finished"):  # never a quietly shorter tape
        loader.trades(D(2026, 10, 1), datetime.now(UTC) + timedelta(days=1))


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
    with pytest.raises(LockboxError):
        loader.candles(pd.DataFrame({"ticker": ["A"], "day": [T("2026-05-02")]}))


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
    assert before.loc["A", "series_ticker"] == "SA" and before.loc["B", "category"] == "Crypto"
    assert before.loc["A", "rules_primary"] == "If ..."
    after = loader.markets(D(2025, 12, 2)).df().set_index("ticker")
    assert after.loc["A", "result"] == "yes" and after.loc["B", "volume"] == 900


WINDOW = [(D(2025, 10, 1), D(2025, 10, 11))]


def test_the_fingerprint_follows_every_input(loader):
    candles = pd.DataFrame({"ticker": ["A"], "day": [T("2025-10-10")]})
    before = loader.fingerprint(WINDOW, candles)
    assert before == loader.fingerprint(WINDOW, candles)
    bf._write([], bf.candles_path(loader.kalshi_dir, "A", T("2025-10-10").date()),
              bf.CANDLE_SCHEMA)
    with_candle = loader.fingerprint(WINDOW, candles)
    assert with_candle != before
    assert loader.fingerprint(WINDOW) != with_candle  # not read
    bf._write([], bf.trades_path(loader.kalshi_dir, T("2025-10-01").date()), bf.TRADE_SCHEMA)
    trades = loader.fingerprint(WINDOW, candles)
    assert trades != with_candle  # a trade day rewritten (here: emptied)
    path = loader.kalshi_dir / "series.parquet"
    path.write_bytes(path.read_bytes() + b" ")
    assert loader.fingerprint(WINDOW, candles) != trades


def test_fee_schedule_from_tables(loader):
    schedule = loader.fee_schedule()
    assert schedule.terms("SA", D(2025, 10, 1)).taker > 0  # before the logged waiver
    assert schedule.terms("SA", D(2025, 12, 1)).taker == 0  # SA → multiplier 0 on Nov 1


def test_confirmation_windows(monkeypatch):
    assert calendar.confirmation_window("H9") == (calendar.BECKER_END, calendar.LOCKBOX_START)
    with pytest.raises(ValueError, match="withdrawn"):
        calendar.confirmation_window("H1")
    monkeypatch.setattr(calendar, "FORWARD", calendar.FORWARD | {"HF"})
    with pytest.raises(ValueError, match="register"):  # forward, its stretch not yet fixed
        calendar.confirmation_window("HF")
    monkeypatch.setitem(calendar.FORWARD_TESTS, "HF", (D(2026, 10, 15), D(2027, 2, 15)))
    assert calendar.confirmation_window("HF") == (D(2026, 10, 15), D(2027, 2, 15))
    monkeypatch.setitem(calendar.STUDY_END, "HX", D(2026, 1, 1))
    assert calendar.confirmation_window("HX")[0] == D(2026, 1, 1)
    monkeypatch.setitem(calendar.STUDY_END, "HX", calendar.LOCKBOX_START)
    with pytest.raises(ValueError, match="forward"):  # nothing left before the lockbox
        calendar.confirmation_window("HX")
