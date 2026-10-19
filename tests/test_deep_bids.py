import logging
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from kalshibot.data import backfill as bf
from kalshibot.kalshi.models import SIZE_SCALE
from kalshibot.research import run
from kalshibot.research.load import Loader
from kalshibot.strategies import deep_bids
from kalshibot.strategies.base import INTENT_COLUMNS

T = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
START, END = datetime(2025, 10, 1, tzinfo=UTC), datetime(2025, 10, 4, tzinfo=UTC)
H8 = deep_bids.DeepBids()
CONFIG = H8.configs()["NO 20¢, 10 s"]
MLB, NHL, WNBA, NCAAMB = ("KXMLBGAME-25OCT01NYYBOS", "KXNHLGAME-25OCT02NYRBOS",
                          "KXWNBAGAME-25OCT02LVANYL", "KXNCAAMBGAME-25OCT02GONSMC")
GAMES = [  # event ticker, teams, listed start (UTC), listing offset in days, exclusive
    (MLB, ("NYY", "BOS"), "2025-10-01 22:05", 14, True),
    (NHL, ("NYR", "BOS", "TIE"), "2025-10-02 23:00", 14, True),
    (WNBA, ("LVA", "NYL"), "2025-10-03 00:00", 14, True),
    ("KXNCAAMBGAME-25OCT02DUKUNC", ("DUK", "UNC"), "2025-10-02 23:00", 14, False),
    ("KXNBAGAME-25OCT02LALBOS", ("LAL", "BOS"), "2025-10-02 23:00", 14, True),  # not H8's
    ("KXNCAAMBGAME-25OCT02KANKST", ("KAN",), "2025-10-02 23:00", 14, True),  # one market
    ("KXMLBGAME-25OCT02LADSDP", ("LAD", "SDP"), "2025-10-02 23:00", 730, True),  # a season ahead
    ("KXNHLGAME-OCT2NJDPHI", ("NJD", "PHI"), "2025-10-02 23:00", 14, True),  # no date
    (NCAAMB, ("GON", "SMC"), "2025-10-02 23:00", 14, True),
]
NYY, BOS = f"{MLB}-NYY", f"{MLB}-BOS"
NYR, BOS2, TIE = f"{NHL}-NYR", f"{NHL}-BOS", f"{NHL}-TIE"
LVA, NYL = f"{WNBA}-LVA", f"{WNBA}-NYL"
GON, SMC = f"{NCAAMB}-GON", f"{NCAAMB}-SMC"
LATE = {TIE: T("2025-10-03 00:00:05"), SMC: T("2025-10-03 00:00:05")}  # listed mid-game
PRINTS = [  # ticker, time, taker side, YES price, size, block trade
    (NYY, "2025-09-25 12:00", "yes", 5000, 100, False),  # before the game: the first reference
    (NYY, "2025-10-01 22:30", "yes", 5500, 100, False),  # YES takers: the sweep's highest
    (NYY, "2025-10-01 22:30", "yes", 5700, 100, False),
    (NYY, "2025-10-01 22:40", "no", 6000, 100, False),  # NO takers: its lowest
    (NYY, "2025-10-01 22:40", "no", 5800, 100, False),
    (NYY, "2025-10-01 22:50", "yes", 6200, 100, False),  # both: the middle of its range
    (NYY, "2025-10-01 22:50", "yes", 6100, 100, False),
    (NYY, "2025-10-01 22:50", "no", 5600, 100, False),
    (NYY, "2025-10-01 23:00", "yes", 5550, 100, False),  # NO 24.5¢ less 20¢: rounds down
    (NYY, "2025-10-01 23:10", "yes", 7800, 100, False),  # a 2¢ bid
    (NYY, "2025-10-01 23:20", "yes", 7900, 100, False),  # 1¢: no bid
    (NYY, "2025-10-01 23:30", "yes", 4000, 100, True),  # a block trade isn't a reference
    (NYY, "2025-10-01 23:40", "yes", 5000, 100, False),
    (BOS, "2025-09-16 22:05:01", "yes", 5000, 100, False),  # 1 s under 15 days before the game
    (BOS, "2025-10-01 23:00:05", "no", 4500, 100, False),  # BOS's first print in them
    # Tier 1 fills a one-contract bid from 20 contracts through it at one instant
    (NYR, "2025-10-02 23:00:05", "yes", 7100, 1000, False),  # through the 30¢ NO bid ...
    (NYR, "2025-10-02 23:00:05", "yes", 7200, 1000, False),  # ... 20 contracts in all
    (BOS2, "2025-10-02 23:00:05", "yes", 7500, 1900, False),  # 19 through
    (BOS2, "2025-10-02 23:00:25", "yes", 9500, 10_000, False),  # at the 5¢ bid, not through
    (SMC, "2025-10-03 00:00:06", "yes", 5000, 100, False),
] + [(f"{event}-{team}", "2025-10-02 22:00", "yes", 5000, 100, False)
     for event, teams, *_ in GAMES[1:] for team in teams if f"{event}-{team}" not in LATE]


