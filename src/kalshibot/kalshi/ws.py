"""Kalshi WebSocket market data: signed connection, subscriptions, sequence checks, books.

The handshake is signed like a REST call (`GET /trade-api/ws/v2`), even for public
channels. Every data message carries `sid` (subscription id) and `seq`; a missing seq
means the books for that subscription can no longer be trusted, so `BookFeed` raises
`SequenceGap` and the caller reconnects for fresh snapshots. Message shapes:
docs/RESEARCH.md §1.
"""

from __future__ import annotations

import itertools
import json
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

from websockets.asyncio.client import connect

from kalshibot.kalshi.auth import KalshiSigner
from kalshibot.kalshi.models import OrderBook, price_units, size_units

WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"
SUBSCRIBE_CHUNK = 500  # tickers per subscribe command


class SequenceGap(RuntimeError):
    def __init__(self, sid: int, expected: int, got: int):
        super().__init__(f"subscription {sid}: expected seq {expected}, got {got}")
        self.sid, self.expected, self.got = sid, expected, got


class BookFeed:
    """Keeps one `OrderBook` per market from snapshot and delta messages."""

    def __init__(self) -> None:
        self.books: dict[str, OrderBook] = {}
        self._next_seq: dict[int, int] = {}

    def on_message(self, msg: dict[str, Any]) -> str | None:
        """Apply one parsed message; return the ticker whose book changed, if any."""
        kind = msg.get("type")
        if kind not in ("orderbook_snapshot", "orderbook_delta"):
            return None
        sid, seq = msg["sid"], msg["seq"]
        expected = self._next_seq.get(sid)
        if expected is not None and seq != expected:
            raise SequenceGap(sid, expected, seq)
        self._next_seq[sid] = seq + 1
        body = msg["msg"]
        ticker = body["market_ticker"]
        if kind == "orderbook_snapshot":
            self.books[ticker] = OrderBook.from_api(ticker, body)
        else:
            book = self.books.get(ticker)
            if book is None:  # a delta before its market's snapshot: the stream is broken
                raise SequenceGap(sid, seq, seq)
            side = book.yes if body["side"] == "yes" else book.no
            side.apply(price_units(body["price_dollars"]), size_units(body["delta_fp"]))
        return ticker


class Connection:
    """One signed WebSocket connection: send subscriptions, iterate frames with their
    receive time (ns since the epoch). `requested` maps each command id to its tickers."""

    def __init__(self, ws) -> None:
        self._ws = ws
        self._ids = itertools.count(1)
        self.requested: dict[int, list[str]] = {}

    async def subscribe(self, channels: list[str], tickers: Iterable[str]) -> None:
        for chunk in itertools.batched(tickers, SUBSCRIBE_CHUNK, strict=False):
            cid = next(self._ids)
            self.requested[cid] = list(chunk)
            await self._ws.send(json.dumps({"id": cid, "cmd": "subscribe", "params": {
                "channels": channels, "market_tickers": list(chunk)}}))

    async def __aiter__(self) -> AsyncIterator[tuple[int, str]]:
        async for frame in self._ws:
            yield time.time_ns(), frame if isinstance(frame, str) else frame.decode()


@asynccontextmanager
async def connect_feed(signer: KalshiSigner, url: str = WS_URL) -> AsyncIterator[Connection]:
    async with connect(url, additional_headers=signer.headers("GET", WS_PATH),
                       max_size=None, compression=None) as ws:
        yield Connection(ws)
