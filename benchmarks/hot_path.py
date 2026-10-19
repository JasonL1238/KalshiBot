"""P1: how fast is a pure-Python hot path, decode → book → decide → sign → send?

Run: python benchmarks/hot_path.py   (needs the [bench] extra)

A `harness.Sender` process replays synthetic Kalshi frames over loopback on an open-loop
schedule with bursts. Each stack consumes the feed, keeps every book, and runs one
decision rule: buy YES (an IOC limit at the ask) when a market's best ask first falls a
tick below where its snapshot put it. Each order is signed (Ed25519, `KalshiSigner`),
serialized, and POSTed without blocking the feed to a local TLS server
(`harness.OrderServer`) that acknowledges it. Stacks:
- incumbent: what the repo uses today. asyncio, websockets through `kalshi.ws.Connection`,
  stdlib json, `kalshi.ws.BookFeed`, httpx (HTTP/1.1);
- P1: uvloop, picows, msgspec typed structs into `decode.StructFeed` (the same
  `kalshi.models.OrderBook`), aiohttp, and `gc.freeze()` once connected.
Both must make the same decisions. Latencies run from a frame's scheduled send: to its
book being updated (every frame), to the signed order being ready (tick-to-order), and
to the acknowledgement being read (tick-to-ack, loopback TLS). Stage costs are timed
inside the callback. Nothing here reaches Kalshi, and the key is generated per run.
"""

from __future__ import annotations

import asyncio
import gc
import json
from array import array

import aiohttp
import decode
import harness
import httpx
import msgspec
from cryptography.hazmat.primitives.asymmetric import ed25519
from picows import WSCloseCode, WSListener, WSMsgType, ws_connect
from websockets.asyncio.client import connect

from kalshibot.kalshi.auth import KalshiSigner
from kalshibot.kalshi.models import PRICE_SCALE, OrderBook
from kalshibot.kalshi.ws import BookFeed, Connection

PATTERNS = {"steady": harness.Pattern(2000),
            "bursty": harness.Pattern(1000, burst_every=0.2, burst_size=200)}
TICK = 100  # price units
PACKAGES = ("uvloop", "picows", "msgspec", "aiohttp", "websockets", "httpx", "cryptography")


class Strategy:
    """The decision rule, plus every stage's timings. Edge-triggered per market, so a
    market fires again only after its ask has gone back above the limit."""

    def __init__(self, signer: KalshiSigner, encode, n: int):
        self.signer, self.encode = signer, encode
        self.limit: dict[str, int] = {}
        self.armed: dict[str, bool] = {}
        self.decisions: list[tuple[int, str, int]] = []  # (frame index, ticker, price)
        self.stage = {k: array("q", bytes(8 * n)) for k in ("decode", "book")}
        self.signing, self.to_order, self.to_ack = array("q"), array("q"), array("q")

    def decide(self, book: OrderBook, i: int) -> tuple[dict, bytes] | None:
        ask, ticker = book.best_yes_ask, book.ticker
        if ask is None:
            return None
        limit = self.limit.setdefault(ticker, ask - TICK)
        if ask > limit:
            self.armed[ticker] = True
            return None
        if not self.armed.get(ticker):
            return None
        self.armed[ticker] = False
        self.decisions.append((i, ticker, ask))
        body = self.encode({
            "ticker": ticker, "side": "bid", "count": "1.00",
            "price": f"{ask // PRICE_SCALE}.{ask % PRICE_SCALE:04d}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": f"p1-{len(self.decisions)}"})
        return self.signed(body)

    def signed(self, body: bytes) -> tuple[dict, bytes]:
        headers = self.signer.headers("POST", harness.ORDER_PATH)
        headers["Content-Type"] = "application/json"
        return headers, body

    def warmup_order(self) -> tuple[dict, bytes]:
        """An order sent before the replay, to open the HTTP connection."""
        return self.signed(self.encode({"ticker": "WARMUP", "side": "bid", "count": "1.00",
                                        "price": "0.0100", "time_in_force": "immediate_or_cancel",
                                        "self_trade_prevention_type": "taker_at_cross"}))


class Stack:
    """Consumes one replay: `on_frame` runs decode → book → decide → sign for each frame,
    then hands any order to `send` (a coroutine function) as a task."""

    def __init__(self, decoder, feed, strategy: Strategy, sink: harness.Recorder, send):
        self.decoder, self.feed, self.strategy, self.sink = decoder, feed, strategy, sink
        self.send, self.tasks = send, set()

    def on_frame(self, payload) -> None:
        sink, clock = self.sink, harness.clock
        if not sink.t0:
            sink.frame(payload)
            return
        i, s = sink.i, self.strategy
        due = sink.t0 + sink.offsets[i]
        t0 = clock()
        msg = self.decoder(payload)
        t1 = clock()
        ticker = self.feed.on_message(msg)
        t2 = clock()
        s.stage["decode"][i], s.stage["book"][i] = t1 - t0, t2 - t1
        if ticker is not None and (order := s.decide(self.feed.books[ticker], i)):
            t3 = clock()
            s.signing.append(t3 - t2)
            s.to_order.append(t3 - due)
            task = asyncio.get_running_loop().create_task(self.post(order, due))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        sink.frame(payload)

    async def post(self, order: tuple[dict, bytes], due: int) -> None:
        await self.send(*order)
        self.strategy.to_ack.append(harness.clock() - due)

    async def drain(self) -> None:
        while self.tasks:
            await asyncio.gather(*self.tasks)


