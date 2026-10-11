from datetime import timedelta

import duckdb
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kalshibot.research.backtest.fills import FILL_COLUMNS, maker_fills, taker_fills

T0 = pd.Timestamp("2026-01-05 12:00:00", tz="UTC")
S = lambda seconds: T0 + pd.Timedelta(seconds=seconds)  # noqa: E731


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("SET TimeZone = 'UTC'")
    trades = pd.DataFrame([  # taker_side = the contract the taker bought
        ("T", S(0), 4200, 500, "yes"),   # the signal's own print
        ("T", S(0.5), 4300, 500, "yes"),  # inside the latency window
        ("T", S(2), 3900, 300, "no"),    # a seller of YES through 4000
        ("T", S(3), 4400, 200, "yes"),   # first YES buy after latency: the ask is 4400
        ("T", S(4), 4000, 900, "no"),    # sells exactly at 4000: not through
        ("T", S(5), 3800, 400, "no"),    # through again
    ], columns=["ticker", "created_time", "yes_price", "count", "taker_side"])
    c.register("trades", trades)
    return c


def intents(con, rows):
    df = pd.DataFrame(rows, columns=["intent_id", "ticker", "side", "decision_time", "count",
                                     "limit"])
    con.register("intents", df)
    return "intents"


def test_taker_fills_at_the_first_same_side_print_after_latency(con):
    name = intents(con, [(1, "T", "yes", S(0), 1000, None),   # buy YES
                         (2, "T", "no", S(0), 100, None),     # buy NO: next YES-seller print
                         (3, "T", "yes", S(0), 100, 4300)])   # limit below the ask: no fill
    out = taker_fills(con, name, "trades", latency=timedelta(seconds=1),
                      participation=1.0).df().set_index("intent_id")
    assert list(out.reset_index().columns) == FILL_COLUMNS
    assert out.loc[1, "fill_time"] == S(3) and out.loc[1, "fill_price"] == 4400
    assert out.loc[1, "fill_count"] == 200  # capped by the print's size
    assert out.loc[2, "fill_time"] == S(2) and out.loc[2, "fill_price"] == 6100
    assert 3 not in out.index


def test_maker_fills_only_when_trades_go_through_the_bid(con):
    name = intents(con, [(1, "T", "yes", S(0), 100, 4000),    # filled by the 3900 print
                         (2, "T", "yes", S(0), 1000, 4000),   # partial: 1.5 + 2 available
                         (3, "T", "yes", S(0), 100, 3700)])   # nothing trades below 3700
    out = maker_fills(con, name, "trades", horizon=timedelta(seconds=10),
                      latency=timedelta(seconds=1), queue_haircut=0.5,
                      participation=1.0).df().set_index("intent_id")
    assert list(out.reset_index().columns) == FILL_COLUMNS
    assert out.loc[1, "fill_time"] == S(2) and out.loc[1, "fill_count"] == 100
    # one fill per sweep, of its own share in whole contracts: 1.5 → 1, then 2
    assert out.loc[[2], ["fill_time", "fill_count"]].values.tolist() == [[S(2), 100], [S(5), 200]]
    assert (out["fill_price"] == 4000).all() and 3 not in out.index


def test_each_sweep_fills_the_whole_contracts_of_its_own_share(con):
    con.register("sweeps", pd.DataFrame([("T", S(2), 4100, 500, "no"),  # above the bid
                                         ("T", S(2), 3900, 300, "no"),
                                         ("T", S(2), 3800, 100, "no"),  # 1.5 + 0.5: 2 in one
                                         ("T", S(3), 3900, 100, "no"),  # 0.5, and 0.5 again:
                                         ("T", S(4), 3900, 100, "no")],  # no whole contract
                                        columns=["ticker", "created_time", "yes_price", "count",
                                                 "taker_side"]))
    name = intents(con, [(1, "T", "yes", S(0), 1000, 4000)])
    out = maker_fills(con, name, "sweeps", horizon=timedelta(seconds=10), queue_haircut=0.5,
                      participation=1.0).df()
    assert out[["fill_time", "fill_count"]].values.tolist() == [[S(2), 200]]


