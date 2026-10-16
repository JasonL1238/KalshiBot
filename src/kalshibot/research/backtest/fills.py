"""Fill rules: which hypothetical orders would have filled, when, at what price.

Only the trade tape exists historically (no order books), so fills are inferred from
prints, and a stale print never fills an order. The rules:

- Taker (`taker_fills`): the order fills at the first print after `decision_time +
  latency`, and within `max_wait` of it, in which a taker bought the same contract. That
  print shows the ask then (in a sweep, the lowest of the prices printed at that
  instant). Size is capped at `participation` × that print's size. The signal's own
  print can't fill it, because the latency puts the fill strictly later. The order fills
  only at or under its limit, with no floor, so the print can be well below the ask
  at decision time (docs/BACKTESTING.md, Fill models).
- Maker (`maker_fills`): a resting bid fills only when later prints trade *through* its
  price, strictly below it for the contract bought. Being at the touch isn't enough,
  because queue position is unknown. Each sweep (one instant's prints by takers selling
  that contract) fills `(1 − queue_haircut) × participation` of what it traded through
  the bid, at the bid and the sweep's time, until the order is full or `horizon`
  expires.
- Fills are whole contracts, rounded down: Kalshi traded whole contracts only until
  March 2026, and fractions since only where a market enables them.

Prices are kalshi.models units; a NO price is PRICE_SCALE − yes_price. Every tier (these
rules and the Tier-2 engines) takes `strategies.base.INTENT_COLUMNS` and returns
`FILL_COLUMNS`.
"""

from __future__ import annotations

from datetime import timedelta

import duckdb
import pandas as pd

from kalshibot.kalshi.models import PRICE_SCALE, SIZE_SCALE

FILL_COLUMNS = ["intent_id", "ticker", "side", "fill_time", "fill_price", "fill_count", "maker"]


def _whole(size: str) -> str:
    """SQL rounding a size in size units down to whole contracts."""
    return f"(floor(({size}) / {SIZE_SCALE}) * {SIZE_SCALE})::BIGINT"


def no_fills() -> tuple[pd.DataFrame, pd.DataFrame]:
    """What a Tier-2 engine returns when nothing can fill: (fills, engine PnL), empty."""
    return pd.DataFrame(columns=FILL_COLUMNS), pd.DataFrame(columns=["ticker", "pnl"])


def tape_order(trades: pd.DataFrame) -> pd.DataFrame:
    """`trades` in the order the Tier-2 engines read them (docs/BACKTESTING.md, Fill models)."""
    return trades.sort_values(["created_time", "trade_id"])


def taker_fills(con: duckdb.DuckDBPyConnection, intents: str, trades: str,
                latency: timedelta = timedelta(seconds=1),
                max_wait: timedelta = timedelta(minutes=5),
                participation: float = 0.1) -> duckdb.DuckDBPyRelation:
    """`intents` and `trades` name relations visible to `con` (tables, views, or DataFrames)."""
    return con.sql(f"""
        WITH i AS (SELECT *, decision_time + INTERVAL '{latency.total_seconds()} seconds'
                             AS earliest FROM {intents}),
             levels AS (
                SELECT ticker, taker_side, created_time, price, sum(count) AS print_count
                FROM (SELECT *, CASE WHEN taker_side = 'yes' THEN yes_price
                                     ELSE {PRICE_SCALE} - yes_price END AS price FROM {trades})
                GROUP BY ticker, taker_side, created_time, price),
             t AS (SELECT * FROM levels  -- a sweep's best level
                   QUALIFY row_number() OVER (PARTITION BY ticker, taker_side, created_time
                                              ORDER BY price) = 1)
        SELECT i.intent_id, i.ticker, i.side, t.created_time AS fill_time, t.price AS fill_price,
               {_whole(f"least(i.count, t.print_count * {participation})")} AS fill_count,
               FALSE AS maker
        FROM i ASOF JOIN t
          ON i.ticker = t.ticker AND i.side = t.taker_side AND t.created_time > i.earliest
        WHERE t.created_time <= i.earliest + INTERVAL '{max_wait.total_seconds()} seconds'
          AND (i."limit" IS NULL OR t.price <= i."limit")
          AND {_whole(f"least(i.count, t.print_count * {participation})")} > 0""")


def maker_fills(con: duckdb.DuckDBPyConnection, intents: str, trades: str,
                horizon: timedelta, latency: timedelta = timedelta(seconds=1),
                queue_haircut: float = 0.5, participation: float = 0.1) -> duckdb.DuckDBPyRelation:
    """Resting bids at `limit` for the contract `side`, live from decision_time + latency
    for `horizon`: one fill per sweep through the bid."""
    share = (1 - queue_haircut) * participation
    return con.sql(f"""
        WITH sweeps AS (
                SELECT i.intent_id, i.ticker, i.side, i.count, i."limit", t.created_time,
                       {_whole(f"sum(t.count) * {share}")} AS available
                FROM {intents} i JOIN {trades} t
                  ON t.ticker = i.ticker
                 AND t.created_time > i.decision_time + INTERVAL '{latency.total_seconds()} seconds'
                 AND t.created_time <= i.decision_time
                     + INTERVAL '{(latency + horizon).total_seconds()} seconds'
                 -- the opposite taker sold our contract below our bid
                 AND ((i.side = 'yes' AND t.taker_side = 'no' AND t.yes_price < i."limit")
                   OR (i.side = 'no' AND t.taker_side = 'yes'
                       AND {PRICE_SCALE} - t.yes_price < i."limit"))
                GROUP BY ALL),
             filled AS (
                SELECT *, least({_whole("count")}, sum(available) OVER (
                              PARTITION BY intent_id ORDER BY created_time
                              ROWS UNBOUNDED PRECEDING))::BIGINT AS so_far FROM sweeps)
        SELECT intent_id, ticker, side, created_time AS fill_time, "limit" AS fill_price,
               so_far - coalesce(lag(so_far) OVER (PARTITION BY intent_id ORDER BY created_time),
                                 0) AS fill_count, TRUE AS maker
        FROM filled
        QUALIFY fill_count > 0""")
