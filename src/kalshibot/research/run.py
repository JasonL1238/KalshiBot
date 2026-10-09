"""The research runner: the only way a family's configurations are evaluated, so every
evaluation is in the trial ledger before anyone sees it, and a family gets exactly one
confirmation test (docs/BACKTESTING.md, Pipeline).

1. `discover`: every configuration on the discovery window, each recorded as it is
   evaluated.
2. `fetch_candles`: the 1-minute candles the null prices from (`quotes.py`), for the
   configuration discovery selected on its confirmation window and for every
   configuration on the last discovery days the dependence estimates use
   (`Gates.dependence_days`). It reads intents and fills, never an outcome.
3. `confirm`: the selected configuration, once, on its confirmation window; the
   gauntlet's verdict. The test is logged as started before any outcome is read, and its
   random draws are seeded by the family, so the verdict is reproducible.

Every evaluation is Tier 1 (`backtest/fills.py`, `backtest/settle.py`), net of the fee
rate at a direct member's precision (`_settled`), on the calendar's windows
(`calendar.py`). A decision counts
only if it falls in its window less one maximum holding period (`Strategy.max_hold`) at
each end, and its market's as-listed latest expiration is at most `max_hold` after it
(ROADMAP, Research calendar).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from typing import Any, NamedTuple

import numpy as np
import pandas as pd

from kalshibot.data.backfill import backfill_candles, public_clients
from kalshibot.kalshi import KalshiClient
from kalshibot.kalshi.fees import DIRECT_MEMBER
from kalshibot.kalshi.models import PRICE_SCALE
from kalshibot.research import calendar, ledger, quotes
from kalshibot.research.backtest import fills, settle
from kalshibot.research.load import Loader
from kalshibot.research.validation import gauntlet, null, stats
from kalshibot.strategies.base import INTENT_COLUMNS, Strategy

_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")
_Window = tuple[datetime, datetime]


def _windows(family: str) -> tuple[_Window, _Window]:
    """(discovery, confirmation), from the calendar."""
    return ((calendar.DISCOVERY_START, calendar.BECKER_END),
            calendar.confirmation_window(family))


class _Until:
    """What a strategy deciding before `until` sees: the loader's data up to then."""

    def __init__(self, loader: Loader, until: datetime):
        self._loader, self._until = loader, until

    def trades(self, start: datetime, end: datetime):
        return self._loader.trades(start, min(end, self._until))

    def markets(self, as_of: datetime):
        return self._loader.markets(min(as_of, self._until))


def _check(strategy: Strategy, loader: Loader) -> None:
    ledger.ledger_dir()  # no ledger to record in: refuse before doing any work
    if loader.family != strategy.family:
        raise ValueError(f"the loader reads for {loader.family!r}, not {strategy.family!r}")


def _fills(strategy: Strategy, loader: Loader, config: dict[str, Any], start: datetime,
           end: datetime) -> tuple[pd.DataFrame, int]:
    """Tier-1 fills, with their decision_time, of the configuration's intents decided in
    [start + max_hold, end − max_hold) on markets whose as-listed latest expiration is
    at most max_hold later; and how many intents in that span the latter rule dropped.
    The fill rules run in the loader's DuckDB, on the tape of the markets traded only."""
    hold, con = strategy.max_hold, loader.con
    first, last = start + hold, end - hold
    intents = strategy.intents(_Until(loader, last), first, last, config)
    if list(intents.columns) != INTENT_COLUMNS or intents["intent_id"].duplicated().any():
        raise ValueError(f"{strategy.family} intents need columns {INTENT_COLUMNS} and "
                         "unique intent ids")
    intents = intents.astype({"ticker": str, "side": str, "count": "int64", "limit": "float64",
                              "decision_time": "datetime64[ns, UTC]"})
    tickers = con.from_df(intents[["ticker"]].drop_duplicates())
    latest = loader.markets(last).project("ticker, latest_expiration_time").join(
        tickers, "ticker").df().set_index("ticker")["latest_expiration_time"]
    t = intents["decision_time"]
    inside = ((t >= first) & (t < last)).to_numpy()
    anchored = (intents["ticker"].map(latest) - t <= hold).to_numpy()
    intents = intents[inside & anchored].reset_index(drop=True)
    con.register("run_intents", intents)
    loader.trades(start, end).join(con.from_df(intents[["ticker"]].drop_duplicates()),
                                   "ticker").create_view("run_trades", replace=True)
    if config.get("maker"):
        filled = fills.maker_fills(con, "run_intents", "run_trades", horizon=config["horizon"])
    else:
        filled = fills.taker_fills(con, "run_intents", "run_trades")
    return (filled.df().merge(intents[["intent_id", "decision_time"]], on="intent_id"),
            int((inside & ~anchored).sum()))


