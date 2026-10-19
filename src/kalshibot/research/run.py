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
   gauntlet's verdict, with its power at the family's smallest meaningful edge and its
   results by category. The test is logged as started before any outcome is read, and
   its random draws are seeded by the family, so the verdict is reproducible.
4. `report`: Tier 2 on the confirmation, after the verdict.

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
from itertools import batched, pairwise
from typing import Any, NamedTuple

import numpy as np
import pandas as pd

from kalshibot.data.backfill import backfill_candles, public_clients
from kalshibot.kalshi import KalshiClient
from kalshibot.kalshi.fees import DIRECT_MEMBER
from kalshibot.kalshi.models import PRICE_SCALE, SIZE_SCALE
from kalshibot.research import calendar, ledger, quotes
from kalshibot.research.backtest import bars, fills, replay, settle
from kalshibot.research.load import Loader
from kalshibot.research.validation import gauntlet, null, stats
from kalshibot.strategies.base import INTENT_COLUMNS, Strategy

_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")
TIER2_MARKETS = 200  # markets per Tier-2 batch (vectorbt holds a bars × markets matrix)
# Decisions are evaluated a calendar month at a time (a pandas frequency of whole days or
# longer, so parts are cut at UTC midnights)
CHUNK = "MS"
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
    """Refuse before any work: no ledger to record in, a loader for another family, or an
    order that can fill past max_hold (each part's fill tape ends max_hold later)."""
    ledger.ledger_dir()
    if loader.family != strategy.family:
        raise ValueError(f"the loader reads for {loader.family!r}, not {strategy.family!r}")
    for name, config in strategy.configs().items():
        reach = fills.reach(config["horizon"] if config.get("maker") else None)
        if reach > strategy.max_hold:
            raise ValueError(f"{strategy.family} {name!r}: an order can fill {reach} after "
                             "its decision, past max_hold")


class _Evaluated(NamedTuple):
    """A configuration's Tier-1 evaluation on a window, read before any outcome."""

    fills: pd.DataFrame  # as `_fill` gives them
    outside_hold: int  # intents the holding rule dropped
    digest: dict  # the intents' `_Digest`
    intents: pd.DataFrame | None  # the intents themselves, if asked for


def _evaluate(strategy: Strategy, loader: Loader, config: dict[str, Any], start: datetime,
              end: datetime, *, keep: bool = False) -> _Evaluated:
    """Tier-1 fills of the configuration's intents decided in [start + max_hold,
    end − max_hold) on markets whose as-listed latest expiration is at most max_hold
    later; how many intents in that span the latter rule dropped.

    A `CHUNK` at a time, cut at UTC midnights, so memory holds one part's intents: the
    strategy decides each part seeing nothing after it (`Strategy.intents` splits there),
    each part's intent ids follow the last part's, and its fills come from the tape up to
    max_hold after it (no fill comes later: `_check`)."""
    hold, family = strategy.max_hold, strategy.family
    first, last = start + hold, end - hold
    cuts = [c for c in pd.date_range(pd.Timestamp(first).floor("D"), last, freq=CHUNK)
            if first < c < last]
    digest, filled, kept, outside, offset = _Digest(), [], [], 0, 0
    bids = pd.DataFrame(columns=INTENT_COLUMNS)  # each contract's latest bid so far
    for a, b in pairwise([first, *(c.to_pydatetime() for c in cuts), last]):
        intents = _checked(strategy.intents(_Until(loader, b), a, b, config), family, a, b)
        t = intents["decision_time"]
        intents["intent_id"] += offset
        offset = int(intents["intent_id"].max()) + 1 if len(intents) else offset
        if config.get("maker") and len(intents):
            bids = _resting(pd.concat([bids, intents]) if len(bids) else intents,
                            config["horizon"], family)
        latest = loader.markets(last).project("ticker, latest_expiration_time").join(
            loader.con.from_df(intents[["ticker"]].drop_duplicates()), "ticker").df(
            ).set_index("ticker")["latest_expiration_time"]
        anchored = (intents["ticker"].map(latest) - t <= hold).to_numpy()
        outside += int((~anchored).sum())
        intents = intents[anchored].reset_index(drop=True)
        digest.add(intents)
        filled.append(_fill(loader, intents, config, a, b + hold))
        if keep:
            kept.append(intents)
    return _Evaluated(pd.concat([f for f in filled if len(f)] or filled[:1], ignore_index=True),
                      outside, digest.value(), pd.concat(kept, ignore_index=True) if keep else None)


