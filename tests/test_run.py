"""The research runner on a small synthetic market: each day, a two-bucket event whose
longshot bucket the test strategy buys NO on."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from kalshibot import cli
from kalshibot.data import backfill as bf
from kalshibot.research import calendar, ledger, run
from kalshibot.research.backtest import settle
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
        rows = [(n, t, "no", pd.Timestamp(t.split("-", 1)[1], tz="UTC") + pd.Timedelta(
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
        at, (bid, ask) = ((day - pd.Timedelta(hours=15), ("0.8800", "0.9200"))
                          if ticker.endswith("-B") else (day + pd.Timedelta(hours=9),
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


def test_discover_fetch_confirm_once(market, monkeypatch):
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
    seen, power = [], gauntlet.power
    monkeypatch.setattr(gauntlet, "power", lambda d, p, *a: seen.append(p) or power(d, p, *a))
    verdict = run.confirm(strategy, market)
    assert len(set(seen[0].day)) == 18  # power's positions dated by decision, one a day
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
                               decision_time=mine["decision_time"] + pd.Timedelta("31min"))
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


def test_power_dates_each_position_by_its_clusters_last_decision():
    n = np.zeros(3)
    decided = gauntlet.Positions(n, n, n, n.astype(bool), n, n, n, n + 1,
                                 cluster=np.array([0, 0, 1]), day=np.array([3, 5, 4]))
    assert run._by_last_decision(decided).day.tolist() == [5, 5, 4]


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
    fills, outside = run._fills(s, market, {"count": 100}, D0, D0 + timedelta(days=10))
    days = fills["decision_time"].dt.floor("D")
    assert outside == 0
    assert days.min() == pd.Timestamp(D0 + timedelta(days=1))
    assert days.max() == pd.Timestamp(D0 + timedelta(days=8))

    class Long(BuyNoEachMorning):
        def intents(self, data, start, end, config):  # LONG may settle long after
            return pd.DataFrame([(0, "LONG", "no", start + timedelta(hours=1), 100, None)],
                                columns=INTENT_COLUMNS)

    fills, outside = run._fills(Long(), market, {}, D0, D0 + timedelta(days=10))
    assert fills.empty and outside == 1


def test_a_configuration_that_never_trades_scores_zero(market):
    class Idle(BuyNoEachMorning):
        def intents(self, data, start, end, config):
            return pd.DataFrame(columns=INTENT_COLUMNS)

    end = D0 + timedelta(days=10)
    positions = run._settled(market, run._fills(Idle(), market, {}, D0, end)[0], end)
    returns = settle.daily_returns(positions, D0, end)
    assert positions.empty and len(returns) == 10 and (returns == 0).all()
