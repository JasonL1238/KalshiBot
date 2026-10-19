"""P1: how fast is a pure-Python hot path, decode → book → decide → sign → send?

Run: python benchmarks/hot_path.py   (needs the [bench] extra)

A `harness.Sender` process replays synthetic Kalshi frames over loopback on an open-loop
schedule with bursts. Each stack consumes the feed, keeps every book, and runs one
decision rule: buy YES (an IOC limit at the ask) when a market's best ask first falls a
tick below where its snapshot put it. Each order is signed (Ed25519, `KalshiSigner`),
serialized, and POSTed, without blocking the feed, to a local TLS server
(`harness.OrderServer`) that acknowledges it. Stacks:
- incumbent: what the repo uses today. asyncio, websockets read through
  `kalshi.ws.Connection`, stdlib json, `kalshi.ws.BookFeed`, httpx (HTTP/1.1);
- P1: uvloop, picows, msgspec typed structs into `decode.StructFeed` (the same
  `kalshi.models.OrderBook`), aiohttp, and `gc.freeze()` once connected.
Both warm their HTTP pools to a burst's worth of connections first, and must make the
same decisions. Latencies run from a frame's scheduled send: to its processing being
done (every frame), to the signed order being ready (tick-to-order), and to the
acknowledgement being read (tick-to-ack, loopback TLS). Stage costs are timed inside
the callback. Nothing here reaches Kalshi, and the key is generated per run.
"""

from __future__ import annotations

import asyncio
import gc
import json
from array import array
from dataclasses import dataclass, field

import decode
import harness
import http_clients
import msgspec
import ws_clients
from cryptography.hazmat.primitives.asymmetric import ed25519
from picows import ws_connect
from websockets.asyncio.client import connect

from kalshibot.kalshi.auth import KalshiSigner
from kalshibot.kalshi.models import OrderBook
from kalshibot.kalshi.ws import BookFeed, Connection

WARM_BURST = 16  # orders that can be in flight at once, as connections to open first
PACKAGES = ("uvloop", "picows", "msgspec", "aiohttp", "websockets", "httpx", "cryptography")


@dataclass
class Timings:
    """One replay's measurements, ns: per frame, and per order."""
    decode: array
    book: array
    decide_sign: array = field(default_factory=lambda: array("q"))
    to_order: array = field(default_factory=lambda: array("q"))
    to_ack: array = field(default_factory=lambda: array("q"))

    @classmethod
    def for_frames(cls, n: int) -> Timings:
        return cls(array("q", bytes(8 * n)), array("q", bytes(8 * n)))


class Strategy:
    """The decision rule. Edge-triggered per market: a market fires again only after its
    ask has gone back above the limit."""

    def __init__(self, signer: KalshiSigner, encode):
        self.signer, self.encode = signer, encode
        self.limit: dict[str, int] = {}
        self.armed: dict[str, bool] = {}
        self.decisions: list[tuple[int, str, int]] = []  # (frame index, ticker, price)

    def decide(self, book: OrderBook, i: int) -> tuple[dict, bytes] | None:
        """The signed order (headers, body) to send now, if any."""
        ask, ticker = book.best_yes_ask, book.ticker
        if ask is None:
            return None
        limit = self.limit.setdefault(ticker, ask - harness.PRICE_GRID)
        if ask > limit:
            self.armed[ticker] = True
            return None
        if not self.armed.get(ticker):
            return None
        self.armed[ticker] = False
        self.decisions.append((i, ticker, ask))
        body = self.encode({**harness.ORDER, "ticker": ticker, "price": harness.dollars(ask),
                            "client_order_id": f"p1-{len(self.decisions)}"})
        headers = self.signer.headers("POST", harness.ORDER_PATH)
        headers["Content-Type"] = "application/json"
        return headers, body


class Stack:
    """Consumes one replay: `on_frame` runs decode → book → decide → sign for each frame,
    then hands any order to `post(body, headers)` as a task."""

    def __init__(self, decoder, feed, strategy: Strategy, timings: Timings,
                 sink: harness.Recorder, post):
        self.decoder, self.feed, self.strategy, self.timings = decoder, feed, strategy, timings
        self.sink, self.post, self.tasks = sink, post, set()

    def on_frame(self, payload) -> None:
        sink, clock, t = self.sink, harness.clock, self.timings
        if not sink.t0:
            sink.frame(payload)
            return
        i = sink.i
        due = sink.t0 + sink.offsets[i]
        t0 = clock()
        msg = self.decoder(payload)
        t1 = clock()
        ticker = self.feed.on_message(msg)
        t2 = clock()
        t.decode[i], t.book[i] = t1 - t0, t2 - t1
        if ticker is not None and (order := self.strategy.decide(self.feed.books[ticker], i)):
            t3 = clock()
            t.decide_sign.append(t3 - t2)
            t.to_order.append(t3 - due)
            task = asyncio.get_running_loop().create_task(self.send(order, due))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        sink.frame(payload)

    async def send(self, order: tuple[dict, bytes], due: int) -> None:
        headers, body = order
        await self.post(body, headers)
        self.timings.to_ack.append(harness.clock() - due)

    async def drain(self) -> None:
        while self.tasks:
            await asyncio.gather(*self.tasks)


