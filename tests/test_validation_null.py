import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from kalshibot.research.validation import null

RNG = np.random.default_rng(11)


@pytest.mark.parametrize("method", ["multiplicative", "shin"])
def test_devig_sums_to_one_and_keeps_order(method):
    prices = np.array([6000, 3000, 1500, 500])  # sums to $1.10
    q = null.devig(prices, method)
    assert q.sum() == pytest.approx(1) and np.all(np.diff(q) < 0)


def test_shin_takes_more_margin_from_longshots_than_multiplicative():
    prices = np.array([8000, 1500, 600])
    mult, shin = (null.devig(prices, m) for m in ("multiplicative", "shin"))
    assert shin[-1] < mult[-1]


def test_buckets_have_exactly_one_winner_with_right_marginals():
    q = np.array([0.1, 0.25, 0.4, 0.25])
    lo, hi = null.bucket_intervals(q)
    yes = null.draw_outcomes(lo, hi, np.zeros(4, dtype=int), 40_000, RNG)
    assert np.all(yes.sum(axis=1) == 1)
    assert yes.mean(axis=0) == pytest.approx(q, abs=0.01)


def test_ladders_settle_monotonically():
    q_above = np.array([0.9, 0.6, 0.3, 0.05])  # "above X" for rising X
    yes = null.draw_outcomes(np.zeros(4), q_above, np.zeros(4, dtype=int), 20_000, RNG)
    assert np.all(np.diff(yes.astype(int), axis=1) <= 0)  # never YES above a NO strike
    assert yes.mean(axis=0) == pytest.approx(q_above, abs=0.01)


def test_independent_clusters_are_independent():
    yes = null.draw_outcomes(np.zeros(2), np.full(2, 0.5), np.array([0, 1]), 40_000, RNG)
    assert np.corrcoef(yes.T)[0, 1] == pytest.approx(0, abs=0.02)


def test_p_values_are_uniform_when_prices_are_right():
    """A strategy with zero edge after costs is rejected at about the test's level."""
    n, draws = 300, 2_000
    q = RNG.uniform(0.05, 0.95, n)
    buy_yes, count = RNG.integers(0, 2, n).astype(bool), np.full(n, 100)
    pvals = []
    for _ in range(300):
        truth = RNG.random(n) < q  # outcomes really are calibrated
        won = np.where(buy_yes, truth, ~truth)
        observed = ((won - np.where(buy_yes, q, 1 - q)) * count * 10_000).sum()  # zero edge
        sims = null.draw_outcomes(np.zeros(n), q, np.arange(n), draws, RNG)
        pvals.append(null.p_value(observed, null.recentered_pnl(sims, buy_yes, count, q)))
    assert 0.02 < np.mean(np.array(pvals) < 0.05) < 0.09


def surprises(days, per_day, rho, rng, phi=0.0, buy_yes=None):
    """Standalone longshots (NO, or YES where `buy_yes`); a same-day factor of strength
    rho, AR(1) across days with persistence phi."""
    n = days * per_day
    q = rng.uniform(0.03, 0.2, n)
    day = np.arange(n) // per_day
    z = rng.standard_normal(days)
    for d in range(1, days):
        z[d] = phi * z[d - 1] + np.sqrt(1 - phi**2) * z[d]
    u = norm.cdf(np.sqrt(rho) * z[day] + np.sqrt(1 - rho) * rng.standard_normal(n))
    yes = u < q
    buy = np.zeros(n, dtype=bool) if buy_yes is None else buy_yes
    return dict(won=np.where(buy, yes, ~yes), buy_yes=buy, count=np.full(n, 100), p_yes=q,
                lo=np.zeros(n), hi=q, cluster=np.arange(n), day=day,
                break_even=np.where(buy, q, 1 - q))


def test_dependence_is_near_none_for_independent_days_and_clusters():
    deps = [null.dependence(**surprises(150, 20, 0.0, RNG)) for _ in range(30)]
    assert np.median([d.rho for d in deps]) < 0.01 and max(d.rho for d in deps) < 0.05
    assert abs(np.median([d.cross_day for d in deps])) < 0.1


