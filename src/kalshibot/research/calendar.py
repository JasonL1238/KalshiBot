"""The research calendar: fixed before any confirmation test runs.

Windows are half-open UTC intervals; how to use them (marking, settlement-date PnL,
embargoes): docs/ROADMAP.md, Research calendar.

    discovery     [DISCOVERY_START, B)           explore and select only
    unused        [B, S_h)                       per hypothesis h, never touched
    confirmation  [max(B, S_h), LOCKBOX_START)   one primary test per hypothesis
    lockbox       [LOCKBOX_START, FORWARD_START) opened once for all survivors, logged
    forward       [FORWARD_START, ...)           paper and live-book tests
    forward test  FORWARD_TESTS[h]               instead of confirmation, for h in FORWARD
                                                 (none for a withdrawn h: WITHDRAWN)
"""

from __future__ import annotations

from datetime import UTC, datetime

DISCOVERY_START = datetime(2021, 6, 30, tzinfo=UTC)  # Kalshi's first trade (Becker dataset)
# B: 00:00 UTC the day after the last Becker trade (2025-11-25 22:00:15 UTC).
BECKER_END = datetime(2025, 11, 26, tzinfo=UTC)
LOCKBOX_START = datetime(2026, 5, 1, tzinfo=UTC)
FORWARD_START = datetime(2026, 10, 1, tzinfo=UTC)

# S_h: 00:00 UTC the day after the latest data date of any study that informed hypothesis
# h: an analysis of Kalshi market data that the confirmation window holds (venue facts and
# studies of other venues don't count). Filled in once a study is known to inform h, at
# the latest at pre-registration (M3); a hypothesis absent here has S_h = B, and a forward
# one needs no entry unless its study is why it is forward.
STUDY_END: dict[str, datetime] = {
    "H1": datetime(2026, 3, 12, tzinfo=UTC),  # why H1 was registered forward (STRATEGIES H1)
    "H2": datetime(2026, 3, 12, tzinfo=UTC),  # Bartlett & O'Hara (STRATEGIES H2, H8)
    "H8": datetime(2026, 3, 12, tzinfo=UTC),
}


# Hypotheses confirmed on a stretch of the forward window instead (rules: docs/ROADMAP.md,
# Research calendar), and those stretches, each fixed at registration and starting after it.
FORWARD = frozenset({"H1", "H5", "H11"})
FORWARD_TESTS: dict[str, tuple[datetime, datetime]] = {}
# Hypotheses withdrawn before their test: never confirmed, so they get no verdict, and a
# forward one keeps its α/12 unspent (docs/ROADMAP.md, Research calendar).
WITHDRAWN = frozenset({"H1"})


def confirmation_start(hypothesis: str) -> datetime:
    return max(BECKER_END, STUDY_END.get(hypothesis, BECKER_END))


def confirmation_window(hypothesis: str) -> tuple[datetime, datetime]:
    """Where the hypothesis has its one confirmation test."""
    if hypothesis in WITHDRAWN:
        raise ValueError(f"{hypothesis} is withdrawn: it has no confirmation test")
    if hypothesis in FORWARD:
        if hypothesis not in FORWARD_TESTS:
            raise ValueError(f"{hypothesis} is confirmed forward; register its window first")
        start, end = FORWARD_TESTS[hypothesis]
        if start < FORWARD_START:
            raise ValueError(f"{hypothesis}'s forward test starts before the forward window")
    else:
        start, end = confirmation_start(hypothesis), LOCKBOX_START
    if start >= end:
        raise ValueError(f"{hypothesis} has no confirmation window left; confirm it forward")
    return start, end
