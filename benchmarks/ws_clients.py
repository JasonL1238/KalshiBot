"""Which event loop and WebSocket client should carry the market-data feed?

Run: python benchmarks/ws_clients.py   (needs the [bench] extra)

A separate process (`harness.Sender`) replays synthetic Kalshi frames to the client over
loopback on an open-loop schedule, bursts in one write. Each candidate hands every text
message to the consumer, with no compression, keepalive pings, or size cap:
- `websockets`, configured and read as the incumbent `kalshi/ws.py` does (str, after
  UTF-8 decoding; `recv(decode=False)` would skip that);
- `aiohttp` `ClientSession.ws_connect` (str: it always decodes text frames);
- `picows`, a Cython client with a callback per frame (bytes, which the JSON decoders
  take directly);
- a bare reader that isolates the loop: an `asyncio.Protocol` that parses the
  replay's unmasked server frames itself, consuming each frame inside `data_received`
  (callback, as picows does) or through an `asyncio.Queue` to a consumer coroutine
  (queue hop, the extra wakeup that websockets and aiohttp put between the socket and
  the caller).
Every candidate runs on the stdlib asyncio loop and on uvloop, in the same rounds.
Latency runs from a frame's scheduled send to the consumer having it.
"""

from __future__ import annotations

import asyncio
import base64
import os
from urllib.parse import urlparse

import aiohttp
import harness
from picows import WSCloseCode, WSListener, WSMsgType, ws_connect
from websockets.asyncio.client import connect

# The incumbent's settings (kalshi/ws.py `connect_feed`), plus no keepalive pings (the
# replay server doesn't answer them) and no proxy from the environment.
WEBSOCKETS_OPTIONS = {"max_size": None, "compression": None, "ping_interval": None,
                      "proxy": None}


async def via_websockets(url: str, sink: harness.Recorder) -> None:
    async with connect(url, **WEBSOCKETS_OPTIONS) as ws:
        async for frame in ws:  # as `kalshi.ws.Connection` reads it: text frames as str
            sink.frame(frame)
            if sink.done:
                break


async def via_aiohttp(url: str, sink: harness.Recorder) -> None:
    async with aiohttp.ClientSession() as session, session.ws_connect(
            url, max_msg_size=0, compress=0, autoping=False) as ws:
        while not sink.done:
            msg = await ws.receive()
            if msg.type is not aiohttp.WSMsgType.TEXT:
                raise ConnectionError(f"aiohttp: unexpected {msg.type}")
            sink.frame(msg.data)


class Listener(WSListener):
    """Hands each text frame's bytes to `on_frame` and closes once `done()`. picows calls
    `on_ws_frame` per frame; Kalshi's aren't fragmented, but a production listener must
    still join continuation frames (`frame.fin`)."""

    def __init__(self, on_frame, done):
        self.on_frame, self.done = on_frame, done

    def on_ws_frame(self, transport, frame) -> None:
        if frame.msg_type is WSMsgType.TEXT:
            self.on_frame(frame.get_payload_as_bytes())
            if self.done():
                transport.send_close(WSCloseCode.OK)
                transport.disconnect()
        elif frame.msg_type is WSMsgType.CLOSE:
            transport.send_close(frame.get_close_code())
            transport.disconnect()


async def via_picows(url: str, sink: harness.Recorder) -> None:
    transport, _ = await ws_connect(lambda: Listener(sink.frame, lambda: sink.done), url)
    await transport.wait_disconnected()


class _Reader(asyncio.Protocol):
    """The bare reader: a WebSocket handshake, then unmasked frames parsed by hand."""

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


async def _read(url: str, on_frame) -> asyncio.Future:
    """Open a bare reader; the returned future resolves when the connection closes."""
    loop = asyncio.get_running_loop()
    closed = loop.create_future()
    u = urlparse(url)
    await loop.create_connection(lambda: _Reader(url, on_frame, closed), u.hostname, u.port)
    return closed


async def via_callback(url: str, sink: harness.Recorder) -> None:
    await (await _read(url, sink.frame))


async def via_queue_hop(url: str, sink: harness.Recorder) -> None:
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    closed = await _read(url, queue.put_nowait)
    closed.add_done_callback(lambda _: queue.put_nowait(None))  # wakes a waiting consumer
    while not sink.done:
        if (frame := await queue.get()) is None:
            raise ConnectionError(f"replay closed after {sink.i} frames")
        sink.frame(frame)
    await closed


CLIENTS = {"websockets": via_websockets, "aiohttp": via_aiohttp, "picows": via_picows,
           "bare reader, callback": via_callback,
           "bare reader, queue hop": via_queue_hop}
PACKAGES = ("websockets", "aiohttp", "picows", "uvloop")


def main(n: int = 40_000, speed: float = 1.0, rounds: int | None = None) -> None:
    harness.header("ws_clients: event loops and WebSocket clients against a local replay",
                   PACKAGES)
    candidates = {f"{name} / {loop}": harness.on_loop(loop, client)
                  for name, client in CLIENTS.items() for loop in ("asyncio", "uvloop")}
    harness.report_ws_replays("client / loop", candidates, n, speed, rounds)


if __name__ == "__main__":
    main()