@pytest.fixture
def tape(tmp_path, monkeypatch):
    monkeypatch.setenv("KALSHIBOT_LEDGER", str(tmp_path))  # no unlock here: lockbox closed
    kalshi = tmp_path / "raw" / "kalshi"
    for n in range(33):  # the runner's window: 15 days either side of [START, END)
        day = date(2025, 9, 16) + timedelta(n)
        rows = [{"trade_id": f"t{k}", "ticker": t, "created_time": T(ts), "taker_side": side,
                 "yes_price": p, "count": size, "is_block_trade": block}
                for k, (t, ts, side, p, size, block) in enumerate(PRINTS) if T(ts).date() == day]
        bf._write(rows, bf.trades_path(kalshi, day), bf.TRADE_SCHEMA)
    blank = {c: None for c in bf.MARKET_SCHEMA.names}
    markets = [{**blank, "ticker": f"{event}-{team}", "event_ticker": event,
                "created_time": LATE.get(f"{event}-{team}", T(start) - timedelta(days=offset)),
                "latest_expiration_time": T(start) + timedelta(days=offset)}
               for event, teams, start, offset, _ in GAMES for team in teams]
    bf._write(markets, kalshi / "markets.parquet", bf.MARKET_SCHEMA)
    pq.write_table(pa.table({"event_ticker": [g[0] for g in GAMES],
                             "series_ticker": [g[0].split("-")[0] for g in GAMES],
                             "mutually_exclusive": [g[4] for g in GAMES]}),
                   kalshi / "events.parquet")
    series = sorted({g[0].split("-")[0] for g in GAMES})
    pq.write_table(pa.table({"series_ticker": series, "category": ["Sports"] * len(series),
                             "fee_type": ["quadratic"] * len(series),
                             "fee_multiplier": ["1"] * len(series)}), kalshi / "series.parquet")
    return Loader(tmp_path)


def bids(intents: pd.DataFrame, ticker: str) -> pd.Series:
    """A market's limits by decision time."""
    return intents[intents["ticker"] == ticker].set_index("decision_time")["limit"]


