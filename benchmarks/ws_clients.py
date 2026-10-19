"""Which WebSocket client should carry the market-data feed?

Run: python benchmarks/ws_clients.py   (needs the [bench] extra)

A separate process (`harness.Sender`) replays synthetic Kalshi frames to the client over
loopback on an open-loop schedule, bursts in one write. Each candidate hands every text
message to the consumer, with no compression, keepalive pings, or size cap:
- `websockets`, configured and read as the incumbent `kalshi/ws.py` does (str, after
  UTF-8 decoding; `recv(decode=False)` would skip that);
- `aiohttp` `ClientSession.ws_connect` (str: it always decodes text frames);
- `picows`, a Cython client with a callback per frame (bytes, which the JSON decoders
  take directly).
Latency runs from a frame's scheduled send to the consumer having it; each client runs on
the stdlib asyncio loop and on uvloop.
"""

from __future__ import annotations

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


CLIENTS = {"websockets": via_websockets, "aiohttp": via_aiohttp, "picows": via_picows}
PACKAGES = ("websockets", "aiohttp", "picows", "uvloop")


def main(n: int = 40_000, speed: float = 1.0, rounds: int | None = None) -> None:
    harness.header("ws_clients: WebSocket clients against a local replay", PACKAGES)
    candidates = {f"{name} / {loop}": harness.on_loop(loop, client)
                  for name, client in CLIENTS.items() for loop in ("asyncio", "uvloop")}
    harness.report_ws_replays("client / loop", candidates, n, speed, rounds)


if __name__ == "__main__":
    main()
