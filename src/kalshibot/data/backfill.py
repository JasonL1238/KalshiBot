"""Backfill Kalshi reference data and trades into Parquet under `data_dir()/raw/kalshi/`.

    series.parquet        every series: category, fee_type, fee_multiplier, frequency
    events.parquet        every event: series_ticker, mutually_exclusive, strike_date
    markets.parquet       every market, live and historical archives, with rules text
    fee_changes.parquet   Kalshi's logged per-series fee changes
    trades/date=YYYY-MM-DD/part.parquet   the trade tape, one file per finished UTC day

Prices and sizes are the fixed-point units of kalshi.models; times are UTC. Combo
(multivariate) rows are dropped from every table (`is_combo`). Each row records when it
was observed (`fetched_at`). Columns such as `result`, `close_time` (which moves on early
close), and `volume` are only final after settlement; research/load.py decides what is
visible when. API behaviors relied on here are in docs/RESEARCH.md §1.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from itertools import batched
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from kalshibot.kalshi import KalshiClient
from kalshibot.kalshi.models import optional_units, price_units, size_units

BATCH = 100_000
UTC_TS = pa.timestamp("us", tz="UTC")
COMBO_PREFIX = "KXMVE"  # multivariate combo series, events, and markets


def is_combo(ticker: str) -> bool:
    """Combos are excluded from research (and from every table here)."""
    return ticker.startswith(COMBO_PREFIX)


def kalshi_dir(data_dir: Path) -> Path:
    return data_dir / "raw" / "kalshi"


def _write(rows: Iterable[dict[str, Any]], path: Path, schema: pa.Schema) -> Path:
    """Stream rows to Parquet in row groups; the file appears only once complete."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        with pq.ParquetWriter(tmp, schema) as writer:
            for chunk in batched(rows, BATCH, strict=False):
                writer.write_table(pa.Table.from_pylist(list(chunk), schema=schema))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)
    return path


def _ts(value: str | None) -> pd.Timestamp | None:
    return pd.Timestamp(value) if value else None


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


def _now() -> datetime:
    return datetime.now(UTC)


SERIES_SCHEMA = pa.schema([
    ("series_ticker", pa.string()), ("title", pa.string()), ("category", pa.string()),
    ("tags", pa.string()), ("frequency", pa.string()), ("fee_type", pa.string()),
    ("fee_multiplier", pa.string()), ("settlement_sources", pa.string()), ("fetched_at", UTC_TS),
])


def backfill_series(client: KalshiClient, out: Path) -> Path:
    rows = ({
        "series_ticker": s["ticker"],
        "title": s.get("title"),
        "category": s.get("category"),
        "tags": json.dumps(s.get("tags") or []),
        "frequency": s.get("frequency"),
        "fee_type": s.get("fee_type"),
        "fee_multiplier": _str(s.get("fee_multiplier")),
        "settlement_sources": json.dumps(s.get("settlement_sources") or []),
        "fetched_at": _now(),
    } for s in client.get_all_series() if not is_combo(s["ticker"]))
    return _write(rows, out / "series.parquet", SERIES_SCHEMA)


FEE_CHANGE_SCHEMA = pa.schema([
    ("series_ticker", pa.string()), ("fee_type", pa.string()), ("fee_multiplier", pa.string()),
    ("scheduled_ts", UTC_TS), ("fetched_at", UTC_TS),
])


def backfill_fee_changes(client: KalshiClient, out: Path) -> Path:
    rows = ({
        "series_ticker": f["series_ticker"],
        "fee_type": f["fee_type"],
        "fee_multiplier": _str(f.get("fee_multiplier")),
        "scheduled_ts": _ts(f["scheduled_ts"]),
        "fetched_at": _now(),
    } for f in client.get_fee_changes() if not is_combo(f["series_ticker"]))
    return _write(rows, out / "fee_changes.parquet", FEE_CHANGE_SCHEMA)


EVENT_SCHEMA = pa.schema([
    ("event_ticker", pa.string()), ("series_ticker", pa.string()), ("category", pa.string()),
    ("title", pa.string()), ("mutually_exclusive", pa.bool_()), ("strike_date", UTC_TS),
    ("strike_period", pa.string()), ("fetched_at", UTC_TS),
])


def backfill_events(client: KalshiClient, out: Path) -> Path:
    rows = ({
        "event_ticker": e["event_ticker"],
        "series_ticker": e.get("series_ticker"),
        "category": e.get("category"),
        "title": e.get("title"),
        "mutually_exclusive": e.get("mutually_exclusive"),
        "strike_date": _ts(e.get("strike_date")),
        "strike_period": e.get("strike_period"),
        "fetched_at": _now(),
    } for e in client.iter_events() if not is_combo(e["event_ticker"]))
    return _write(rows, out / "events.parquet", EVENT_SCHEMA)


MARKET_TIMES = ["created_time", "open_time", "close_time", "expected_expiration_time",
                "latest_expiration_time", "expiration_time", "settlement_ts",
                "occurrence_datetime", "updated_time"]
MARKET_TEXT = ["market_type", "title", "yes_sub_title", "no_sub_title", "strike_type",
               "price_level_structure", "status", "result", "rules_primary",
               "rules_secondary", "early_close_condition", "expiration_value"]
MARKET_SCHEMA = pa.schema([
    ("ticker", pa.string()), ("event_ticker", pa.string()),
    *((col, pa.string()) for col in MARKET_TEXT),
    ("floor_strike", pa.float64()), ("cap_strike", pa.float64()),
    ("custom_strike", pa.string()), ("can_close_early", pa.bool_()),
    ("price_ranges", pa.string()), *((col, UTC_TS) for col in MARKET_TIMES),
    ("settlement_value", pa.int64()), ("notional_value", pa.int64()),
    ("settlement_timer_seconds", pa.int64()), ("volume", pa.int64()),
    ("open_interest", pa.int64()), ("exchange_index", pa.int64()),
    ("source", pa.string()), ("fetched_at", UTC_TS),
])


