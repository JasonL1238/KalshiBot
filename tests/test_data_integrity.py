"""Cross-checks on the real data (run with local data; skipped in CI)."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from kalshibot.config import data_dir
from kalshibot.data.backfill import COMBO_PREFIX
from kalshibot.kalshi.fee_history import FeeTerms
from kalshibot.kalshi.models import on_tick
from kalshibot.research.calendar import BECKER_END, DISCOVERY_START
from kalshibot.research.load import Loader, connect

pytestmark = pytest.mark.data
BECKER = data_dir() / "external" / "becker" / "data" / "kalshi" / "trades" / "*.parquet"
API = data_dir() / "raw" / "kalshi"


# Becker stores integer cents and whole contracts; scale them to kalshi.models units. From
# 2025-10-09 it truncates sub-cent prices, so it is compared only before then, on the 15th
# of each month (RESEARCH §1).
BECKER_TRADES = f"""
    SELECT trade_id, ticker, created_time, yes_price * 100 AS yes_price, count * 100 AS count,
           taker_side
    FROM read_parquet($becker) WHERE ticker NOT LIKE '{COMBO_PREFIX}%'"""
BECKER_EXACT_UNTIL = date(2025, 10, 9)
SAMPLE_DAYS = [date(y, m, 15).isoformat() for y in range(2021, 2026) for m in range(1, 13)
               if date(2021, 7, 1) <= date(y, m, 15) < BECKER_EXACT_UNTIL]


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


def test_becker_is_an_exact_subset_of_the_api_tape_missing_only_whole_markets():
    """On the sample days, every Becker trade is in the API tape with identical fields, and
    every API trade Becker lacks belongs to a market Becker lacks entirely."""
    days = api_days(before=BECKER_EXACT_UNTIL.isoformat())
    missing = sorted(set(SAMPLE_DAYS) - set(days))
    assert not missing, (f"{len(missing)} sample days missing, e.g. {missing[0]}: run "
                         f"`kalshibot backfill trades --start {missing[0]} --end <next day>` "
                         "for each (RESEARCH §1)")
    on_days = "created_time::DATE IN (" + ", ".join(f"DATE '{d}'" for d in SAMPLE_DAYS) + ")"
    con = connect()
    params = {"becker": str(BECKER)}
    becker = con.sql(f"SELECT * FROM ({BECKER_TRADES}) WHERE {on_days}",  # noqa: F841
                     params=params)
    becker_markets = con.sql(f"SELECT DISTINCT ticker FROM ({BECKER_TRADES})",  # noqa: F841
                             params=params)
    api = con.sql(f"SELECT * FROM read_parquet({api_files(SAMPLE_DAYS)}, "  # noqa: F841
                  "hive_partitioning = false)")
    missing_from_api, differing = con.sql("""
        SELECT count(*) FILTER (WHERE a.trade_id IS NULL),
               count(*) FILTER (WHERE a.trade_id IS NOT NULL AND (a.ticker <> b.ticker
                   OR a.yes_price <> b.yes_price OR a.count <> b.count
                   OR a.taker_side <> b.taker_side OR a.created_time <> b.created_time))
        FROM becker b LEFT JOIN api a USING (trade_id)""").fetchone()
    in_known_markets, only_api, only_api_markets, api_total = con.sql("""
        SELECT count(*) FILTER (WHERE b.trade_id IS NULL
                                AND a.ticker IN (SELECT ticker FROM becker_markets)),
               count(*) FILTER (WHERE b.trade_id IS NULL),
               count(DISTINCT a.ticker) FILTER (WHERE b.trade_id IS NULL), count(*)
        FROM api a LEFT JOIN becker b USING (trade_id)""").fetchone()
    assert missing_from_api == 0 and differing == 0, (missing_from_api, differing)
    assert in_known_markets == 0, f"{in_known_markets} trades dropped within Becker's markets"
    # the gap RESEARCH §1 reports
    assert (only_api, api_total, only_api_markets) == (6_733, 1_109_042, 3_714)


def test_every_trade_has_a_market():
    days = api_days()
    if not days or not (API / "markets.parquet").exists():
        pytest.skip("API trades or markets not downloaded yet")
    # Trades are counted per key before the join (Loader.trades says why).
    orphans, total = connect().sql(f"""
        WITH t AS (SELECT ticker, count(*) AS n
                   FROM read_parquet({api_files(days)}, hive_partitioning = false) GROUP BY ALL)
        SELECT coalesce(sum(n) FILTER (WHERE m.ticker IS NULL), 0), sum(n)
        FROM t LEFT JOIN read_parquet('{API}/markets.parquet') m USING (ticker)""").fetchone()
    assert orphans == 0, f"{orphans} of {total} trades have no row in markets.parquet"


def test_trades_sit_on_their_market_tick_grid():
    """`price_ranges` is a market's current grid, and a long-lived market can change grids
    (KXGREENLAND-29 traded on $0.001 in Oct 2025, KXCITRINI-28JUL01 through March 2026, and
    both are coarser now), so some trades may be off it, but under 0.1%, and never off the
    $0.001 grid: a units bug would put nearly all of them off."""
    days = api_days()
    if not days or not (API / "markets.parquet").exists():
        pytest.skip("API trades or markets not downloaded yet")
    rows = connect().sql(f"""
        WITH t AS (SELECT ticker, yes_price, count(*) AS n  -- see Loader.trades
                   FROM read_parquet({api_files(days)}, hive_partitioning = false) GROUP BY ALL)
        SELECT t.yes_price, m.price_ranges, sum(n)
        FROM t JOIN read_parquet('{API}/markets.parquet') m USING (ticker)
        GROUP BY ALL""").fetchall()
    off = [(p, n) for p, r, n in rows if not on_tick(p, json.loads(r))]
    assert sum(n for _, n in off) <= 1e-3 * sum(n for *_, n in rows), off[:10]
    assert all(p % 10 == 0 for p, _ in off), f"off even the $0.001 grid: {off[:10]}"


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


def test_positive_control_the_favorite_longshot_pattern():
    """The criterion in BACKTESTING.md's positive control, on discovery."""
    from kalshibot.research.calibration import taker_calibration
    from kalshibot.research.load import MissingData

    try:
        table = taker_calibration(Loader(), DISCOVERY_START, BECKER_END,
                                  BECKER_END).set_index("bin_cents")
    except MissingData:
        pytest.skip("discovery-period API tape not downloaded yet")
    longshots, favorites = table.loc[table.index < 20], table.loc[table.index >= 85]
    assert (longshots["win_rate"] < longshots["avg_price"]).all(), longshots
    assert table.loc[0, "gross_return"] < -0.20
    assert (favorites["gross_return"].abs() < 0.03).all(), favorites
