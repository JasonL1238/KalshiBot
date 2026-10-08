"""REST order-book snapshots for every open market in a series.

Written Hive-style (series=.../date=.../HHMMSS.parquet) so pandas and DuckDB read the
folder as one table. Why record books at all: docs/RESEARCH.md §1.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from kalshibot.kalshi import KalshiClient
from kalshibot.kalshi.models import to_float


def snapshot_series(client: KalshiClient, series_ticker: str, depth: int = 10) -> pd.DataFrame:
    markets = list(client.iter_markets(series_ticker=series_ticker, status="open"))
    # Batched in groups of 100 tickers; each batch is priced at one instant.
    books = client.get_orderbooks([m["ticker"] for m in markets])
    taken_at = datetime.now(UTC)
    rows = []
    for m in markets:
        book = books.get(m["ticker"])
        if book is None:
            continue
        rows.append({
            "snapshot_ts": taken_at,
            "ticker": m["ticker"],
            "event_ticker": m["event_ticker"],
            "close_time": pd.Timestamp(m["close_time"]),
            "best_yes_bid": book.best_yes_bid,
            "best_yes_ask": book.best_yes_ask,
            "yes_bid_size": book.yes_bid_size,
            "yes_ask_size": book.yes_ask_size,
            "mid": book.mid,
            "spread": book.spread,
            "last_price": to_float(m.get("last_price_dollars")),
            "volume": to_float(m.get("volume_fp")),
            "open_interest": to_float(m.get("open_interest_fp")),
            "yes_bids": json.dumps(book.yes_bids[:depth]),
            "no_bids": json.dumps(book.no_bids[:depth]),
        })
    return pd.DataFrame(rows)


def write_snapshot(df: pd.DataFrame, data_dir: Path, series_ticker: str) -> Path | None:
    if df.empty:
        return None
    ts: datetime = df["snapshot_ts"].iloc[0]
    folder = (data_dir / "raw" / "snapshots" / f"series={series_ticker}"
              / f"date={ts:%Y-%m-%d}")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{ts:%H%M%S}.parquet"
    df.to_parquet(path, index=False)
    return path