async def incumbent(url: str, sink, strategy: Strategy, timings: Timings,
                    server: harness.OrderServer) -> None:
    async with http_clients.httpx_client(server) as post:
        await http_clients.warm(post, WARM_BURST)
        stack = Stack(json.loads, BookFeed(), strategy, timings, sink, post)
        async with connect(url, **ws_clients.WEBSOCKETS_OPTIONS) as ws:
            async for _, frame in Connection(ws):
                stack.on_frame(frame)
                if sink.done:
                    break
        await stack.drain()


async def p1(url: str, sink, strategy: Strategy, timings: Timings,
             server: harness.OrderServer) -> None:
    async with http_clients.aiohttp_client(server) as post:
        await http_clients.warm(post, WARM_BURST)
        stack = Stack(decode.typed_decoder.decode, decode.StructFeed(), strategy, timings,
                      sink, post)
        transport, _ = await ws_connect(
            lambda: ws_clients.Listener(stack.on_frame, lambda: sink.done), url)
        gc.freeze()
        try:
            await transport.wait_disconnected()
            await stack.drain()
        finally:
            gc.unfreeze()


STACKS = {  # name -> (event loop, consumer, order-body encoder to bytes)
    "incumbent (asyncio, websockets, json, httpx)":
        ("asyncio", incumbent, lambda order: json.dumps(order).encode()),
    "P1 (uvloop, picows, msgspec, aiohttp)": ("uvloop", p1, msgspec.json.encode),
}


@dataclass
class StackRuns:
    runs: list[tuple[Strategy, Timings]]  # one per timed replay
    frames: array  # frame → processed, ns
    cpu: float  # µs per frame

    def pooled(self, name: str) -> array:
        out = array("q")
        for _, t in self.runs:
            out.extend(getattr(t, name))
        return out


def run(n: int, pattern: harness.Pattern, rounds: int) -> tuple[dict[str, StackRuns], array]:
    """Each stack consumes `rounds` replays of one stream (interleaved). Returns each
    stack's runs, and the sender's lag."""
    signer = KalshiSigner("bench", ed25519.Ed25519PrivateKey.generate())
    stream = harness.Stream(n // rounds)
    offsets = harness.arrivals(n // rounds, pattern)
    runs: dict[str, list] = {name: [] for name in STACKS}
    with harness.OrderServer() as server:
        def candidate(name: str):
            loop, consume, encode = STACKS[name]

            def go(url: str, sink: harness.Recorder) -> None:
                strategy, timings = Strategy(signer, encode), Timings.for_frames(len(offsets))
                runs[name].append((strategy, timings))
                harness.run_on(loop, lambda: consume(url, sink, strategy, timings, server))
            return go

        result = harness.replay({name: candidate(name) for name in STACKS}, stream.frames,
                                offsets, rounds)
    # replay() runs each candidate once untimed before its rounds: drop that warm-up
    return {name: StackRuns(r[1:], result.lat[name], result.cpu[name])
            for name, r in runs.items()}, result.lag


def check_agreement(results: dict[str, StackRuns]) -> int:
    """Every stack made the same decisions in every round. Returns decisions per round."""
    decisions = [s.decisions for r in results.values() for s, _ in r.runs]
    assert all(d == decisions[0] for d in decisions), "stacks or rounds decided differently"
    assert decisions[0], "the rule never fired, so the comparison would be vacuous"
    return len(decisions[0])


def main(n: int = 40_000, speed: float = 1.0, rounds: int | None = None) -> None:
    harness.header("hot_path (P1): decode → book → decide → sign → send, end to end",
                   PACKAGES)
    rounds = rounds or harness.default_rounds(len(STACKS))
    for label, pattern in harness.PATTERNS.items():
        pattern = pattern.faster(speed)
        with harness.Steal() as steal:
            results, lag = run(n, pattern, rounds)
        decisions = check_agreement(results)
        print(f"open-loop replay, {label} ({pattern}), {rounds} × {n // rounds:,} frames "
              f"per stack, {decisions} orders per replay, host CPU steal {steal}")
        print(harness.table_head("") + f"{'CPU µs/frame':>14}")
        print(harness.row("(sender lag, per write)", lag))
        for name, r in results.items():
            print(name)
            print(harness.row("  frame → processed", r.frames) + f"{r.cpu:>14.1f}")
            print(harness.row("  frame → signed order", r.pooled("to_order")))
            print(harness.row("  frame → ack (loopback TLS)", r.pooled("to_ack")))
            print(harness.row("  stage: decode", r.pooled("decode")))
            print(harness.row("  stage: book", r.pooled("book")))
            print(harness.row("  stage: decide + sign", r.pooled("decide_sign")))
        print()


if __name__ == "__main__":
    main()
