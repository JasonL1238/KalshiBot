from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
from scipy import stats as scipy_stats

from kalshibot.research import calendar
from kalshibot.research.validation import gauntlet, null, stats

RNG = np.random.default_rng(3)


def test_holm_runs_over_every_place(monkeypatch):
    # at m = 12 the smallest p needs ≤ 0.05/12 ≈ 0.0042 and the next ≤ 0.05/11 ≈ 0.0045;
    # at m = 2, ≤ 0.025 and ≤ 0.05
    monkeypatch.setattr(calendar, "FORWARD", frozenset())
    gates = gauntlet.Gates(families=12)
    assert gauntlet.decide({"H1": 0.004, "H2": 0.006}, gates) == {"H1": True, "H2": False}
    assert gauntlet.decide({"H1": 0.004, "H2": 0.006}, gauntlet.Gates(families=2)) == {
        "H1": True, "H2": True}
    with pytest.raises(ValueError):
        gauntlet.decide({f"H{i}": 0.5 for i in range(13)}, gates)


def test_a_forward_family_spends_only_its_own_share_of_alpha(monkeypatch):
    # H1 confirmed forward: alone at 0.05/12; Holm over the other 11 places at 0.05 · 11/12
    monkeypatch.setattr(calendar, "FORWARD", frozenset({"H1"}))
    out = gauntlet.decide({"H1": 0.004, "H2": 0.004, "H3": 0.0045})
    assert out == {"H1": True, "H2": True, "H3": True}  # H3: 10 × 0.0045 ≤ 0.05 · 11/12
    assert gauntlet.decide({"H1": 0.0042, "H2": 0.004}) == {
        "H1": False, "H2": True}  # 0.0042 > 0.05/12
    assert gauntlet.decide({"H2": 0.004}) == {"H2": True}  # H1 pending
    monkeypatch.undo()  # the registered forward set: H1, H5 and H11 leave 9 places
    with pytest.raises(ValueError, match="places"):
        gauntlet.decide({f"H{i}": 0.5 for i in (2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13)})


def test_a_family_passes_only_if_both_tests_reject():
    v = gauntlet.Verdict("x", p_null=0.001, p_boot=0.2, p_independent=0.001, rho=0.0,
                         cross_day=0.0, widening=1.0, dsr=0.99, pbo=0.0, n_trials=3)
    assert v.p_value == 0.2


def positions(n, days, edge, rng, first_day=0):
    """Buy NO at the fair price on independent binaries where YES is `edge` less likely."""
    p_yes = rng.uniform(0.05, 0.3, n)
    yes = rng.random(n) < p_yes - edge
    paid = ((1 - p_yes) * 1_000_000).round()
    return gauntlet.Positions(net_pnl=np.where(yes, 0, 1_000_000) - paid, paid=paid, won=~yes,
                              buy_yes=np.zeros(n, dtype=bool),
                              count=np.full(n, 100), p_yes=p_yes, lo=np.zeros(n), hi=p_yes,
                              cluster=np.arange(n), day=first_day + np.arange(n) % days)


def test_evaluate_finds_a_large_edge_and_reports_diagnostics():
    days, configs, n = 120, 4, 2400
    discovery = pd.DataFrame(RNG.normal(0.01, 0.02, (days, configs)), columns=list("abcd"))
    discovery["c"] += 0.01  # the best config
    conf = positions(n, days, 0.04, RNG, first_day=days)
    daily = pd.Series(pd.Series(conf.net_pnl).groupby(conf.day).sum().to_numpy() / 1e6)
    v = gauntlet.evaluate(discovery, positions(n, days, 0.04, RNG), daily, conf, RNG,
                          gauntlet.Gates(draws=2_000))
    assert v.config == "c" and v.p_value < 0.01 and v.p_null >= v.p_independent
    assert 0 <= v.pbo <= 1 and 1 <= v.n_trials <= configs and np.isfinite(v.dsr)
    assert v.rho < 0.05 and abs(v.cross_day) < 0.3 and v.widening < 1.6  # independent


GATES = gauntlet.Gates(draws=2_000, power_worlds=200, power_resamples=500)


