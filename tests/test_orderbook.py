import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from kalshibot.kalshi.models import (
    BookSide,
    OrderBook,
    on_tick,
    optional_units,
    price_units,
    size_units,
)

# Shape copied from a real GET /markets/{ticker}/orderbook response (levels ascending).
REST = {
    "orderbook_fp": {
        "no_dollars": [["0.4000", "92.00"], ["0.4300", "47.00"], ["0.4400", "13.00"]],
        "yes_dollars": [["0.5100", "37.00"], ["0.5400", "60.00"], ["0.5500", "34.01"]],
    }
}


def test_fixed_point_parsing_is_exact():
    assert price_units("0.5600") == 5600
    assert price_units("0.0001") == 1
    assert price_units("0.57000") == 5700
    assert price_units("1") == 10_000
    assert size_units("61.04") == 6104
    assert size_units("-54.00") == -5400
    assert size_units("0.5") == 50
    assert optional_units(price_units, "") is None and optional_units(size_units, None) is None


@pytest.mark.parametrize("bad", ["0.00005", "", "-", "1e-5", "0.5.1", "²", "nan", "1.0001", "-0.5"])
def test_bad_prices_rejected(bad):
    with pytest.raises(ValueError):
        price_units(bad)


def test_floats_rejected():
    with pytest.raises(TypeError):
        price_units(0.57)


@given(st.integers(0, 10_000))
def test_price_round_trip(units):
    assert price_units(f"{units // 10_000}.{units % 10_000:04d}") == units


def test_tick_grid():
    cent = [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}]
    tapered = [{"start": "0.0000", "end": "0.1000", "step": "0.0010"},
               {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
               {"start": "0.9000", "end": "1.0000", "step": "0.0010"}]
    assert on_tick(5600, cent) and not on_tick(5650, cent)
    assert on_tick(550, tapered) and on_tick(9990, tapered) and not on_tick(5050, tapered)


def test_rest_book_best_levels_and_derived_asks():
    book = OrderBook.from_api("T", REST)
    assert (book.best_yes_bid, book.best_no_bid, book.yes_bid_size) == (5500, 4400, 3401)
    assert (book.best_yes_ask, book.best_no_ask, book.yes_ask_size) == (5600, 4500, 1300)
    assert book.spread == 100
    assert book.yes.levels(2) == [(5500, 3401), (5400, 6000)]


def test_empty_rest_side():
    book = OrderBook.from_api("T", {"orderbook_fp": {"yes_dollars": None, "no_dollars": []}})
    assert book.best_yes_bid is None and book.best_yes_ask is None and book.spread is None


def test_websocket_snapshot_with_an_omitted_side():
    msg = {"market_ticker": "T", "no_dollars_fp": [["0.5600", "146.00"]]}
    book = OrderBook.from_api("T", msg)
    assert (book.best_yes_bid, book.best_yes_ask, book.yes_ask_size) == (None, 4400, 14600)


@pytest.mark.parametrize("payload", [
    {"type": "orderbook_snapshot", "msg": {"market_ticker": "T"}},  # envelope, not msg
    {"market_ticker": "OTHER", "yes_dollars_fp": []},  # ticker mismatch
    {"orderbook_fp": {"yes_dollar": []}},  # missing REST keys
    {},
])
def test_wrong_book_shapes_rejected(payload):
    with pytest.raises(ValueError):
        OrderBook.from_api("T", payload)


def test_numpy_ints_are_normalized():
    side = BookSide()
    side.apply(np.int64(7400), np.int64(100))
    assert side.best() == 7400 and side.levels() == [(7400, 100)]


@pytest.mark.parametrize("price, delta, error", [
    (-1, 100, ValueError), (10_001, 100, ValueError), (5000.0, 100, TypeError),
    (5000, -101, ValueError),
])
def test_rejected_updates_change_nothing(price, delta, error):
    side = BookSide([(5000, 100)])
    with pytest.raises(error):
        side.apply(price, delta)
    assert side.levels() == [(5000, 100)] and side.size_at(10_001) == 0
    side.apply(5000, 50)
    assert side.levels() == [(5000, 150)]


PRICES = st.one_of(st.sampled_from([0, 1, 62, 63, 64, 65, 127, 128, 5000, 5001, 9999, 10_000]),
                   st.integers(0, 10_000))


class BookSideVsDict(RuleBasedStateMachine):
    """Random applies/sets (clustered so levels empty, incl. the best) vs a dict model."""

    def __init__(self):
        super().__init__()
        self.side, self.model = BookSide(), {}

    @rule(price=PRICES, delta=st.integers(-2_000, 2_000))
    def apply(self, price, delta):
        held = self.model.get(price, 0)
        if held + delta < 0:
            with pytest.raises(ValueError):
                self.side.apply(price, delta)
            return
        self.side.apply(price, delta)
        self.model[price] = held + delta

    @rule(price=PRICES, size=st.integers(0, 3))
    def set(self, price, size):
        self.side.set(price, size)
        self.model[price] = size

    @rule()
    def empty_the_best(self):
        if (best := self.side.best()) is not None:
            self.side.apply(best, -self.model[best])
            self.model[best] = 0

    @invariant()
    def matches_model(self):
        live = {p: s for p, s in self.model.items() if s}
        assert self.side.best() == (max(live) if live else None)
        assert self.side.levels() == sorted(live.items(), reverse=True)
        assert bool(self.side) == bool(live)
        assert all(self.side.size_at(p) == s for p, s in self.model.items())


TestBookSideVsDict = BookSideVsDict.TestCase
