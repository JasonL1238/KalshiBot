from datetime import UTC, date, datetime

import httpx
import pandas as pd
import pyarrow.parquet as pq
import pytest

from kalshibot.data import backfill as bf
from kalshibot.kalshi import KalshiClient

MARKET = {  # shape of a real /historical/markets row (trimmed)
    "ticker": "KXBTCD-26AUG0819-T73799.99", "event_ticker": "KXBTCD-26AUG0819",
    "market_type": "binary", "strike_type": "greater", "floor_strike": 73799.99,
    "can_close_early": True, "price_level_structure": "linear_cent",
    "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}],
    "close_time": "2026-08-08T23:00:00Z", "settlement_ts": "2026-08-08T23:02:45.71192Z",
    "updated_time": "2026-08-08T23:03:00Z", "status": "finalized", "result": "no",
    "settlement_value_dollars": "0.0000", "notional_value_dollars": "1.0000",
    "volume_fp": "1234.56", "open_interest_fp": "", "exchange_index": 2,
    "rules_primary": "If the BRTI 60-second average ...", "expiration_value": "73512.55",
    "settlement_timer_seconds": 300, "custom_strike": {"team": "X"},
}


def trade(trade_id, ticker, created, price="0.5000", count="6.00"):
    return {"trade_id": trade_id, "ticker": ticker, "created_time": created, "count_fp": count,
            "yes_price_dollars": price, "taker_side": "yes", "is_block_trade": False}


class FakeClient:
    """Serves canned trades by window and records which endpoint each day used."""

    def __init__(self, cutoffs, historical=(), live=()):
        self.cutoffs, self.calls = list(cutoffs), []
        self.historical, self.live = list(historical), list(live)

    def historical_cutoff(self):
        cut = self.cutoffs.pop(0) if len(self.cutoffs) > 1 else self.cutoffs[0]
        return {"trades_created_ts": cut}

    def _serve(self, name, rows, min_ts, max_ts):
        self.calls.append((name, min_ts, max_ts))
        lo, hi = (datetime.fromtimestamp(t, UTC) for t in (min_ts, max_ts))
        return [r for r in rows if lo <= datetime.fromisoformat(r["created_time"]) <= hi]

    def iter_historical_trades(self, min_ts, max_ts):
        return self._serve("historical", self.historical, min_ts, max_ts)

    def iter_trades(self, min_ts, max_ts):
        return self._serve("live", self.live, min_ts, max_ts)


DAY = date(2025, 12, 1)
EDGES = [trade("a", "T", "2025-12-01T00:00:00Z"), trade("b", "T", "2025-12-01T23:59:59.9Z"),
         trade("c", "T", "2025-12-02T00:00:00Z"), trade("d", "KXMVE-X", "2025-12-01T12:00:00Z")]


def ids(path):
    return [r["trade_id"] for r in pq.read_table(path).to_pylist()]


def test_a_day_is_the_whole_half_open_utc_day(tmp_path):
    client = FakeClient(["2026-08-09T00:00:00Z"], historical=EDGES)
    path = bf.backfill_trades_day(client, tmp_path, DAY)
    assert ids(path) == ["a", "b"]  # last second kept; next midnight and combos dropped
    _, min_ts, max_ts = client.calls[0]
    assert max_ts - min_ts == 86_400


@pytest.mark.parametrize("cutoff, sources", [
    ("2026-08-09T00:00:00Z", ["historical"]),
    ("2025-11-01T00:00:00Z", ["live"]),
    ("2025-12-01T12:00:00Z", ["historical", "live"]),  # cutoff inside the day: both
])
def test_each_day_reads_the_archive_that_holds_it(tmp_path, cutoff, sources):
    client = FakeClient([cutoff], historical=EDGES[:2], live=EDGES[:2])
    path = bf.backfill_trades_day(client, tmp_path, DAY)
    assert [c[0] for c in client.calls] == sources and ids(path) == ["a", "b"]


def test_a_cutoff_move_over_the_day_refetches_it(tmp_path):
    client = FakeClient(["2025-12-01T00:00:00Z", "2025-12-03T00:00:00Z",
                         "2025-12-03T00:00:00Z"], historical=EDGES[:2], live=[])
    path = bf.backfill_trades_day(client, tmp_path, DAY)
    assert [c[0] for c in client.calls] == ["live", "historical"] and ids(path) == ["a", "b"]


def test_unfinished_days_are_refused_and_finished_ones_resume(tmp_path):
    with pytest.raises(ValueError):
        bf.backfill_trades_day(FakeClient(["2026-08-09T00:00:00Z"]), tmp_path,
                               datetime.now(UTC).date())
    bf._write([], bf.trades_path(tmp_path, DAY), bf.TRADE_SCHEMA)
    made = []
    bf.backfill_trades(lambda: made.append(1) or FakeContext(), tmp_path, DAY,
                       date(2025, 12, 2))
    assert made == []  # the existing day was skipped


class FakeContext(FakeClient):
    def __init__(self):
        super().__init__(["2026-08-09T00:00:00Z"])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def test_market_rows_keep_rules_and_fixed_point_units(tmp_path):
    row = bf.flatten_market(MARKET, "historical")
    assert (row["settlement_value"], row["notional_value"], row["volume"]) == (0, 10_000, 123_456)
    assert row["open_interest"] is None and row["expiration_value"] == "73512.55"
    path = bf._write([row], tmp_path / "m.parquet", bf.MARKET_SCHEMA)
    assert pq.read_schema(path) == bf.MARKET_SCHEMA


