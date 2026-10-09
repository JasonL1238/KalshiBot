"""The Tier-2 engines (NautilusTrader replay, vectorbt bar closes) against Tier 1, on a
tape with known answers."""

from datetime import UTC, datetime, timedelta

import duckdb
import pandas as pd
import pytest

from kalshibot.kalshi.fee_history import FeeSchedule
from kalshibot.kalshi.fees import DIRECT_MEMBER
from kalshibot.research.backtest import fills
from kalshibot.research.backtest.bars import bars
from kalshibot.research.backtest.replay import replay
from kalshibot.research.backtest.settle import settle
from kalshibot.strategies.base import INTENT_COLUMNS

T = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
END = datetime(2026, 1, 3, tzinfo=UTC)
TRADES = pd.DataFrame([
    ("KXT-A", T("2026-01-01 00:00:00"), 4000, 1000, "yes"),
    ("KXT-A", T("2026-01-01 00:00:30.5"), 4100, 200, "no"),  # inside the 1 s latency
    ("KXT-A", T("2026-01-01 00:01:00"), 4200, 800, "no"),  # next print: a NO taker's
    ("KXT-A", T("2026-01-01 00:02:00"), 4300, 1000, "yes"),  # next YES taker's print
    ("KXT-A", T("2026-01-01 00:03:00"), 3800, 500, "no"),  # trades through a 39¢ bid
    ("KXT-B", T("2026-01-01 00:00:10"), 6000, 500, "yes"),
    ("KXT-B", T("2026-01-01 00:01:30"), 5900, 500, "no"),  # a NO taker paid 41¢
    ("KXT-C", T("2026-01-01 00:05:00"), 2000, 500, "yes"),
    ("KXT-S", T("2026-01-01 00:00:00"), 3000, 1000, "yes"),
], columns=["ticker", "created_time", "yes_price", "count", "taker_side"])
MARKETS = pd.DataFrame([("KXT-A", "yes", 10_000, T("2026-01-02")),
                        ("KXT-B", "no", 0, T("2026-01-02")),
                        ("KXT-C", None, None, None),
                        ("KXT-S", "scalar", 3_700, T("2026-01-02"))],  # pays 37¢ per YES
                       columns=["ticker", "result", "settlement_value", "settlement_ts"])
TAKERS = pd.DataFrame([(1, "KXT-A", "yes", T("2026-01-01 00:00:30"), 1000, None),
                       (2, "KXT-B", "no", T("2026-01-01 00:01:00"), 300, None),
                       (3, "KXT-C", "yes", T("2026-01-01 00:04:00"), 100, None)],
                      columns=INTENT_COLUMNS)
# YES then NO in one market and one bar: a close and reopen in the replay, two columns
# (not one netted order) in the bars
CYCLE = pd.DataFrame([(6, "KXT-S", "yes", T("2026-01-01 00:00:20"), 200, None),
                      (7, "KXT-S", "no", T("2026-01-01 00:00:25"), 200, None),
                      (8, "KXT-S", "yes", T("2026-01-01 00:00:30"), 500, None)],
                     columns=INTENT_COLUMNS)
SCHEDULE = FeeSchedule([], {"KXT": ("quadratic", "1", "Test")})


def series_of(ticker):
    return ticker.split("-")[0]


def run(intents, engine=replay, **kwargs):
    got, engine_pnl = engine(intents, TRADES, MARKETS, series_of, SCHEDULE, END, DIRECT_MEMBER,
                             **kwargs)
    return got, engine_pnl, settle(got, MARKETS, series_of, SCHEDULE, END, DIRECT_MEMBER)


def tier1(fn, intents, **kwargs):
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.register("intents", intents)
    con.register("trades", TRADES)
    return fn(con, "intents", "trades", **kwargs).df().set_index("intent_id")


def by_intent(df):
    return {(r.intent_id, r.fill_price, r.fill_count) for r in df.itertuples()}


@pytest.mark.parametrize("engine", [replay, bars])
@pytest.mark.parametrize("intents", [TAKERS, CYCLE])
def test_engine_accounting_matches_settle_to_the_micro_dollar(engine, intents):
    got, engine_pnl, priced = run(intents, engine)
    ours = priced[priced["status"] == "settled"].groupby("ticker")["pnl"].sum()
    assert len(got) and len(ours)
    assert engine_pnl.set_index("ticker")["pnl"].to_dict() == ours.to_dict()


