"""Point-in-time access to the canonical Kalshi tables.

Research code reads market data only through `Loader`, so the calendar and the
look-ahead rules are enforced in one place:

- One trade tape, Kalshi's API, combos excluded (`data/backfill.py`), and 1-minute
  candles (`candles`) where a family fetched them. Becker's dataset is only a
  cross-check: it lacks whole markets (docs/RESEARCH.md §1, Historical data). Asking
  for trade days or candle files that aren't on disk, or for days that haven't finished,
  raises `MissingData` instead of returning less.
- No lockbox data: reading trades or candles in [LOCKBOX_START, FORWARD_START) raises
  `LockboxError`, and outcomes of markets that settled then stay hidden, until the
  ledger's one unlock names this loader's `family` as a survivor. The forward window is
  always open.
- Markets listed after `as_of` don't exist yet. Post-hoc columns (known only once the
  market settles: `result`, `volume`, a `close_time` that moves on early close, ...) are
  NULL unless it settled before `as_of`.
- Relations are lazy: each execution reads the files then, so narrow them before `.df()`.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from kalshibot.config import data_dir as default_data_dir
from kalshibot.data.backfill import CANDLE_SCHEMA, TRADE_SCHEMA, candles_path, trades_path
from kalshibot.kalshi.fee_history import FeeSchedule
from kalshibot.research.calendar import FORWARD_START, LOCKBOX_START
from kalshibot.research.ledger import lockbox_survivors

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
# A market's series: its event's, or (some 13k markets have no event row) the event
# ticker's prefix, which names the series for 98% of events
SERIES = "coalesce(e.series_ticker, split_part(m.event_ticker, '-', 1))"


class LockboxError(RuntimeError):
    """Research code asked for lockbox-window data before the logged unlock."""


class MissingData(RuntimeError):
    """The request needs trade days or candle files that haven't been backfilled."""


