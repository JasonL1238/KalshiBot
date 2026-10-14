"""Conformance: fast end-to-end smoke tests and checks of the maker and H1-shaped worlds
(their fills and repeated decisions at the boundary, and their coupling R), and reduced
runs (slow) showing the gauntlet keeps its size where naive selection doesn't and finds a
large edge. The full grid is `python -m kalshibot.research.validation.conformance`."""

import math
import warnings
from datetime import timedelta
from itertools import pairwise
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from kalshibot.kalshi.models import PRICE_SCALE, SIZE_SCALE
from kalshibot.research.backtest import fills
from kalshibot.research.validation import conformance, gauntlet, null, synthetic
from kalshibot.research.validation.gauntlet import Gates

DEPTHS = conformance.MAKERS["depths"]


def makers(days, events_per_day, seed):
    """`conformance.MAKERS`' world, one period."""
    return synthetic.world(days, events_per_day, np.random.default_rng(seed), "boundary",
                           bucket_share=0.0, sweeps=conformance.MAKERS["sweeps"],
                           sweep_scale=min(DEPTHS) / PRICE_SCALE)


@pytest.mark.parametrize("kwargs", [{"favorites": True}, conformance.MAKERS,
                                    conformance.MAKERS_STRESS, conformance.H1_SHAPED],
                         ids=["takers", "makers", "makers under stress", "H1-shaped"])
def test_a_small_world_yields_every_procedures_decision(kwargs):
    """Fast: the grid's machinery runs end to end and reports every procedure."""
    results = [conformance.one_world(np.random.default_rng(i), discovery_days=30,
                                     confirmation_days=20, events_per_day=10,
                                     gates=Gates(draws=200, power_worlds=20, power_resamples=100),
                                     predict=True, **kwargs) for i in range(2)]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        r = conformance.rates(results)
    assert set(conformance.PROCEDURES) <= set(r)
    assert all(0 <= s["power_predicted"] <= 1 for s in results)
    if "depths" in kwargs:  # at the boundary, a contract to a cluster: R̄ = R
        assert r["edge_per_position"] == 0 and r["coupling"] == r["r_bar"] > 0
    else:  # R and R̄ wherever positions repeat
        for key in ("coupling", "r_bar"):
            assert (r[key] > 0) if "snapshots" in kwargs else math.isnan(r[key])


