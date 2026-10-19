"""Which event loop should the hot path run on: stdlib asyncio or uvloop?

Run: python benchmarks/loop.py   (needs the [bench] extra)

This isolates the loop. A minimal WebSocket reader (an `asyncio.Protocol` that parses
the replay's unmasked server frames itself) consumes a `harness.Sender` replay of
synthetic Kalshi frames over loopback, two ways:
- callback: each frame is consumed inside `data_received`, as picows does;
- queue hop: frames go through an `asyncio.Queue` to a consumer coroutine, the extra
  wakeup that websockets and aiohttp put between the socket and the caller.
ws_clients.py measures the client libraries on both loops.
"""

from __future__ import annotations

import asyncio
import base64
import os
from urllib.parse import urlparse

import harness


class _Reader(asyncio.Protocol):
    def __init__(self, url: str, on_frame, closed: asyncio.Future):
        self.url, self.on_frame, self.closed = urlparse(url), on_frame, closed
        self.buf, self.open = bytearray(), False

    def connection_made(self, transport) -> None:
        self.transport = transport
        key = base64.b64encode(os.urandom(16)).decode()
        transport.write((f"GET {self.url.path} HTTP/1.1\r\nHost: {self.url.netloc}\r\n"
                         "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                         f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
                        .encode())

    def data_received(self, data: bytes) -> None:
        buf = self.buf
        buf += data
        pos = 0
        if not self.open:
            end = buf.find(b"\r\n\r\n")
            if end < 0:
                return
            self.open, pos = True, end + 4
        while len(buf) - pos >= 2:
            opcode, length, head = buf[pos] & 0x0F, buf[pos + 1] & 0x7F, 2
            if length >= 126:
                head = 4 if length == 126 else 10
                if len(buf) - pos < head:
                    break
                length = int.from_bytes(buf[pos + 2:pos + head])
            if len(buf) - pos < head + length:
                break
            payload = bytes(buf[pos + head:pos + head + length])
            pos += head + length
            if opcode == 0x1:
                self.on_frame(payload)
            elif opcode == 0x8:
                self.transport.close()
        del buf[:pos]

    def connection_lost(self, exc) -> None:
        if not self.closed.done():
            self.closed.set_result(None)


async def _connect(url: str, on_frame) -> asyncio.Future:
    """Open the reader; the returned future resolves when the connection closes."""
    loop = asyncio.get_running_loop()
    closed = loop.create_future()
    u = urlparse(url)
    await loop.create_connection(lambda: _Reader(url, on_frame, closed), u.hostname, u.port)
    return closed


async def callback(url: str, sink: harness.Recorder) -> None:
    await (await _connect(url, sink.frame))


async def queue_hop(url: str, sink: harness.Recorder) -> None:
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    closed = await _connect(url, queue.put_nowait)
    closed.add_done_callback(lambda _: queue.put_nowait(None))  # wakes a waiting consumer
    while not sink.done:
        if (frame := await queue.get()) is None:
            raise ConnectionError(f"replay closed after {sink.i} frames")
        sink.frame(frame)
    await closed


CONSUMERS = {"callback": callback, "queue hop": queue_hop}


def main(n: int = 40_000, speed: float = 1.0, rounds: int | None = None) -> None:
    harness.header("loop: asyncio vs uvloop under a local WebSocket replay", ("uvloop",))
    candidates = {f"{loop} / {name}": harness.on_loop(loop, consumer)
                  for loop in ("asyncio", "uvloop") for name, consumer in CONSUMERS.items()}
    harness.report_ws_replays("loop / consumer", candidates, n, speed, rounds)


if __name__ == "__main__":
    main()
