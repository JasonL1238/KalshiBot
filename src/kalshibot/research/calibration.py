"""Taker calibration: how often the contract a taker bought won, by the price paid.

Every trade is a taker buying YES or NO at its printed price. Grouping trades by that
price and comparing the share of contracts that won with the average price paid gives
calibration as takers experienced it, the basis of the favorite–longshot literature
(Bürgi, Deng & Whelan 2025 on Kalshi). Gross of fees; contract-weighted.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from kalshibot.kalshi.models import PRICE_SCALE, SIZE_SCALE
from kalshibot.research.backtest.settle import BINARY_PAYOFF_KNOWN
from kalshibot.research.load import Loader


def taker_calibration(loader: Loader, start: datetime, end: datetime, as_of: datetime,
                      bin_cents: int = 5) -> pd.DataFrame:
    """Trades in [start, end) on binary markets that settled before `as_of`, by the taker's
    price in `bin_cents` bins: trades, contracts, avg_price and win_rate (both 0–1), and
    the gross return per dollar, win_rate / avg_price − 1."""
    width = bin_cents * PRICE_SCALE // 100
    paid = f"CASE WHEN taker_side = 'yes' THEN yes_price ELSE {PRICE_SCALE} - yes_price END"
    # Trades are summed per market, side and bin before the join (Loader.trades says why).
    trades = loader.trades(start, end).aggregate(f"""
        ticker, taker_side, {paid} // {width} * {bin_cents} AS bin_cents, count(*) AS trades,
        sum(count) AS contracts, sum(count * {paid}) AS paid""", "ALL").set_alias("t")
    markets = loader.markets(as_of).filter(BINARY_PAYOFF_KNOWN).set_alias("m")
    out = trades.join(markets, "t.ticker = m.ticker").aggregate(f"""
        t.bin_cents,
        sum(t.trades)::BIGINT AS trades,
        sum(t.contracts) / {SIZE_SCALE} AS contracts,
        sum(t.paid) / sum(t.contracts) / {PRICE_SCALE} AS avg_price,
        sum(t.contracts * (m.result = t.taker_side)::INT) / sum(t.contracts) AS win_rate""",
        "t.bin_cents").order("bin_cents").df()
    out["gross_return"] = out["win_rate"] / out["avg_price"] - 1
    return out
