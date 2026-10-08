from kalshibot.kalshi.models import OrderBook

# Shape copied from a real GET /markets/{ticker}/orderbook response (levels ascending).
PAYLOAD = {
    "orderbook_fp": {
        "no_dollars": [["0.4000", "92.00"], ["0.4300", "47.00"], ["0.4400", "13.00"]],
        "yes_dollars": [["0.5100", "37.00"], ["0.5400", "60.00"], ["0.5500", "34.01"]],
    }
}


def test_best_levels_are_highest_bids():
    book = OrderBook.from_api("T", PAYLOAD)
    assert book.best_yes_bid == 0.55
    assert book.best_no_bid == 0.44
    assert book.yes_bid_size == 34.01


def test_asks_are_derived_from_opposite_side_bids():
    book = OrderBook.from_api("T", PAYLOAD)
    assert book.best_yes_ask == 0.56  # 1 - best NO bid
    assert book.best_no_ask == 0.45  # 1 - best YES bid
    assert book.yes_ask_size == 13.0
    assert book.spread == 0.01
    assert book.mid == 0.555


def test_empty_side():
    book = OrderBook.from_api("T", {"orderbook_fp": {"yes_dollars": None, "no_dollars": []}})
    assert book.best_yes_bid is None
    assert book.best_yes_ask is None
    assert book.mid is None
