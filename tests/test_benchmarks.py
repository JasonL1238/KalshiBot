"""Keep the benchmark scripts runnable: their candidates must agree on every workload."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import book  # noqa: E402


def test_book_candidates_agree_on_every_workload():
    for make in book.WORKLOADS.values():
        book.check_agreement(make(4_000))