@pytest.mark.parametrize(("event", "start", "offset", "registered"), [
    ("KXMLBGAME-25OCT01NYYBOS", "2025-10-01 22:05", 14, True),
    ("KXNHLGAME-25OCT25NYRBOS", "2025-10-25 04:30", 14, True),  # 00:30 EDT, expires 23:30 EST
    ("KXNHLGAME-26NOV05NYRBOS", "2026-11-06 00:00", 2, True),
    ("KXMLBGAME-26OCT151905NYYTOR", "2026-10-15 23:05", 3, True),  # MLB's adds the time
    ("KXMLBGAME-26SEP221305TBNYYG1", "2026-09-22 17:05", 3, True),  # a doubleheader's first
    ("KXMLBGAME-26OCT300030LADTOR", "2026-10-30 04:30", 3, True),  # 00:30 EDT, expires 23:30 EST
    ("KXNCAAMBGAME-27MAR13GONSMC", "2027-03-14 04:30", 2, True),  # 23:30 EST, 00:30 EDT
    # one row per league and offset, whatever the game's year: 14-day listings ran into 2026
    ("KXNCAAMBGAME-26JAN05DUKUNC", "2026-01-06 00:00", 14, True),
    ("KXWNBAGAME-26JUL05LVANYL", "2026-07-05 23:00", 14, True),
    ("KXWNBAGAME-26AUG18LACONN", "2026-08-18 23:00", 2, True),
    ("KXNCAAMBGAME-27JAN05DUKUNC", "2027-01-06 00:00", 3, False),  # MLB's offset
    ("KXMLBGAME-27APR061905NYYTOR", "2027-04-06 23:05", 2, False),  # and the others' for MLB
    ("KXMLBGAME-25OCT02LADSDP", "2025-10-02 23:00", 730, False),  # a season ahead
    ("KXNHLGAME-OCT2NJDPHI", "2025-10-02 23:00", 14, False),  # no date
    ("KXNHLGAME-25FEB30NJDPHI", "2025-03-02 23:00", 14, False),  # no such date
])
def test_the_listed_start_is_expiration_less_one_of_its_leagues_offsets(event, start, offset,
                                                                        registered):
    """The DST rows: the expiration's Eastern date is a day off the offset, the start's isn't."""
    markets = pd.DataFrame({"event_ticker": [event], "series_ticker": [event.split("-")[0]],
                            "latest_expiration_time": [T(start) + timedelta(days=offset)]})
    got = deep_bids._listed_start(markets).iloc[0]
    assert got == T(start) if registered else pd.isna(got)


def test_a_tickers_own_time_is_the_start_where_the_expiration_counts_from_a_placeholder():
    events = ["KXMLBGAME-26APR161235WSHPIT", "KXMLBGAME-26APR161000WSHPIT",
              "KXMLBGAME-26SEP221305TBNYYG2", "KXMLBGAME-26APR16WSHPIT"]
    markets = pd.DataFrame({"event_ticker": events, "series_ticker": "KXMLBGAME",
                            # 10:00 EDT plus 3 days, as Kalshi listed the 2026 season's opening
                            "latest_expiration_time": T("2026-04-16 14:00") + timedelta(3)})
    markets.loc[2, "latest_expiration_time"] = T("2026-09-22 14:00") + timedelta(3)
    assert deep_bids._listed_start(markets).tolist() == [
        T("2026-04-16 16:35"), T("2026-04-16 14:00"), T("2026-09-22 17:05"),
        T("2026-04-16 14:00")]  # without a time, the placeholder is all there is


def test_two_offsets_that_could_both_give_a_start_are_refused(monkeypatch):
    """A day apart, both land on the game's Eastern date when the clocks fall back."""
    monkeypatch.setitem(deep_bids.LEAGUES, "KXNHLGAME",
                        deep_bids.LEAGUES["KXNHLGAME"]._replace(offsets=(2, 3)))
    markets = pd.DataFrame({"event_ticker": ["KXNHLGAME-26NOV01NYRBOS"],
                            "series_ticker": ["KXNHLGAME"],
                            # 00:30 EDT, expiring 3 days on
                            "latest_expiration_time": [T("2026-11-01 04:30") + timedelta(3)]})
    with pytest.raises(ValueError, match="KXNHLGAME-26NOV01NYRBOS"):
        deep_bids._listed_start(markets)


def test_listings_without_a_registered_start_are_logged_and_get_no_bids(tape, caplog):
    with caplog.at_level(logging.WARNING, logger=deep_bids.__name__):
        intents = H8.intents(tape, START, END, CONFIG)
    assert "2 listings, by series: {'KXMLBGAME': 2}" in caplog.text  # the game in the window
    # only the exclusive two-market events of H8's leagues with a listed start
    assert set(intents["ticker"]) == {NYY, BOS, NYR, BOS2, LVA, NYL, GON, SMC}
    listed = tape.markets(END).filter("series_ticker IN ('KXMLBGAME', 'KXNHLGAME')").df()
    for day, counted in (("2025-09-18", ["KXNHLGAME"] * 2), ("2025-09-19", [])):
        when = T(day).to_pydatetime()  # a dateless ticker counts on the day it was listed;
        # LADSDP, listed long before, on its game day (Oct 2), after these windows
        assert deep_bids._live(listed, when, when + timedelta(1), CONFIG["horizon"])[
            1].tolist() == counted


