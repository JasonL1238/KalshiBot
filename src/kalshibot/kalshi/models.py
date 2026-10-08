"""Small typed views over Kalshi API payloads.

Kalshi now returns prices as fixed-point dollar strings ("0.5600") and sizes as
fixed-point strings ("61.04" — contracts can be fractional). We parse them to floats
for analysis. When we start placing orders we'll send prices back as strings.
"""

from __future__ import annotations

from dataclasses import dataclass

Level = tuple[float, float]  # (price in dollars, size in contracts)


def to_float(value: str | float | int | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


@dataclass(frozen=True)
class OrderBook:
    """Top of book for one binary market.

    Kalshi's book only lists *bids*, on both sides. That's enough, because buying YES
    at p is the same trade as selling NO at 1 - p. So:

        best YES ask = 1 - best NO bid
        best NO  ask = 1 - best YES bid
    """

    ticker: str
    yes_bids: tuple[Level, ...]  # best (highest) first
    no_bids: tuple[Level, ...]  # best (highest) first

    @classmethod
    def from_api(cls, ticker: str, payload: dict) -> OrderBook:
        book = payload.get("orderbook_fp") or {}

        def levels(key: str) -> tuple[Level, ...]:
            raw = book.get(key) or []
            parsed = [(float(price), float(size)) for price, size in raw]
            return tuple(sorted(parsed, key=lambda lvl: lvl[0], reverse=True))

        return cls(ticker=ticker, yes_bids=levels("yes_dollars"), no_bids=levels("no_dollars"))

    @property
    def best_yes_bid(self) -> float | None:
        return self.yes_bids[0][0] if self.yes_bids else None

    @property
    def best_no_bid(self) -> float | None:
        return self.no_bids[0][0] if self.no_bids else None

    @property
    def best_yes_ask(self) -> float | None:
        return round(1 - self.best_no_bid, 4) if self.no_bids else None

    @property
    def best_no_ask(self) -> float | None:
        return round(1 - self.best_yes_bid, 4) if self.yes_bids else None

    @property
    def yes_bid_size(self) -> float | None:
        return self.yes_bids[0][1] if self.yes_bids else None

    @property
    def yes_ask_size(self) -> float | None:
        return self.no_bids[0][1] if self.no_bids else None

    @property
    def mid(self) -> float | None:
        if self.best_yes_bid is None or self.best_yes_ask is None:
            return None
        return round((self.best_yes_bid + self.best_yes_ask) / 2, 4)

    @property
    def spread(self) -> float | None:
        if self.best_yes_bid is None or self.best_yes_ask is None:
            return None
        return round(self.best_yes_ask - self.best_yes_bid, 4)