def test_sweep_fills_are_tier_ones():
    """Each depth's fills are Tier 1's: one-contract bids at the configuration's price,
    decided at each sweep's minute and resting 59 s, against one 20-contract print per
    sweep."""
    df = makers(3, 3, seed=8)
    decided = (conformance.BASE + pd.to_timedelta(df["day"], unit="D")
               + pd.to_timedelta(df["minute"], unit="min"))
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.register("tape", pd.DataFrame({
        "ticker": df["ticker"], "created_time": decided + pd.Timedelta(seconds=30),
        "yes_price": PRICE_SCALE - np.floor(df["bottom"]).astype("int64"),
        "count": 20 * SIZE_SCALE, "taker_side": "yes"}))  # a YES buyer sells NO down
    for config in conformance._configs("no", False, DEPTHS):
        con.register("bids", pd.DataFrame({
            "intent_id": np.arange(len(df)), "ticker": df["ticker"], "side": "no",
            "decision_time": decided, "count": SIZE_SCALE,
            "limit": ((1 - df["q"]) * PRICE_SCALE - config[2]) // 100 * 100}))
        tier1 = fills.maker_fills(con, "bids", "tape", horizon=timedelta(seconds=59)).df()
        ours = conformance._positions(df, config)
        price = ours["paid"] / SIZE_SCALE  # pegged on the cent grid, `depth` below the truth
        assert len(ours) > 0 and (price % 100 == 0).all()
        assert (price <= (1 - ours["q"]) * PRICE_SCALE - config[2]).all()
        assert (tier1.sort_values("intent_id")[["ticker", "fill_time", "fill_price",
                                                "fill_count"]].values.tolist()
                == ours.assign(price=price)[["ticker", "fill_time", "price",
                                             "fill_count"]].values.tolist())


@pytest.mark.parametrize("settings", [conformance.MAKERS, conformance.MAKERS_STRESS],
                         ids=["makers", "makers under stress"])
def test_maker_worlds_put_every_depth_at_the_boundary(settings):
    """`one_world`'s maker world: each depth's fills average zero net edge (within 3
    standard errors, contracts independent), which a grossly wrong sweep scale (none, or
    not the shallowest depth's) breaks."""
    df = conformance._world(np.random.default_rng(10), 1600, 400, 20, "no", "boundary",
                            **settings)
    for config in conformance._configs("no", False, settings["depths"]):
        pos = conformance._positions(df, config)
        by_contract = (pos["pnl"] - pos["expected"]).groupby(pos["ticker"]).sum()
        se = by_contract.std() * np.sqrt(len(by_contract)) / len(pos)
        assert abs(by_contract.sum() / len(pos)) <= 3 * se


def test_a_maker_family_needs_a_sweep_world_in_both_periods():
    for settings in ({"depths": DEPTHS}, {"bucket_share": 0.0, "sweeps": 16},
                     {**conformance.MAKERS, "confirmation": {"sweeps": 0}}):
        with pytest.raises(ValueError):
            conformance._world(np.random.default_rng(0), 2, 1, 2, "no", "boundary", **settings)


def test_a_maker_worlds_sweeps_reach_its_shallowest_bid_in_both_periods(monkeypatch):
    scales, world = [], synthetic.world
    monkeypatch.setattr(synthetic, "world",
                        lambda *a, **kw: scales.append(kw["sweep_scale"]) or world(*a, **kw))
    conformance._world(np.random.default_rng(0), 2, 1, 2, "no", "boundary",
                       **conformance.MAKERS_STRESS, confirmation={"sweep_scale": 0.05})
    assert scales == [min(conformance.MAKERS_STRESS["depths"]) / PRICE_SCALE] * 2


def test_configurations_are_whole_cents_or_distinct_asks():
    for depths, thresholds in (((150,), None), ((0, 100), None), ((-100,), None),
                               ((500,), (2000,)), (None, (0,)), (None, (10_000,)),
                               (None, (600, 600))):
        with pytest.raises(ValueError):
            conformance._configs("no", False, depths, thresholds)
    assert conformance._configs("no", False, None, (2000,)) == [("no ≤ 2000", "no", 2000)]


def test_a_maker_edge_cuts_each_fill_by_its_expected_net():
    """Cheaper fills on the same paths: each fill's PnL rises by exactly the edge
    `expected` says it has."""
    def world(**edge):
        return conformance._world(np.random.default_rng(11), 20, 5, 10, "no", "boundary",
                                  **conformance.MAKERS, **edge)

    base, edged = world(), world(edge=synthetic.Edge(0.0, 1.0, 3.5))
    for config in conformance._configs("no", False, DEPTHS):
        before, after = conformance._positions(base, config), conformance._positions(edged, config)
        assert len(before) > 0 and before.index.equals(after.index)
        assert (before["expected"] == 0).all() and (after["expected"] > 0).all()
        assert ((after["pnl"] - before["pnl"]) == after["expected"]).all()


def test_fills_follow_adverse_sweeps_and_share_their_contracts_outcome():
    df = makers(2000, 20, seed=4)
    assert (df.groupby("ticker")["yes"].nunique() == 1).all()
    bought = conformance._positions(df, ("", "no", min(DEPTHS)))
    mids = bought.groupby("ticker")["mid"].agg(["size", "min", "max"])
    assert ((mids["size"] >= 2) & (mids["max"] - mids["min"] >= 100)).mean() >= 0.25
    assert bought["won"].mean() <= (1 - bought["mid"] / PRICE_SCALE).mean() - 0.04


def test_maker_configurations_nest_and_the_dependence_sample_counts_each_sweep_once():
    df = makers(20, 10, seed=9)
    each = [conformance._positions(df, c) for c in conformance._configs("no", False, DEPTHS)]
    assert len(each[-1]) > 0
    assert all(set(deeper.index) < set(shallower.index) for shallower, deeper in pairwise(each))
    pd.testing.assert_frame_equal(gauntlet.pooled(each), each[0].reset_index(drop=True))


def test_coupling_is_exact_by_hand():
    def coupling(*rows, buckets=()):
        """(R, R̄); a row: ticker, cluster (an exclusive event if in `buckets`), hour, the
        null's lo and hi, the break-even if bought, then optionally the truth (if not
        hi − lo), the side bought (NO) and contracts (1)."""
        quotes = pd.DataFrame([row + (None, "no", 1)[len(row) - 6:] for row in rows],
                              columns=["ticker", "cluster", "hour", "lo", "hi", "be", "q",
                                       "side", "contracts"])
        quotes = quotes.assign(
            event=quotes["cluster"], mutually_exclusive=quotes["cluster"].isin(buckets),
            position=0.0, fill_count=quotes["contracts"] * SIZE_SCALE, day=0,
            p_yes=quotes["hi"] - quotes["lo"],
            q=quotes["q"].astype(float).fillna(quotes["hi"] - quotes["lo"]),
            paid=quotes["be"] * PRICE_SCALE * quotes["contracts"] * SIZE_SCALE,
            priced_at=conformance.BASE + pd.to_timedelta(quotes["hour"], unit="h"))
        return conformance._coupling(quotes.dropna(subset=["be"]), quotes)

    assert coupling(("A", 0, 0, 0.0, 0.3, 0.7)) == pytest.approx((1, 1))
    # one contract, nested: 1·0.5·0.5 + 3·0.3·0.7 over the null's 0.24 + 0.24 + 2·0.4·0.4,
    # scaled by the break-even variances over its own, (0.25 + 0.24) / 0.48
    nested = [("A", 0, 0, 0.0, 0.6, 0.5), ("A", 0, 1, 0.0, 0.4, 0.3)]
    assert coupling(*nested) == pytest.approx((0.88 / (0.8 * 0.49 / 0.48),) * 2)
    # one bucket sliding from [0.2, 0.5) to [0.35, 0.55): 0.21 + 3·0.1875 over the null's
    # 0.21 + 0.16 + 2·(0.15 − 0.3·0.2), scaled by (0.21 + 0.1875) / 0.37
    sliding = [("B", 1, 0, 0.2, 0.5, 0.7), ("B", 1, 1, 0.35, 0.55, 0.75)]
    assert coupling(*sliding) == pytest.approx((0.7725 / (0.55 * 0.3975 / 0.37),) * 2)
    # two buckets of one event, X bought at hour 0 and Y at 1: R 0.21 + 0.24 + 2·(0 −
    # 0.25·0.4), exclusive at hour 1, R̄ the two buckets' term as 0; the null
    # 0.45 + 2·(0.05 − 0.3·0.4)
    buckets = [("X", 2, 0, 0.0, 0.3, 0.7), ("Y", 2, 0, 0.3, 0.7, None),
               ("X", 2, 1, 0.0, 0.25, None), ("Y", 2, 1, 0.25, 0.65, 0.6)]
    assert coupling(*buckets, buckets=[2]) == pytest.approx((0.25 / 0.31, 0.45 / 0.31))
    # two strikes of one ladder, K1 bought at hour 0 and K2 at 1, truths at hour 1 below
    # the null's: R 0.24 + 0.21 + 2·(0.4 − 0.4·0.6), R̄ the same from prices,
    # 0.45 + 2·(0.45 − 0.45·0.7), here below R; the null 0.45 + 2·(0.4 − 0.4·0.7)
    ladder = [("K1", 3, 0, 0.0, 0.4, 0.6), ("K2", 3, 0, 0.0, 0.75, None),
              ("K1", 3, 1, 0.0, 0.45, None, 0.4), ("K2", 3, 1, 0.0, 0.7, 0.3, 0.6)]
    assert coupling(*ladder) == pytest.approx((0.77 / 0.69, 0.72 / 0.69))
    # the same ladder as L1, L2: L1 bought as YES (2 contracts, break-even 0.4), L2 as NO
    # (3), signed +2 and −3: R 4·0.24 + 9·0.21 − 2·6·0.16, R̄ the same with 0.135; the
    # null 2.85 − 2·6·0.12
    mixed = [("L1", 5, 0, 0.0, 0.4, 0.4, None, "yes", 2), ("L2", 5, 0, 0.0, 0.75, None),
             ("L1", 5, 1, 0.0, 0.45, None, 0.4), ("L2", 5, 1, 0.0, 0.7, 0.3, 0.6, "no", 3)]
    assert coupling(*mixed) == pytest.approx((0.93 / 1.41, 1.23 / 1.41))
    # one contract bought as NO (2 contracts), YES (1), then NO (1), signed −2, +1, −1:
    # 4·0.24 + 0.25 + 0.21 + 2·(−2·0.25 + 2·0.21 − 0.21) over the null's, nested,
    # 1.42 + 2·(−2·0.2 + 2·0.18 − 0.15)
    sides = [("M", 4, 0, 0.0, 0.4, 0.6, None, "no", 2), ("M", 4, 1, 0.0, 0.5, 0.5, None, "yes", 1),
             ("M", 4, 2, 0.0, 0.3, 0.7, None, "no", 1)]
    assert coupling(*sides) == pytest.approx((0.84 / 1.04,) * 2)
    # clusters add
    null_var = 0.8 * 0.49 / 0.48 + 0.55 * 0.3975 / 0.37 + 0.31 + 0.69
    assert coupling(*nested, *sliding, *buckets, *ladder, buckets=[2]) == pytest.approx(
        ((0.88 + 0.7725 + 0.25 + 0.77) / null_var, (0.88 + 0.7725 + 0.45 + 0.72) / null_var))


def test_r_bar_bounds_r_world_by_world_in_the_strike_ladder_row():
    """R̄ ≥ R isn't guaranteed on strikes, so the ladder row's worlds check it, on the
    confirmation positions `one_world` scores."""
    settings = next(kwargs for label, _, kwargs in conformance.GRID
                    if label.startswith("H1-shaped") and "strike ladders" in label)
    world = {k: v for k, v in settings.items() if k != "thresholds"}
    config = conformance._configs("no", False, None, settings["thresholds"])[0]
    for seed in range(3):
        df = conformance._world(np.random.default_rng(seed), 400, 100, 20, "no", "boundary",
                                **world)
        conf = null.inputs(df.assign(priced_at=conformance._priced_at(df)),
                           "multiplicative").query("day >= 400")
        r, r_bar = conformance._coupling(conformance._positions(conf, config), conf)
        assert r_bar >= r > 0


def test_romano_wolf_in_chunks_is_one_draw():
    df = conformance._world(np.random.default_rng(15), 6, 3, 10, "no", "boundary")
    df = null.inputs(df.assign(priced_at=conformance._priced_at(df)), "multiplicative")
    conf, configs = df[df["day"] >= 6], conformance._configs("no", True)
    runs = []
    for chunk in (7, 60):
        rng = np.random.default_rng(16)
        runs.append((conformance._romano_wolf(conf, configs, rng, 60, chunk),
                     rng.bit_generator.state))
    assert runs[0] == runs[1]


def h1_shaped(**settings):
    """`conformance.H1_SHAPED`'s world settings, and its thresholds."""
    world = {**conformance.H1_SHAPED, **settings}
    return world, world.pop("thresholds")


@pytest.mark.parametrize("settings", [{}, {"bucket_share": 0.0}, {"strike_share": 1.0}],
                         ids=["buckets", "binaries", "strike ladders"])
def test_h1_shaped_decisions_are_at_the_boundary(settings):
    """Each threshold's daily decisions win as often as their truths say (within 3
    standard errors, by event), so their net edge is the ask grid's, under $0.0002."""
    world, thresholds = h1_shaped(**settings)
    df = conformance._world(np.random.default_rng(13), 320, 80, 20, "no", "boundary", **world)
    for config in conformance._configs("no", False, None, thresholds):
        pos = conformance._positions(df, config)
        by_event = (pos["pnl"] - pos["expected"]).groupby(pos["event"]).sum()
        se = by_event.std() * np.sqrt(len(by_event)) / len(pos)
        assert abs(by_event.sum() / len(pos)) <= 3 * se
        assert 0 <= pos["expected"].min() and pos["expected"].max() < 200


def test_h1_shaped_contracts_are_bought_again_and_buckets_slide():
    """A contract is bought on many snapshots, each priced on its own, and the null lays a
    bucket's interval out afresh at each, de-vigging the event's snapshot, so it slides."""
    df = conformance._world(np.random.default_rng(14), 8, 2, 20, "no", "boundary",
                            **h1_shaped()[0])
    df = null.inputs(df.assign(priced_at=conformance._priced_at(df)), "multiplicative")
    devigged = df[df["mutually_exclusive"]].groupby(["event", "priced_at"])["p_yes"].sum()
    assert np.abs(devigged - 1).max() < 1e-9
    truth = conformance._true_intervals(df)  # the truths' own layout tiles the draw too
    tiled = truth[truth["mutually_exclusive"]].groupby(["event", "priced_at"])["hi"].max()
    assert np.abs(tiled - 1).max() < 1e-9
    assert np.abs(truth["hi"] - truth["lo"] - truth["q"]).max() < 1e-12
    pos = conformance._positions(df, ("", "no", max(conformance.THRESHOLDS)))
    assert pos.groupby("ticker")["priced_at"].nunique().equals(pos.groupby("ticker").size())
    assert (pos.groupby("ticker").size() >= 2).mean() > 0.5
    assert (pos[pos["mutually_exclusive"]].groupby("ticker")["lo"].nunique() > 1).mean() > 0.5


def test_only_keeps_each_rows_grid_seed(monkeypatch):
    seeds = []
    monkeypatch.setattr(conformance, "simulate", lambda n, seed, **_: seeds.append(seed) or [])
    labels = [row[0] for row in conformance._rows(conformance.GRID, 0.01, only="makers:")]
    grid = [(i, row[0]) for i, row in enumerate(conformance.GRID) if row[0].startswith("makers:")]
    assert grid and list(zip(seeds, labels, strict=True)) == grid


def test_backtesting_doc_shows_the_reports_tables():
    doc = (Path(__file__).parents[1] / "docs" / "BACKTESTING.md").read_text()
    assert conformance.SIZE_HEADER in doc and conformance.POWER_HEADER in doc


@pytest.mark.slow
def test_size_at_the_boundary_null():
    worlds, alpha = 300, 0.05
    r = conformance.rates(conformance.simulate(worlds, seed=101), alpha)
    slack = 3 * np.sqrt(alpha * (1 - alpha) / worlds)
    assert r["gauntlet"] <= alpha + slack
    assert r["naive in-sample"] > alpha + slack  # selection on the same data overstates


@pytest.mark.slow
def test_size_at_the_maker_boundary():
    worlds, alpha = 300, 0.05
    r = conformance.rates(conformance.simulate(worlds, seed=103, **conformance.MAKERS), alpha)
    assert r["gauntlet"] <= alpha + 3 * np.sqrt(alpha * (1 - alpha) / worlds)


@pytest.mark.slow
def test_size_at_the_h1_shaped_boundary():
    worlds, alpha = 300, 0.05
    r = conformance.rates(conformance.simulate(worlds, seed=104, **conformance.H1_COUPLED),
                          alpha)
    assert r["gauntlet"] <= alpha + 3 * np.sqrt(alpha * (1 - alpha) / worlds)


@pytest.mark.slow
def test_power_against_a_large_edge():
    results = conformance.simulate(100, seed=102, edge=synthetic.Edge(0.0, 0.15, 3.0))
    assert conformance.rates(results, conformance.GATES.level)["gauntlet"] >= 0.85
