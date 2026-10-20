"""Which JSON decoder should the hot path use for Kalshi's WebSocket frames?

Run: python benchmarks/decode.py   (msgspec is in the [bench] extra and also arrives with
nautilus_trader; orjson is optional)

Candidates decode a frame and pull out the fields the next stage needs (book updates,
trade prints, our fills), so typed structs pay for nothing a dict reader skips:
- stdlib `json.loads` and `orjson.loads` (skipped where it has no wheel) into dicts;
- `msgspec.json` untyped (dicts) and typed: a tagged union of `msgspec.Struct`s that
  validates the schema while decoding (gc=False: structs the GC never tracks).
Frames are synthetic (`harness.Stream`) until recorded WebSocket frames exist. The script
prints ns per frame for each message type, then latency percentiles from an open-loop
replay of a mixed stream, steady and with bursts.
"""

from __future__ import annotations

import json
import statistics

import harness
import msgspec

from kalshibot.kalshi.models import OrderBook, price_units, size_units
from kalshibot.kalshi.ws import SequenceGap

try:
    import orjson
except ImportError:  # no wheel for free-threaded builds yet
    orjson = None


class Snapshot(msgspec.Struct, gc=False):
    market_ticker: str
    yes_dollars_fp: list[tuple[str, str]] = []
    no_dollars_fp: list[tuple[str, str]] = []


class Delta(msgspec.Struct, gc=False):
    market_ticker: str
    price_dollars: str
    delta_fp: str
    side: str
    ts_ms: int


class Trade(msgspec.Struct, gc=False):
    market_ticker: str
    yes_price_dollars: str
    count_fp: str
    taker_side: str
    ts: int


class Fill(msgspec.Struct, gc=False):
    order_id: str
    market_ticker: str
    side: str
    action: str
    yes_price_dollars: str
    count_fp: str
    is_taker: bool
    ts: int


class SnapshotMsg(msgspec.Struct, tag="orderbook_snapshot", tag_field="type", gc=False):
    sid: int
    seq: int
    msg: Snapshot


class DeltaMsg(msgspec.Struct, tag="orderbook_delta", tag_field="type", gc=False):
    sid: int
    seq: int
    msg: Delta


class TradeMsg(msgspec.Struct, tag="trade", tag_field="type", gc=False):
    sid: int
    msg: Trade
    seq: int | None = None  # only book messages are known to carry one


class FillMsg(msgspec.Struct, tag="fill", tag_field="type", gc=False):
    sid: int
    msg: Fill
    seq: int | None = None


Message = SnapshotMsg | DeltaMsg | TradeMsg | FillMsg
typed_decoder = msgspec.json.Decoder(Message)


class StructFeed:
    """`kalshi.ws.BookFeed` for typed messages: the same books, sequence checks, and
    errors on well-formed input. (It parses before it checks the sequence, so a malformed
    price or size raises ValueError where BookFeed would advance the sequence first.)
    `on_message` is `apply(parse(m))`, two steps so that `hot_path.book_split_ns` can
    time parsing alone."""

    def __init__(self) -> None:
        self.books: dict[str, OrderBook] = {}
        self._next_seq: dict[int, int] = {}

    def on_message(self, m: Message) -> str | None:
        return self.apply(self.parse(m))

    @staticmethod
    def parse(m: Message) -> tuple:
        """(message, a, b) with its dollar and size strings as fixed-point units: for a
        delta, a and b are its price and size delta; for a snapshot, its yes and no
        levels; otherwise None."""
        b = m.msg
        if type(m) is DeltaMsg:
            return m, price_units(b.price_dollars), size_units(b.delta_fp)
        if type(m) is SnapshotMsg:
            return (m, [(price_units(p), size_units(s)) for p, s in b.yes_dollars_fp],
                    [(price_units(p), size_units(s)) for p, s in b.no_dollars_fp])
        return m, None, None

    def apply(self, parsed: tuple) -> str | None:
        """Check the sequence and update the books; the ticker changed, if any."""
        m, a, b = parsed
        kind = type(m)
        if kind is not DeltaMsg and kind is not SnapshotMsg:
            return None
        expected = self._next_seq.get(m.sid)
        if expected is not None and m.seq != expected:
            raise SequenceGap(m.sid, expected, m.seq)
        self._next_seq[m.sid] = m.seq + 1
        ticker = m.msg.market_ticker
        if kind is SnapshotMsg:
            self.books[ticker] = OrderBook(ticker, a, b)
        else:
            book = self.books.get(ticker)
            if book is None:
                raise SequenceGap(m.sid, m.seq, m.seq)
            (book.yes if m.msg.side == "yes" else book.no).apply(a, b)
        return ticker