def _checked(intents: pd.DataFrame, family: str, start: datetime, end: datetime
             ) -> pd.DataFrame:
    """A strategy's intents for [start, end), cast to the intent schema's types; refused
    unless they keep its contract (`Strategy.intents`)."""
    if list(intents.columns) != INTENT_COLUMNS:
        raise ValueError(f"{family} intents need columns {INTENT_COLUMNS}")
    ids = pd.to_numeric(intents["intent_id"], errors="coerce")
    if ids.isna().any() or (ids < 0).any() or (ids % 1 != 0).any() or ids.duplicated().any():
        raise ValueError(f"{family} intents need unique, non-negative integer intent ids")
    intents = intents.astype({"intent_id": "int64", "ticker": str, "side": str,
                              "count": "int64", "limit": "float64",
                              "decision_time": "datetime64[ns, UTC]"})
    t = intents["decision_time"]
    if not ((t >= start) & (t < end)).all():
        raise ValueError(f"{family} intents decided outside the window [{start}, {end})")
    return intents


def _resting(intents: pd.DataFrame, horizon: timedelta, family: str) -> pd.DataFrame:
    """Refuse maker intents Tier 1 can't fill as one trader's: a bid without a limit, or
    two bids for one contract within `horizon` (each would take its share of the same
    sweep). Returns each contract's latest bid."""
    ordered = intents.sort_values("decision_time")
    gaps = ordered.groupby(["ticker", "side"])["decision_time"]
    if intents["limit"].isna().any() or (gaps.diff() < horizon).any():
        raise ValueError(f"{family} maker intents need a limit, and at most one bid per "
                         "contract resting at a time")
    return ordered.groupby(["ticker", "side"]).tail(1)


def _fill(loader: Loader, intents: pd.DataFrame, config: dict[str, Any], start: datetime,
          end: datetime) -> pd.DataFrame:
    """Tier-1 fills of `intents`, each with the time the null prices it at (`priced_at`):
    a taker's decision, a maker's fill (BACKTESTING, Prices). The fill rules run in the
    loader's DuckDB, on the tape of the markets traded only, in [start, end)."""
    con = loader.con
    con.register("run_intents", intents)
    loader.trades(start, end).join(con.from_df(intents[["ticker"]].drop_duplicates()),
                                   "ticker").create_view("run_trades", replace=True)
    if config.get("maker"):
        out = fills.maker_fills(con, "run_intents", "run_trades",
                                horizon=config["horizon"]).df()
        priced_at = out["fill_time"]
    else:
        out = fills.taker_fills(con, "run_intents", "run_trades").df()
        priced_at = out["intent_id"].map(intents.set_index("intent_id")["decision_time"])
    return out.assign(priced_at=priced_at).sort_values(["intent_id", "fill_time"],
                                                       ignore_index=True)


def _markets(loader: Loader, tickers: pd.Series, end: datetime,
             outcomes: bool = True) -> pd.DataFrame:
    """ticker, series_ticker, category and outcome columns of `tickers` as known at `end`.
    Without `outcomes`, every market reads as unsettled."""
    known = ("result, settlement_value, settlement_ts" if outcomes else
             "NULL::VARCHAR AS result, NULL::BIGINT AS settlement_value, "
             "NULL::TIMESTAMPTZ AS settlement_ts")
    return loader.markets(end).project(f"ticker, series_ticker, category, {known}").join(
        loader.con.from_df(pd.DataFrame({"ticker": pd.unique(tickers)})), "ticker").df()


