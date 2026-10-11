"""Conformance: fast end-to-end smoke tests and checks of the maker family's fills, and
reduced runs (slow) showing the gauntlet keeps its size where naive selection doesn't and
finds a large edge. The full grid is `python -m kalshibot.research.validation.conformance`."""

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
                                    conformance.MAKERS_STRESS],
                         ids=["takers", "makers", "makers under stress"])
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
    if "depths" in kwargs:  # at the boundary
        assert r["edge_per_position"] == 0 and r["coupling"] > 0
    else:
        assert math.isnan(r["coupling"])


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


def test_maker_depths_are_whole_cents():
    for depths in ((150,), (0, 100), (-100,)):
        with pytest.raises(ValueError):
            conformance._configs("no", False, depths)


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
    def positions(p_yes, break_even):  # one contract's fills, in time order
        return pd.DataFrame({
            "p_yes": p_yes, "paid": np.multiply(break_even, PRICE_SCALE * SIZE_SCALE),
            "fill_count": SIZE_SCALE, "cluster": 0,
            "priced_at": conformance.BASE + pd.to_timedelta(np.arange(len(p_yes)), unit="min")})

    assert conformance._coupling(positions([0.3], [0.3])) == pytest.approx(1)
    two = positions([0.6, 0.4], [0.5, 0.3])
    # truth: 1·0.5·0.5 + 3·0.3·0.7; the null: 0.24 + 0.24 + 2·0.4·(1 − 0.6), scaled by the
    # break-even variances over the null's, (0.25 + 0.24) / 0.48
    assert conformance._coupling(two) == pytest.approx(0.88 / (0.8 * 0.49 / 0.48))
    var_b = null._clusters(np.zeros(2, bool), np.full(2, SIZE_SCALE), two["p_yes"].to_numpy(),
                           np.zeros(2), two["p_yes"].to_numpy(), np.zeros(2, int),
                           np.zeros(2, int), np.array([0.5, 0.3]))["var_b"].sum()
    assert 0.88 / conformance._coupling(two) == pytest.approx(var_b / SIZE_SCALE**2)


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
def test_power_against_a_large_edge():
    results = conformance.simulate(100, seed=102, edge=synthetic.Edge(0.0, 0.15, 3.0))
    assert conformance.rates(results, conformance.GATES.level)["gauntlet"] >= 0.85
