from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from kalshibot.data import backfill as bf
from kalshibot.research import quotes
from kalshibot.research.load import MissingData

T = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
AS_OF = datetime(2025, 12, 2, tzinfo=UTC)
WANTED = pd.DataFrame({"ticker": ["A", "B"], "priced_at": [T("2025-10-10 12:00")] * 2})


@pytest.fixture
def listed(loader):
    """The conftest markets plus two buckets of A's exclusive event EA: A2, untraded, and
    A3, which closed (and lost) before WANTED is priced."""
    path = loader.kalshi_dir / "markets.parquet"
    rows = pq.read_table(path).to_pylist()
    rows.append({**rows[0], "ticker": "A2", "created_time": T("2025-02-01")})
    rows.append({**rows[0], "ticker": "A3", "created_time": T("2025-02-01"),
                 "close_time": T("2025-10-05"), "settlement_ts": T("2025-10-05 01:00"),
                 "result": "no", "settlement_value": 0})
    bf._write(rows, path, bf.MARKET_SCHEMA)
    return loader


def candles(loader, ticker, day, rows=()):
    blank = {c: None for c in bf.CANDLE_SCHEMA.names}
    bf._write([{**blank, "ticker": ticker, "end_period_ts": T(t), "yes_bid_close": b,
                "yes_ask_close": a} for t, b, a in rows],
              bf.candles_path(loader.kalshi_dir, ticker, day), bf.CANDLE_SCHEMA)


def need(loader):
    out = quotes.needed(loader, WANTED, AS_OF)
    return sorted((t, str(pd.Timestamp(d).date())) for t, d in zip(out.ticker, out.day,
                                                                    strict=True))


def test_the_search_walks_back_a_day_at_a_time_over_every_exclusive_member(listed):
    # EA is exclusive, so A2 is needed though untraded (A3 has closed); EB isn't (C, D are
    # listed later anyway)
    assert need(listed) == [("A", "2025-10-10"), ("A2", "2025-10-10"), ("B", "2025-10-10")]
    candles(listed, "A", date(2025, 10, 10), [("2025-10-10 10:00", 6000, 6200)])
    candles(listed, "A2", date(2025, 10, 10), [("2025-10-10 13:00", 3000, 3200)])  # later
    candles(listed, "B", date(2025, 10, 10))
    assert need(listed) == [("A2", "2025-10-09"), ("B", "2025-10-09")]  # A is done
    candles(listed, "A2", date(2025, 10, 9), [("2025-10-09 20:00", 3500, 3700)])
    for day in range(9, 2, -1):  # B never quotes: seven days back, then it gives up
        assert need(listed) == [("B", f"2025-10-{day:02d}")]
        candles(listed, "B", date(2025, 10, day))
    assert need(listed) == []
    out, read = quotes.mids(listed, WANTED, AS_OF)
    out = out.set_index("ticker")
    assert out.loc["A", "mid"] == 6100 and out.loc["A2", "mid"] == 3600
    assert pd.isna(out.loc["B", "mid"]) and not out.loc["B", "mutually_exclusive"]
    assert len(read) == 1 + 2 + 8  # every file the search opened, for the data hash


def test_mids_carry_the_last_quote_and_bound_an_empty_side(loader):
    candles(loader, "A", date(2025, 10, 10), [("2025-10-10 10:00", 6000, 6200),
                                              ("2025-10-10 11:00", 6400, 6600),
                                              ("2025-10-10 13:00", 9000, 9200)])  # later
    candles(loader, "B", date(2025, 10, 10), [("2025-10-10 08:00", None, 3000)])  # no bid
    out = quotes.mids(loader, WANTED, AS_OF)[0].set_index("ticker")
    assert out.loc["A", "mid"] == 6500 and bool(out.loc["A", "mutually_exclusive"])
    assert out.loc["B", "mid"] == 1500


def test_mids_refuse_without_their_candles(loader):
    with pytest.raises(MissingData):
        quotes.mids(loader, WANTED, AS_OF)


def test_a_ladder_is_one_underlying_whatever_its_strike_type():
    e, nba, nhl, epl = "KXBTCD-25DEC0214", "KXNBAPTS-25DEC02WASPHI", "KXNHLSPREAD-25DEC06", "G"
    members = pd.DataFrame({
        "event": [e, e, nba, nba, nba, nhl, nhl, epl, epl, "KXHIGHNY-25DEC03"],
        "ticker": [f"{e}-T77249.99", f"{e}-T78249.99", f"{nba}-PHITMAXEYPG-35",
                   f"{nba}-PHITMAXEYPG-40", f"{nba}-WASCMCCOLLUMSG-25", f"{nhl}-EDM2",
                   f"{nhl}-SEA1", f"{epl}-WOLEAGBAD12", f"{epl}-WOLSMITH9",
                   "KXHIGHNY-25DEC03-T38"],
        "mutually_exclusive": [False] * 9 + [True],
        "strike_type": ["greater"] * 2 + ["structured"] * 3 + ["greater"] * 2
        + ["structured"] * 2 + ["less"],
        "floor_strike": [77249.99, 78249.99, 34.5, 39.5, 24.5, 2.5, 1.5, None, None, None],
        "cap_strike": [None] * 9 + [38.0]})
    draws = quotes._draws(members).set_index(members["ticker"])
    ladder = draws["ladder"]
    # BTC at one fix, and one player's points, are each one ladder
    assert ladder[f"{e}-T77249.99"] == ladder[f"{e}-T78249.99"] == ""
    assert ladder[f"{nba}-PHITMAXEYPG-35"] == ladder[f"{nba}-PHITMAXEYPG-40"] == "PHITMAXEYPG"
    # another player, each team's margin; goal scorers (a shirt number, no strike): none
    assert ladder[f"{nba}-WASCMCCOLLUMSG-25"] == "WASCMCCOLLUMSG"
    assert (ladder[f"{nhl}-EDM2"], ladder[f"{nhl}-SEA1"]) == ("EDM", "SEA")
    assert ladder[[f"{epl}-WOLEAGBAD12", f"{epl}-WOLSMITH9"]].isna().all()
    # an exclusive event's "below" tail: a bucket placed first, not a ladder
    tail = draws.loc["KXHIGHNY-25DEC03-T38"]
    assert pd.isna(tail["ladder"]) and tail["position"] == -np.inf and tail["below"]