def _settled(loader: Loader, filled: pd.DataFrame, end: datetime,
             outcomes: bool = True) -> pd.DataFrame:
    """`settle.settle` of fills in a window ending at `end`, as known then, net of the
    fee rate itself; `pnl_cent_rounded` is the PnL with each fill's fee rounded up to a
    cent as the regime may have (order size decides which a trader pays, and tiny fills
    pay the cent). Without `outcomes`, every market reads as unsettled: a dry run that
    reads no result."""
    known = ("result, settlement_value, settlement_ts" if outcomes else
             "NULL::VARCHAR AS result, NULL::BIGINT AS settlement_value, "
             "NULL::TIMESTAMPTZ AS settlement_ts")
    markets = loader.markets(end).project(f"ticker, series_ticker, {known}").join(
        loader.con.from_df(filled[["ticker"]].drop_duplicates()), "ticker").df()
    series, schedule = markets.set_index("ticker")["series_ticker"].get, loader.fee_schedule()
    out = settle.settle(filled, markets, series, schedule, end, DIRECT_MEMBER,
                        rounding="balance")
    rounded = settle.settle(filled, markets, series, schedule, end, DIRECT_MEMBER)
    return out.assign(pnl_cent_rounded=rounded["pnl"].to_numpy())


def _trial(strategy: Strategy, name: str, config: dict[str, Any], window: _Window) -> dict:
    """A trial as the ledger records it: everything that shapes the evaluation besides
    the code (its commit) and the data (its hash)."""
    return {"name": name, "config": config, "max_hold": strategy.max_hold,
            "window": list(window)}


def _summary(positions: pd.DataFrame, returns: pd.Series, outside_hold: int) -> dict:
    return {"positions": len(positions), "net_pnl": int(positions["pnl"].sum()),
            "net_pnl_cent_rounded": int(positions["pnl_cent_rounded"].sum()),
            "open_at_end": int((positions["status"] != "settled").sum()),
            "outside_hold": outside_hold, "sharpe": float(stats.sharpe(returns.to_numpy()))}


def discover(strategy: Strategy, loader: Loader) -> pd.DataFrame:
    """Every configuration on discovery, each recorded before the next starts; days ×
    configurations of daily returns on one zero-filled calendar."""
    _check(strategy, loader)
    window = start, end = _windows(strategy.family)[0]
    data = loader.fingerprint([window])
    out = {}
    for name, config in strategy.configs().items():
        filled, outside = _fills(strategy, loader, config, start, end)
        positions = _settled(loader, filled, end)
        returns = settle.daily_returns(positions, start, end)
        ledger.record_trial(strategy.family, _trial(strategy, name, config, window),
                            "discovery", data, returns, _summary(positions, returns, outside))
        out[name] = returns
    return pd.DataFrame(out)


def _discovery(strategy: Strategy) -> tuple[pd.DataFrame, list[dict]]:
    """(days × configurations of discovery returns, the trials they come from): each
    configuration's latest discovery trial. Refuses unless those are exactly the
    configurations as defined now, from one run (one data hash and commit)."""
    window = _windows(strategy.family)[0]
    latest = {t["trial"]["name"]: t for t in ledger.trials(strategy.family)
              if t["window"] == "discovery"}
    configs = strategy.configs()
    if (set(latest) != set(configs) or len({(t["data_hash"], t["commit"])
                                            for t in latest.values()}) != 1
            or any(ledger.canonical(latest[n]["trial"])
                   != ledger.canonical(_trial(strategy, n, c, window))
                   for n, c in configs.items())):
        raise RuntimeError(f"{strategy.family}: the latest discovery trials aren't one run "
                           "of the configurations as defined now; run discover")
    trials = [latest[n] for n in configs]
    returns = ledger.trial_returns(strategy.family, [t["trial_id"] for t in trials])
    returns.columns = list(configs)
    return returns, trials


def _dependence_fills(strategy: Strategy, loader: Loader) -> pd.DataFrame:
    """Every configuration's fills decided in discovery's last `Gates.dependence_days`,
    each bet once (`gauntlet.pooled`)."""
    (start, end), _ = _windows(strategy.family)
    first = max(start, end - strategy.max_hold
                - timedelta(days=gauntlet.PREREGISTERED.dependence_days) - strategy.max_hold)
    return gauntlet.pooled([_fills(strategy, loader, c, first, end)[0]
                            for c in strategy.configs().values()])


class _Plan(NamedTuple):
    """Everything `confirm` evaluates, known before any outcome."""

    discovery: pd.DataFrame  # days × configurations of discovery returns
    trials: list[dict]  # the discovery trials they come from
    selected: str  # the configuration discovery selected
    fills: pd.DataFrame  # its confirmation fills
    outside_hold: int  # intents the holding rule dropped from those
    dependence: pd.DataFrame  # the dependence sample's fills


