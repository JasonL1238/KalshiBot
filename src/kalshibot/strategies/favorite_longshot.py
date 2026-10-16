"""H1, the favorite–longshot bias (docs/STRATEGIES.md): takers overpay for longshots, so
buying the favorite's side as a taker should earn more than it costs.

A configuration watches every market's prints (block trades aside) and decides at the
first print of each UTC day past its threshold: YES at or below θ buys NO, YES at or
above θ buys YES. It buys one contract with a limit 2¢ past that print's price for the
side it buys, so it doesn't chase a market that has moved against it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from kalshibot.kalshi.models import PRICE_SCALE, SIZE_SCALE
from kalshibot.strategies.base import INTENT_COLUMNS, MarketData

SLIPPAGE = 200  # price units the limit allows past the deciding print


class FavoriteLongshot:
    family = "H1"
    max_hold = timedelta(days=15)
    min_edge = 50  # 0.5¢ a contract

    def configs(self) -> dict[str, dict[str, Any]]:
        no = {f"NO at YES ≤ {c}¢": {"buy": "no", "yes_price": c * 100} for c in (3, 5, 10, 15, 20)}
        yes = {f"YES at YES ≥ {c}¢": {"buy": "yes", "yes_price": c * 100}
               for c in (80, 85, 90, 95)}
        return no | yes

    def intents(self, data: MarketData, start: datetime, end: datetime,
                config: dict[str, Any]) -> pd.DataFrame:
        buy, threshold = config["buy"], config["yes_price"]
        past = f"yes_price {'<=' if buy == 'no' else '>='} {threshold}"
        # trade_id doesn't order an instant's prints (docs/RESEARCH.md): a tie can pick
        # any level of a sweep
        first = data.trades(start, end).query("t", f"""
            SELECT ticker, created_time AS decision_time, yes_price FROM t
            WHERE {past} AND NOT coalesce(is_block_trade, FALSE)
            QUALIFY row_number() OVER (PARTITION BY ticker, timezone('UTC', created_time)::DATE
                                       ORDER BY created_time, trade_id) = 1
            ORDER BY decision_time, ticker""").df()
        price = first["yes_price"] if buy == "yes" else PRICE_SCALE - first["yes_price"]
        return pd.DataFrame({
            "intent_id": range(len(first)), "ticker": first["ticker"], "side": buy,
            "decision_time": first["decision_time"], "count": SIZE_SCALE,
            "limit": (price + SLIPPAGE).clip(upper=PRICE_SCALE)}, columns=INTENT_COLUMNS)
