import numpy as np
import pandas as pd
import pytest
from scipy.integrate import quad

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


def test_taker_worlds_draw_as_before():
    """Aggregates recorded before sweeps were added: GRID's rows keep their draws."""
    for kwargs, totals in [({}, (1765, 443, 13408013)),
                           ({"quotes": "boundary", "shock": 0.5, "persistence": 0.5,
                             "strike_share": 0.5, "side": "yes"}, (1765, 600, 11666200))]:
        df = synthetic.world(30, 20, np.random.default_rng(7), **kwargs)
        assert (len(df), df["yes"].sum(), df["ask_no"].sum()) == totals


def test_sweep_worlds_are_boundary_binaries():
    for kwargs in ({"bucket_share": 0.5}, {"bucket_share": 0.0, "quotes": "market"}):
        with pytest.raises(ValueError):
            synthetic.world(5, 5, RNG, **{"quotes": "boundary", "sweeps": 4, **kwargs})
    df = synthetic.world(5, 5, RNG, quotes="boundary")
    assert (df["mid"] == (df["ask_yes"] + PRICE_SCALE - df["ask_no"]) / 2).all()


@pytest.mark.parametrize("scale", [0.01, 0.05])
@pytest.mark.parametrize("p", [0.08, 0.3, 0.5, 0.75, 0.97])
def test_a_bid_at_least_the_scale_deep_breaks_even_exactly(p, scale):
    """E[price after | bottom < b] = b for cent bids b from 1¢ to p − scale, by quadrature
    of `_sweep` over both its uniforms; at p − scale every sweep fills, so there it is
    E[price after] = p − scale. The breakpoints only guide the integrator (where the book
    empties, where sweeps stop reaching b, where noise ends)."""
    tol = {"epsabs": 1e-13, "epsrel": 1e-13, "limit": 200}

    def expect(b, price):  # E[(price after, or 1) · 1{bottom < b}]
        def at(v, w):
            bottom, after = synthetic._sweep(p, scale, v, w)
            return float((after if price else 1.0) * (bottom < b))

        reach = (scale / (p - b)) ** 2
        breaks = [(scale / p) ** 2] + ([reach] if reach < 1 - 1e-12 else [])
        return quad(lambda v: quad(lambda w: at(v, w), 0, 1, points=[synthetic.NOISE],
                                   **tol)[0], 0, 1, points=breaks, **tol)[0]

    top = round((p - scale) * 100)
    for b in np.unique(np.linspace(1, top, 6).round()) / 100:
        assert expect(b, True) / expect(b, False) == pytest.approx(b, abs=1e-9)
    assert expect(top / 100, False) == pytest.approx(1, abs=1e-12)


@pytest.mark.parametrize("beta", [0.05, 0.1, 0.5, 0.9])
def test_news_is_a_martingale(beta):
    """Averaged over its uniform, news leaves q exactly where it was, and it moves it."""
    for q in (0.0, 0.01, 0.2, 0.5, 0.77, 0.99, 1.0):
        turn = (1 + beta * (2 * q - 1)) / 2  # where up turns to down: a hint for quad
        mean = quad(lambda u, q=q: float(synthetic._news(q, beta, u)), 0, 1, points=[turn])[0]
        assert mean == pytest.approx(q, abs=1e-12)
    assert synthetic._news(0.3, beta, 0.0) > 0.3 > synthetic._news(0.3, beta, 0.999)


@pytest.mark.parametrize("news", [0.0, 0.1])
@pytest.mark.parametrize("shock", [0.0, 0.5])
def test_sweep_paths_are_calibrated(shock, news):
    """A contract settles YES with its truth before a sweep: the paths are martingales and
    the outcome is drawn from where they end. (One side's sweeps alone are adverse: a NO
    sweep raises YES's truth by the scale on average.)"""
    df = pd.concat([synthetic.world(400, 20, np.random.default_rng(3), "boundary",
                                    bucket_share=0.0, sweeps=16, shock=shock, news=news,
                                    side=side)
                    for side in ("no", "yes")])  # the same paths: every sweep
    bins = np.digitize(df["q"], [0.1, 0.3, 0.6])
    for b in range(4):
        part = df[bins == b]
        assert part["yes"].mean() == pytest.approx(part["q"].mean(), abs=0.03)


def test_a_maker_edge_only_cuts_fill_prices():
    def world(**edge):
        return synthetic.world(30, 20, np.random.default_rng(6), "boundary", bucket_share=0.0,
                               sweeps=16, **edge)

    base, edged = world(), world(edge=synthetic.Edge(0.0, 1.0, 2))
    pd.testing.assert_frame_equal(base.drop(columns="extra"), edged.drop(columns="extra"))
    assert (base["extra"] == 0).all() and (edged["extra"] == 200).all()
