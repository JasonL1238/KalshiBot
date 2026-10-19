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
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import aiohttp
import harness
import httpx

PATTERN = harness.Pattern(200, burst_every=0.25, burst_size=10)
BODY = json.dumps({"ticker": "KXSYN000-26OCT20-B50.5", "side": "bid", "count": "1.00",
                   "price": "0.5600", "time_in_force": "immediate_or_cancel",
                   "self_trade_prevention_type": "taker_at_cross",
                   "client_order_id": "6f1c0a52-0f43-4f53-9a4d-6d0c3e0e2b7a"}).encode()
HEADERS = {"Content-Type": "application/json", "KALSHI-ACCESS-KEY": "bench",
           "KALSHI-ACCESS-TIMESTAMP": "1792000000000", "KALSHI-ACCESS-SIGNATURE": "x" * 88}
POOL = 64


@asynccontextmanager
async def httpx_client(server: harness.OrderServer, http2: bool):
    limits = httpx.Limits(max_connections=POOL, max_keepalive_connections=POOL,
                          keepalive_expiry=None)
    async with httpx.AsyncClient(http1=not http2, http2=http2, verify=server.ssl_context(),
                                 trust_env=False, limits=limits) as client:
        async def post() -> bytes:
            r = await client.post(server.url, content=BODY, headers=HEADERS)
            assert r.status_code == 201 and r.http_version == ("HTTP/2" if http2 else
                                                               "HTTP/1.1"), r
            return r.content
        yield post


@asynccontextmanager
async def aiohttp_client(server: harness.OrderServer):
    connector = aiohttp.TCPConnector(ssl=server.ssl_context(), limit=POOL,
                                     keepalive_timeout=600)
    async with aiohttp.ClientSession(connector=connector) as session:
        async def post() -> bytes:
            async with session.post(server.url, data=BODY, headers=HEADERS) as r:
                assert r.status == 201, r
                return await r.read()
        yield post


CLIENTS = {"httpx HTTP/1.1": lambda s: httpx_client(s, http2=False),
           "httpx HTTP/2": lambda s: httpx_client(s, http2=True),
           "aiohttp HTTP/1.1": aiohttp_client}
PACKAGES = ("httpx", "h2", "aiohttp", "uvloop")


class Triggers(asyncio.Protocol):
    """Reads the Sender's raw stream: 8 bytes of t0, then one byte per order."""

    def __init__(self, sink: harness.Recorder, fire):
        self.sink, self.fire, self.buf, self.next = sink, fire, b"", 0

    def data_received(self, data: bytes) -> None:
        if not self.sink.t0:
            self.buf += data
            if len(self.buf) < 8:
                return
            self.sink.t0 = struct.unpack("!q", self.buf[:8])[0]
            data = self.buf[8:]
        for _ in data:
            self.fire(self.next)
            self.next += 1


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
            await post()
            sink.hit(i)
            if sink.done:
                finished.set_result(None)

        def fire(i: int) -> None:
            task = asyncio.create_task(order(i))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        u = urlparse(url)
        start = time.process_time_ns()
        transport, _ = await loop.create_connection(lambda: Triggers(sink, fire),
                                                    u.hostname, u.port)
        await finished
        sink.cpu_ns = time.process_time_ns() - start
        transport.close()


def candidate(make_client, server: harness.OrderServer, burst: int):
    return lambda url, sink: harness.run_on(
        "uvloop", lambda: drive(make_client, server, url, sink, burst))


def main(n: int = 6_000, speed: float = 1.0, rounds: int = 4) -> None:
    harness.header("http_clients: order POSTs to a local TLS server", PACKAGES)
    pattern = PATTERN.faster(speed)
    offsets = harness.arrivals(n // rounds, pattern)
    with harness.OrderServer() as server, harness.Steal() as steal:
        candidates = {name: candidate(make, server, pattern.burst_size)
                      for name, make in CLIENTS.items()}
        lat, lag, cpu = harness.replay(candidates, [b"\x01"] * len(offsets), offsets, rounds,
                                       websocket=False)
    print(f"open-loop orders ({pattern}), {rounds} × {n // rounds:,} per client, "
          f"host CPU steal {steal}")
    print(harness.replay_table("client", lat, lag, cpu, "(trigger lag)"))


if __name__ == "__main__":
    main()
