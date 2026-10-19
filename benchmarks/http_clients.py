"""Which HTTP client should send orders: httpx over HTTP/1.1 or HTTP/2, or aiohttp?

Run: python benchmarks/http_clients.py   (needs the [bench] extra)

Orders are triggered on an open-loop schedule with bursts (a requote sends several at
once): a `harness.Sender` process writes one byte per order over loopback, and the
client starts each POST the moment its byte arrives, without waiting for earlier ones.
Each POST carries a V2 order body and pre-signed headers (sign.py prices signing) to a
local TLS server (`harness.OrderServer`, its own process) that acknowledges it with a
canned 201. Latency runs from the order's scheduled time to its response body being read. All
clients run on uvloop and keep warm connections: HTTP/1.1 opens one per order in
flight, HTTP/2 multiplexes them on one. Nothing here reaches Kalshi.
"""

from __future__ import annotations

import asyncio
import json
import struct
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import aiohttp
import harness
import httpx

PATTERN = harness.Pattern(200, burst_every=0.25, burst_size=10)
BODY = json.dumps({**harness.ORDER, "client_order_id": "6f1c0a52-0f43-4f53-9a4d-6d0c3e0e2b7a"}
                  ).encode()
HEADERS = {"Content-Type": "application/json", "KALSHI-ACCESS-KEY": "bench",
           "KALSHI-ACCESS-TIMESTAMP": "1792000000000", "KALSHI-ACCESS-SIGNATURE": "x" * 88}
POOL = 64  # connections each client may keep open


@asynccontextmanager
async def httpx_client(server: harness.OrderServer, http2: bool = False):
    """Yields `post(body, headers)`, which sends an order and reads the acknowledgement."""
    limits = httpx.Limits(max_connections=POOL, max_keepalive_connections=POOL,
                          keepalive_expiry=None)
    async with httpx.AsyncClient(http1=not http2, http2=http2, verify=server.ssl_context(),
                                 trust_env=False, limits=limits) as client:
        async def post(body: bytes = BODY, headers: dict = HEADERS) -> bytes:
            r = await client.post(server.url, content=body, headers=headers)
            assert r.status_code == 201 and r.http_version == ("HTTP/2" if http2 else
                                                               "HTTP/1.1"), r
            return r.content
        yield post


@asynccontextmanager
async def aiohttp_client(server: harness.OrderServer):
    connector = aiohttp.TCPConnector(ssl=server.ssl_context(), limit=POOL,
                                     keepalive_timeout=600)
    async with aiohttp.ClientSession(connector=connector) as session:
        async def post(body: bytes = BODY, headers: dict = HEADERS) -> bytes:
            async with session.post(server.url, data=body, headers=headers) as r:
                assert r.status == 201, r
                return await r.read()
        yield post


CLIENTS = {"httpx HTTP/1.1": httpx_client,
           "httpx HTTP/2": lambda s: httpx_client(s, http2=True),
           "aiohttp HTTP/1.1": aiohttp_client}
PACKAGES = ("httpx", "h2", "aiohttp", "uvloop")


class Triggers(asyncio.Protocol):
    """Reads the Sender's raw stream: 8 bytes of t0, then one byte per order. Fails
    `finished` if the stream ends before every order was triggered."""

    def __init__(self, sink: harness.Recorder, fire, finished: asyncio.Future):
        self.sink, self.fire, self.finished, self.buf, self.next = sink, fire, finished, b"", 0

    def data_received(self, data: bytes) -> None:
        if not self.sink.t0:
            self.buf += data
            if len(self.buf) < 8:
                return
            self.sink.start(struct.unpack("!q", self.buf[:8])[0])
            data = self.buf[8:]
        for _ in data:
            self.fire(self.next)
            self.next += 1

    def connection_lost(self, exc) -> None:
        if self.next < len(self.sink.offsets) and not self.finished.done():
            self.finished.set_exception(ConnectionError(f"triggers ended at {self.next}"))


async def warm(post, burst: int) -> None:
    """Open enough connections for a burst, and run the code paths hot."""
    for _ in range(3):
        await asyncio.gather(*(post() for _ in range(2 * burst)))
    for _ in range(200):
        await post()


async def drive(make_client, server: harness.OrderServer, url: str,
                sink: harness.Recorder, burst: int) -> None:
    async with make_client(server) as post:
        await warm(post, burst)
        loop, tasks = asyncio.get_running_loop(), set()
        finished = loop.create_future()

        async def order(i: int) -> None:
            try:
                await post()
            except Exception as e:  # fail the replay instead of hanging it
                if not finished.done():
                    finished.set_exception(e)
                return
            sink.hit(i)
            if sink.done:
                finished.set_result(None)

        def fire(i: int) -> None:
            task = asyncio.create_task(order(i))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        transport, _ = await loop.create_connection(
            lambda: Triggers(sink, fire, finished), "127.0.0.1", urlparse(url).port)
        try:
            await asyncio.wait_for(finished, sink.offsets[-1] / 1e9 + 60)
        finally:
            transport.close()


def main(n: int = 10_000, speed: float = 1.0, rounds: int | None = None) -> None:
    harness.header("http_clients: order POSTs to a local TLS server", PACKAGES)
    pattern = PATTERN.faster(speed)
    rounds = rounds or harness.default_rounds(len(CLIENTS))
    offsets = harness.arrivals(n // rounds, pattern)
    with harness.OrderServer() as server, harness.Steal() as steal:
        candidates = {name: harness.on_loop("uvloop", lambda url, sink, make=make: drive(
            make, server, url, sink, pattern.burst_size)) for name, make in CLIENTS.items()}
        result = harness.replay(candidates, [b"\x01"] * len(offsets), offsets, rounds,
                                websocket=False)
    print(f"open-loop orders ({pattern}), {rounds} × {n // rounds:,} per client, "
          f"host CPU steal {steal}")
    print(result.table("client", "(trigger lag, per write)"))


if __name__ == "__main__":
    main()