def test_market_merge_keeps_the_newest_copy(tmp_path):
    old = {**MARKET, "status": "closed", "updated_time": "2026-08-08T23:00:30Z"}
    other = {**MARKET, "ticker": "ONLY-HISTORICAL"}

    class Markets:
        def iter_markets(self, mve_filter):
            return [old]

        def iter_historical_markets(self, mve_filter):
            return [MARKET, other]

    rows = pq.read_table(bf.backfill_markets(Markets(), tmp_path)).to_pylist()
    assert {r["ticker"]: r["status"] for r in rows} == {MARKET["ticker"]: "finalized",
                                                       "ONLY-HISTORICAL": "finalized"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["markets.parquet"]


def test_a_failed_write_leaves_nothing_behind(tmp_path):
    def rows():
        yield bf.flatten_market(MARKET, "live")
        raise RuntimeError("connection dropped")

    with pytest.raises(RuntimeError):
        bf._write(rows(), tmp_path / "m.parquet", bf.MARKET_SCHEMA)
    assert list(tmp_path.iterdir()) == []


def test_client_retries_dropped_gets_but_never_other_methods():
    attempts = []

    def handler(request):
        attempts.append(request.method)
        if len(attempts) == 1:
            raise httpx.ConnectError("dropped")
        return httpx.Response(200, json={"ok": True})

    client = KalshiClient("https://x.test/trade-api/v2", min_interval=0)
    client._http = httpx.Client(base_url="https://x.test/trade-api/v2",
                                transport=httpx.MockTransport(handler))
    assert client._request("GET", "/exchange/status") == {"ok": True}
    attempts.clear()
    with pytest.raises(httpx.ConnectError):
        client._request("POST", "/portfolio/events/orders", json={})
    assert attempts == ["POST"]


def test_candles_route_by_the_raw_tables_and_read_both_payload_shapes(tmp_path):
    calls = []
    archive = {"end_period_ts": None, "volume": "321.67", "open_interest": "12034.40",
               "price": {"open": "0.4900", "high": "0.5400", "low": "0.4800",
                         "close": "0.5200", "mean": "0.5061", "previous": "0.5000"},
               "yes_bid": {"open": "0.4900", "high": "0.5100", "low": "0.4800",
                           "close": "0.5000"},
               "yes_ask": {"open": "0.5000", "high": "0.5400", "low": "0.4900",
                           "close": "0.5200"}}
    live = {"end_period_ts": None, "volume_fp": "0.00", "open_interest_fp": "1599.17",
            "price": {"previous_dollars": "0.0100"},
            "yes_bid": {f"{k}_dollars": "0.0000" for k in bf.OHLC},
            "yes_ask": {f"{k}_dollars": "0.0100" for k in bf.OHLC}}

    class Candles:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def historical_cutoff(self):
            return {"market_settled_ts": "2025-12-01T00:00:00Z"}

        def get_candlesticks(self, ticker, start_ts, end_ts, period_interval, series_ticker):
            calls.append((ticker, series_ticker, end_ts - start_ts, period_interval))
            if ticker == "MOVED" and series_ticker:  # archived since the tables were read
                raise httpx.HTTPStatusError("gone", request=httpx.Request("GET", "x"),
                                            response=httpx.Response(404))
            shape = live if series_ticker else archive
            return [{**shape, "end_period_ts": ts} for ts in (start_ts, start_ts + 60)]

    blank = {c: None for c in bf.MARKET_SCHEMA.names}
    bf._write([{**blank, "ticker": t, "event_ticker": "E", "settlement_ts": s}
               for t, s in (("OLD", datetime(2025, 11, 2, tzinfo=UTC)), ("NEW", None),
                            ("MOVED", None))], tmp_path / "markets.parquet", bf.MARKET_SCHEMA)
    bf._write([{"event_ticker": "E", "series_ticker": "S"}], tmp_path / "events.parquet",
              bf.EVENT_SCHEMA)
    needs = pd.DataFrame({"ticker": ["OLD", "NEW", "MOVED"], "day": [date(2025, 11, 1)] * 3})
    bf.backfill_candles(Candles, tmp_path, needs, workers=2)
    assert len(calls) == 4 and set(calls) == {
        ("MOVED", "S", 86_400, 1), ("MOVED", None, 86_400, 1),  # live 404s: the archive
        ("NEW", "S", 86_400, 1), ("OLD", None, 86_400, 1)}
    read = {t: pq.read_table(bf.candles_path(tmp_path, t, date(2025, 11, 1))).to_pylist()
            for t in ("OLD", "NEW")}
    assert len(read["OLD"]) == 1  # a candle ending at the day's start belongs to the day before
    old, new = read["OLD"][0], read["NEW"][0]
    assert (old["yes_bid_low"], old["yes_ask_high"], old["price_mean"]) == (4800, 5400, 5061)
    assert (old["volume"], old["open_interest"]) == (32167, 1203440)
    assert (new["yes_bid_close"], new["yes_ask_close"], new["price_close"]) == (0, 100, None)
    assert (new["price_previous"], new["open_interest"]) == (100, 159917)
    bf.backfill_candles(Candles, tmp_path, needs, workers=1)  # resumes: nothing refetched
    assert len(calls) == 4


def test_an_unknown_candle_shape_is_refused(tmp_path):
    with pytest.raises(ValueError, match="unrecognized candle"):
        bf._candle_row("X", {"end_period_ts": 60, "yes_bid": {"last": "0.5"}, "yes_ask": {}})
