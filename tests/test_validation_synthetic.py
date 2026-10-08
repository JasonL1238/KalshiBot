import numpy as np
import pytest

from kalshibot.kalshi.models import PRICE_SCALE
from kalshibot.research.validation import synthetic

RNG = np.random.default_rng(5)


@pytest.mark.parametrize("side", ["no", "yes"])
def test_boundary_quotes_are_the_highest_break_even_asks(side):
    df = synthetic.world(50, 20, RNG, quotes="boundary", side=side)
    p_win = df["q"].to_numpy() if side == "yes" else 1 - df["q"].to_numpy()
    ask = df[f"ask_{side}"].to_numpy()
    net = synthetic.taker_net(p_win, ask)
    assert net.min() >= 0 and net.max() < 200  # under $0.0002 per contract
    assert (synthetic.taker_net(p_win, ask + 1) < 0).all()


def test_an_edge_lowers_no_asks_by_exactly_its_cents_in_its_range():
    edge = synthetic.Edge(0.0, 0.15, 1.5)
    base = synthetic.world(30, 20, np.random.default_rng(1), quotes="boundary")
    edged = synthetic.world(30, 20, np.random.default_rng(1), quotes="boundary", edge=edge)
    shift = (base["ask_no"] - edged["ask_no"]).to_numpy()
    assert set(shift[base["q"] < 0.15]) == {150} and set(shift[base["q"] >= 0.15]) == {0}


@pytest.mark.parametrize("shock", [0.0, 0.5])
def test_outcomes_are_calibrated_with_one_winner_per_ladder(shock):
    df = synthetic.world(400, 25, RNG, shock=shock)
    winners = df.groupby("event")["yes"].agg(["sum", "size"])
    assert (winners.loc[winners["size"] > 1, "sum"] == 1).all()
    bins = np.digitize(df["q"], [0.1, 0.3, 0.6])
    for b in range(4):
        part = df[bins == b]
        assert part["yes"].mean() == pytest.approx(part["q"].mean(), abs=0.02)


def test_a_same_day_shock_correlates_events_within_a_day():
    def day_spread(shock):
        df = synthetic.world(600, 30, np.random.default_rng(2), bucket_share=0.0, shock=shock)
        return (df["yes"] - df["q"]).groupby(df["day"]).mean().std()

    assert day_spread(0.5) > 1.5 * day_spread(0.0)


def test_market_quotes_straddle_the_truth():
    df = synthetic.world(10, 10, RNG, quotes="market", half_spread_cents=1.0)
    mid_no = df["ask_no"] - 100
    assert np.abs(mid_no / PRICE_SCALE - (1 - df["q"])).max() <= 0.5 / PRICE_SCALE
    assert ((df["ask_yes"] + df["ask_no"]) == PRICE_SCALE + 200).all()