def test_scalar_markets_settle_at_their_value_in_every_tier():
    got, engine_pnl, priced = run(CYCLE[CYCLE.intent_id == 6], bars)
    # 2 YES at 30¢ paid 37¢: +$0.14 less the 2026-01 cent-rounded fee ($0.03)
    assert priced.loc[0, "pnl"] == 140_000 - 30_000 == engine_pnl.loc[0, "pnl"]


def test_takers_fill_at_the_last_print_when_they_arrive():
    got, _, priced = run(TAKERS)
    # intent 1 arrives 00:00:31: last print 41¢ for 2 contracts, the other 8 a tick worse;
    # intent 2's last B print was 60¢ YES (40¢ NO); intent 3's market hasn't printed yet
    assert by_intent(got) == {(1, 4100, 200), (1, 4101, 800), (2, 4000, 300)}
    arrival = got["intent_id"].map(TAKERS.set_index("intent_id")["decision_time"]) + timedelta(
        seconds=1)
    assert (got["fill_time"] == arrival + pd.Timedelta(1)).all()  # processed at arrival + 1 ns
    # the same fills when other markets print in between (no dependence on tape density)
    busy = pd.concat([TRADES, TRADES.assign(ticker="KXT-Z")], ignore_index=True)
    again, _ = replay(TAKERS, busy, MARKETS, series_of, SCHEDULE, END, DIRECT_MEMBER)
    assert by_intent(again) == by_intent(got)
    # Tier 1 waits for the next YES taker's print, which shows the ask: 43¢ for intent 1
    assert tier1(fills.taker_fills, TAKERS).loc[1, "fill_price"] == 4300


def test_bar_closes_fill_whole_orders_even_at_stale_prices():
    got, _, _ = run(TAKERS, bars)
    assert by_intent(got) == {(1, 4200, 1000), (2, 4100, 300), (3, 2000, 100)}
    late = pd.DataFrame([(5, "KXT-B", "yes", T("2026-01-01 00:05:00"), 100, None)],
                        columns=INTENT_COLUMNS)
    got, _, _ = run(late, bars)  # KXT-B last printed at 00:01:30
    assert by_intent(got) == by_intent(run(late)[0]) == {(5, 5900, 100)}  # so does Nautilus
    assert tier1(fills.taker_fills, late).empty  # no later print: no fill


@pytest.mark.parametrize("engine", [replay, bars])
def test_engines_return_the_fill_schema(engine):
    assert list(run(TAKERS, engine)[0].columns) == fills.FILL_COLUMNS


LIMITS = pd.DataFrame([  # each pair straddles the price the engine fills at
    (11, "KXT-A", "yes", T("2026-01-01 00:00:30"), 1000, 4100),  # replay: 41¢ (2 there)
    (12, "KXT-A", "yes", T("2026-01-01 00:00:30"), 1000, 4099),
    (13, "KXT-B", "no", T("2026-01-01 00:01:00"), 300, 4000),  # replay: 40¢ NO
    (14, "KXT-B", "no", T("2026-01-01 00:01:00"), 300, 3999),
], columns=INTENT_COLUMNS)


def test_replay_limits_fill_only_within_their_price():
    assert by_intent(run(LIMITS)[0]) == {(11, 4100, 200), (13, 4000, 300)}


def test_bar_limits_fill_only_within_their_price():
    # bar closes: KXT-A 42¢ at 00:01, KXT-B 59¢ YES (41¢ NO) at 00:02
    limits = LIMITS.assign(limit=[4200, 4199, 4100, 4099])
    assert by_intent(run(limits, bars)[0]) == {(11, 4200, 1000), (13, 4100, 300)}


def test_replay_timing_doesnt_depend_on_where_the_tape_starts():
    # decided before KXT-C's first print and before the engine's first event: still
    # processed on arrival, when KXT-C has no print yet, so rejected, alone or not
    early = pd.DataFrame([(21, "KXT-C", "yes", T("2025-12-31 23:59:00"), 100, None)],
                         columns=INTENT_COLUMNS)
    assert run(early)[0].empty
    assert run(pd.concat([early, TAKERS]))[0].query("intent_id == 21").empty


@pytest.mark.parametrize("engine", [replay, bars])
def test_engines_take_nothing_or_too_late_quietly(engine):
    late = pd.DataFrame([(22, "KXT-A", "yes", T("2026-01-02 00:00:00"), 100, None)],
                        columns=INTENT_COLUMNS)  # arrives after KXT-A settled
    for intents in (TAKERS.iloc[:0], late):
        got, pnl = engine(intents, TRADES, MARKETS, series_of, SCHEDULE, END, DIRECT_MEMBER)
        assert got.empty and pnl.empty and list(got.columns) == fills.FILL_COLUMNS
