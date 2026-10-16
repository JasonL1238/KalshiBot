"""Tier 2, vectorized: the classic bar-close backtest, run by vectorbt.

Most vectorized backtests fill an order at the close of the bar after its signal. Here
the bars are 1 minute long and the close is the last print at or before the bar's end
(either taker side, however stale): a forward-filled matrix with one column per market
and side bought, quoted in YES dollars (buying NO is shorting YES). Each taker intent is
an order at the first bar ending at or after `decision_time + latency`, for its whole
size, and vectorbt fills it at that bar's close. The matrix is evaluated only at bars
with orders and at a final row, which equals resampling every minute and forward-filling.

vectorbt has no limit orders, so an intent whose limit is below its bar's close is
dropped before booking; so is one whose bar ends after its market settled or after
`window_end`. The final row values each settled market at its payoff
(`settle.yes_payoff`). `bars` returns the fills as `fills.FILL_COLUMNS`, so
`settle.settle` prices them like any other tier, and vectorbt's own PnL per settled
market for the accounting cross-check.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import vectorbt as vbt

from kalshibot.kalshi.fee_history import FeeSchedule
from kalshibot.kalshi.models import MICROS, PRICE_SCALE, SIZE_SCALE
from kalshibot.research.backtest.fills import no_fills, tape_order
from kalshibot.research.backtest.settle import charged_fee, settled, yes_payoff

BAR = pd.Timedelta(1, unit="min")


def bars(intents: pd.DataFrame, trades: pd.DataFrame, markets: pd.DataFrame,
         series_of: Callable[[str], str], schedule: FeeSchedule, window_end: datetime,
         precision: int, *, latency: timedelta = timedelta(seconds=1)
         ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fill taker `intents` (`strategies.base.INTENT_COLUMNS`) at bar closes of `trades`
    (trade_id, ticker, created_time, yes_price, count, taker_side). Returns (fills,
    vectorbt's PnL in micro-dollars per market that settled before `window_end`)."""
    end = pd.Timestamp(window_end).tz_convert("UTC")
    m = markets.set_index("ticker").reindex(sorted(set(intents["ticker"])))
    m = m.assign(paid=yes_payoff(m), done=settled(m, window_end))
    orders = intents.assign(bar=(pd.to_datetime(intents["decision_time"], utc=True)
                                 .dt.as_unit("ns") + latency).dt.ceil(BAR))
    closed = orders["bar"] >= orders["ticker"].map(m["settlement_ts"])
    orders = orders[(orders["bar"] < end) & ~closed.fillna(False).astype(bool)]

    # the close at every order bar: the last print at or before the bar's end
    tape = tape_order(trades.assign(
        created_time=pd.to_datetime(trades["created_time"], utc=True).dt.as_unit("ns")))
    last = pd.merge_asof(orders[["ticker", "bar"]].drop_duplicates().sort_values("bar"),
                         tape[["ticker", "created_time", "yes_price"]],
                         left_on="bar", right_on="created_time", by="ticker")
    orders = orders.merge(last[["ticker", "bar", "yes_price"]], on=["ticker", "bar"])
    orders = orders[orders["yes_price"].notna()]  # no print yet: no close to fill at
    orders["price"] = np.where(orders["side"] == "yes", orders["yes_price"],
                               PRICE_SCALE - orders["yes_price"]).astype("int64")
    limit = pd.to_numeric(orders["limit"])
    orders = orders[(limit.isna() | (orders["price"] <= limit)).to_numpy()]
    if orders.empty:
        return no_fills()

    columns = pd.MultiIndex.from_frame(orders[["ticker", "side"]].drop_duplicates()
                                       .sort_values(["ticker", "side"]))
    rows = pd.DatetimeIndex(sorted(set(orders["bar"]))).append(pd.DatetimeIndex([end]))
    close = (orders.drop_duplicates(["bar", "ticker"])
             .pivot(index="bar", columns="ticker", values="yes_price").reindex(rows).ffill()
             [columns.get_level_values("ticker")] / PRICE_SCALE)
    close.columns = columns
    payoff = m["paid"].reindex(columns.get_level_values("ticker")).to_numpy() / PRICE_SCALE
    close.iloc[-1] = np.where(np.isnan(payoff), close.iloc[-2], payoff)  # the final row

    orders["fee"] = [charged_fee(schedule, series_of(t), b.to_pydatetime(), int(p), int(c),
                                 buy=True, maker=False, precision=precision)
                     for t, b, p, c in orders[["ticker", "bar", "price", "count"]].itertuples(
                         index=False)]
    cell = (orders.assign(size=np.where(orders["side"] == "yes", 1, -1) * orders["count"]
                          / SIZE_SCALE, fee=orders["fee"] / MICROS)
            .groupby(["bar", "ticker", "side"])[["size", "fee"]].sum())
    size = cell["size"].unstack(["ticker", "side"]).reindex(index=rows, columns=columns)
    fees = cell["fee"].unstack(["ticker", "side"]).reindex(index=rows, columns=columns)
    portfolio = vbt.Portfolio.from_orders(close=close, size=size, fixed_fees=fees.fillna(0),
                                          direction="both", init_cash=1e9, freq=BAR)

    records = portfolio.orders.records_readable  # vectorbt's fills, one per (bar, column)
    filled = pd.DataFrame({"bar": records["Timestamp"],
                           "ticker": [c[0] for c in records["Column"]],
                           "side": [c[1] for c in records["Column"]],
                           "yes": (records["Price"] * PRICE_SCALE).round().astype("int64")})
    got = orders.merge(filled, on=["bar", "ticker", "side"])
    fills = pd.DataFrame({
        "intent_id": got["intent_id"], "ticker": got["ticker"], "side": got["side"],
        "fill_time": got["bar"],
        "fill_price": np.where(got["side"] == "yes", got["yes"], PRICE_SCALE - got["yes"]),
        "fill_count": got["count"].astype("int64"), "maker": False})
    profit = (portfolio.total_profit() * MICROS).round().astype("int64")
    done = m["done"].reindex(columns.get_level_values("ticker")).to_numpy()
    pnl = profit[done].groupby(level="ticker").sum().rename("pnl").reset_index()
    return fills.sort_values(["fill_time", "intent_id"], ignore_index=True), pnl