def test_a_bid_every_10_s_from_the_listed_start_until_the_cap(tape):
    intents = H8.intents(tape, START, END, CONFIG)
    assert list(intents.columns) == INTENT_COLUMNS
    assert (intents["intent_id"] == range(len(intents))).all()  # in time order
    assert intents["decision_time"].is_monotonic_increasing
    assert (intents["side"] == "no").all() and (intents["count"] == SIZE_SCALE).all()
    every = pd.date_range(T("2025-10-01 22:05"), T("2025-10-02 03:05"), freq="10s",
                          inclusive="left")  # MLB: 5 hours
    priced_out = (every > T("2025-10-01 23:20")) & (every <= T("2025-10-01 23:40"))
    assert list(bids(intents, NYY).index) == list(every[~priced_out])
    assert list(bids(intents, LVA).index) == list(pd.date_range(  # WNBA: 3 hours
        T("2025-10-03 00:00"), T("2025-10-03 03:00"), freq="10s", inclusive="left"))
    # on the clock, not from the window's start
    later = H8.intents(tape, T("2025-10-01 22:05:03").to_pydatetime(), END, CONFIG)
    assert bids(later, NYY).index[0] == T("2025-10-01 22:05:10")


def test_the_limit_is_20c_under_the_last_instant_before_the_tick(tape):
    nyy = bids(H8.intents(tape, START, END, CONFIG), NYY)
    assert nyy[T("2025-10-01 22:05")] == 3000  # the print a week before the game
    assert nyy[T("2025-10-01 22:30")] == 3000  # a print at the tick isn't seen yet
    for tick, limit in (("22:30:10", 2300), ("22:40:10", 2200), ("22:50:10", 2100),
                        ("23:00:10", 2400), ("23:10:10", 200), ("23:40:10", 3000)):
        assert nyy[T(f"2025-10-01 {tick}")] == limit, tick
    assert (nyy % 100 == 0).all()
    bos = bids(H8.intents(tape, START, END, CONFIG), BOS)  # the lookback is 15 days: ...
    assert bos.index[0] == T("2025-10-01 22:05") and bos.iloc[0] == 3000
    assert bos.index[1] == T("2025-10-01 23:00:10") and bos.iloc[1] == 3500  # ... then no print


def test_bids_only_while_the_event_shows_two_markets(tape):
    intents = H8.intents(tape, START, END, CONFIG)
    assert bids(intents, NYR).index[-1] == T("2025-10-03 00:00")  # the last tick before a third
    assert TIE not in set(intents["ticker"])
    for market in (GON, SMC):  # from the first tick after the second, until NCAAMB's cap
        assert bids(intents, market).index[0] == T("2025-10-03 00:00:10")
        assert bids(intents, market).index[-1] == T("2025-10-03 01:59:50")


@pytest.mark.parametrize(("series", "cap"), [("KXMLBGAME", "5h"), ("KXNHLGAME", "3.5h"),
                                             ("KXNCAAMBGAME", "3h"), ("KXWNBAGAME", "3h")])
def test_each_league_is_live_until_its_cap(series, cap):
    event, begins = f"{series}-25OCT02AAABBB", T("2025-10-02 23:00")
    markets = pd.DataFrame({"ticker": [f"{event}-AAA", f"{event}-BBB"], "event_ticker": event,
                            "series_ticker": series, "created_time": begins - timedelta(1),
                            "latest_expiration_time": begins + timedelta(14),
                            "mutually_exclusive": True})
    live, _ = deep_bids._live(markets, START, END, CONFIG["horizon"])
    assert (live["first_tick"] == begins).all()
    assert (live["stop"] == begins + pd.Timedelta(cap)).all()