def _priced(filled: pd.DataFrame, markets: pd.DataFrame, schedule, end: datetime
            ) -> pd.DataFrame:
    """`settle.settle` of fills net of the fee rate itself; `pnl_cent_rounded` is the PnL
    with each fill's fee rounded up to a cent as the regime may have (order size decides
    which a trader pays, and tiny fills pay the cent)."""
    series = markets.set_index("ticker")["series_ticker"].get
    out = settle.settle(filled, markets, series, schedule, end, DIRECT_MEMBER,
                        rounding="balance")
    rounded = settle.settle(filled, markets, series, schedule, end, DIRECT_MEMBER)
    return out.assign(pnl_cent_rounded=rounded["pnl"].to_numpy())


def _settled(loader: Loader, filled: pd.DataFrame, end: datetime,
             outcomes: bool = True) -> pd.DataFrame:
    """`_priced` fills in a window ending at `end`, as known then. Without `outcomes`, a
    dry run that reads no result."""
    return _priced(filled, _markets(loader, filled["ticker"], end, outcomes),
                   loader.fee_schedule(), end)


def _trial(strategy: Strategy, name: str, config: dict[str, Any], window: _Window) -> dict:
    """A trial as the ledger records it: everything that shapes the evaluation besides
    the code (its commit) and the data (its hash), and the edge its power is judged at."""
    return {"name": name, "config": config, "max_hold": strategy.max_hold,
            "min_edge": strategy.min_edge, "window": list(window)}


def _same_trial(recorded: dict, strategy: Strategy, name: str, config: dict[str, Any],
                window: _Window) -> bool:
    return ledger.canonical(recorded["trial"]) == ledger.canonical(
        _trial(strategy, name, config, window))


class _Digest:
    """What identifies a set of intents: how many, and a SHA-256 of their rows as one
    JSON array in intent-id order, fed in parts (the bytes of the whole set at once)."""

    def __init__(self) -> None:
        self._sha, self._count = hashlib.sha256(b"["), 0

    def add(self, intents: pd.DataFrame) -> None:
        """Intents whose ids all follow those added before."""
        if len(intents):
            rows = intents.sort_values("intent_id").to_json(
                orient="values", date_format="iso", date_unit="ns")[1:-1]
            self._sha.update((("," if self._count else "") + rows).encode())
            self._count += len(intents)

    def value(self) -> dict:
        sha = self._sha.copy()
        sha.update(b"]")
        return {"count": self._count, "sha256": sha.hexdigest()}


def _totals(positions: pd.DataFrame) -> dict:
    return {"positions": len(positions), "net_pnl": int(positions["pnl"].sum()),
            "net_pnl_cent_rounded": int(positions["pnl_cent_rounded"].sum())}


def _summary(positions: pd.DataFrame, returns: pd.Series, outside_hold: int) -> dict:
    return {**_totals(positions), "open_at_end": int((positions["status"] != "settled").sum()),
            "outside_hold": outside_hold, "sharpe": float(stats.sharpe(returns.to_numpy()))}


