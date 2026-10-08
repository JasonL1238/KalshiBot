from datetime import datetime, timedelta

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from kalshibot.config import data_dir
from kalshibot.data import backfill as bf
from kalshibot.research.load import Loader

T = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731


def pytest_collection_modifyitems(config, items):
    """Skip `data` tests without local data (e.g. in CI) and `slow` tests unless -m asks."""
    raw = data_dir() / "raw"
    skips = {}
    if not raw.exists():
        skips["data"] = pytest.mark.skip(reason=f"no local data under {raw}")
    if "slow" not in (config.getoption("markexpr") or ""):
        skips["slow"] = pytest.mark.skip(reason="slow; run with -m slow")
    for item in items:
        for marker, skip in skips.items():
            if item.get_closest_marker(marker):
                item.add_marker(skip)


@pytest.fixture
def loader(tmp_path, monkeypatch):
    """A Loader over small tables: API trade days 2025-09-29 → 2025-10-10, markets A–D,
    events, series, and a fee change; an empty ledger."""
    (tmp_path / "ledger").mkdir()
    monkeypatch.setenv("KALSHIBOT_LEDGER", str(tmp_path / "ledger"))
    kalshi = tmp_path / "raw" / "kalshi"
    trades = {
        "2025-10-01": [("a2", "A", "yes", T("2025-10-01 12:00"), 5500, 300)],
        "2025-10-09": [("a0", "B", "no", T("2025-10-09 12:00"), 9000, 100)],
        "2025-10-10": [("a1", "A", "yes", T("2025-10-10"), 650, 200)],
    }
    for n in range(12):
        day = (datetime(2025, 9, 29) + timedelta(n)).date().isoformat()
        rows = [dict(zip(["trade_id", "ticker", "taker_side", "created_time", "yes_price",
                          "count"], r, strict=True), is_block_trade=False)
                for r in trades.get(day, [])]
        bf._write(rows, kalshi / "trades" / f"date={day}" / "part.parquet", bf.TRADE_SCHEMA)
    market = {c: None for c in bf.MARKET_SCHEMA.names}
    bf._write([
        {**market, "ticker": "A", "event_ticker": "EA", "can_close_early": False,
         "created_time": T("2025-01-01"), "close_time": T("2025-12-01"),
         "settlement_ts": T("2025-12-01 01:00"), "result": "yes", "settlement_value": 10_000,
         "volume": 500,
         "rules_primary": "If ..."},
        {**market, "ticker": "B", "event_ticker": "EB", "can_close_early": True,
         "created_time": T("2025-01-01"), "close_time": T("2025-11-01"),
         "settlement_ts": T("2025-11-01 00:05"), "result": "yes", "settlement_value": 10_000,
         "volume": 900},
        {**market, "ticker": "C", "event_ticker": "EB", "created_time": T("2025-11-15")},
        {**market, "ticker": "D", "event_ticker": "EB", "can_close_early": False,
         "created_time": T("2026-01-01"), "close_time": T("2026-06-01"),
         "settlement_ts": T("2026-06-01 01:00"), "result": "no",
         "settlement_value": 0},  # settles in the lockbox
    ], kalshi / "markets.parquet", bf.MARKET_SCHEMA)
    pq.write_table(pa.table({"event_ticker": ["EA", "EB"], "series_ticker": ["SA", "SB"],
                             "mutually_exclusive": [True, False]}), kalshi / "events.parquet")
    pq.write_table(pa.table({"series_ticker": ["SA", "SB"], "category": ["Weather", "Crypto"],
                             "fee_type": ["quadratic"] * 2, "fee_multiplier": ["1"] * 2}),
                   kalshi / "series.parquet")
    bf._write([{"series_ticker": "SA", "fee_type": "quadratic", "fee_multiplier": "0",
                "scheduled_ts": T("2025-11-01"), "fetched_at": T("2026-10-08")}],
              kalshi / "fee_changes.parquet", bf.FEE_CHANGE_SCHEMA)
    return Loader(tmp_path)
