"""Point-in-time access to the canonical Kalshi tables.

Research code reads market data only through `Loader`, so the calendar and the
look-ahead rules are enforced in one place:

- One trade tape: Becker's dataset before `API_FROM`, Kalshi's API from then on (why:
  docs/RESEARCH.md §1, Historical data). Combos are excluded. Asking for API days that
  aren't on disk raises `MissingData` instead of returning a thinner tape.
- No lockbox data: reading trades in [LOCKBOX_START, FORWARD_START) raises
  `LockboxError`, and outcomes of markets that settled then stay hidden, until the
  ledger's one unlock names this loader's `family` as a survivor. The forward window is
  always open.
- Markets listed after `as_of` don't exist yet. Post-hoc columns (known only once the
  market settles: `result`, `volume`, a `close_time` that moves on early close, ...) are
  NULL unless it settled before `as_of`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb

from kalshibot.config import data_dir as default_data_dir
from kalshibot.data.backfill import COMBO_PREFIX, trades_path
from kalshibot.kalshi.fee_history import FeeSchedule
from kalshibot.research.calendar import FORWARD_START, LOCKBOX_START
from kalshibot.research.ledger import lockbox_survivors

API_FROM = datetime(2025, 10, 9, tzinfo=UTC)

# Every markets.parquet column is exactly one of these (tests/test_research_load.py).
AS_LISTED = ["ticker", "event_ticker", "market_type", "title", "yes_sub_title", "no_sub_title",
             "strike_type", "floor_strike", "cap_strike", "custom_strike", "can_close_early",
             "created_time", "open_time", "latest_expiration_time", "rules_primary",
             "rules_secondary", "early_close_condition", "notional_value",
             "settlement_timer_seconds", "exchange_index"]
POST_HOC = ["close_time", "expected_expiration_time", "expiration_time", "occurrence_datetime",
            "settlement_ts", "status", "result", "settlement_value", "expiration_value",
            "volume", "open_interest", "price_level_structure", "price_ranges", "updated_time"]
META = ["source", "fetched_at"]

# Becker stores integer cents and whole contracts; scale them to kalshi.models units.
BECKER_TRADES = f"""
    SELECT trade_id, ticker, created_time, yes_price * 100 AS yes_price, count * 100 AS count,
           taker_side, NULL::BOOLEAN AS is_block_trade, 'becker' AS source
    FROM read_parquet($becker) WHERE ticker NOT LIKE '{COMBO_PREFIX}%'"""


class LockboxError(RuntimeError):
    """Research code asked for lockbox-window data before the logged unlock."""


class MissingData(RuntimeError):
    """The requested range needs API trade days that haven't been backfilled."""


class Loader:
    def __init__(self, data_dir: Path | None = None, family: str | None = None):
        self.family = family  # the hypothesis family reading; gates lockbox access
        root = data_dir or default_data_dir()
        self._kalshi = root / "raw" / "kalshi"
        self._becker = root / "external" / "becker" / "data" / "kalshi" / "trades" / "*.parquet"
        self._con = duckdb.connect()
        self._con.execute("SET TimeZone = 'UTC'")  # date casts and display in UTC

    def _parquet(self, name: str) -> str:
        return str(self._kalshi / f"{name}.parquet")

    def _lockbox_open(self) -> bool:
        survivors = lockbox_survivors()
        return survivors is not None and self.family in survivors

    def trades(self, start: datetime, end: datetime) -> duckdb.DuckDBPyRelation:
        """Trades with created_time in [start, end)."""
        start, end = _utc(start), _utc(end)
        if start < FORWARD_START and end > LOCKBOX_START and not self._lockbox_open():
            raise LockboxError(f"[{start}, {end}) overlaps the lockbox [{LOCKBOX_START}, "
                               f"{FORWARD_START}), open only to logged survivors")
        missing = [d for d in _days(max(start, API_FROM), end)
                   if not trades_path(self._kalshi, d).exists()]
        if missing:
            raise MissingData(f"{len(missing)} API trade days missing (first {missing[0]}); "
                              "run `kalshibot backfill trades`")
        parts, params = [], {"start": start, "end": end, "api_from": API_FROM}
        if start < API_FROM:
            parts.append(BECKER_TRADES + " AND created_time < $api_from")
            params["becker"] = str(self._becker)
        if end > API_FROM:
            parts.append("SELECT trade_id, ticker, created_time, yes_price, count, taker_side, "
                         "is_block_trade, 'api' AS source "
                         "FROM read_parquet($api, hive_partitioning = false) "
                         "WHERE created_time >= $api_from")
            params["api"] = str(self._kalshi / "trades" / "*" / "part.parquet")
        return self._con.sql(f"SELECT * FROM ({' UNION ALL '.join(parts)}) "
                             "WHERE created_time >= $start AND created_time < $end",
                             params=params)

    def markets(self, as_of: datetime) -> duckdb.DuckDBPyRelation:
        """Markets listed by `as_of`, with their series and category; post-hoc columns are
        NULL unless the market settled before `as_of`. `scheduled_close` is the listed
        close for markets that can't close early, else NULL."""
        as_of = _utc(as_of)
        known = "m.settlement_ts < $as_of"
        if not self._lockbox_open():  # lockbox-era outcomes stay hidden
            known += " AND NOT (m.settlement_ts >= $lock_start AND m.settlement_ts < $lock_end)"
        cols = ([f"m.{c}" for c in AS_LISTED]
                + [f"CASE WHEN {known} THEN m.{c} END AS {c}" for c in POST_HOC]
                + ["CASE WHEN NOT m.can_close_early THEN m.close_time END AS scheduled_close",
                   "e.series_ticker", "s.category", "e.mutually_exclusive"])
        return self._con.sql(
            f"SELECT {', '.join(cols)} FROM read_parquet($markets) m "
            "LEFT JOIN read_parquet($events) e USING (event_ticker) "
            "LEFT JOIN read_parquet($series) s ON s.series_ticker = e.series_ticker "
            "WHERE m.created_time < $as_of",
            params={"as_of": as_of, "markets": self._parquet("markets"),
                    "events": self._parquet("events"), "series": self._parquet("series"),
                    **({} if self._lockbox_open() else
                       {"lock_start": LOCKBOX_START, "lock_end": FORWARD_START})})

    def fee_schedule(self) -> FeeSchedule:
        log = self._con.sql("SELECT series_ticker, scheduled_ts, fee_type, fee_multiplier "
                            "FROM read_parquet($p)", params={"p": self._parquet("fee_changes")})
        series = self._con.sql("SELECT series_ticker, fee_type, fee_multiplier, category "
                               "FROM read_parquet($p)", params={"p": self._parquet("series")})
        return FeeSchedule(log.fetchall(), {s: (t, m, c) for s, t, m, c in series.fetchall()})


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("times must be timezone-aware")
    return ts.astimezone(UTC)


def _days(start: datetime, end: datetime) -> list[date]:
    """UTC days that [start, end) touches, up to yesterday (today isn't finished)."""
    last = min((end - timedelta(microseconds=1)).date(), datetime.now(UTC).date() - timedelta(1))
    return [start.date() + timedelta(n) for n in range((last - start.date()).days + 1)]
