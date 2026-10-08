"""Fixed-point units and the order book shared by recording, research, and execution.

Kalshi sends prices as dollar strings with up to 4 decimals ("0.5600") and sizes as
strings with up to 2 decimals ("61.04"; contracts can be fractional). Floats can't hold
these exactly, so every price and size in this codebase is an integer:

    price: 1 unit = $0.0001, so a binary contract trades in 0..10_000 (PRICE_SCALE)
    size:  1 unit = 0.01 contracts

Kalshi's book lists *bids only*, on both sides. Buying YES at p is the same trade as
selling NO at 1 − p, so the best YES ask is 10_000 − best NO bid, and vice versa.
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Iterable

PRICE_SCALE = 10_000  # price units per dollar

Level = tuple[int, int]  # (price units, size units)


def _fixed(text: str, decimals: int) -> int:
    """Parse a decimal string exactly ("0.5600" -> 5600 at 4 decimals)."""
    if not isinstance(text, str):
        raise TypeError(f"expected a decimal string, got {type(text).__name__}: {text!r}")
    sign, digits = (-1, text[1:]) if text.startswith("-") else (1, text.removeprefix("+"))
    whole, _, frac = digits.partition(".")
    if not (whole or frac) or not ((whole + frac).isascii() and (whole + frac).isdigit()):
        raise ValueError(f"not a decimal string: {text!r}")
    if frac[decimals:].strip("0"):
        raise ValueError(f"{text!r} has more than {decimals} decimals")
    return sign * (int(whole or "0") * 10**decimals + int(frac[:decimals].ljust(decimals, "0")))


def price_units(text: str) -> int:
    """A contract price, "0.5600" -> 5600. Must lie in 0..PRICE_SCALE."""
    price = _fixed(text, 4)
    if not 0 <= price <= PRICE_SCALE:
        raise ValueError(f"price {text!r} outside $0..$1")
    return price


def size_units(text: str) -> int:
    """A size or signed size delta, "61.04" -> 6104, "-54.00" -> -5400."""
    return _fixed(text, 2)


def optional_units(parse: Callable[[str], int], value: str | None) -> int | None:
    """`parse(value)`, or None for an absent API field (None or "")."""
    return None if value is None or value == "" else parse(value)


def on_tick(price: int, price_ranges: list[dict[str, str]]) -> bool:
    """Is `price` on the market's tick grid? `price_ranges` is the market's list of
    {"start", "end", "step"} dollar strings (some markets use sub-penny steps near 0 and 1)."""
    for r in price_ranges:
        start, end, step = (price_units(r[k]) for k in ("start", "end", "step"))
        if start <= price <= end and (price - start) % step == 0:
            return True
    return False


class BookSide:
    """Bids on one side of one market: sizes in a dict plus a two-level price bitmap.

    `apply`/`set`/`size_at` are O(1). `best` is O(1): `int.bit_length` on a summary word
    over 157 64-bit words. Memory is a small fixed bitmap plus a dict entry per level.
    The choice and its alternatives are measured in docs/EXECUTION.md.
    """

    __slots__ = ("_size", "_words", "_summary")

    def __init__(self, levels: Iterable[Level] = ()):
        self._size: dict[int, int] = {}
        self._words = [0] * ((PRICE_SCALE >> 6) + 1)
        self._summary = 0
        for price, size in levels:
            self.set(price, size)

    def set(self, price: int, size: int) -> None:
        price = operator.index(price)
        self.apply(price, operator.index(size) - self._size.get(price, 0))

    def apply(self, price: int, delta: int) -> None:
        """Add `delta` size units at `price`. Invalid input raises and changes nothing."""
        price, delta = operator.index(price), operator.index(delta)  # numpy ints -> int
        if not 0 <= price <= PRICE_SCALE:
            raise ValueError(f"price {price} outside 0..{PRICE_SCALE}")
        sizes = self._size
        old = sizes.get(price, 0)
        size = old + delta
        if size < 0:
            raise ValueError(f"size at price {price} would go negative ({size})")
        if size:
            sizes[price] = size
            if not old:
                self._flip(price, True)
        elif old:
            del sizes[price]
            self._flip(price, False)

    def _flip(self, price: int, on: bool) -> None:
        word, bit = price >> 6, 1 << (price & 63)
        if on:
            self._words[word] |= bit
            self._summary |= 1 << word
        else:
            self._words[word] &= ~bit
            if not self._words[word]:
                self._summary &= ~(1 << word)

    def size_at(self, price: int) -> int:
        return self._size.get(price, 0)

    def best(self) -> int | None:
        if not self._summary:
            return None
        word = self._summary.bit_length() - 1
        return (word << 6) | (self._words[word].bit_length() - 1)

    def levels(self, depth: int | None = None) -> list[Level]:
        """The best `depth` non-empty levels (all if None), highest price first."""
        out: list[Level] = []
        summary = self._summary
        while summary and (depth is None or len(out) < depth):
            word = summary.bit_length() - 1
            bits = self._words[word]
            while bits and (depth is None or len(out) < depth):
                bit = bits.bit_length() - 1
                price = (word << 6) | bit
                out.append((price, self._size[price]))
                bits &= ~(1 << bit)
            summary &= ~(1 << word)
        return out

    def __bool__(self) -> bool:
        return bool(self._summary)


class OrderBook:
    """Both sides of one binary market. Mutable: snapshots build it, deltas update it."""

    __slots__ = ("ticker", "yes", "no")

    def __init__(self, ticker: str, yes: Iterable[Level] = (), no: Iterable[Level] = ()):
        self.ticker = ticker
        self.yes = BookSide(yes)
        self.no = BookSide(no)

    @classmethod
    def from_api(cls, ticker: str, payload: dict) -> OrderBook:
        """From a REST orderbook response (`{"orderbook_fp": {...}}`) or a WebSocket
        `orderbook_snapshot` msg (the `msg` object, whose `market_ticker` must match).
        The WebSocket format omits a side's key when that side is empty."""
        if "orderbook_fp" in payload:
            book, keys = payload["orderbook_fp"], ("yes_dollars", "no_dollars")
            if not all(k in book for k in keys):
                raise ValueError(f"REST orderbook for {ticker} lacks {keys}: {sorted(book)}")
        elif payload.get("market_ticker") == ticker:
            book, keys = payload, ("yes_dollars_fp", "no_dollars_fp")
        else:
            raise ValueError(f"not an order book for {ticker}: keys {sorted(payload)}")

        def levels(key: str) -> list[Level]:
            return [(price_units(p), size_units(s)) for p, s in book.get(key) or []]

        return cls(ticker, levels(keys[0]), levels(keys[1]))

    @property
    def best_yes_bid(self) -> int | None:
        return self.yes.best()

    @property
    def best_no_bid(self) -> int | None:
        return self.no.best()

    @property
    def best_yes_ask(self) -> int | None:
        bid = self.no.best()
        return None if bid is None else PRICE_SCALE - bid

    @property
    def best_no_ask(self) -> int | None:
        bid = self.yes.best()
        return None if bid is None else PRICE_SCALE - bid

    @property
    def yes_bid_size(self) -> int | None:
        bid = self.yes.best()
        return None if bid is None else self.yes.size_at(bid)

    @property
    def yes_ask_size(self) -> int | None:
        bid = self.no.best()
        return None if bid is None else self.no.size_at(bid)

    @property
    def spread(self) -> int | None:
        bid, ask = self.best_yes_bid, self.best_yes_ask
        return None if bid is None or ask is None else ask - bid