def test_rho_is_a_correlation_the_same_at_any_density():
    sparse = np.median([null.dependence(**surprises(300, 10, 0.5, RNG)).rho for _ in range(10)])
    dense = np.median([null.dependence(**surprises(300, 40, 0.5, RNG)).rho for _ in range(10)])
    assert 0.05 < sparse < 1 and dense == pytest.approx(sparse, rel=0.35)


def test_rho_aligns_yes_and_no_positions():
    # a shock that makes YES likelier helps YES holders and hurts NO holders: in the YES
    # direction they still move together, so mixing sides doesn't hide the dependence
    mixed = RNG.random(300 * 20) < 0.5
    one_side = np.median([null.dependence(**surprises(300, 20, 0.5, RNG)).rho
                          for _ in range(8)])
    both = np.median([null.dependence(**surprises(300, 20, 0.5, RNG, buy_yes=mixed)).rho
                      for _ in range(8)])
    assert both == pytest.approx(one_side, rel=0.35)


def test_cross_day_correlation_measures_persistence_on_any_side():
    calm = np.median([null.dependence(**surprises(400, 20, 0.5, RNG)).cross_day
                      for _ in range(8)])
    lasting = [null.dependence(**surprises(400, 20, 0.5, RNG, phi=0.8)) for _ in range(8)]
    assert np.median([d.cross_day for d in lasting]) > calm + 0.3
    assert np.median([d.cross[0] for d in lasting]) > 0.05  # clusters a day apart
    # YES and NO holders move apart, but together in the YES direction: no cancelling
    mixed = RNG.random(400 * 20) < 0.5
    both = np.median([null.dependence(**surprises(400, 20, 0.5, RNG, phi=0.8,
                                                  buy_yes=mixed)).cross_day for _ in range(8)])
    assert both > calm + 0.3


def test_dependence_counts_repeats_within_a_cluster_as_correlated():
    # the same NO bought twice in one cluster is one bet at double size, not comovement
    one = surprises(150, 20, 0.0, RNG)
    twice = {k: np.concatenate([v, v]) for k, v in one.items()}
    assert null.dependence(**twice).rho == pytest.approx(null.dependence(**one).rho)


def test_a_cluster_counts_on_the_day_its_last_position_settles():
    # a ladder whose strikes settle on different days (a season's win totals)
    one = {k: v[:3] for k, v in surprises(1, 3, 0.0, RNG).items() if k != "won"}
    days = null._clusters(**{**one, "cluster": np.zeros(3, dtype=int),
                             "day": np.array([3, 5, 4])})["day"]
    assert days.tolist() == [5]


def test_widening_grows_with_density_dependence_and_break_even_variance():
    base = surprises(100, 10, 0.0, RNG)
    args = {k: v for k, v in base.items() if k != "won"}
    none = null.Dependence(0.0, ())
    assert null.widening(none, **args) == pytest.approx(1)
    assert null.widening(null.Dependence(0.1, ()), **args) > 1.5
    assert null.widening(null.Dependence(0.0, (0.05, 0.03)), **args) > 1.5  # across days
    dense = {k: v for k, v in surprises(100, 40, 0.0, RNG).items() if k != "won"}
    for dep in (null.Dependence(0.1, ()), null.Dependence(0.0, (0.05,))):
        assert null.widening(dep, **dense) > null.widening(dep, **args)  # more pairs
    # buying a longshot (YES at 10%) at a break-even of 12% is more variable than the null
    yes = {**args, "buy_yes": np.ones(1000, dtype=bool), "p_yes": np.full(1000, 0.1),
           "hi": np.full(1000, 0.1), "break_even": np.full(1000, 0.12)}
    assert null.widening(none, **yes) == pytest.approx(0.12 * 0.88 / (0.1 * 0.9))
    # YES and NO holders that offset never narrow the null
    hedged = {**args, "buy_yes": np.arange(1000) % 2 == 0}
    assert null.widening(null.Dependence(0.5, (0.3,)), **hedged) >= 1


