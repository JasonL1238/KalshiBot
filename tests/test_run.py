"""The research runner on a small synthetic market: each day, a two-bucket event whose
longshot bucket the test strategy buys NO on."""

import hashlib
import json
import tracemalloc
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from itertools import pairwise

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from kalshibot import cli
from kalshibot.data import backfill as bf
from kalshibot.research import calendar, ledger, run
from kalshibot.research.backtest import fills, settle
from kalshibot.research.load import Loader, MissingData
from kalshibot.research.validation import gauntlet
from kalshibot.strategies.base import INTENT_COLUMNS

D0 = datetime(2025, 1, 1, tzinfo=UTC)
DAYS = 40  # discovery: the first 20, confirmation: the rest
FETCHED = D0 + timedelta(days=DAYS + 5)  # the markets table, fetched after the windows


class BuyNoEachMorning:
    """Buy NO on each day's longshot bucket at 09:59."""

    family = "HX"
    max_hold = timedelta(days=1)
    min_edge = 50

    def configs(self):
        return {"one": {"count": 100}, "two": {"count": 200}}

    def intents(self, data, start, end, config):
        listed = data.markets(end).filter(
            f"created_time >= '{start}' AND ticker LIKE 'KXT-%' AND ticker NOT LIKE '%-B'"
        ).project("ticker").df()
        rows = [(n, t, "no", pd.Timestamp(t.split("-", 1)[1], tz="UTC") + timedelta(
            hours=9, minutes=59), config["count"], None) for n, t in enumerate(listed.ticker)]
        return pd.DataFrame(rows, columns=INTENT_COLUMNS)


class Candles:
    """The candle API for the fixture's markets: a quote change at 09:00 on the bucket's
    day, and on its sibling the day before it, so finding it takes a second round."""

    calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def historical_cutoff(self):
        return {"market_settled_ts": "2030-01-01T00:00:00Z"}

    def get_candlesticks(self, ticker, start_ts, end_ts, period_interval, series_ticker):
        Candles.calls.append((ticker, start_ts))
        day = pd.Timestamp(ticker.split("-", 1)[1].removesuffix("-B"), tz="UTC")
        at, (bid, ask) = ((day - timedelta(hours=15), ("0.8800", "0.9200"))
                          if ticker.endswith("-B") else (day + timedelta(hours=9),
                                                         ("0.0900", "0.1100")))
        ts = int(at.timestamp())
        return [] if not start_ts < ts <= end_ts else [{
            "end_period_ts": ts, "volume": "0.00", "open_interest": "10.00", "price": {},
            "yes_bid": {"open": bid, "high": bid, "low": bid, "close": bid},
            "yes_ask": {"open": ask, "high": ask, "low": ask, "close": ask}}]


