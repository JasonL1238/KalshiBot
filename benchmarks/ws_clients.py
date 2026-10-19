"""Which WebSocket client should carry the market-data feed?

Run: python benchmarks/ws_clients.py   (needs the [bench] extra)

A separate process (`harness.Sender`) replays synthetic Kalshi frames to the client over
loopback on an open-loop schedule, bursts in one write. Each candidate hands every text
message to the consumer as bytes or str, in its fastest documented configuration (no
compression, no keepalive pings, no size cap):
- `websockets` (the incumbent, kalshi/ws.py), `recv(decode=False)`;
- `aiohttp` `ClientSession.ws_connect`;
- `picows`, a Cython client with a callback per frame.
Latency runs from a frame's scheduled send to the consumer having it; each client runs on
the stdlib asyncio loop and on uvloop.
"""

from __future__ import annotations

import aiohttp
import harness
from picows import WSCloseCode, WSListener, WSMsgType, ws_connect
from websockets.asyncio.client import connect


async def via_websockets(url: str, sink: harness.Recorder) -> None:
    async with connect(url, max_size=None, compression=None, ping_interval=None,
                       proxy=None) as ws:
        while not sink.done:
            sink.frame(await ws.recv(decode=False))


async def via_aiohttp(url: str, sink: harness.Recorder) -> None:
    async with aiohttp.ClientSession() as session, session.ws_connect(
            url, max_msg_size=0, compress=0, autoping=False) as ws:
        while not sink.done:
            msg = await ws.receive()
            if msg.type is not aiohttp.WSMsgType.TEXT:
                raise ConnectionError(f"aiohttp: unexpected {msg.type}")
            sink.frame(msg.data)


class _Listener(WSListener):
    """picows calls `on_ws_frame` per frame. Kalshi's frames aren't fragmented; a
    production listener must still join continuation frames (`frame.fin`)."""

    def __init__(self, sink: harness.Recorder):
        self.sink = sink

    def on_ws_frame(self, transport, frame) -> None:
        if frame.msg_type is WSMsgType.TEXT:
            self.sink.frame(frame.get_payload_as_bytes())
            if self.sink.done:
                transport.send_close(WSCloseCode.OK)
                transport.disconnect()
        elif frame.msg_type is WSMsgType.CLOSE:
            transport.send_close(frame.get_close_code())
            transport.disconnect()


async def via_picows(url: str, sink: harness.Recorder) -> None:
    transport, _ = await ws_connect(lambda: _Listener(sink), url)
    await transport.wait_disconnected()


CLIENTS = {"websockets": via_websockets, "aiohttp": via_aiohttp, "picows": via_picows}
LOOPS = ("asyncio", "uvloop")
PATTERNS = {"steady": harness.Pattern(2000),
            "bursty": harness.Pattern(1000, burst_every=0.2, burst_size=200)}
PACKAGES = ("websockets", "aiohttp", "picows", "uvloop")


def candidate(loop: str, client):
    return lambda url, sink: harness.run_on(loop, lambda: client(url, sink))


def main(n: int = 40_000, speed: float = 1.0, rounds: int = 4) -> None:
    harness.header("ws_clients: WebSocket clients against a local replay", PACKAGES)
    stream = harness.Stream(n // rounds)
    candidates = {f"{name} / {loop}": candidate(loop, client)
                  for name, client in CLIENTS.items() for loop in LOOPS}
    for label, pattern in PATTERNS.items():
        pattern = pattern.faster(speed)
        offsets = harness.arrivals(n // rounds, pattern)
        with harness.Steal() as steal:
            lat, lag, cpu = harness.replay(candidates, stream.frames, offsets, rounds)
        print(f"open-loop replay, {label} ({pattern}), {rounds} × {n // rounds:,} frames "
              f"per client, host CPU steal {steal}")
        print(harness.replay_table("client / loop", lat, lag, cpu))
        print()


if __name__ == "__main__":
    main()
