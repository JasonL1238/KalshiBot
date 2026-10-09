"""Quotes for the null: the YES mids a position's null price depends on, at its decision
time.

The null de-vigs a mutually exclusive event as a whole (`null.inputs`), so it needs every
market of the event listed by then; any other contract needs only its own price. Prices
come from 1-minute candles (`Loader.candles`): a market's mid at time t is the midpoint
of the closing YES bid and ask of its last candle ending at or before t, with an empty
side at its bound (no bid: 0; no ask: $1). Candle files hold one market's UTC day, so
that candle is searched for from the decision day back, a day at a time, for at most
`LOOKBACK_DAYS` days before it and never before the day the market was listed.

`needed` lists the files the search reads next and that aren't on disk; fetch them and
ask again until it lists none (`research.run.fetch_candles` does). `mids` reads them,
and says how each market enters the null's draws (`null.inputs`; `_draws`).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from kalshibot.kalshi.models import PRICE_SCALE
from kalshibot.research.backtest.settle import close_day
from kalshibot.research.load import Loader, MissingData

LOOKBACK_DAYS = 7
STRIKE = r"[A-Z]?-?[\d.]+"  # a ticker suffix that is only a strike: T92499.99, B2850
MEMBER_COLUMNS = ["event", "mutually_exclusive", "strike_type", "floor_strike", "cap_strike",
                  "decision_time", "ticker"]
DRAW_COLUMNS = ["position", "ladder", "below"]


def _members(loader: Loader, wanted: pd.DataFrame, as_of: datetime) -> pd.DataFrame:
    """For each (ticker, decision_time) of `wanted`: every market of the ticker's event
    listed before then and not yet closed, if the event is mutually exclusive (a closed
    member shows an emptied book, and it lost), else the ticker alone. Columns
    `MEMBER_COLUMNS` plus created_time."""
    wanted = wanted[["ticker", "decision_time"]].drop_duplicates()
    if wanted.empty:
        return pd.DataFrame(columns=[*MEMBER_COLUMNS, "created_time"])
    listed = loader.markets(as_of).project(
        "ticker, event_ticker AS event, coalesce(mutually_exclusive, FALSE) AS "
        "mutually_exclusive, strike_type, floor_strike, cap_strike, created_time, close_time")
    own = listed.join(loader.con.from_df(wanted[["ticker"]].drop_duplicates()), "ticker").df()
    pairs = wanted.merge(own, on="ticker")
    exclusive = pairs[pairs["mutually_exclusive"]][["event", "decision_time"]].drop_duplicates()
    events = exclusive[["event"]].drop_duplicates()
    others = (listed.join(loader.con.from_df(events), "event").df() if len(events)
              else own.iloc[:0])
    every = exclusive.merge(others, on="event")
    every = every[(every["created_time"] < every["decision_time"])
                  & ~(every["close_time"] <= every["decision_time"])]
    out = pd.concat([pairs[~pairs["mutually_exclusive"]], every], ignore_index=True)
    return out[[*MEMBER_COLUMNS, "created_time"]].astype(
        {"decision_time": "datetime64[ns, UTC]", "mutually_exclusive": bool})


def _draws(members: pd.DataFrame) -> pd.DataFrame:
    """`DRAW_COLUMNS` for `null.inputs`, from Kalshi's tickers and strikes:

    - position: a bucket's place in its exclusive event, its floor strike (a "below"
      tail, with none, comes first);
    - ladder: the underlying a strike ladder in a non-exclusive event is on, whatever
      Kalshi calls its strike type: markets whose ticker ends in a strike and that have
      one (not a "between" bucket), keyed by the ticker less that strike
      (`KXBTCD-25DEC0214-T77249.99`: the event; `…-PHITMAXEYPG-35`: one player;
      `KXNHLSPREAD-…-EDM2`: one team's margin; `…-WOLEAGBAD12`, a goal scorer's shirt
      number with no strike: none);
    - below: a "below X" ladder market (strike type less, or only a cap)."""
    suffix = pd.Series([t[len(e) + 1:] if t.startswith(f"{e}-") else ""
                        for e, t in zip(members["event"], members["ticker"], strict=True)],
                       members.index)
    only_strike = suffix.str.fullmatch(STRIKE)
    has_strike = only_strike | members["floor_strike"].notna() | members["cap_strike"].notna()
    ladder = (~members["mutually_exclusive"] & suffix.str.contains(r"\d$") & has_strike
              & (members["strike_type"] != "between"))
    underlying = suffix.str.replace(r"-?[\d.]+$", "", regex=True).where(~only_strike, "")
    return pd.DataFrame({
        "position": members["floor_strike"].astype(float).fillna(-np.inf),
        "ladder": underlying.where(ladder),
        "below": members["strike_type"].isin(["less", "less_or_equal"])
        | (members["cap_strike"].notna() & members["floor_strike"].isna())}, members.index)


def _search(loader: Loader, members: pd.DataFrame
            ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(members with their last candle's end_period_ts, yes_bid_close and yes_ask_close,
    NaN where the search found none; the (ticker, day) files it still needs; the files
    it read)."""
    day = members["decision_time"].dt.floor("D")
    listed = pd.to_datetime(members["created_time"], utc=True).dt.floor("D")
    oldest = day - timedelta(days=LOOKBACK_DAYS)
    pending = members.assign(day=day, floor=listed.where(listed > oldest, oldest))
    found, missing, read = [], [], []
    while len(pending):
        on_disk = loader.has_candles(pending)
        if not on_disk.all():
            missing.append(pending.loc[~on_disk, ["ticker", "day"]])
        here = pending[on_disk]
        read.append(here[["ticker", "day"]])
        candles = loader.candles(here[["ticker", "day"]]).df()
        candles["end_period_ts"] = candles["end_period_ts"].astype("datetime64[ns, UTC]")
        candles["day"] = close_day(candles["end_period_ts"])  # the file it is in
        last = pd.merge_asof(
            here.reset_index().sort_values("decision_time"),
            candles[["ticker", "day", "end_period_ts", "yes_bid_close", "yes_ask_close"]]
            .sort_values("end_period_ts"), left_on="decision_time", right_on="end_period_ts",
            by=["ticker", "day"], direction="backward").set_index("index")
        hit = last["end_period_ts"].notna()
        found.append(last[hit])
        earlier = here[~hit.reindex(here.index).to_numpy()]
        pending = earlier.assign(day=earlier["day"] - timedelta(days=1))
        pending = pending[pending["day"] >= pending["floor"]]
    need = (pd.concat(missing, ignore_index=True).drop_duplicates() if missing
            else pd.DataFrame(columns=["ticker", "day"]))
    hits = pd.concat(found) if found else pd.DataFrame()
    files = (pd.concat(read).drop_duplicates() if read
             else pd.DataFrame(columns=["ticker", "day"]))
    return members.join(hits.reindex(columns=["end_period_ts", "yes_bid_close",
                                              "yes_ask_close"])), need, files