def quotes(**columns):
    n = len(columns["ticker"])
    return pd.DataFrame({"mutually_exclusive": False, "position": 0.0, "ladder": None,
                         "below": False, **columns}, range(n))


def test_inputs_devig_only_exclusive_events_within_each_snapshot():
    out = null.inputs(quotes(
        event=["L", "L", "L", "B", "S", "S", "M", "M"],
        ticker=["L-1", "L-2", "L-3", "B-X", "S-1", "S-2", "M-X", "M-X"],
        mutually_exclusive=[True, True, True, False, False, False, False, False],
        position=[1.0, 2.0, 3.0, 0, 0, 0, 0, 0], ladder=[None] * 4 + [""] * 2 + [None] * 2,
        below=[False] * 4 + [True] * 2 + [False] * 2, decision_time=[0, 0, 0, 0, 0, 0, 0, 1],
        mid=[6000, 3000, 2000, 2500, 7000, 3000, 3000, 3500]), "multiplicative")
    buckets = out[out.event == "L"]
    assert buckets["p_yes"].sum() == pytest.approx(1) and buckets["cluster"].nunique() == 1
    assert buckets["hi"].iloc[-1] == 1
    # a binary, "below X" strikes, and one market quoted twice: nested [0, p)
    rest = out[out.event != "L"]
    assert (rest["lo"] == 0).all() and rest["hi"].tolist() == [0.25, 0.7, 0.3, 0.3, 0.35]
    assert out[out.event == "M"]["cluster"].nunique() == 1
    assert out[out.event == "S"]["cluster"].nunique() == 1  # one ladder, one draw


def test_a_bucket_keeps_its_interval_while_prices_hold():
    # rows arrive in a different order at each snapshot; the layout must not follow them
    out = null.inputs(quotes(
        event="E", mutually_exclusive=True, ticker=["E-B9", "E-B10", "E-B11"] * 2,
        position=[9.0, 10.0, 11.0] * 2, decision_time=[0, 0, 0, 1, 1, 1],
        mid=[5000, 3000, 2000] * 2).iloc[[0, 1, 2, 5, 3, 4]], "multiplicative")
    out = out.set_index(["decision_time", "ticker"])
    assert out.loc[0, ["lo", "hi"]].equals(out.loc[1, ["lo", "hi"]].loc[["E-B9", "E-B10",
                                                                         "E-B11"]])
    assert out.loc[0, "lo"].tolist() == [0.0, 0.5, 0.8]  # by strike, not "B10" < "B9"


def test_ladders_share_a_draw_from_their_end_and_others_get_their_own():
    out = null.inputs(quotes(
        event="A", ticker=["A-1", "A-2", "A-3", "A-X", "A-Y"],
        ladder=["", "", "P", None, None], below=[False, True, False, False, False],
        mid=[7000, 2000, 4000, 4000, 4000]), "multiplicative").set_index("ticker")
    assert out.loc["A-1", ["lo", "hi"]].tolist() == pytest.approx([0.3, 1.0])  # above X
    assert out.loc["A-2", ["lo", "hi"]].tolist() == pytest.approx([0.0, 0.2])  # below X
    assert out.loc["A-1", "cluster"] == out.loc["A-2", "cluster"]
    assert out["cluster"][["A-1", "A-3", "A-X", "A-Y"]].nunique() == 4


def test_simulated_totals_match_one_big_draw():
    n = 50
    args = dict(lo=np.zeros(n), hi=np.full(n, 0.3), cluster=np.arange(n) % 7,
                buy_yes=np.arange(n) % 2 == 0, count=np.full(n, 100), p_yes=np.full(n, 0.3))
    chunked = null.simulate_totals(**args, n_draws=2_500, rng=np.random.default_rng(1),
                                   chunk=1_000)
    yes = null.draw_outcomes(args["lo"], args["hi"], args["cluster"], 2_500,
                             np.random.default_rng(1))
    whole = null.recentered_pnl(yes, args["buy_yes"], args["count"], args["p_yes"])
    assert np.allclose(chunked, whole)