async def incumbent(url: str, sink, strategy: Strategy, server: harness.OrderServer) -> None:
    async with httpx.AsyncClient(verify=server.ssl_context(), trust_env=False) as http:
        async def send(headers: dict, body: bytes) -> None:
            (await http.post(server.url, content=body, headers=headers)).raise_for_status()

        await send(*strategy.warmup_order())
        stack = Stack(json.loads, BookFeed(), strategy, sink, send)
        async with connect(url, max_size=None, compression=None, ping_interval=None,
                           proxy=None) as ws:
            async for _, frame in Connection(ws):
                stack.on_frame(frame)
                if sink.done:
                    break
        await stack.drain()


class _Listener(WSListener):
    def __init__(self, stack: Stack):
        self.stack = stack

    def on_ws_frame(self, transport, frame) -> None:
        if frame.msg_type is WSMsgType.TEXT:  # Kalshi's frames aren't fragmented
            self.stack.on_frame(frame.get_payload_as_bytes())
            if self.stack.sink.done:
                transport.send_close(WSCloseCode.OK)
                transport.disconnect()
        elif frame.msg_type is WSMsgType.CLOSE:
            transport.send_close(frame.get_close_code())
            transport.disconnect()


async def p1(url: str, sink, strategy: Strategy, server: harness.OrderServer) -> None:
    connector = aiohttp.TCPConnector(ssl=server.ssl_context(), keepalive_timeout=600)
    async with aiohttp.ClientSession(connector=connector) as http:
        async def send(headers: dict, body: bytes) -> None:
            async with http.post(server.url, data=body, headers=headers) as r:
                r.raise_for_status()
                await r.read()

        await send(*strategy.warmup_order())
        stack = Stack(decode.typed_decoder.decode, decode.StructFeed(), strategy, sink, send)
        transport, _ = await ws_connect(lambda: _Listener(stack), url)
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


def run(n: int, pattern: harness.Pattern, rounds: int) -> dict[str, dict]:
    """Each stack consumes `rounds` replays (interleaved). Returns per stack: frame
    latencies, stage costs, decision latencies, and the decisions it made."""
    signer = KalshiSigner("bench", ed25519.Ed25519PrivateKey.generate())
    stream = harness.Stream(n // rounds)
    offsets = harness.arrivals(n // rounds, pattern)
    results = {name: {"strategies": []} for name in STACKS}
    with harness.OrderServer() as server:
        def candidate(name: str):
            loop, consume, encode = STACKS[name]

            def go(url: str, sink: harness.Recorder) -> None:
                strategy = Strategy(signer, encode, len(offsets))
                results[name]["strategies"].append(strategy)
                harness.run_on(loop, lambda: consume(url, sink, strategy, server))
            return go

        lat, lag, cpu = harness.replay({name: candidate(name) for name in STACKS},
                                       stream.frames, offsets, rounds)
    for name, r in results.items():
        r.update(frames=lat[name], cpu=cpu[name], lag=lag)
    return results


def check_agreement(results: dict[str, dict]) -> int:
    """Every stack made the same decisions in every round. Returns decisions per round."""
    rounds = [[s.decisions for s in r["strategies"]] for r in results.values()]
    first = rounds[0]
    assert all(other == first for other in rounds[1:]), "stacks decided differently"
    assert all(d == first[0] for d in first), "rounds decided differently"
    return len(first[0])


def pooled(strategies: list[Strategy], attr: str, key: str | None = None) -> array:
    out = array("q")
    for s in strategies:
        out.extend(getattr(s, attr)[key] if key else getattr(s, attr))
    return out


def main(n: int = 40_000, speed: float = 1.0, rounds: int = 4) -> None:
    harness.header("hot_path (P1): decode → book → decide → sign → send, end to end",
                   PACKAGES)
    for label, pattern in PATTERNS.items():
        pattern = pattern.faster(speed)
        with harness.Steal() as steal:
            results = run(n, pattern, rounds)
        decisions = check_agreement(results)
        print(f"open-loop replay, {label} ({pattern}), {rounds} × {n // rounds:,} frames "
              f"per stack, {decisions} orders per replay, host CPU steal {steal}")
        print(harness.table_head("") + f"{'CPU µs/frame':>14}")
        print(harness.row("(sender lag)", next(iter(results.values()))["lag"]))
        for name, r in results.items():
            st = r["strategies"]
            print(name)
            print(harness.row("  frame → processed", r["frames"]) + f"{r['cpu']:>14.1f}")
            print(harness.row("  frame → signed order", pooled(st, "to_order")))
            print(harness.row("  frame → ack (loopback TLS)", pooled(st, "to_ack")))
            print(harness.row("  stage: decode", pooled(st, "stage", "decode")))
            print(harness.row("  stage: book", pooled(st, "stage", "book")))
            print(harness.row("  stage: decide + sign", pooled(st, "signing")))
        print()


if __name__ == "__main__":
    main()
