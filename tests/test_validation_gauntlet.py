from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from kalshibot.research.validation import gauntlet

RNG = np.random.default_rng(3)


def test_holm_runs_over_every_preregistered_family():
    # at m = 12 the smallest p needs ≤ 0.05/12 ≈ 0.0042 and the next ≤ 0.05/11 ≈ 0.0045;
    # at m = 2, ≤ 0.025 and ≤ 0.05
    gates = gauntlet.Gates(families=12)
    assert gauntlet.decide({"H1": 0.004, "H2": 0.006}, gates) == {"H1": True, "H2": False}
    assert gauntlet.decide({"H1": 0.004, "H2": 0.006}, gauntlet.Gates(families=2)) == {
        "H1": True, "H2": True}
    with pytest.raises(ValueError):
        gauntlet.decide({f"H{i}": 0.5 for i in range(13)}, gates)


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


def test_evaluate_refuses_inputs_outside_the_nulls_scope():
    days = 50
    discovery = pd.DataFrame(RNG.normal(0, 0.01, (days, 2)), columns=["a", "b"])
    disc, conf = positions(500, days, 0, RNG), positions(500, days, 0, RNG, first_day=days)
    daily = pd.Series(np.zeros(days))
    with pytest.raises(ValueError):  # discovery outcomes leaking past the seam
        gauntlet.evaluate(discovery, conf, daily, conf, RNG, gauntlet.Gates(draws=50))
    scalar = replace(conf, won=np.full(500, 0.4))  # not a binary payoff
    seconds = replace(conf, day=conf.day * 86_400)  # not day numbers
    for bad in (scalar, seconds):
        with pytest.raises(ValueError):
            gauntlet.evaluate(discovery, disc, daily, bad, RNG, gauntlet.Gates(draws=50))
