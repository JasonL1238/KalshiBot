"""Cross-checks on the real data (run with local data; skipped in CI)."""

import json
from datetime import UTC, date, datetime, timedelta

import duckdb
import pytest

from kalshibot.config import data_dir
from kalshibot.kalshi.fee_history import FeeTerms
from kalshibot.kalshi.models import on_tick
from kalshibot.research.calendar import BECKER_END
from kalshibot.research.load import API_FROM, BECKER_TRADES, Loader

pytestmark = pytest.mark.data
BECKER = data_dir() / "external" / "becker" / "data" / "kalshi" / "trades" / "*.parquet"
API = data_dir() / "raw" / "kalshi"


# The 15th of each month that Becker covers before API_FROM: the cross-check sample.
SAMPLE_DAYS = [date(y, m, 15).isoformat() for y in range(2021, 2026) for m in range(1, 13)
               if date(2021, 7, 1) <= date(y, m, 15) < API_FROM.date()]


def test_becker_archive_is_the_pinned_one():
    """The calendar's B and the tape boundary rest on this exact archive (RESEARCH §1)."""
    if not list(BECKER.parent.glob("*.parquet")):
        pytest.skip("Becker dataset not downloaded")
    count, last = connect().sql(
        f"SELECT count(*), max(created_time) FROM read_parquet('{BECKER}')").fetchone()
    assert count == 72_134_741
    assert last.astimezone(UTC) == datetime(2025, 11, 25, 22, 0, 15, 194245, tzinfo=UTC)
    assert BECKER_END == datetime.combine(last.astimezone(UTC).date() + timedelta(1),
                                          datetime.min.time(), UTC)


def api_days(before=None) -> list[str]:
    days = sorted(p.parent.name.removeprefix("date=") for p in API.glob("trades/*/part.parquet"))
    return [d for d in days if before is None or d < before]


def api_files(days) -> list[str]:
    return [str(API / "trades" / f"date={d}" / "part.parquet") for d in days]


def connect():
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    return con


def test_becker_and_the_api_agree_where_becker_is_used():
    """On sample days before API_FROM (where research reads Becker), every Becker trade is
    in the API tape with identical fields, and every API trade is in Becker."""
    days = api_days(before=API_FROM.date().isoformat())
    missing = sorted(set(SAMPLE_DAYS) - set(days))
    assert not missing, (f"{len(missing)} sample days missing, e.g. {missing[0]}: run "
                         f"`kalshibot backfill trades --start {missing[0]} --end <next day>` "
                         "for each (RESEARCH §1)")
    on_days = "created_time::DATE IN (" + ", ".join(f"DATE '{d}'" for d in days) + ")"
    con = connect()
    becker = con.sql(f"SELECT * FROM ({BECKER_TRADES}) WHERE {on_days}",  # noqa: F841
                     params={"becker": str(BECKER)})
    api = con.sql(f"SELECT * FROM read_parquet({api_files(days)}, "  # noqa: F841
                  "hive_partitioning = false)")
    missing_from_api, differing = con.sql("""
        SELECT count(*) FILTER (WHERE a.trade_id IS NULL),
               count(*) FILTER (WHERE a.trade_id IS NOT NULL AND (a.ticker <> b.ticker
                   OR a.yes_price <> b.yes_price OR a.count <> b.count
                   OR a.taker_side <> b.taker_side OR a.created_time <> b.created_time))
        FROM becker b LEFT JOIN api a USING (trade_id)""").fetchone()
    missing_from_becker, api_total = con.sql("""
        SELECT count(*) FILTER (WHERE b.trade_id IS NULL), count(*)
        FROM api a LEFT JOIN becker b USING (trade_id)""").fetchone()
    assert api_total > 0
    assert missing_from_api == 0 and differing == 0, (missing_from_api, differing)
    assert missing_from_becker == 0, f"{missing_from_becker}/{api_total} API trades not in Becker"


def test_every_trade_has_a_market():
    days = api_days()
    if not days or not (API / "markets.parquet").exists():
        pytest.skip("API trades or markets not downloaded yet")
    orphans, total = connect().sql(f"""
        SELECT count(*) FILTER (WHERE m.ticker IS NULL), count(*)
        FROM read_parquet({api_files(days)}, hive_partitioning = false) t
        LEFT JOIN read_parquet('{API}/markets.parquet') m USING (ticker)""").fetchone()
    assert orphans == 0, f"{orphans} of {total} trades have no row in markets.parquet"


def test_trades_sit_on_their_market_tick_grid():
    days = api_days()
    if not days or not (API / "markets.parquet").exists():
        pytest.skip("API trades or markets not downloaded yet")
    pairs = connect().sql(f"""
        SELECT DISTINCT t.yes_price, m.price_ranges
        FROM read_parquet({api_files(days)}, hive_partitioning = false) t
        JOIN read_parquet('{API}/markets.parquet') m USING (ticker)""").fetchall()
    off = [(p, r) for p, r in pairs if not on_tick(p, json.loads(r))]
    assert off == [], f"off-grid prices (tick structure may have changed): {off[:10]}"


def test_fee_schedule_reproduces_every_series_current_terms():
    if not (API / "series.parquet").exists():
        pytest.skip("series table not downloaded")
    schedule = Loader().fee_schedule()
    rows = connect().sql(f"SELECT series_ticker, fee_type, fee_multiplier, fetched_at "
                         f"FROM read_parquet('{API}/series.parquet')").fetchall()
    wrong = [s for s, ft, m, at in rows
             if schedule.terms(s, at).taker != FeeTerms.from_series(ft, m, "balance").taker
             or schedule.terms(s, at).maker != FeeTerms.from_series(ft, m, "balance").maker]
    assert wrong == [], f"{len(wrong)} series disagree, e.g. {wrong[:5]}"