def test_power_is_near_alpha_without_an_edge_and_near_one_with_a_big_one():
    disc = positions(400, 40, 0, np.random.default_rng(5))
    conf = positions(1500, 60, 0, np.random.default_rng(6), first_day=40)
    days = np.arange(40, 100)
    none = gauntlet.power(disc, conf, days, 0, np.random.default_rng(7), GATES)
    big = gauntlet.power(disc, conf, days, 1000, np.random.default_rng(7), GATES)
    assert none <= 0.02 and big >= 0.95
    assert gauntlet.power(disc, conf, days, 0, np.random.default_rng(7), GATES) == none


def shocked(n, days, rho, rng, first_day=0, edge=0.0, p_yes=None):
    """Like `positions`, but clusters on a day share a Gaussian factor of strength rho, and
    a NO position wins with its break-even probability plus `edge`."""
    p_yes = rng.uniform(0.05, 0.3, n) if p_yes is None else p_yes
    day = np.arange(n) % days
    z = rng.standard_normal(days)[day]
    u = scipy_stats.norm.cdf(np.sqrt(rho) * z + np.sqrt(1 - rho) * rng.standard_normal(n))
    yes = u < p_yes - edge
    paid = ((1 - p_yes) * 1_000_000).round()
    return gauntlet.Positions(net_pnl=np.where(yes, 0, 1_000_000) - paid, paid=paid, won=~yes,
                              buy_yes=np.zeros(n, dtype=bool), count=np.full(n, 100),
                              p_yes=p_yes, lo=np.zeros(n), hi=p_yes, cluster=np.arange(n),
                              day=first_day + day)


@pytest.mark.parametrize("cross, edge", [(None, 0.055), (-0.5, 0.045)])
def test_power_carries_the_dependence_the_null_is_widened_for(monkeypatch, cross, edge):
    # 50 positions a day share a same-day shock (widening ~6, estimated on 2,000 days, so
    # the test checks power() rather than the estimate); the measured rate is how often
    # the two tests reject such worlds with the edge, against the null power() draws.
    # power() must not overstate it: drawn without the dependence it said +0.14 to +0.20
    # more over seeds. With `cross`, the estimate also puts the cross-day sum at that
    # multiple of ρ, as a 400-day sample's lower tail does when there is none: the null
    # narrows, but each day still varies as much, so the bootstrap decides; days shrunk
    # to the net factor said +0.09 to +0.15 more.
    if cross is not None:
        estimate = null.dependence

        def tilted(*args):
            rho = estimate(*args).rho
            return null.Dependence(rho, (cross * rho,))

        monkeypatch.setattr(null, "dependence", tilted)
    gates = replace(GATES, power_worlds=600)
    rng = np.random.default_rng(8)
    disc = shocked(100_000, 2000, 0.2, rng)
    days = np.arange(2000, 2060)
    template = shocked(3000, 60, 0.2, rng, first_day=2000)
    predicted = gauntlet.power(disc, template, days, round(edge * 10_000),
                               np.random.default_rng(9), gates)
    _, widen, totals = gauntlet._null(disc, template, np.random.default_rng(9), gates)
    null_net = totals * np.sqrt(widen)
    hits = []
    for _ in range(600):
        world = shocked(3000, 60, 0.2, rng, first_day=2000, edge=edge, p_yes=template.p_yes)
        daily = pd.Series(world.net_pnl).groupby(world.day).sum().reindex(days, fill_value=0)
        p = max(null.p_value(world.net_pnl.sum(), null_net),
                stats.bootstrap_p_value(daily.to_numpy(), gates.power_resamples, rng))
        hits.append(p <= gates.level)
    measured = np.mean(hits)
    assert widen > (5 if cross is None else 3) and measured - 0.15 < predicted < measured + 0.05


def test_the_alternative_keeps_each_true_probability_from_its_anchor():
    # YES buyers on a middle bucket (lo > 0), an "above X" rung (hi = 1), a standalone, and
    # a bucket whose true probability no longer fits above its start
    n, edge = 4, 300
    p = gauntlet.Positions(
        net_pnl=np.zeros(n), paid=np.array([300_000.0, 500_000, 200_000, 600_000]),
        won=np.zeros(n), buy_yes=np.ones(n, dtype=bool), count=np.full(n, 100),
        p_yes=np.array([0.25, 0.5, 0.2, 0.3]), lo=np.array([0.6, 0.5, 0.0, 0.65]),
        hi=np.array([0.85, 1.0, 0.2, 0.95]), cluster=np.arange(n), day=np.zeros(n, int))
    win, lo, hi = gauntlet._alternative(p, edge)
    assert win == pytest.approx([0.33, 0.53, 0.23, 0.63])
    assert (lo[:3] == [0.6, 0.47, 0.0]).all() and lo[3] == pytest.approx(0.37)
    sims = null.draw_outcomes(lo, hi, p.cluster, 40_000, np.random.default_rng(9))
    assert sims.mean(axis=0) == pytest.approx(win, abs=0.01)