def test_markets_count_in_creation_order_and_one_created_on_a_tick_is_seen_after_it():
    begins = T("2025-10-02 23:00")
    rows = [("A", "AAA", T("2025-10-03 00:00")), ("A", "BOS", begins - timedelta(1)),
            ("A", "NYR", begins - timedelta(1)),  # AAA, created last, is the third
            ("B", "BOS", begins - timedelta(1)), ("B", "NYR", begins)]  # NYR at the start
    markets = pd.DataFrame({"ticker": [f"KXNHLGAME-25OCT02{e}-{t}" for e, t, _ in rows],
                            "event_ticker": [f"KXNHLGAME-25OCT02{e}" for e, _, _ in rows],
                            "series_ticker": "KXNHLGAME", "created_time": [c for *_, c in rows],
                            "latest_expiration_time": begins + timedelta(14),
                            "mutually_exclusive": True})
    live = deep_bids._live(markets, START, END, CONFIG["horizon"])[0].set_index("ticker")
    assert set(live.index) == {f"KXNHLGAME-25OCT02{e}-{t}" for e in "AB" for t in ("BOS", "NYR")}
    a, b = live.loc["KXNHLGAME-25OCT02A-BOS"], live.loc["KXNHLGAME-25OCT02B-BOS"]
    assert a["stop"] == T("2025-10-03 00:00:10")  # the tick at the third's creation sees two
    assert b["first_tick"] == T("2025-10-02 23:00:10")  # the tick at the second's doesn't


@pytest.mark.parametrize("cut", ["2025-10-01 22:30", "2025-10-01 23:00:07",
                                 "2025-10-02 23:00:05", "2025-10-03 00:00:05",
                                 "2025-10-03 01:00"])
def test_intents_before_t_do_not_change_when_the_window_ends_at_t(tape, cut):
    whole = H8.intents(tape, START, END, CONFIG)
    early = H8.intents(tape, START, T(cut).to_pydatetime(), CONFIG)
    assert early.equals(whole[whole["decision_time"] < T(cut)])


@pytest.mark.parametrize("later", ["2025-10-01 22:00", "2025-10-01 23:00:05",
                                   "2025-10-02 12:00"])
def test_where_the_window_starts_does_not_change_later_bids(tape, later):
    whole = H8.intents(tape, START, END, CONFIG)
    part = H8.intents(tape, T(later).to_pydatetime(), END, CONFIG)
    kept = whole[whole["decision_time"] >= T(later)].reset_index(drop=True)
    assert part.drop(columns="intent_id").equals(kept.drop(columns="intent_id"))


def test_the_runner_rests_them_and_tier1_fills_20_contracts_through_only(tape):
    # the runner's window, embargoes included; it refuses bids `run._resting` doesn't take
    evaluated = run._evaluate(H8, tape, CONFIG, START - H8.max_hold, END + H8.max_hold,
                              keep=True)
    intents, got = evaluated.intents, evaluated.fills  # Tier 1's participation and haircut
    assert evaluated.outside_hold == 0 and len(intents) == len(H8.intents(tape, START, END,
                                                                          CONFIG))
    assert got[["ticker", "fill_time", "fill_price", "fill_count"]].values.tolist() == [
        [NYR, T("2025-10-02 23:00:05"), 3000, SIZE_SCALE]]
    decided = intents.set_index("intent_id").loc[got["intent_id"].iloc[0], "decision_time"]
    assert decided == T("2025-10-02 23:00")
    assert bids(intents, NYR)[T("2025-10-02 23:00")] == 3000  # filled: 20 through it
    assert bids(intents, BOS2)[T("2025-10-02 23:00")] == 3000  # unfilled: 19 through it
    assert bids(intents, BOS2)[T("2025-10-02 23:00:20")] == 500  # the bid the 95¢ print met


def test_the_runner_evaluates_it_in_parts_as_if_whole(tape, monkeypatch):
    got = []
    for chunk in ("YS", "D"):  # whole; split at midnights, games in play across them
        monkeypatch.setattr(run, "CHUNK", chunk)
        got.append(run._evaluate(H8, tape, CONFIG, START - H8.max_hold, END + H8.max_hold,
                                 keep=True))
    whole, part = got
    assert part.intents.equals(whole.intents) and part.fills.equals(whole.fills)
    assert part.digest == whole.digest and part.outside_hold == whole.outside_hold
