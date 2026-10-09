"""Backfill Kalshi reference data and trades into Parquet under `data_dir()/raw/kalshi/`.

    series.parquet        every series: category, fee_type, fee_multiplier, frequency
    events.parquet        every event: series_ticker, mutually_exclusive, strike_date
    markets.parquet       every market, live and historical archives, with rules text
    fee_changes.parquet   Kalshi's logged per-series fee changes
    incentive_programs.parquet   every incentive program seen, ended ones included: a
                          market's reward per window for two-sided depth
    trades/date=YYYY-MM-DD/part.parquet   the trade tape, one file per finished UTC day
    candles/date=YYYY-MM-DD/<ticker>.parquet   1-minute candles (YES bid, YES ask and
                          trade price OHLC, volume, open interest), fetched only for
                          the markets and days a research family needs

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
from itertools import batched, chain
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from kalshibot.config import MARKET_DATA_URL, READ_BUDGET
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


def public_clients(workers: int, **options) -> Callable[[], KalshiClient]:
    """Clients for `workers` parallel fetchers that together stay within the API's read
    budget (`options`: KalshiClient's, such as max_retries)."""
    return lambda: KalshiClient(MARKET_DATA_URL, min_interval=workers / READ_BUDGET,
                                **options)


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


PROGRAM_SCHEMA = pa.schema([
    ("id", pa.string()), ("market_ticker", pa.string()), ("incentive_type", pa.string()),
    ("incentive_description", pa.string()), ("start_date", UTC_TS), ("end_date", UTC_TS),
    ("period_reward", pa.int64()),  # as served; its unit is unverified
    ("target_size", pa.int64()), ("discount_factor_bps", pa.int64()), ("paid_out", pa.bool_()),
    ("fetched_at", UTC_TS),
])


def backfill_incentive_programs(client: KalshiClient, out: Path) -> Path:
    """Every program, once each (pages can shift while new ones are added). Programs a
    previous run saw and Kalshi no longer serves are kept."""
    path = out / "incentive_programs.parquet"
    programs = {p["id"]: p for p in client.iter_incentive_programs()
                if not is_combo(p["market_ticker"])}
    kept = ([r for r in pq.read_table(path).to_pylist() if r["id"] not in programs]
            if path.exists() else [])
    rows = chain(kept, ({
        "id": p["id"],
        "market_ticker": p["market_ticker"],
        "incentive_type": p.get("incentive_type"),
        "incentive_description": p.get("incentive_description"),
        "start_date": _ts(p.get("start_date")),
        "end_date": _ts(p.get("end_date")),
        "period_reward": p.get("period_reward"),
        "target_size": optional_units(size_units, p.get("target_size_fp")),
        "discount_factor_bps": p.get("discount_factor_bps"),
        "paid_out": p.get("paid_out"),
        "fetched_at": _now(),
    } for p in programs.values()))
    return _write(rows, path, PROGRAM_SCHEMA)


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


OHLC = ("open", "high", "low", "close")
CANDLE_SCHEMA = pa.schema([
    ("ticker", pa.string()), ("end_period_ts", UTC_TS),
    *((f"{side}_{k}", pa.int64()) for side in ("yes_bid", "yes_ask") for k in OHLC),
    *((f"price_{k}", pa.int64()) for k in (*OHLC, "mean", "previous")),
    ("volume", pa.int64()), ("open_interest", pa.int64()),
])


def candles_path(out: Path, ticker: str, day: date) -> Path:
    return out / "candles" / f"date={day.isoformat()}" / f"{ticker}.parquet"


def _field(group: dict[str, Any], key: str) -> Any:
    """The archive names a candle field `key`; the live endpoint `key_dollars` or `key_fp`."""
    return next((group[k] for k in (key, f"{key}_dollars", f"{key}_fp") if k in group), None)


def _candle_row(ticker: str, candle: dict[str, Any]) -> dict[str, Any]:
    row = {"ticker": ticker,
           "end_period_ts": datetime.fromtimestamp(candle["end_period_ts"], UTC),
           "volume": optional_units(size_units, _field(candle, "volume")),
           "open_interest": optional_units(size_units, _field(candle, "open_interest"))}
    for side in ("yes_bid", "yes_ask"):
        quote = candle.get(side)
        if not isinstance(quote, dict) or not {"close", "close_dollars"} & quote.keys():
            raise ValueError(f"unrecognized candle for {ticker}: {side} = {quote!r}")
        row |= {f"{side}_{k}": optional_units(price_units, _field(quote, k)) for k in OHLC}
    price = candle.get("price") or {}
    return row | {f"price_{k}": optional_units(price_units, _field(price, k))
                  for k in (*OHLC, "mean", "previous")}


def backfill_candles_day(client: KalshiClient, out: Path, ticker: str, day: date,
                         series_ticker: str | None, archived: bool) -> Path:
    """One finished UTC day of 1-minute candles for one market: every minute ending in
    (day, day + 1] that had a change, with the OHLC of its YES bid, YES ask and trade
    price, volume and open interest. Markets settled before the API's market cutoff
    (`archived`) are served by the historical archive, the rest by the live endpoint; a
    404 from one tries the other, since the cutoff moves."""
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    if start + timedelta(days=1) > _now():
        raise ValueError(f"{day} hasn't finished yet")
    first = int(start.timestamp())

    def fetch(archive: bool) -> list[dict[str, Any]]:
        return client.get_candlesticks(ticker, start_ts=first, end_ts=first + 86_400,
                                       period_interval=1,
                                       series_ticker=None if archive else series_ticker)

    try:
        candles = fetch(archived)
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404 or (archived and series_ticker is None):
            raise
        candles = fetch(not archived)
    return _write((_candle_row(ticker, c) for c in candles
                   if first < c["end_period_ts"] <= first + 86_400),
                  candles_path(out, ticker, day), CANDLE_SCHEMA)


def backfill_candles(make_client: Callable[[], KalshiClient], out: Path, needs: pd.DataFrame,
                     workers: int = 2) -> list[Path]:
    """Candles for every (ticker, day) row of `needs`, split across `workers`, each with
    one client so its throttle holds. Series and settlement come from the raw markets
    and events tables in `out`. Files already on disk are skipped, so an interrupted run
    resumes."""
    todo = sorted({(t, pd.Timestamp(d).date()) for t, d in needs[["ticker", "day"]].itertuples(
        index=False) if not candles_path(out, t, pd.Timestamp(d).date()).exists()})
    if not todo:
        return []
    con = duckdb.connect()
    con.register("wanted", pd.DataFrame({"ticker": sorted({t for t, _ in todo})}))
    known = con.sql("SELECT m.ticker, e.series_ticker, m.settlement_ts "
                    "FROM read_parquet($m) m LEFT JOIN read_parquet($e) e USING (event_ticker) "
                    "SEMI JOIN wanted USING (ticker)",
                    params={"m": str(out / "markets.parquet"),
                            "e": str(out / "events.parquet")}).df().set_index("ticker")
    with make_client() as client:
        cut = pd.Timestamp(client.historical_cutoff()["market_settled_ts"])

    def run(chunk: list[tuple[str, date]]) -> list[Path]:
        with make_client() as client:
            return [backfill_candles_day(
                client, out, ticker, day, known["series_ticker"].get(ticker),
                bool(known["settlement_ts"].get(ticker, pd.NaT) < cut)) for ticker, day in chunk]

    with ThreadPoolExecutor(workers) as pool:
        return [p for paths in pool.map(run, [todo[k::workers] for k in range(workers)])
                for p in paths]