def test_pooled_counts_each_bet_once():
    a = pd.DataFrame({"ticker": ["X", "X", "Y"], "side": ["no", "yes", "no"],
                      "priced_at": [1, 1, 1], "fill_count": [100, 100, 100]})
    b = a.assign(fill_count=200)  # another configuration, the same bets at another size
    assert len(gauntlet.pooled([a, b])) == 3


def test_evaluate_refuses_inputs_outside_the_nulls_scope():
    days = 50
    discovery = pd.DataFrame(RNG.normal(0, 0.01, (days, 2)), columns=["a", "b"])
    disc, conf = positions(500, days, 0, RNG), positions(500, days, 0, RNG, first_day=days)
    daily = pd.Series(np.zeros(days))
    with pytest.raises(ValueError):  # discovery outcomes leaking past the seam
        gauntlet.evaluate(discovery, conf, daily, conf, RNG, gauntlet.Gates(draws=50))
    overpaid = replace(conf, won=np.full(500, 1.4))  # more than $1 a contract
    seconds = replace(conf, day=conf.day * 86_400)  # not day numbers
    for bad in (overpaid, seconds):
        with pytest.raises(ValueError):
            gauntlet.evaluate(discovery, disc, daily, bad, RNG, gauntlet.Gates(draws=50))


def maker_sample(rng, n, days, first_day=0, per_contract=4):
    """Fair NO bids filled `per_contract` times on each of n contracts, at drifting prices."""
    contracts = n // per_contract
    p0 = rng.uniform(0.05, 0.3, contracts)
    p_yes = np.clip(np.repeat(p0, per_contract) + rng.normal(0, 0.02, n), 0.01, 0.5)
    yes = np.repeat(rng.random(contracts) < p0, per_contract)
    paid = ((1 - p_yes) * 1_000_000).round()
    return gauntlet.Positions(
        net_pnl=np.where(yes, 0, 1_000_000) - paid, paid=paid, won=~yes,
        buy_yes=np.zeros(n, bool), count=np.full(n, 100), p_yes=p_yes, lo=np.zeros(n),
        hi=p_yes, cluster=np.repeat(np.arange(contracts), per_contract),
        day=first_day + np.repeat(np.arange(contracts) % days, per_contract),
        maker=np.ones(n, bool), order=np.arange(n))


def test_a_takers_positions_flagged_as_not_maker_are_unchanged():
    rng = np.random.default_rng(5)
    disc, conf = positions(3000, 300, 0.0, rng), positions(600, 60, 0.02, rng, first_day=300)
    returns, daily = pd.DataFrame({"a": rng.normal(0, 1, 300)}), pd.Series(rng.normal(0, 1, 60))
    flagged = [replace(p, maker=np.zeros(len(p.day), bool)) for p in (disc, conf)]
    gates = replace(GATES, draws=500)
    plain = gauntlet.evaluate(returns, disc, daily, conf, np.random.default_rng(1), gates)
    other = gauntlet.evaluate(returns, flagged[0], daily, flagged[1],
                              np.random.default_rng(1), gates)
    assert repr(other) == repr(plain)  # pbo is NaN with one configuration


def test_a_makers_null_widens_for_fills_sharing_an_outcome():
    """Flagged as maker fills, the same positions get the covariance of fills on one
    outcome: a wider null (the jackknife SE is 0 here, without lasting dependence)."""
    rng = np.random.default_rng(6)
    disc, conf = maker_sample(rng, 4000, 200), maker_sample(rng, 800, 40, first_day=200)
    plain = [replace(p, maker=None) for p in (disc, conf)]
    _, as_taker, _ = gauntlet._null(*plain, np.random.default_rng(1), GATES)
    dep, as_maker, _ = gauntlet._null(disc, conf, np.random.default_rng(1), GATES)
    point = null.widening(dep, conf.buy_yes, conf.count, conf.p_yes, conf.lo, conf.hi,
                          conf.cluster, conf.day, conf.break_even, exact=conf.maker,
                          order=conf.order)
    assert dep.flat and point > as_taker  # the exact covariance alone widens it
    assert as_maker == pytest.approx(point)  # every replicate floors here, so the SE is 0


