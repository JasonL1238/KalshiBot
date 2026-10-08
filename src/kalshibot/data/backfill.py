"""Backfill every settled market in a series into one tidy table.

One row per contract with its strike definition and how it resolved (the labels a
backtest scores against). Columns such as `result`, `volume`, and `close_time` are only
known at settlement, so features must never read them before then.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from kalshibot.kalshi import KalshiClient
from kalshibot.kalshi.models import to_float

TIME_COLUMNS = ["open_time", "close_time", "expiration_time", "occurrence_datetime"]


def flatten_market(m: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "ticker": m["ticker"],
        "event_ticker": m["event_ticker"],
        "title": m.get("title"),
        "yes_sub_title": m.get("yes_sub_title"),
        "strike_type": m.get("strike_type"),
        "floor_strike": m.get("floor_strike"),
        "cap_strike": m.get("cap_strike"),
        "open_time": m.get("open_time"),
        "close_time": m.get("close_time"),
        "expiration_time": m.get("expiration_time"),
        "occurrence_datetime": m.get("occurrence_datetime"),
        "status": m.get("status"),
        "result": m.get("result"),
        "expiration_value": to_float(m.get("expiration_value")),
        "last_price": to_float(m.get("last_price_dollars")),
        "volume": to_float(m.get("volume_fp")),
        "open_interest": to_float(m.get("open_interest_fp")),
        "rules_primary": m.get("rules_primary"),
        "source": source,
    }


def backfill_markets(client: KalshiClient, series_ticker: str) -> pd.DataFrame:
    rows = [flatten_market(m, "historical")
            for m in client.iter_historical_markets(series_ticker=series_ticker)]
    rows += [flatten_market(m, "live")
             for m in client.iter_markets(series_ticker=series_ticker, status="settled")]

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for col in TIME_COLUMNS:
        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
    df["series_ticker"] = series_ticker
    return (df.drop_duplicates("ticker", keep="last")
              .sort_values(["close_time", "ticker"])
              .reset_index(drop=True))


def markets_path(data_dir: Path, series_ticker: str) -> Path:
    return data_dir / "raw" / "markets" / f"{series_ticker}.parquet"