def test_maker_horizon_cuts_off_later_prints(con):
    name = intents(con, [(1, "T", "yes", S(0), 1000, 4000)])
    out = maker_fills(con, name, "trades", horizon=timedelta(seconds=2),
                      latency=timedelta(seconds=1), queue_haircut=0.5, participation=1.0).df()
    assert out.fill_count.tolist() == [100]  # only S(2)'s 1.5 is within 1–3 s; whole


def test_participation_caps_each_print_and_defaults_to_a_tenth(con):
    name = intents(con, [(1, "T", "yes", S(0), 1000, None),
                         (2, "T", "no", S(1.5), 1000, 4000)])  # a resting NO bid at 40¢
    assert taker_fills(con, name, "trades").df().empty  # 10% of 2 contracts: none whole
    con.register("big", pd.DataFrame([("T", S(3), 4400, 2500, "yes")],
                                     columns=["ticker", "created_time", "yes_price", "count",
                                              "taker_side"]))
    taker = taker_fills(con, name, "big").df().set_index("intent_id")
    assert taker.loc[1, "fill_count"] == 200  # 10% of 25 contracts, in whole contracts
    maker = maker_fills(con, name, "trades", horizon=timedelta(seconds=10)).df()
    # YES buyers paying 44¢ at S(3) sold NO at 56¢, not below 40¢: nothing goes through a
    # NO bid at 40¢ here
    assert 2 not in set(maker.intent_id)


def test_no_side_makers_fill_when_yes_buyers_trade_through(con):
    con.register("sweep", pd.DataFrame([("T", S(2), 6500, 400, "yes")],  # NO sold at 35¢
                                       columns=["ticker", "created_time", "yes_price", "count",
                                                "taker_side"]))
    name = intents(con, [(1, "T", "no", S(0), 100, 4000)])
    out = maker_fills(con, name, "sweep", horizon=timedelta(seconds=10), queue_haircut=0.5,
                      participation=1.0).df()
    assert out[["fill_price", "fill_count"]].values.tolist() == [[4000, 100]]


def test_takers_wait_at_most_max_wait(con):
    name = intents(con, [(1, "T", "yes", S(0), 100, None)])  # next YES buy is 3 s later
    assert taker_fills(con, name, "trades", max_wait=timedelta(seconds=1)).df().empty


def test_a_sweep_shows_its_best_level(con):
    con.register("sweep", pd.DataFrame([("T", S(2), 4500, 300, "yes"),
                                        ("T", S(2), 4400, 100, "yes"),
                                        ("T", S(2), 4400, 50, "yes")],
                                       columns=["ticker", "created_time", "yes_price", "count",
                                                "taker_side"]))
    name = intents(con, [(1, "T", "yes", S(0), 1000, None)])
    out = taker_fills(con, name, "sweep", participation=1.0).df()
    assert out[["fill_price", "fill_count"]].values.tolist() == [[4400, 100]]  # 1.5: 1


PRINTS = st.lists(st.tuples(st.integers(0, 60), st.integers(1, 99), st.integers(1, 9),
                            st.sampled_from(["yes", "no"])), min_size=1, max_size=30)


@settings(max_examples=60, deadline=None)
@given(PRINTS, st.integers(1, 59))
def test_fills_before_a_cut_dont_depend_on_later_prints(prints, cut):
    tape = pd.DataFrame([("T", S(t), p * 100, c * 100, side) for t, p, c, side in prints],
                        columns=["ticker", "created_time", "yes_price", "count", "taker_side"])
    orders = pd.DataFrame([(i, "T", side, S(t), 300, lim)
                           for i, (t, side, lim) in enumerate([(0, "yes", None), (5, "no", None),
                                                               (1, "yes", 6000), (2, "no", 5000)])],
                          columns=["intent_id", "ticker", "side", "decision_time", "count",
                                   "limit"])
    c = duckdb.connect()
    c.execute("SET TimeZone = 'UTC'")
    c.register("intents", orders)
    results = []
    for name, tp in (("tape_all", tape), ("tape_cut", tape[tape.created_time < S(cut)])):
        c.register(name, tp)
        both = pd.concat([taker_fills(c, "intents", name, participation=0.5).df(),
                          maker_fills(c, "intents", name, horizon=timedelta(seconds=30),
                                      participation=0.5).df()])
        results.append(both[both.fill_time < S(cut)].sort_values(
            ["intent_id", "maker", "fill_time"]).reset_index(drop=True))
    full, cut_ = results
    pd.testing.assert_frame_equal(full, cut_, check_dtype=False)