def _plan(strategy: Strategy, loader: Loader) -> _Plan:
    discovery, trials = _discovery(strategy)
    selected = gauntlet.select(discovery)
    _, (start, end) = _windows(strategy.family)
    conf, outside = _fills(strategy, loader, strategy.configs()[selected], start, end)
    return _Plan(discovery, trials, selected, conf, outside, _dependence_fills(strategy, loader))


def fetch_candles(strategy: Strategy, loader: Loader, workers: int = 2,
                  make_client: Callable[[], KalshiClient] | None = None) -> int:
    """Fetch the candles `confirm` needs into the loader's data, round after round (each
    looks a day further back for markets that hadn't quoted, `quotes.needed`) until none
    are missing; `workers` clients share the API's read budget
    (`data.backfill.public_clients`). Returns how many files it wrote."""
    _check(strategy, loader)
    make_client = make_client or public_clients(workers)
    plan = _plan(strategy, loader)
    (_, d_end), (_, end) = _windows(strategy.family)
    written = 0
    while len(need := pd.concat([quotes.needed(loader, plan.fills, end),
                                 quotes.needed(loader, plan.dependence, d_end)]
                                ).drop_duplicates()):
        written += len(backfill_candles(make_client, loader.kalshi_dir, need, workers))
    return written


def _null_positions(positions: pd.DataFrame, q: pd.DataFrame) -> tuple[gauntlet.Positions, int]:
    """The positions the null can price (`q`: their `quotes.mids`), as
    `gauntlet.Positions`, and how many it can't: an exclusive event lacking a member's
    quote, or a contract lacking its own. A position open at its window's end counts as
    lost; a scalar settlement, as the share of $1 it paid."""
    quoted = q["mid"].notna()
    whole = quoted.groupby([q["event"], q["decision_time"]]).transform("all")
    priced = null.inputs(q[np.where(q["mutually_exclusive"], whole, quoted)],
                         gauntlet.PREREGISTERED.devig)
    rows = positions.merge(priced[["ticker", "decision_time", "p_yes", "lo", "hi", "cluster"]],
                           on=["ticker", "decision_time"])
    rows = rows.assign(paid=rows["cost"] + rows["entry_fee"],
                       won=rows["payoff"] / (rows["fill_count"] * PRICE_SCALE),
                       day=(settle.close_day(rows["close_time"]) - _EPOCH).dt.days.astype("int64"))
    return gauntlet.Positions.from_frame(rows), len(positions) - len(rows)


def _seed(family: str) -> int:
    return int(hashlib.sha256(family.encode()).hexdigest()[:16], 16)


def confirm(strategy: Strategy, loader: Loader) -> gauntlet.Verdict:
    """The family's one confirmation test of the configuration discovery selected.
    Refuses before its window has finished (the loader won't read unfinished days) or
    while the markets table predates the window's end. Fills and quotes come first
    (`fetch_candles` must have run); then the test is logged as started, and only then
    are outcomes read."""
    _check(strategy, loader)
    family = strategy.family
    (d_start, d_end), window = _windows(family)
    start, end = window
    if (fetched := loader.fetched()) is None or fetched < end:
        raise RuntimeError(f"the markets and events tables predate {end}, so they lack "
                           "outcomes the test reads; run `kalshibot backfill reference`")
    plan = _plan(strategy, loader)
    (conf_quotes, conf_files), (sample_quotes, sample_files) = (
        quotes.mids(loader, plan.fills, end), quotes.mids(loader, plan.dependence, d_end))
    dependence, _ = _null_positions(_settled(loader, plan.dependence, d_end), sample_quotes)
    dry = _settled(loader, plan.fills, end, outcomes=False)  # everything after the start,
    gauntlet.evaluate(plan.discovery, dependence,  # on no outcome: it fails here, if at all
                      settle.daily_returns(dry, start, end), _null_positions(dry, conf_quotes)[0],
                      np.random.default_rng(0), replace(gauntlet.PREREGISTERED, draws=100))
    trial = _trial(strategy, plan.selected, strategy.configs()[plan.selected], window)
    data = loader.fingerprint([(d_start, d_end), window], pd.concat([conf_files, sample_files]))
    ledger.begin_confirmation(family, trial, data)
    positions = _settled(loader, plan.fills, end)
    tested, dropped = _null_positions(positions, conf_quotes)
    returns = settle.daily_returns(positions, start, end)
    seed = _seed(family)
    verdict = gauntlet.evaluate(plan.discovery, dependence, returns, tested,
                                np.random.default_rng(seed))
    ledger.record_trial(family, trial, "confirmation", data, returns, {
        "verdict": asdict(verdict) | {"p_value": verdict.p_value},
        **_summary(positions, returns, plan.outside_hold), "tested": len(tested.net_pnl),
        "dropped_from_null": dropped, "seed": seed,
        "discovery_trials": [t["trial_id"] for t in plan.trials]})
    return verdict
