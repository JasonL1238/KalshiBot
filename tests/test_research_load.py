from datetime import UTC, datetime

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from kalshibot.data import backfill as bf
from kalshibot.research import calendar, ledger, load
from kalshibot.research.load import Loader, LockboxError, MissingData

T = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
D = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731


@pytest.fixture
def loader(tmp_path, monkeypatch):
    (tmp_path / "ledger").mkdir()
    monkeypatch.setenv("KALSHIBOT_LEDGER", str(tmp_path / "ledger"))
    becker = tmp_path / "external" / "becker" / "data" / "kalshi" / "trades"
    becker.mkdir(parents=True)
    pd.DataFrame({  # Becker: integer cents and whole contracts
        "trade_id": ["b1", "b2", "b3"], "ticker": ["A", "KXMVE-X", "A"],
        "count": [3, 1, 2], "yes_price": [55, 20, 6], "no_price": [45, 80, 93],
        "taker_side": ["yes", "no", "yes"],
        "created_time": [T("2025-06-01"), T("2025-06-01"), T("2025-10-10")],
    }).to_parquet(becker / "part.parquet")
    kalshi = tmp_path / "raw" / "kalshi"
    trade = {"ticker": "A", "taker_side": "yes", "is_block_trade": False}
    for day, rows in {"2025-10-09": [], "2025-10-10": [
            {**trade, "trade_id": "a1", "created_time": T("2025-10-10"), "yes_price": 650,
             "count": 200}]}.items():
        bf._write(rows, kalshi / "trades" / f"date={day}" / "part.parquet", bf.TRADE_SCHEMA)
    market = {c: None for c in bf.MARKET_SCHEMA.names}
    bf._write([
        {**market, "ticker": "A", "event_ticker": "EA", "can_close_early": False,
         "created_time": T("2025-01-01"), "close_time": T("2025-12-01"),
         "settlement_ts": T("2025-12-01 01:00"), "result": "yes", "volume": 500,
         "rules_primary": "If ..."},
        {**market, "ticker": "B", "event_ticker": "EB", "can_close_early": True,
         "created_time": T("2025-01-01"), "close_time": T("2025-11-01"),
         "settlement_ts": T("2025-11-01 00:05"), "result": "yes", "volume": 900},
        {**market, "ticker": "C", "event_ticker": "EB", "created_time": T("2025-11-15")},
        {**market, "ticker": "D", "event_ticker": "EB", "can_close_early": False,
         "created_time": T("2026-01-01"), "close_time": T("2026-06-01"),
         "settlement_ts": T("2026-06-01 01:00"), "result": "no"},  # settles in the lockbox
    ], kalshi / "markets.parquet", bf.MARKET_SCHEMA)
    pq.write_table(pa.table({"event_ticker": ["EA", "EB"], "series_ticker": ["SA", "SB"],
                             "mutually_exclusive": [True, False]}), kalshi / "events.parquet")
    pq.write_table(pa.table({"series_ticker": ["SA", "SB"], "category": ["Weather", "Crypto"],
                             "fee_type": ["quadratic"] * 2, "fee_multiplier": ["1"] * 2}),
                   kalshi / "series.parquet")
    bf._write([{"series_ticker": "SA", "fee_type": "quadratic", "fee_multiplier": "0",
                "scheduled_ts": T("2025-11-01"), "fetched_at": T("2026-10-08")}],
              kalshi / "fee_changes.parquet", bf.FEE_CHANGE_SCHEMA)
    return Loader(tmp_path)


def test_every_market_column_is_classified():
    columns = load.AS_LISTED + load.POST_HOC + load.META
    assert sorted(columns) == sorted(bf.MARKET_SCHEMA.names)
    assert len(set(columns)) == len(columns)


def test_one_tape_in_fixed_point_units_without_combos(loader):
    df = loader.trades(D(2025, 1, 1), D(2025, 10, 11)).df().set_index("trade_id")
    assert sorted(df.index) == ["a1", "b1"]  # combo b2 dropped; truncated b3 replaced by a1
    assert (df.loc["b1", "yes_price"], df.loc["b1", "count"], df.loc["b1", "source"]) == (
        5500, 300, "becker")
    assert df.loc["a1", "source"] == "api"


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
