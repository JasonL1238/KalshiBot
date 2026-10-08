"""Conformance: a fast end-to-end smoke test, and reduced runs (slow) showing the gauntlet
keeps its size where naive selection doesn't and finds a large edge. The full grid is
`python -m kalshibot.research.validation.conformance`."""

from pathlib import Path

import numpy as np
import pytest

from kalshibot.research.validation import conformance, synthetic
from kalshibot.research.validation.gauntlet import Gates


def test_a_small_world_yields_every_procedures_decision():
    """Fast: the grid's machinery runs end to end and reports every procedure."""
    results = [conformance.one_world(np.random.default_rng(i), discovery_days=30,
                                     confirmation_days=20, events_per_day=10, favorites=True,
                                     gates=Gates(draws=200)) for i in range(2)]
    assert set(conformance.PROCEDURES) <= set(conformance.rates(results))


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
def test_power_against_a_large_edge():
    results = conformance.simulate(100, seed=102, edge=synthetic.Edge(0.0, 0.15, 3.0))
    assert conformance.rates(results, 0.05 / 12)["gauntlet"] >= 0.85