def discover(strategy: Strategy, loader: Loader) -> pd.DataFrame:
    """Every configuration on discovery, each recorded before the next starts; days ×
    configurations of daily returns on one zero-filled calendar."""
    _check(strategy, loader)
    window = start, end = _windows(strategy.family)[0]
    data = loader.fingerprint([window])
    out = {}
    for name, config in strategy.configs().items():
        evaluated = _evaluate(strategy, loader, config, start, end)
        positions = _settled(loader, evaluated.fills, end)
        returns = settle.daily_returns(positions, start, end)
        ledger.record_trial(strategy.family, _trial(strategy, name, config, window),
                            "discovery", data, returns,
                            _summary(positions, returns, evaluated.outside_hold))
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
            or not all(_same_trial(latest[n], strategy, n, c, window)
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
    return gauntlet.pooled([_evaluate(strategy, loader, c, first, end).fills
                            for c in strategy.configs().values()])


class _Plan(NamedTuple):
    """Everything `confirm` evaluates, known before any outcome."""

    discovery: pd.DataFrame  # days × configurations of discovery returns
    trials: list[dict]  # the discovery trials they come from
    selected: str  # the configuration discovery selected
    digest: dict  # its confirmation intents' `_Digest`
    fills: pd.DataFrame  # its confirmation fills
    outside_hold: int  # intents the holding rule dropped from those
    dependence: pd.DataFrame  # the dependence sample's fills


def _plan(strategy: Strategy, loader: Loader) -> _Plan:
    discovery, trials = _discovery(strategy)
    selected = gauntlet.select(discovery)
    _, (start, end) = _windows(strategy.family)
    tested = _evaluate(strategy, loader, strategy.configs()[selected], start, end)
    return _Plan(discovery, trials, selected, tested.digest, tested.fills,
                 tested.outside_hold, _dependence_fills(strategy, loader))


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


def _null_positions(positions: pd.DataFrame, q: pd.DataFrame, day: str = "close_time"
                    ) -> tuple[gauntlet.Positions, int]:
    """The positions the null can price (`q`: their `quotes.mids`), as
    `gauntlet.Positions` dated by `day`, and how many it can't: an exclusive event lacking
    a member's quote, or a contract lacking its own. A position open at its window's end
    counts as lost; a scalar settlement, as the share of $1 it paid."""
    quoted = q["mid"].notna()
    whole = quoted.groupby([q["event"], q["priced_at"]]).transform("all")
    priced = null.inputs(q[np.where(q["mutually_exclusive"], whole, quoted)],
                         gauntlet.PREREGISTERED.devig)
    rows = positions.merge(priced[["ticker", "priced_at", "p_yes", "lo", "hi", "cluster",
                                   "mutually_exclusive"]], on=["ticker", "priced_at"])
    rows = rows.assign(paid=rows["cost"] + rows["entry_fee"],
                       won=rows["payoff"] / (rows["fill_count"] * PRICE_SCALE),
                       day=(settle.close_day(rows[day]) - _EPOCH).dt.days.astype("int64"))
    return gauntlet.Positions.from_frame(rows), len(positions) - len(rows)


def _by_last_priced(positions: gauntlet.Positions) -> gauntlet.Positions:
    """`positions` dated by `priced_at`, redated by the last of those in each one's
    cluster: never after the cluster's last settlement, so power's stand-in for when
    each settles (no outcome read)."""
    day = pd.Series(positions.day).groupby(positions.cluster).transform("max").to_numpy()
    return replace(positions, day=day)


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
    seed, gates = _seed(family), gauntlet.PREREGISTERED
    window_days = np.arange(*((pd.DatetimeIndex([start, end]) - _EPOCH).days))
    before = _by_last_priced(_null_positions(dry, conf_quotes, "priced_at")[0])
    power = {"edge": strategy.min_edge, "worlds": gates.power_worlds,
             "resamples": gates.power_resamples, "power": gauntlet.power(
                 dependence, before, window_days, strategy.min_edge,
                 np.random.default_rng(seed + 1), gates)}
    categories = _markets(loader, plan.fills["ticker"], end, outcomes=False).set_index(
        "ticker")["category"]
    trial = _trial(strategy, plan.selected, strategy.configs()[plan.selected], window)
    data = loader.fingerprint([(d_start, d_end), window], pd.concat([conf_files, sample_files]))
    ledger.begin_confirmation(family, trial, data, {"power": power})
    positions = _settled(loader, plan.fills, end)
    tested, dropped = _null_positions(positions, conf_quotes)
    returns = settle.daily_returns(positions, start, end)
    verdict = gauntlet.evaluate(plan.discovery, dependence, returns, tested,
                                np.random.default_rng(seed), gates)
    ledger.record_trial(family, trial, "confirmation", data, returns, {
        "verdict": asdict(verdict) | {"p_value": verdict.p_value}, "power": power,
        **_summary(positions, returns, plan.outside_hold), "tested": len(tested.net_pnl),
        "dropped_from_null": dropped, "seed": seed, "intents": plan.digest,
        "by_category": {c: _totals(g) for c, g in positions.groupby(
            positions["ticker"].map(categories).fillna("unknown"))},
        "discovery_trials": [t["trial_id"] for t in plan.trials]})
    return verdict


def _filled(positions: pd.DataFrame) -> dict:
    """What one tier filled and made: intents filled, contracts, net PnL (µ$)."""
    return {"intents": int(positions["intent_id"].nunique()),
            "contracts": float(positions["fill_count"].sum() / SIZE_SCALE),
            "net_pnl": int(positions["pnl"].sum())}


def report(strategy: Strategy, loader: Loader) -> dict:
    """Tier 2 on the family's confirmation, after its verdict: the tested
    configuration's taker intents filled by NautilusTrader and by vectorbt instead
    (`backtest/replay.py`, `backtest/bars.py`), priced as Tier 1 is (`_priced`), so the
    gap is the fill model alone. Each tier logs the intents it filled, contracts and net
    PnL; each engine also the gap between its own PnL and `settle`'s on the same fills,
    summed over markets in absolute value (0 when the accounting agrees). Refuses unless
    the configuration and window are the ones tested, and the intents and Tier 1,
    recomputed now, are the ones the confirmation recorded (so the engines get the
    intents, fills and prices the verdict used; their earlier prints aren't checked).
    Appended to the ledger; it never touches the verdict, so it can be run again."""
    _check(strategy, loader)
    tested = ledger.confirmation(strategy.family)
    if tested is None:
        raise RuntimeError(f"{strategy.family} has no confirmation verdict to report on")
    name = tested["trial"]["name"]
    config = strategy.configs().get(name, {})
    window = _windows(strategy.family)[1]
    if not _same_trial(tested, strategy, name, config, window):
        raise RuntimeError(f"{strategy.family}'s {name!r} isn't what its confirmation tested")
    out = {"confirmation": tested["trial_id"], "configuration": name} | (
        {"tier2": "makers get a Tier 2 only from recorded order books"} if config.get("maker")
        else _tiers(strategy, loader, config, tested["summary"], window))
    ledger.record_report(strategy.family, "confirmation", out)
    return out


def _tiers(strategy: Strategy, loader: Loader, config: dict[str, Any], recorded: dict,
           window: _Window) -> dict:
    start, end = window
    evaluated = _evaluate(strategy, loader, config, start, end, keep=True)
    intents, tier1 = evaluated.intents, _settled(loader, evaluated.fills, end)
    totals = _totals(tier1)
    if evaluated.digest != recorded["intents"] or totals != {k: recorded[k] for k in totals}:
        raise RuntimeError(f"{strategy.family}'s intents or Tier 1 are no longer what its "
                           "confirmation recorded: the code or data changed since")
    schedule = loader.fee_schedule()
    tiers = {e: {"intents": 0, "contracts": 0.0, "net_pnl": 0, "accounting_gap": 0}
             for e in ("nautilus", "vectorbt")}
    for chunk in batched(sorted(set(intents["ticker"])), TIER2_MARKETS, strict=False):
        markets = _markets(loader, pd.Series(chunk), end)
        trades = loader.trades(start, end).join(
            loader.con.from_df(pd.DataFrame({"ticker": chunk})), "ticker").df()
        series = markets.set_index("ticker")["series_ticker"].get
        mine = intents[intents["ticker"].isin(chunk)]
        for engine, fill in (("nautilus", replay.replay), ("vectorbt", bars.bars)):
            got, own = fill(mine, trades, markets, series, schedule, end, DIRECT_MEMBER)
            if got.empty:
                continue
            priced, tier = _priced(got, markets, schedule, end), tiers[engine]
            for k, v in _filled(priced).items():
                tier[k] += v
            ours = priced[priced["status"] == "settled"].groupby("ticker")["pnl_cent_rounded"]
            tier["accounting_gap"] += int(own.set_index("ticker")["pnl"].sub(
                ours.sum(), fill_value=0).abs().sum())
    return {"tier1": _filled(tier1), "tier2": tiers}
