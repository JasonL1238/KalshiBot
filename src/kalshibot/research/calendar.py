"""The research calendar: fixed before any confirmation test runs.

Windows are half-open UTC intervals; how to use them (marking, settlement-date PnL,
embargoes): docs/ROADMAP.md, Research calendar.

    discovery     [DISCOVERY_START, B)           explore and select only
    unused        [B, S_h)                       per hypothesis h, never touched
    confirmation  [max(B, S_h), LOCKBOX_START)   one primary test per hypothesis
    lockbox       [LOCKBOX_START, FORWARD_START) opened once for all survivors, logged
    forward       [FORWARD_START, ...)           paper and live-book tests
"""

from __future__ import annotations

from datetime import UTC, datetime

DISCOVERY_START = datetime(2021, 6, 30, tzinfo=UTC)  # Kalshi's first trade (Becker dataset)
# B: 00:00 UTC the day after the last Becker trade (2025-11-25 22:00:15 UTC).
BECKER_END = datetime(2025, 11, 26, tzinfo=UTC)
LOCKBOX_START = datetime(2026, 5, 1, tzinfo=UTC)
FORWARD_START = datetime(2026, 10, 1, tzinfo=UTC)

# S_h: 00:00 UTC the day after the latest data date of any study that informed hypothesis
# h. Filled in at pre-registration (M3); a hypothesis absent here has S_h = B.
STUDY_END: dict[str, datetime] = {}


def confirmation_start(hypothesis: str) -> datetime:
    return max(BECKER_END, STUDY_END.get(hypothesis, BECKER_END))


def window(ts: datetime, hypothesis: str | None = None) -> str:
    """Which window a decision time falls in (for a hypothesis, if given)."""
    if ts.tzinfo is None:
        raise ValueError("decision times must be timezone-aware UTC")
    if ts < BECKER_END:
        return "discovery"
    if ts >= FORWARD_START:
        return "forward"
    if ts >= LOCKBOX_START:
        return "lockbox"
    if hypothesis is not None and ts < confirmation_start(hypothesis):
        return "unused"
    return "confirmation"