def test_maker_fills_must_stay_on_standalone_contracts():
    row = {"pnl": 0, "paid": 800_000, "won": 0.0, "side": "no", "fill_count": 1,
           "p_yes": 0.2, "lo": 0.0, "hi": 0.2, "cluster": 0, "day": 0, "maker": True}
    frame = pd.DataFrame([{**row, "ticker": "A-60"}, {**row, "ticker": "A-70"}])
    with pytest.raises(ValueError, match="standalone"):
        gauntlet.Positions.from_frame(frame)
    assert gauntlet.Positions.from_frame(frame.assign(maker=False)).maker.sum() == 0
    both = pd.DataFrame([{**row, "ticker": "A-60"}, {**row, "ticker": "A-60", "side": "yes"}])
    with pytest.raises(ValueError, match="one side"):  # the exact covariance needs one sign
        gauntlet.Positions.from_frame(both)


def test_a_makers_null_adds_its_jackknife_se_when_shocks_last():
    """With a lasting shock the dependence estimate moves between jackknife replicates, so
    the maker null's widening is the point widening plus their SE, strictly wider."""
    rng = np.random.default_rng(7)
    disc, conf = maker_sample(rng, 4000, 200), maker_sample(rng, 800, 40, first_day=200)
    z = np.zeros(240)
    for d in range(1, 240):  # an AR(1) day factor, φ = 0.8, shared by both windows
        z[d] = 0.8 * z[d - 1] + np.sqrt(1 - 0.8**2) * rng.standard_normal()
    for p in (disc, conf):
        contracts = p.cluster[::4]
        u = scipy_stats.norm.cdf(np.sqrt(0.5) * z[p.day[::4]]
                                 + np.sqrt(0.5) * rng.standard_normal(len(contracts)))
        p.won[:] = np.repeat(u >= p.p_yes[::4], 4)  # NO wins when the draw is above p
    dep, as_maker, _ = gauntlet._null(disc, conf, np.random.default_rng(1), GATES)
    sample = (disc.won, disc.buy_yes, disc.count, disc.p_yes, disc.lo, disc.hi, disc.cluster,
              disc.day, disc.break_even)
    scored = (conf.buy_yes, conf.count, conf.p_yes, conf.lo, conf.hi, conf.cluster, conf.day,
              conf.break_even)
    point = null.widening(dep, *scored, exact=conf.maker, order=conf.order)
    replicates = null.jackknife(*sample, GATES.jackknife_blocks, exact=disc.maker,
                                order=disc.order, flat=True)
    se = null.jackknife_se(replicates, *scored, exact=conf.maker, order=conf.order)
    assert se > 0 and as_maker == pytest.approx(point + se)


def test_repeated_methods_leave_one_decision_takers_alone():
    rng = np.random.default_rng(5)
    disc, conf = positions(2000, 200, 0.0, rng), positions(400, 40, 0.02, rng, first_day=200)
    disc, conf = (replace(p, contract=np.arange(len(p.day)), order=np.zeros(len(p.day), int))
                  for p in (disc, conf))
    returns, daily = pd.DataFrame({"a": rng.normal(0, 1, 200)}), pd.Series(rng.normal(0, 1, 40))
    gates = replace(GATES, draws=500)
    plain = gauntlet.evaluate(returns, disc, daily, conf, np.random.default_rng(1), gates)
    for method in gauntlet.REPEATED_METHODS:
        other = gauntlet.evaluate(returns, disc, daily, conf, np.random.default_rng(1),
                                  replace(gates, repeated=method))
        assert repr(other) == repr(plain)
    with pytest.raises(ValueError, match="repeated must be one of"):
        gauntlet.Gates(repeated="unknown")


def test_repeated_finds_a_contract_bought_at_two_times():
    p = replace(positions(4, 1, 0.0, np.random.default_rng(1)),
                cluster=np.array([0, 0, 1, 2]), contract=np.array([0, 0, 1, 2]),
                order=np.array([1, 2, 1, 1]), maker=np.array([False, False, False, True]))
    rep = gauntlet._repeated(p, replace(GATES, repeated="h1-D"))["repeated"]
    assert rep.rows.tolist() == [True, True, False, False] and rep.cross == "zero"
    assert gauntlet._repeated(p, GATES) == {}