def flatten_market(m: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "ticker": m["ticker"],
        "event_ticker": m["event_ticker"],
        **{col: _str(m.get(col)) for col in MARKET_TEXT},
        "floor_strike": m.get("floor_strike"),
        "cap_strike": m.get("cap_strike"),
        "custom_strike": json.dumps(m["custom_strike"]) if m.get("custom_strike") else None,
        "can_close_early": m.get("can_close_early"),
        "price_ranges": json.dumps(m.get("price_ranges") or []),
        **{col: _ts(m.get(col)) for col in MARKET_TIMES},
        "settlement_value": optional_units(price_units, m.get("settlement_value_dollars")),
        "notional_value": optional_units(price_units, m.get("notional_value_dollars")),
        "settlement_timer_seconds": m.get("settlement_timer_seconds"),
        "volume": optional_units(size_units, m.get("volume_fp")),
        "open_interest": optional_units(size_units, m.get("open_interest_fp")),
        "exchange_index": m.get("exchange_index"),
        "source": source,
        "fetched_at": _now(),
    }


def backfill_markets(client: KalshiClient, out: Path) -> Path:
    """Live list first, then the historical archive, so a market archived mid-run lands in
    at least one pass. Where a ticker appears twice, the most recently updated copy wins."""
    parts = [
        _write((flatten_market(m, "live") for m in client.iter_markets(mve_filter="exclude")),
               out / "markets_live.parquet", MARKET_SCHEMA),
        _write((flatten_market(m, "historical")
                for m in client.iter_historical_markets(mve_filter="exclude")),
               out / "markets_historical.parquet", MARKET_SCHEMA),
    ]
    path, tmp = out / "markets.parquet", out / "markets.tmp"
    duckdb.execute("""
        COPY (SELECT * FROM read_parquet($parts)
              QUALIFY row_number() OVER (PARTITION BY ticker
                  ORDER BY updated_time DESC NULLS LAST, source = 'historical' DESC) = 1)
        TO '""" + str(tmp).replace("'", "''") + "' (FORMAT parquet)",
        {"parts": [str(p) for p in parts]})
    tmp.replace(path)
    for part in parts:
        part.unlink()
    return path


TRADE_SCHEMA = pa.schema([
    ("trade_id", pa.string()), ("ticker", pa.string()), ("created_time", UTC_TS),
    ("yes_price", pa.int64()), ("count", pa.int64()), ("taker_side", pa.string()),
    ("is_block_trade", pa.bool_()),
])


def _trade_rows(trades: Iterable[dict[str, Any]], end: datetime) -> Iterator[dict[str, Any]]:
    for t in trades:
        created = _ts(t["created_time"])
        if is_combo(t["ticker"]) or created >= end:  # [start, end): no midnight double-count
            continue
        yield {
            "trade_id": t["trade_id"],
            "ticker": t["ticker"],
            "created_time": created,
            "yes_price": price_units(t["yes_price_dollars"]),
            "count": size_units(t["count_fp"]),
            "taker_side": t.get("taker_side"),
            "is_block_trade": t.get("is_block_trade"),
        }


def trades_path(out: Path, day: date) -> Path:
    return out / "trades" / f"date={day.isoformat()}" / "part.parquet"


def backfill_trades_day(client: KalshiClient, out: Path, day: date) -> Path:
    """One finished UTC day of trades. Trades older than the API's cutoff are only served
    by /historical/trades, so the source is chosen per day (both, deduplicated, if the
    cutoff falls inside the day), and the cutoff is re-read afterwards in case it moved."""
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    end = start + timedelta(days=1)
    if end > _now():
        raise ValueError(f"{day} hasn't finished yet")
    window = {"min_ts": int(start.timestamp()), "max_ts": int(end.timestamp())}  # max exclusive

    def source() -> str:
        cut = datetime.fromisoformat(client.historical_cutoff()["trades_created_ts"])
        return "historical" if end <= cut else "live" if start >= cut else "both"

    before = source()
    if before == "historical":
        rows: Iterable[dict[str, Any]] = _trade_rows(client.iter_historical_trades(**window), end)
    elif before == "live":
        rows = _trade_rows(client.iter_trades(**window), end)
    else:  # the cutoff splits the day: take both archives, one copy of each trade
        both = {r["trade_id"]: r for fetch in (client.iter_historical_trades, client.iter_trades)
                for r in _trade_rows(fetch(**window), end)}
        rows = sorted(both.values(), key=lambda r: r["created_time"])
    path = _write(rows, trades_path(out, day), TRADE_SCHEMA)
    if source() != before:  # the archive moved over this day mid-fetch; take it again
        return backfill_trades_day(client, out, day)
    return path


def backfill_trades(make_client: Callable[[], KalshiClient], out: Path, start: date, end: date,
                    workers: int = 2) -> list[Path]:
    """Every finished UTC day in [start, end), `workers` at a time, each with its own client.
    Days that already have a file are skipped, so an interrupted run resumes."""
    end = min(end, _now().date())  # today isn't finished
    days = [start + timedelta(n) for n in range((end - start).days)]
    todo = [d for d in days if not trades_path(out, d).exists()]

    def run(day: date) -> Path:
        with make_client() as client:
            return backfill_trades_day(client, out, day)

    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(run, todo))