class Loader:
    def __init__(self, data_dir: Path | None = None, family: str | None = None):
        self.family = family  # the hypothesis family reading; gates lockbox access
        root = data_dir or default_data_dir()
        self._kalshi = root / "raw" / "kalshi"
        self._con = duckdb.connect()
        self._con.execute("SET TimeZone = 'UTC'")  # date casts and display in UTC

    @property
    def kalshi_dir(self) -> Path:
        """Where the raw Kalshi tables live (`data.backfill` writes them)."""
        return self._kalshi

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        """The connection every relation here lives on: register a frame on it to join
        the frame with them."""
        return self._con

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
        if end > datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0):
            raise MissingData(f"[{start}, {end}) runs past the last finished UTC day")
        days = _days(start, end)
        missing = [d for d in days if not trades_path(self._kalshi, d).exists()]
        if missing:
            raise MissingData(f"{len(missing)} API trade days missing (first {missing[0]}); "
                              "run `kalshibot backfill trades`")
        if not days:
            return self._con.from_arrow(TRADE_SCHEMA.empty_table())
        return self._con.sql(
            f"SELECT * FROM read_parquet({_sql([trades_path(self._kalshi, d) for d in days])}, "
            f"hive_partitioning = false) WHERE created_time >= {_sql(start)} "
            f"AND created_time < {_sql(end)}")

    def _candle_paths(self, wanted: pd.DataFrame) -> list[Path]:
        return [candles_path(self._kalshi, t, pd.Timestamp(d).date())
                for t, d in wanted[["ticker", "day"]].itertuples(index=False)]

    def has_candles(self, wanted: pd.DataFrame) -> np.ndarray:
        """Which (ticker, day) rows of `wanted` have a candle file on disk."""
        return np.array([p.exists() for p in self._candle_paths(wanted)], bool)

    def candles(self, wanted: pd.DataFrame) -> duckdb.DuckDBPyRelation:
        """1-minute candles (`data.backfill.CANDLE_SCHEMA`) for the (ticker, day) rows of
        `wanted`, under the same lockbox and missing-data rules as `trades`."""
        days = sorted({pd.Timestamp(d).date() for d in wanted["day"]})
        if days and not self._lockbox_open() and any(
                LOCKBOX_START.date() <= d < FORWARD_START.date() for d in days):
            raise LockboxError("candles in the lockbox are open only to logged survivors")
        paths = self._candle_paths(wanted[["ticker", "day"]].drop_duplicates())
        missing = [p for p in paths if not p.exists()]
        if missing:
            raise MissingData(f"{len(missing)} candle files missing (first {missing[0]}); "
                              "fetch them with research.run.fetch_candles")
        if not paths:
            return self._con.from_arrow(CANDLE_SCHEMA.empty_table())
        return self._con.sql(
            f"SELECT * FROM read_parquet({_sql(paths)}, hive_partitioning = false)")

    def markets(self, as_of: datetime) -> duckdb.DuckDBPyRelation:
        """Markets listed by `as_of`, with their series and category; post-hoc columns are
        NULL unless the market settled before `as_of`. Every market has a series
        (`SERIES`)."""
        cutoff = _sql(as_of)
        known = f"m.settlement_ts < {cutoff}"
        if not self._lockbox_open():  # lockbox-era outcomes stay hidden
            known += (f" AND NOT (m.settlement_ts >= {_sql(LOCKBOX_START)} "
                      f"AND m.settlement_ts < {_sql(FORWARD_START)})")
        cols = ([f"m.{c}" for c in AS_LISTED]
                + [f"CASE WHEN {known} THEN m.{c} END AS {c}" for c in POST_HOC]
                + [f"{SERIES} AS series_ticker", "s.category", "e.mutually_exclusive"])
        return self._con.sql(
            f"SELECT {', '.join(cols)} FROM read_parquet({_sql(self._parquet('markets'))}) m "
            f"LEFT JOIN read_parquet({_sql(self._parquet('events'))}) e USING (event_ticker) "
            f"LEFT JOIN read_parquet({_sql(self._parquet('series'))}) s "
            f"ON s.series_ticker = {SERIES} WHERE m.created_time < {cutoff}")

    def fetched(self) -> datetime | None:
        """When the markets and events tables were fetched (their oldest row; None if
        either is empty): outcomes after this aren't in them yet."""
        return self._con.sql(
            "SELECT least(min(m), min(e)) FROM (SELECT min(fetched_at) AS m FROM "
            f"read_parquet({_sql(self._parquet('markets'))})), (SELECT min(fetched_at) AS e "
            f"FROM read_parquet({_sql(self._parquet('events'))}))").fetchone()[0]

    def fingerprint(self, windows: list[tuple[datetime, datetime]],
                    candles: pd.DataFrame | None = None) -> str:
        """A hash of the data an evaluation reads: the contents of the reference tables,
        the name and size of every trade-day file of the `windows` ([start, end) each),
        and of every file among the (ticker, day) rows of `candles` that exists."""
        windows = [(_utc(start), _utc(end)) for start, end in windows]
        digest = hashlib.sha256(repr(windows).encode())
        for name in ("series", "events", "markets", "fee_changes"):
            with open(self._parquet(name), "rb") as f:
                digest.update(hashlib.file_digest(f, "sha256").digest())
        wanted = (candles[["ticker", "day"]].drop_duplicates().sort_values(["ticker", "day"])
                  if candles is not None else pd.DataFrame(columns=["ticker", "day"]))
        for path in [trades_path(self._kalshi, d) for w in windows for d in _days(*w)] + \
                self._candle_paths(wanted):
            if path.exists():
                digest.update(f"{path.relative_to(self._kalshi)}:{path.stat().st_size};".encode())
        return digest.hexdigest()[:16]

    def fee_schedule(self) -> FeeSchedule:
        log = self._con.sql("SELECT series_ticker, scheduled_ts, fee_type, fee_multiplier "
                            f"FROM read_parquet({_sql(self._parquet('fee_changes'))})")
        series = self._con.sql("SELECT series_ticker, fee_type, fee_multiplier, category "
                               f"FROM read_parquet({_sql(self._parquet('series'))})")
        return FeeSchedule(log.fetchall(), {s: (t, m, c) for s, t, m, c in series.fetchall()})


def _sql(value: datetime | str | Path | list) -> str:
    """A SQL literal for a UTC time, a path or a list of paths. DuckDB runs a query with
    parameters at once and keeps its whole result; with literals a relation stays lazy, so
    the projections, filters and joins callers add run as one streamed query over the
    Parquet files."""
    if isinstance(value, list):
        return f"[{', '.join(_sql(v) for v in value)}]"
    if isinstance(value, datetime):
        return f"TIMESTAMPTZ '{_utc(value).isoformat()}'"
    return "'" + str(value).replace("'", "''") + "'"


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("times must be timezone-aware")
    return ts.astimezone(UTC)


def _days(start: datetime, end: datetime) -> list[date]:
    """UTC days that [start, end) touches."""
    last = (end - timedelta(microseconds=1)).date()
    return [start.date() + timedelta(n) for n in range((last - start.date()).days + 1)]
