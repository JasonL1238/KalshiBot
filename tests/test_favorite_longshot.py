from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

import pandas as pd
import pytest

from kalshibot.data import backfill as bf
from kalshibot.research.load import Loader
from kalshibot.strategies.base import INTENT_COLUMNS
from kalshibot.strategies.favorite_longshot import FavoriteLongshot

T = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
START, END = datetime(2025, 10, 1, tzinfo=UTC), datetime(2025, 10, 4, tzinfo=UTC)
H1 = FavoriteLongshot()
PRINTS = [  # ticker, time, YES price, block trade
    ("X", "2025-10-01 23:00", 800, False),  # X's first print at ≤ 10¢ on Oct 1
    ("X", "2025-10-01 23:30", 700, False),  # the same UTC day: no second decision
    ("X", "2025-10-02 00:30", 900, False),  # a new UTC day
    ("X", "2025-10-03 02:00", 600, True),   # a block trade doesn't count
    ("X", "2025-10-03 03:00", 950, False),
    ("Y", "2025-10-02 12:00", 9900, False),  # 99¢: the YES limit stops at $1
]


@pytest.fixture
def tape(tmp_path, monkeypatch):
    monkeypatch.setenv("KALSHIBOT_LEDGER", str(tmp_path))  # no unlock here: lockbox closed
    kalshi = tmp_path / "raw" / "kalshi"
    for n in range(3):
        day = date(2025, 10, 1) + timedelta(n)
        rows = [{"trade_id": f"{t}{k}", "ticker": t, "created_time": T(ts), "yes_price": p,
                 "count": 1000, "taker_side": "yes", "is_block_trade": block}
                for k, (t, ts, p, block) in enumerate(PRINTS) if T(ts).date() == day]
        bf._write(rows, bf.trades_path(kalshi, day), bf.TRADE_SCHEMA)
    bf._write([], kalshi / "markets.parquet", bf.MARKET_SCHEMA)
    return Loader(tmp_path)


def test_the_first_print_past_the_threshold_each_utc_day(tape):
    no = H1.intents(tape, START, END, H1.configs()["NO at YES ≤ 10¢"])
    assert list(no.columns) == INTENT_COLUMNS
    assert no[["ticker", "decision_time", "side", "count", "limit"]].values.tolist() == [
        ["X", T("2025-10-01 23:00"), "no", 100, 9400],  # NO at 92¢, at most 94¢
        ["X", T("2025-10-02 00:30"), "no", 100, 9300],
        ["X", T("2025-10-03 03:00"), "no", 100, 9250]]
    yes = H1.intents(tape, START, END, H1.configs()["YES at YES ≥ 95¢"])
    assert yes[["ticker", "limit"]].values.tolist() == [["Y", 10_000]]


@pytest.mark.parametrize("cut", ["2025-10-01 23:15", "2025-10-02 06:00", "2025-10-03 02:30",
                                 "2025-10-03 12:00"])
def test_intents_before_t_do_not_change_when_the_window_ends_at_t(tape, cut):
    for config in H1.configs().values():
        whole = H1.intents(tape, START, END, config)
        early = H1.intents(tape, START, T(cut).to_pydatetime(), config)
        kept = whole[whole["decision_time"] < T(cut)].drop(columns="intent_id")
        assert early.drop(columns="intent_id").equals(kept.reset_index(drop=True))


@pytest.mark.parametrize("cut", ["2025-10-02", "2025-10-03"])
def test_a_window_split_at_a_utc_midnight_splits_its_intents(tape, cut):
    for config in H1.configs().values():
        whole = H1.intents(tape, START, END, config)
        parts = [H1.intents(tape, a, b, config) for a, b in pairwise(
            [START, T(cut).to_pydatetime(), END])]
        split = pd.concat([p for p in parts if len(p)] or parts[:1], ignore_index=True)
        assert split.drop(columns="intent_id").equals(whole.drop(columns="intent_id"))