def needed(loader: Loader, wanted: pd.DataFrame, as_of: datetime) -> pd.DataFrame:
    """The candle files (ticker, day) `mids` reads next for `wanted` (ticker,
    decision_time) and that aren't on disk."""
    return _search(loader, _members(loader, wanted, as_of))[1].reset_index(drop=True)


def mids(loader: Loader, wanted: pd.DataFrame, as_of: datetime
         ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(what `null.inputs` takes for every market the null prices `wanted` (ticker,
    decision_time) from: event, mutually_exclusive, decision_time, ticker, `DRAW_COLUMNS`,
    and mid in price units, NaN where the market had no candle within the lookback; the
    candle files (ticker, day) read, which a hash of the data must cover). Rows come in
    one order for one input, so the null's cluster numbers, and its seeded draws, do too.
    Raises `MissingData` while `needed` lists files."""
    members = _members(loader, wanted, as_of)
    priced, need, files = _search(loader, members)
    if len(need):
        raise MissingData(f"{len(need)} candle files missing (first {need.iloc[0].tolist()}); "
                          "fetch them with research.run.fetch_candles")
    bid = priced["yes_bid_close"].astype(float).fillna(0)
    ask = priced["yes_ask_close"].astype(float).fillna(PRICE_SCALE)
    priced["mid"] = ((bid + ask) / 2).where(priced["end_period_ts"].notna())
    out = priced[["event", "mutually_exclusive", "decision_time", "ticker", "mid"]].join(
        _draws(members))
    return out.sort_values(["event", "decision_time", "ticker"]).reset_index(drop=True), files
