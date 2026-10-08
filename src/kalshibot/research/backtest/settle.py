"""Tier-1 accounting: fills → net PnL per fill and a daily return series.

Every verdict in docs/STRATEGIES.md is computed here (the open-source engines are run
alongside to measure model uncertainty, docs/BACKTESTING.md).

- Fees: the costliest regime that could have applied (`FeeSchedule.plausible`), at the
  stated member precision.
- A fill whose market settled before `window_end` is paid its contract's settlement
  value (`yes_payoff`). Otherwise it is marked at the executable bid for its contract at
  `window_end`, less the taker fee to sell there. With no bid it is marked at 0.
- Money is in micro-dollars (`models.MICROS`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import numpy as np
import pandas as pd

from kalshibot.kalshi.fee_history import FeeSchedule
from kalshibot.kalshi.fees import fee
from kalshibot.kalshi.models import PRICE_SCALE


def charged_fee(schedule: FeeSchedule, series: str, ts: datetime, price: int, count: int, *,
                buy: bool, maker: bool, precision: int) -> int:
    """Micro-dollar fee of one fill under the costliest regime that could have applied."""
    return max(fee(price, count, buy=buy, maker=maker, terms=t, precision=precision)
               for t in schedule.plausible(series, ts))


def yes_payoff(markets: pd.DataFrame) -> pd.Series:
    """Price units paid per YES contract at settlement (NO gets the rest): `settlement_value`
    when the result agrees with it (yes = 10000, no = 0, scalar anything), else NaN."""
    value, result = markets["settlement_value"], markets["result"]
    agrees = ((result == "scalar") & value.notna()) | ((result == "yes") & (value == PRICE_SCALE))
    agrees |= (result == "no") & (value.isna() | (value == 0))
    return value.fillna(0).where(agrees).astype(float)


# `yes_payoff`'s rule for binary results, for SQL readers of the markets table
BINARY_PAYOFF_KNOWN = (f"((result = 'yes' AND settlement_value = {PRICE_SCALE}) "
                       "OR (result = 'no' AND coalesce(settlement_value, 0) = 0))")


def settled(markets: pd.DataFrame, window_end: datetime) -> pd.Series:
    """Markets that settled before `window_end` with a known payoff."""
    return (markets["settlement_ts"] < window_end).fillna(False) & yes_payoff(markets).notna()


def settle(fills: pd.DataFrame, markets: pd.DataFrame, series_of: Callable[[str], str],
           schedule: FeeSchedule, window_end: datetime, precision: int,
           bids: pd.DataFrame | None = None) -> pd.DataFrame:
    """fills: `fills.FILL_COLUMNS`. markets: ticker, result, settlement_value, settlement_ts
    (as known at window_end).
    bids: ticker, side, bid (price units of that contract at window_end), optional."""
    m = markets.set_index("ticker")
    m = m.assign(paid=yes_payoff(m), done=settled(m, window_end))
    out = fills.merge(m[["settlement_ts", "paid", "done"]], left_on="ticker", right_index=True,
                      how="left")
    late = out["fill_time"] >= out["settlement_ts"]
    if late.any():
        raise ValueError(f"{late.sum()} fills at or after their market's settlement")
    if bids is not None:
        out = out.merge(bids, on=["ticker", "side"], how="left")
    else:
        out["bid"] = np.nan
    done = out["done"].fillna(False).astype(bool)
    series = out["ticker"].map(series_of)
    out["entry_fee"] = [
        charged_fee(schedule, s, t, int(p), int(c), buy=True, maker=bool(m), precision=precision)
        for s, t, p, c, m in zip(series, out.fill_time, out.fill_price, out.fill_count,
                                 out.maker, strict=True)]
    per_contract = np.where(out["side"] == "yes", out["paid"], PRICE_SCALE - out["paid"])
    mark = out["bid"].fillna(0).astype("int64").where(~done, 0)
    out["exit_fee"] = [
        0 if d or b == 0 else charged_fee(schedule, s, window_end, int(b), int(c), buy=False,
                                          maker=False, precision=precision)
        for d, s, b, c in zip(done, series, mark, out.fill_count, strict=True)]
    out["payoff"] = (np.where(done, per_contract, mark) * out["fill_count"]).round().astype("int64")
    out["cost"] = out["fill_price"] * out["fill_count"]
    out["pnl"] = out["payoff"] - out["cost"] - out["entry_fee"] - out["exit_fee"]
    out["close_time"] = out["settlement_ts"].where(done, window_end)
    out["status"] = np.where(done, "settled", np.where(out["bid"].notna(), "marked",
                                                       "marked_at_zero"))
    return out.drop(columns=["paid", "done"])


def close_day(ts: pd.Series) -> pd.Series:
    """The UTC day a close counts on: a close at exactly midnight (a mark at a window's
    end) belongs to the day before. Settlement days everywhere (`daily_returns`, the
    null's `Positions.day`) come from here."""
    return (ts - pd.Timedelta(1)).dt.floor("D")


def daily_returns(positions: pd.DataFrame, start: datetime, end: datetime) -> pd.Series:
    """Daily PnL over the peak capital at risk (cost of positions open at once), for every
    UTC day in [start, end), zero on days nothing closed: a fixed denominator, so no day
    looks better by using less money, and one calendar for every configuration. Closes
    count on their `close_day`."""
    days = pd.date_range(pd.Timestamp(start).floor("D"), pd.Timestamp(end), freq="D",
                         inclusive="left")
    closed_on = close_day(positions["close_time"])
    outside = ~closed_on.isin(days)
    if outside.any():
        raise ValueError(f"{outside.sum()} positions close outside [{start}, {end})")
    opened = positions.groupby(positions["fill_time"].dt.floor("D"))["cost"].sum()
    closed = positions.groupby(closed_on)["cost"].sum()
    span = opened.index.union(closed.index)
    at_risk = (opened.reindex(span, fill_value=0) - closed.reindex(span, fill_value=0)).cumsum()
    at_risk = at_risk + closed.reindex(span, fill_value=0)  # capital is busy on its close day
    peak = at_risk.max() if len(at_risk) else 0
    pnl = positions.groupby(closed_on)["pnl"].sum().reindex(days, fill_value=0)
    return pnl / peak if peak > 0 else pnl * 0.0
