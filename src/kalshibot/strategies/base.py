"""What every strategy is: shared by research, which backtests it, and execution, which
will run it. Neither of those imports the other; both import this.

A strategy belongs to one hypothesis family and has a fixed set of named
configurations, set at pre-registration. Given point-in-time market data, a
configuration turns the decision times in a window into intents: buy this contract, at
this time, this many, up to this price.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Protocol

import duckdb
import pandas as pd

# side: 'yes' | 'no', the contract bought; decision_time: UTC; count in size units, whole
# contracts (research fills whole contracts only); limit in price units of that contract
# (NULL: any price, takers only)
INTENT_COLUMNS = ["intent_id", "ticker", "side", "decision_time", "count", "limit"]


class MarketData(Protocol):
    """Point-in-time market data (research: `research.load.Loader`)."""

    def trades(self, start: datetime, end: datetime) -> duckdb.DuckDBPyRelation: ...

    def markets(self, as_of: datetime) -> duckdb.DuckDBPyRelation: ...


class Strategy(Protocol):
    family: str  # the hypothesis it tests, e.g. "H8"
    # Longest any position stays open. Research keeps a decision only if its market's
    # as-listed latest expiration is at most this long after it; Kalshi lists that about a
    # week after the scheduled close, so this is usually the hold plus a week.
    max_hold: timedelta
    min_edge: int  # the smallest net edge worth having, price units per contract

    def configs(self) -> dict[str, dict[str, Any]]:
        """Every configuration by name, fixed at pre-registration. Keys the backtest
        reads besides the strategy's own: "maker" (True: intents are resting bids;
        default False, takers) and, for makers, "horizon" (a timedelta: how long a bid
        rests; at most one per contract rests at a time)."""
        ...

    def intents(self, data: MarketData, start: datetime, end: datetime,
                config: dict[str, Any]) -> pd.DataFrame:
        """`INTENT_COLUMNS` for decisions in [start, end), each seeing only what `data`
        shows at its decision time, with unique, non-negative integer intent ids. A window
        split at a UTC midnight splits them (the research runner evaluates a window in
        parts)."""
        ...
