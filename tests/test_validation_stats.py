"""Calibration checks: under a true null, each statistic must reject at about its level."""

import numpy as np
import pytest

from kalshibot.research.validation import stats

RNG = np.random.default_rng(7)


def test_psr_is_calibrated_under_the_null():
    hits = [stats.psr(RNG.normal(0, 1, 250)) > 0.95 for _ in range(2000)]
    assert 0.03 < np.mean(hits) < 0.07


def test_psr_detects_a_real_edge():
    assert stats.psr(RNG.normal(0.2, 1, 500)) > 0.99


def test_selecting_the_best_trial_breaks_psr_but_not_dsr():
    naive, deflated = [], []
    for _ in range(400):
        trials = RNG.normal(0, 1, (250, 50))  # 50 zero-skill strategies
        srs = stats.sharpe(trials)
        best = trials[:, np.argmax(srs)]
        naive.append(stats.psr(best) > 0.95)
        deflated.append(stats.dsr(best, n_trials=50, trial_sr_variance=srs.var(ddof=1)) > 0.95)
    assert np.mean(naive) > 0.5  # selection makes noise look significant
    assert np.mean(deflated) < 0.08


def test_effective_trials_counts_distinct_strategies():
    base = RNG.normal(0, 1, (300, 3))
    trials = np.repeat(base, 5, axis=1) + RNG.normal(0, 0.05, (300, 15))
    assert stats.effective_trials(trials, max_clusters=8) == 3


def test_pbo_is_high_for_noise_and_low_for_a_real_winner():
    noise = RNG.normal(0, 1, (480, 20))
    assert 0.3 < stats.pbo(noise, n_blocks=10) < 0.7
    real = noise.copy()
    real[:, 0] += 0.5
    assert stats.pbo(real, n_blocks=10) < 0.05


def test_romano_wolf_controls_familywise_error():
    rejections = 0
    for _ in range(300):
        data = RNG.normal(0, 1, (100, 10))
        observed = data.mean(axis=0) * np.sqrt(100)
        draws = RNG.normal(0, 1, (1000, 10))  # the joint null of the 10 statistics
        rejections += stats.romano_wolf(observed, draws).min() < 0.05
    assert rejections / 300 < 0.08


def test_romano_wolf_adjusted_p_values_are_monotone_and_find_a_signal():
    draws = RNG.normal(0, 1, (5000, 4))
    p = stats.romano_wolf(np.array([5.0, 0.1, 2.5, -1.0]), draws)
    assert p[0] < 0.001 and p[0] <= p[2] <= p[1] <= p[3]


def test_holm_known_values():
    adjusted = stats.holm(np.array([0.01, 0.04, 0.03, 0.005]))
    assert adjusted == pytest.approx([0.03, 0.06, 0.06, 0.02])


def test_sharpe_refuses_returns_with_gaps():
    with pytest.raises(ValueError):
        stats.sharpe(np.array([[0.1, np.nan], [0.2, 0.1], [0.0, 0.3]]))


def test_bootstrap_needs_a_few_days_before_it_rejects():
    assert stats.bootstrap_p_value(np.array([0.5, -0.4]), 1_000, RNG) == 1.0
    assert stats.bootstrap_p_value(RNG.normal(0.5, 1, 300), 1_000, RNG) < 0.01