def read_dict(m: dict) -> tuple:
    """What the hot path takes from each message, read from a dict."""
    kind, b = m["type"], m["msg"]
    if kind == "orderbook_delta":
        return (m["seq"], b["market_ticker"], b["side"], b["price_dollars"], b["delta_fp"])
    if kind == "trade":
        return (b["market_ticker"], b["yes_price_dollars"], b["count_fp"], b["taker_side"])
    if kind == "fill":
        return (b["order_id"], b["side"], b["action"], b["yes_price_dollars"], b["count_fp"])
    return (m["seq"], b["market_ticker"], b.get("yes_dollars_fp", ()), b.get("no_dollars_fp", ()))


def read_struct(m: Message) -> tuple:
    """The same fields from a typed struct."""
    b = m.msg
    if type(m) is DeltaMsg:
        return (m.seq, b.market_ticker, b.side, b.price_dollars, b.delta_fp)
    if type(m) is TradeMsg:
        return (b.market_ticker, b.yes_price_dollars, b.count_fp, b.taker_side)
    if type(m) is FillMsg:
        return (b.order_id, b.side, b.action, b.yes_price_dollars, b.count_fp)
    return (m.seq, b.market_ticker, b.yes_dollars_fp, b.no_dollars_fp)


_untyped = msgspec.json.Decoder()
CANDIDATES = {
    "json (stdlib)": lambda f: read_dict(json.loads(f)),
    "orjson": orjson and (lambda f: read_dict(orjson.loads(f))),
    "msgspec untyped": lambda f: read_dict(_untyped.decode(f)),
    "msgspec typed structs": lambda f: read_struct(typed_decoder.decode(f)),
}
CANDIDATES = {name: read for name, read in CANDIDATES.items() if read}
PACKAGES = ("orjson", "msgspec")


def check_agreement(frames: list[bytes]) -> None:
    for f in frames:  # compared as JSON, where tuples and lists are both arrays
        assert len({msgspec.json.encode(read(f)) for read in CANDIDATES.values()}) == 1, f


def by_type(stream: harness.Stream, snapshots: harness.Stream) -> dict[str, list[bytes]]:
    """Frames per message type. Snapshots come from a separate stream of many markets."""
    out: dict[str, list[bytes]] = {"orderbook_snapshot": snapshots.frames}
    for m, f in zip(stream.messages, stream.frames, strict=True):
        if m["type"] != "orderbook_snapshot":
            out.setdefault(m["type"], []).append(f)
    return out


def main(n: int = 40_000, speed: float = 1.0) -> None:
    harness.header("decode: JSON decoders on Kalshi WebSocket frames", PACKAGES)
    stream = harness.Stream(n)
    snapshots = harness.Stream(2_000, markets=2_000, seed=2)
    check_agreement(stream.frames[:5_000] + snapshots.frames[:200])
    kinds = by_type(stream, snapshots)
    sizes = {k: statistics.median(map(len, v)) for k, v in kinds.items()}
    print("median frame bytes: " + ", ".join(f"{k} {v:.0f}" for k, v in sizes.items()))
    print(f"\n{'ns per frame':<24}" + "".join(f"{k:>20}" for k in kinds))
    per_frame = {kind: harness.per_call_ns(CANDIDATES, frames) for kind, frames in kinds.items()}
    for name in CANDIDATES:
        print(f"{name:<24}" + "".join(f"{per_frame[kind][name]:>20.0f}" for kind in kinds))
    for label, pattern in harness.PATTERNS.items():
        pattern = pattern.faster(speed)
        offsets = harness.arrivals(n, pattern)
        with harness.Steal() as steal:
            lat = harness.compare({"(harness floor)": harness.noop, **CANDIDATES},
                                  stream.frames, offsets)
        print(f"\nopen-loop replay, {label} ({pattern}), {n:,} frames, host CPU steal {steal}")
        print(harness.table_head("decoder"))
        for name, ns in lat.items():
            print(harness.row(name, ns))


if __name__ == "__main__":
    main()