@pytest.fixture
def market(tmp_path, monkeypatch):
    (tmp_path / "ledger").mkdir()
    monkeypatch.setenv("KALSHIBOT_LEDGER", str(tmp_path / "ledger"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(calendar, "DISCOVERY_START", D0)
    monkeypatch.setattr(calendar, "BECKER_END", D0 + timedelta(days=20))
    monkeypatch.setattr(calendar, "LOCKBOX_START", D0 + timedelta(days=DAYS))
    monkeypatch.setattr(gauntlet, "PREREGISTERED",  # power's machinery, not its precision
                        replace(gauntlet.PREREGISTERED, power_worlds=20, power_resamples=200))
    Candles.calls = []
    kalshi = tmp_path / "raw" / "kalshi"
    rng = np.random.default_rng(0)
    markets = []
    blank = {c: None for c in bf.MARKET_SCHEMA.names}
    for n in range(-1, DAYS + 1):
        day = D0 + timedelta(days=n)
        ticker = f"KXT-{day.date()}"
        yes = bool(rng.random() < 0.1)
        rows = [{"trade_id": f"{ticker}-{k}", "ticker": ticker, "taker_side": side,
                 "created_time": day + timedelta(hours=10, minutes=k), "yes_price": 1000,
                 "count": 1000, "is_block_trade": False}
                for k, side in ((-60, "yes"), (0, "no"), (1, "yes"))]
        bf._write(rows, bf.trades_path(kalshi, day.date()), bf.TRADE_SCHEMA)
        for t, won, listed in ((ticker, yes, day), (f"{ticker}-B", not yes, day - timedelta(1))):
            markets.append({**blank, "ticker": t, "event_ticker": f"EV-{day.date()}",
                            "can_close_early": True, "created_time": listed,
                            "strike_type": "between",
                            "latest_expiration_time": day + timedelta(hours=20),
                            "close_time": day + timedelta(hours=20),
                            "settlement_ts": day + timedelta(hours=21),
                            "result": "yes" if won else "no",
                            "settlement_value": 10_000 if won else 0, "fetched_at": FETCHED})
    markets.append({**blank, "ticker": "LONG", "event_ticker": "EV-LONG",  # expires late
                    "created_time": D0 - timedelta(days=5),
                    "latest_expiration_time": D0 + timedelta(days=DAYS), "fetched_at": FETCHED})
    bf._write(markets, kalshi / "markets.parquet", bf.MARKET_SCHEMA)
    events = sorted({m["event_ticker"] for m in markets})
    pq.write_table(pa.table({"event_ticker": events, "series_ticker": ["KXT"] * len(events),
                             "mutually_exclusive": [e != "EV-LONG" for e in events],
                             "fetched_at": [FETCHED] * len(events)}),
                   kalshi / "events.parquet")
    pq.write_table(pa.table({"series_ticker": ["KXT"], "category": ["Test"],
                             "fee_type": ["quadratic"], "fee_multiplier": ["1"]}),
                   kalshi / "series.parquet")
    bf._write([], kalshi / "fee_changes.parquet", bf.FEE_CHANGE_SCHEMA)
    return Loader(tmp_path, family="HX")


@pytest.fixture
def power_calls(monkeypatch):
    """The positions each `gauntlet.power` call is given."""
    seen, power = [], gauntlet.power
    monkeypatch.setattr(gauntlet, "power", lambda d, p, *a: seen.append(p) or power(d, p, *a))
    return seen


def test_discover_fetch_confirm_once(market, power_calls):
    strategy = BuyNoEachMorning()
    discovery = run.discover(strategy, market)
    assert list(discovery.columns) == ["one", "two"] and len(discovery) == 20
    trials = ledger.trials("HX")
    assert [t["window"] for t in trials] == ["discovery", "discovery"]
    assert trials[0]["trial"]["max_hold"] == "1 day, 0:00:00"
    assert trials[0]["trial"]["min_edge"] == 50
    with pytest.raises(MissingData):  # candles first; refusing doesn't use up the test
        run.confirm(strategy, market)
    lines = (market.kalshi_dir.parents[1] / "ledger" / "HX.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert run.fetch_candles(strategy, market, workers=2, make_client=Candles) > 0
    rounds = len({start for _, start in Candles.calls})
    assert rounds > 1 and run.fetch_candles(strategy, market, make_client=Candles) == 0
    verdict = run.confirm(strategy, market)
    assert len(set(power_calls[0].day)) == 18  # dated when priced (a taker's decision), one a day
    assert 0 < verdict.p_value <= 1 and verdict.config in {"one", "two"}
    done = ledger.trials("HX")[-1]
    assert done["window"] == "confirmation" and done["summary"]["seed"] == run._seed("HX")
    assert done["summary"]["tested"] == done["summary"]["positions"] == 18
    assert done["summary"]["discovery_trials"] == [t["trial_id"] for t in trials]
    assert 0 <= done["summary"]["power"]["power"] <= 1
    assert done["summary"]["by_category"]["Test"]["positions"] == 18
    entries = [json.loads(line) for line in (market.kalshi_dir.parents[1] / "ledger"
                                             / "HX.jsonl").read_text().splitlines()]
    assert [e["before"] for e in entries if e["kind"] == "start"] == [
        {"power": done["summary"]["power"]}]  # fixed before any outcome was read
    with pytest.raises(RuntimeError):
        run.confirm(strategy, market)
    assert ledger.trials("HX")[-1] == done


def test_tier2_reports_after_the_verdict_and_again(market, monkeypatch):
    strategy = BuyNoEachMorning()
    run.discover(strategy, market)
    with pytest.raises(RuntimeError, match="no confirmation"):
        run.report(strategy, market)
    run.fetch_candles(strategy, market, make_client=Candles)
    run.confirm(strategy, market)
    done = ledger.trials("HX")[-1]
    report = run.report(strategy, market)
    tier1, tiers = report["tier1"], report["tier2"]
    assert tier1 == {"intents": 18, "contracts": 18.0, "net_pnl": done["summary"]["net_pnl"]}
    assert report["confirmation"] == done["trial_id"]
    for engine in ("nautilus", "vectorbt"):  # both fill all 18 as Tier 1 does; books agree
        assert tiers[engine] == tier1 | {"accounting_gap": 0}
    monkeypatch.setattr(run, "TIER2_MARKETS", 1)  # batching changes nothing
    assert run.report(strategy, market)["tier2"] == tiers
    lines = (market.kalshi_dir.parents[1] / "ledger" / "HX.jsonl").read_text().splitlines()
    kinds = [json.loads(line)["kind"] for line in lines]
    assert kinds[-3:] == ["trial", "report", "report"] and ledger.trials("HX")[-1] == done
    assert json.loads(lines[-2])["report"] == json.loads(json.dumps(report))

    monkeypatch.setattr(run, "TIER2_MARKETS", 200)
    replay = run.replay.replay

    def skewed(*args):  # its books disagree by ±0.5¢ a market, which cancel in a sum
        got, own = replay(*args)
        return got, own.assign(pnl=own["pnl"] + np.resize([5_000, -5_000], len(own)))

    monkeypatch.setattr(run.replay, "replay", skewed)
    assert run.report(strategy, market)["tier2"]["nautilus"]["accounting_gap"] == 18 * 5_000

    class Fewer(BuyNoEachMorning):  # its code changed since the confirmation
        def intents(self, *args):
            return super().intents(*args).iloc[::2]

    class More(BuyNoEachMorning):  # ... adding intents only Tier 2 fills (an older print)
        def intents(self, *args):
            mine = super().intents(*args)
            late = mine.assign(intent_id=mine["intent_id"] + len(mine), side="yes",
                               decision_time=mine["decision_time"] + timedelta(minutes=31))
            return pd.concat([mine, late], ignore_index=True)

    for changed in (Fewer(), More()):
        with pytest.raises(RuntimeError, match="no longer what"):
            run.report(changed, market)


def test_tier2_refuses_a_changed_configuration_and_skips_makers(market):
    window = list(calendar.confirmation_window("HX"))

    class Maker(BuyNoEachMorning):
        def configs(self):
            return {"one": {"count": 100, "maker": True, "horizon": timedelta(hours=1)}}

    trial = {"name": "one", "config": Maker().configs()["one"], "max_hold": timedelta(days=1),
             "min_edge": 50, "window": window}
    ledger.record_trial("HX", trial, "confirmation", "x", pd.Series(dtype=float),
                        {"positions": 0, "net_pnl": 0})
    assert "order books" in run.report(Maker(), market)["tier2"]
    with pytest.raises(RuntimeError, match="isn't what"):
        run.report(BuyNoEachMorning(), market)


def test_the_cli_runs_the_confirmation_and_its_report(market, monkeypatch):
    strategy = BuyNoEachMorning()
    monkeypatch.setitem(cli.FAMILIES, "HX", strategy)
    runner = CliRunner()
    assert runner.invoke(cli.app, ["research", "discover", "HX"]).exit_code == 0
    run.fetch_candles(strategy, market, make_client=Candles)
    out = runner.invoke(cli.app, ["research", "confirm", "HX"])
    assert out.exit_code == 0 and "power at 0.5¢" in out.output and "Tier 2" in out.output
    assert runner.invoke(cli.app, ["research", "report", "HX"]).exit_code == 0


def test_a_forward_family_confirms_on_its_own_window(market, monkeypatch):
    monkeypatch.setattr(calendar, "FORWARD_START", D0 + timedelta(days=20))
    monkeypatch.setattr(calendar, "FORWARD", frozenset({"HX"}))
    monkeypatch.setattr(calendar, "FORWARD_TESTS", {"HX": (D0 + timedelta(days=25),
                                                           D0 + timedelta(days=DAYS))})
    strategy = BuyNoEachMorning()
    run.discover(strategy, market)
    run.fetch_candles(strategy, market, make_client=Candles)
    run.confirm(strategy, market)
    done = ledger.trials("HX")[-1]
    assert done["trial"]["window"] == [str(D0 + timedelta(days=25)), str(D0 + timedelta(DAYS))]
    assert done["summary"]["positions"] == 13  # decisions on days 26–38


def test_confirm_refuses_markets_fetched_before_the_window_ended(market, monkeypatch):
    monkeypatch.setattr(calendar, "LOCKBOX_START", FETCHED + timedelta(days=1))
    strategy = BuyNoEachMorning()
    run.discover(strategy, market)
    with pytest.raises(RuntimeError, match="backfill reference"):
        run.confirm(strategy, market)
    ledger_file = market.kalshi_dir.parents[1] / "ledger" / "HX.jsonl"
    assert '"confirmation"' not in ledger_file.read_text()  # no start marker either


@pytest.mark.parametrize("module, name", [(settle, "charged_fee"), (gauntlet, "power")])
def test_a_crash_settling_or_in_power_happens_before_the_test_starts(market, monkeypatch,
                                                                       module, name):
    strategy = BuyNoEachMorning()
    run.discover(strategy, market)
    run.fetch_candles(strategy, market, make_client=Candles)
    monkeypatch.setattr(module, name, lambda *a, **k: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        run.confirm(strategy, market)
    ledger_file = market.kalshi_dir.parents[1] / "ledger" / "HX.jsonl"
    assert '"confirmation"' not in ledger_file.read_text()


def test_power_dates_each_position_by_its_clusters_last_pricing():
    n = np.zeros(3)
    priced = gauntlet.Positions(n, n, n, n.astype(bool), n, n, n, n + 1,
                                cluster=np.array([0, 0, 1]), day=np.array([3, 5, 4]))
    assert run._by_last_priced(priced).day.tolist() == [5, 5, 4]


def test_confirm_needs_discovery_as_configured_now(market):
    class Changed(BuyNoEachMorning):
        def configs(self):
            return {"one": {"count": 100}, "two": {"count": 300}}

    class Fewer(BuyNoEachMorning):
        def configs(self):
            return {"one": {"count": 100}}

    class Pickier(BuyNoEachMorning):  # power judged at another edge
        min_edge = 100

    run.discover(BuyNoEachMorning(), market)
    for changed in (Changed(), Fewer(), Pickier()):
        with pytest.raises(RuntimeError, match="run discover"):
            run.confirm(changed, market)
    with pytest.raises(ValueError):
        run.discover(BuyNoEachMorning(), Loader(market.kalshi_dir.parent.parent, family="HY"))


def test_decisions_stay_inside_their_window_and_holding_period(market):
    s = BuyNoEachMorning()
    evaluated = run._evaluate(s, market, {"count": 100}, D0, D0 + timedelta(days=10))
    filled, outside = evaluated.fills, evaluated.outside_hold
    days = filled["priced_at"].dt.floor("D")
    assert (filled["priced_at"].dt.strftime("%H:%M") == "09:59").all()  # a taker's decision
    assert outside == 0
    assert days.min() == pd.Timestamp(D0 + timedelta(days=1))
    assert days.max() == pd.Timestamp(D0 + timedelta(days=8))

    class Long(BuyNoEachMorning):
        def intents(self, data, start, end, config):  # LONG may settle long after
            return pd.DataFrame([(0, "LONG", "no", start + timedelta(hours=1), 100, None)],
                                columns=INTENT_COLUMNS)

    evaluated = run._evaluate(Long(), market, {}, D0, D0 + timedelta(days=10))
    assert evaluated.fills.empty and evaluated.outside_hold == 1


class Fixed(BuyNoEachMorning):
    """The same intents (ids, decision times) whatever the window."""

    def __init__(self, ids, times):
        self.rows = [(n, "KXT-2025-01-03", "no", t, 100, None)
                     for n, t in zip(ids, times, strict=True)]

    def intents(self, data, start, end, config):
        return pd.DataFrame(self.rows, columns=INTENT_COLUMNS)


@pytest.mark.parametrize(("ids", "times", "refusal"), [
    ([0, -1], [3, 4], "integer intent ids"), ([0, 0.5], [3, 4], "integer intent ids"),
    ([1, 1], [3, 4], "integer intent ids"),
    ([0, 1], [3, 0], "outside"), ([0, 1], [3, 9], "outside"), ([0, 1], [3, None], "outside")])
def test_the_runner_refuses_intents_off_the_contract(market, ids, times, refusal):
    times = [pd.NaT if d is None else D0 + timedelta(days=d, hours=10) for d in times]
    with pytest.raises(ValueError, match=refusal):  # decisions in [D0 + 1, D0 + 9)
        run._evaluate(Fixed(ids, times), market, {}, D0, D0 + timedelta(days=10))


def test_the_runner_refuses_an_order_that_can_fill_past_max_hold(market):
    class LongBid(RestingNo):  # a bid resting as long as a position may be held
        def configs(self):
            return {"one": {"count": 200, "maker": True, "horizon": timedelta(days=1)}}

    class QuickTaker(BuyNoEachMorning):  # a taker waits up to 5 minutes
        max_hold = timedelta(minutes=5)

    for strategy in (LongBid(), QuickTaker()):
        with pytest.raises(ValueError, match="past max_hold"):
            run.discover(strategy, market)
    assert ledger.trials("HX") == []  # before any configuration is recorded


def inside(intents: pd.DataFrame, start, end) -> pd.DataFrame:
    """The intents decided in [start, end), all a strategy may return for that window."""
    t = intents["decision_time"]
    return intents[(t >= start) & (t < end)]


class RestingNo(BuyNoEachMorning):
    """Bid 95¢ for 2 NO on each day's longshot at 21:59 the evening before it lists,
    resting 13 hours: the YES buyers at 10¢ at 09:00 and at 10:01 each sell 10 NO
    through it, one contract apiece at a tenth (`participation`, with no queue haircut)."""

    def configs(self):
        return {"one": {"count": 200, "maker": True, "horizon": timedelta(hours=13)}}

    def intents(self, data, start, end, config):
        bids = super().intents(data, start, end, config)
        return inside(bids.assign(decision_time=bids["decision_time"] - timedelta(hours=12),
                                  limit=9500), start, end)


def test_a_makers_fills_are_priced_and_dated_when_they_fill(market, monkeypatch,
                                                            power_calls):
    monkeypatch.setattr(fills, "maker_fills", partial(fills.maker_fills, queue_haircut=0.0))
    strategy, config = RestingNo(), RestingNo().configs()["one"]
    got = run._evaluate(strategy, market, config, D0, D0 + timedelta(10)).fills
    assert (got.groupby("intent_id").size() == 2).all() and (got["fill_count"] == 100).all()
    assert (got["priced_at"] == got["fill_time"]).all()
    run.discover(strategy, market)
    run.fetch_candles(strategy, market, make_client=Candles)
    run.confirm(strategy, market)
    done = ledger.trials("HX")[-1]["summary"]
    # priced at the decision, before the longshot listed, none would be tested, and power
    # would date each position a day early
    assert done["tested"] == done["positions"] == 2 * done["intents"]["count"] > 0
    tested = run._evaluate(strategy, market, config, *calendar.confirmation_window("HX")).fills
    fill_days = (settle.close_day(tested["fill_time"]) - run._EPOCH).dt.days
    assert set(power_calls[0].day) == set(fill_days)


def test_maker_intents_rest_one_bid_per_contract_with_a_limit(market):
    t = pd.Timestamp("2026-01-05", tz="UTC")
    bids = pd.DataFrame([(0, "T", "no", t, 100, 9500.0),
                         (1, "T", "no", t + timedelta(hours=3), 100, 9500.0),
                         (2, "T", "yes", t + timedelta(hours=1), 100, 500.0)],
                        columns=INTENT_COLUMNS)
    run._resting(bids, timedelta(hours=3), "HX")  # back to back, and the other side
    for bad in (bids.assign(limit=[9500.0, np.nan, 500.0]),
                bids.assign(decision_time=[t, t + timedelta(hours=2), t])):
        with pytest.raises(ValueError, match="one bid per contract"):
            run._resting(bad, timedelta(hours=3), "HX")

    class Twice(RestingNo):  # a second bid on each contract an hour later
        def intents(self, *args):
            bids = super().intents(*args)
            return pd.concat([bids, bids.assign(intent_id=bids["intent_id"] + len(bids),
                                                decision_time=bids["decision_time"]
                                                + timedelta(hours=1))], ignore_index=True)

    with pytest.raises(ValueError, match="one bid per contract"):  # the runner checks
        run._evaluate(Twice(), market, Twice().configs()["one"], D0, D0 + timedelta(10))

    class Straddle(RestingNo):  # the last of one contract's bids in January, an hour
        def intents(self, data, start, end, config):  # before February's first
            return inside(pd.DataFrame([(n, "KXT-2025-02-01", "no", pd.Timestamp(
                "2025-02-01", tz="UTC") + timedelta(hours=h), 100, 9500.0) for n, h in enumerate(
                (-14, -1, 1))], columns=INTENT_COLUMNS), start, end)

    with pytest.raises(ValueError, match="one bid per contract"):  # across months too
        run._evaluate(Straddle(), market, Straddle().configs()["one"],
                      *calendar.confirmation_window("HX"))


def test_a_bid_decided_before_a_month_ends_fills_after_it(market, monkeypatch):
    monkeypatch.setattr(fills, "maker_fills", partial(fills.maker_fills, queue_haircut=0.0))

    class LateBid(RestingNo):  # on Jan 31, for the longshot of Feb 1
        def intents(self, data, start, end, config):
            return inside(pd.DataFrame([(0, "KXT-2025-02-01", "no", pd.Timestamp(
                "2025-01-31 21:59", tz="UTC"), 200, 9500.0)], columns=INTENT_COLUMNS), start, end)

    got = run._evaluate(LateBid(), market, LateBid().configs()["one"],
                        *calendar.confirmation_window("HX")).fills
    assert got["fill_time"].tolist() == [pd.Timestamp(f"2025-02-01 {t}", tz="UTC")
                                         for t in ("09:00", "10:01")]


def test_a_window_is_evaluated_a_month_at_a_time_as_if_whole(market, monkeypatch):
    strategy, window = BuyNoEachMorning(), calendar.confirmation_window("HX")
    calls, intents = [], strategy.intents

    def seen(data, a, b, config):  # nothing from the end of its part on
        calls.append((a, b))
        far = b + timedelta(days=30)
        assert data.trades(a, far).aggregate("max(created_time)").fetchone()[0] < b
        assert data.markets(far).aggregate("max(created_time)").fetchone()[0] < b
        return intents(data, a, b, config)

    monkeypatch.setattr(strategy, "intents", seen)
    got = {}
    for chunk, cuts in (("YS", []), ("MS", [datetime(2025, 2, 1, tzinfo=UTC)]),
                        ("D", [D0 + timedelta(days=d) for d in range(22, 39)])):
        calls.clear()
        monkeypatch.setattr(run, "CHUNK", chunk)
        got[chunk] = run._evaluate(strategy, market, {"count": 100}, *window, keep=True)
        assert calls == list(pairwise([D0 + timedelta(21), *cuts, D0 + timedelta(39)]))

    def decided(e):  # the fixture strategy numbers its intents in no particular order
        when = e.intents.set_index("intent_id")["decision_time"]
        return (e.intents.drop(columns="intent_id").sort_values("decision_time",
                                                                 ignore_index=True),
                e.fills.assign(intent_id=e.fills["intent_id"].map(when)))

    whole = decided(got["YS"])
    assert len(whole[0]) == 18 and len(whole[1]) == 18
    for chunk in ("MS", "D"):
        assert got[chunk].intents["intent_id"].is_unique
        assert got[chunk].outside_hold == got["YS"].outside_hold
        for part, full in zip(decided(got[chunk]), whole, strict=True):
            assert part.equals(full)
    calls.clear()  # a window starting at noon is still cut at midnight
    monkeypatch.setattr(run, "CHUNK", "MS")
    run._evaluate(strategy, market, {"count": 100}, window[0] + timedelta(hours=12), window[1])
    assert calls[0][1] == datetime(2025, 2, 1, tzinfo=UTC)


def test_the_intents_digest_is_the_whole_sets_fed_in_any_parts():
    t = pd.Timestamp("2026-01-05", tz="UTC")
    intents = pd.DataFrame([(n, f"T{n % 3}", "no", t + timedelta(minutes=n), 100, 9500.0)
                            for n in range(7)], columns=INTENT_COLUMNS)
    rows = intents.to_json(orient="values", date_format="iso", date_unit="ns")
    whole = {"count": 7, "sha256": hashlib.sha256(rows.encode()).hexdigest()}
    for cuts in ([], [3], [0, 2, 2, 6]):
        digest = run._Digest()
        for i, j in pairwise([0, *cuts, 7]):
            digest.add(intents.iloc[i:j])
        assert digest.value() == whole
    assert run._Digest().value() == {"count": 0, "sha256": hashlib.sha256(b"[]").hexdigest()}


def test_memory_holds_one_parts_intents(market, monkeypatch):
    class Dense(BuyNoEachMorning):  # a NO bid every 30 s on each day's longshot until 20:00
        def configs(self):
            return {"one": {"count": 100, "maker": True, "horizon": timedelta(seconds=30)}}

        def intents(self, data, start, end, config):
            t = pd.date_range(start, end, freq="30s", inclusive="left")
            t = t[t - t.floor("D") < pd.Timedelta(hours=20)]
            return pd.DataFrame({"intent_id": range(len(t)), "ticker": "KXT-" + t.strftime("%F"),
                                 "side": "no", "decision_time": t, "count": 100, "limit": 9500.0},
                                columns=INTENT_COLUMNS)

    def peak(chunk):
        monkeypatch.setattr(run, "CHUNK", chunk)
        tracemalloc.start()
        run._evaluate(Dense(), market, Dense().configs()["one"],
                      *calendar.confirmation_window("HX"))
        top = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        return top

    assert peak("D") < peak("YS") / 4  # 18 days of bids: a day's at a time, not all at once


def test_a_configuration_that_never_trades_scores_zero(market):
    class Idle(BuyNoEachMorning):
        def intents(self, data, start, end, config):
            return pd.DataFrame(columns=INTENT_COLUMNS)

    end = D0 + timedelta(days=10)
    positions = run._settled(market, run._evaluate(Idle(), market, {}, D0, end).fills, end)
    returns = settle.daily_returns(positions, D0, end)
    assert positions.empty and len(returns) == 10 and (returns == 0).all()
