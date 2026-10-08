"""Record raw Kalshi WebSocket frames for later replay (books, trades, latency studies).

Every frame is stored verbatim with its local receive time (ns since the epoch), in
Parquet under `raw/ws/date=YYYY-MM-DD/`; a separate writer task does the disk work, so
receiving and timestamping never wait on it. `raw/ws/coverage.jsonl` logs connect, each
acknowledged subscription (sid, channel, tickers), gap, disconnect, error, and stop.

Coverage rule for loaders: data is missing between a gap/disconnect/stop and the next
subscription. A `connect` with no preceding gap/disconnect/stop (the process was killed
or the machine lost power) ends the earlier run's coverage at its last stored frame. The
market list is re-read every `refresh` seconds, so markets listed while connected are
subscribed too.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from kalshibot.kalshi.auth import KalshiSigner
from kalshibot.kalshi.ws import WS_URL, BookFeed, Connection, SequenceGap, connect_feed

FRAME_SCHEMA = pa.schema([("recv_ns", pa.int64()), ("frame", pa.string())])


class Recorder:
    def __init__(self, out: Path, flush_frames: int = 100_000, flush_seconds: float = 60):
        self.out, self.flush_frames, self.flush_seconds = out, flush_frames, flush_seconds
        self.buffer: list[tuple[int, str]] = []
        self.last_flush = time.monotonic()

    def log(self, event: str, **detail) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        with (self.out / "coverage.jsonl").open("a") as f:
            f.write(json.dumps({"t": time.time_ns(), "event": event, **detail}) + "\n")

    def add(self, recv_ns: int, frame: str) -> list[tuple[int, str]] | None:
        """Buffer a frame; when a flush is due, hand back the batch to write."""
        self.buffer.append((recv_ns, frame))
        if (len(self.buffer) >= self.flush_frames
                or time.monotonic() - self.last_flush >= self.flush_seconds):
            return self.take()
        return None

    def take(self) -> list[tuple[int, str]]:
        batch, self.buffer, self.last_flush = self.buffer, [], time.monotonic()
        return batch

    def write(self, batch: list[tuple[int, str]]) -> Path | None:
        if not batch:
            return None
        first = datetime.fromtimestamp(batch[0][0] / 1e9, UTC)
        folder = self.out / f"date={first:%Y-%m-%d}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{first:%H%M%S}-{batch[0][0]}.parquet"
        recv, frame = zip(*batch, strict=True)
        table = pa.table({"recv_ns": recv, "frame": frame}, schema=FRAME_SCHEMA)
        pq.write_table(table, path.with_suffix(".tmp"), compression="zstd")
        path.with_suffix(".tmp").replace(path)
        return path

    def flush(self) -> Path | None:
        return self.write(self.take())


async def _refresh(conn: Connection, tickers: Callable[[], list[str]], channels: list[str],
                   subscribed: set[str], every: float, recorder: Recorder) -> None:
    while True:
        await asyncio.sleep(every)
        try:
            new = sorted(set(await asyncio.to_thread(tickers)) - subscribed)
        except Exception as exc:  # a failed listing must not stop recording
            recorder.log("error", where="refresh", reason=repr(exc))
            continue
        if new:
            subscribed.update(new)
            await conn.subscribe(channels, new)


async def _writer(recorder: Recorder, queue: asyncio.Queue) -> None:
    """Write batches in order, off the event loop, until it receives None."""
    while (batch := await queue.get()) is not None:
        await asyncio.to_thread(recorder.write, batch)


async def _session(signer: KalshiSigner, tickers: Callable[[], list[str]], recorder: Recorder,
                   channels: list[str], url: str, refresh: float, queue: asyncio.Queue) -> None:
    universe = await asyncio.to_thread(tickers)
    async with connect_feed(signer, url) as conn:
        recorder.log("connect", channels=channels, requested=len(universe))
        await conn.subscribe(channels, universe)
        refresher = asyncio.create_task(
            _refresh(conn, tickers, channels, set(universe), refresh, recorder))
        feed = BookFeed()
        try:
            async for recv_ns, frame in conn:
                if batch := recorder.add(recv_ns, frame):
                    queue.put_nowait(batch)
                msg = json.loads(frame)
                if msg.get("type") == "subscribed":
                    recorder.log("subscribed", sid=msg["msg"]["sid"],
                                 channel=msg["msg"].get("channel"),
                                 tickers=conn.requested.get(msg.get("id"), []))
                elif msg.get("type") == "error":
                    recorder.log("error", where="server", msg=msg.get("msg"))
                feed.on_message(msg)
        finally:
            refresher.cancel()
    recorder.log("disconnect", reason="closed by server")


async def record(signer: KalshiSigner, tickers: Callable[[], list[str]], out: Path,
                 channels: list[str], url: str = WS_URL, refresh: float = 300,
                 max_backoff: float = 60, flush_frames: int = 100_000) -> None:
    """Record until cancelled, reconnecting with backoff. Any failure inside a session
    (dropped socket, sequence gap, malformed message) is logged and ends that session."""
    recorder, backoff, queue = Recorder(out, flush_frames), 1.0, asyncio.Queue()
    writer = asyncio.create_task(_writer(recorder, queue))
    try:
        while True:
            started = time.monotonic()
            try:
                await _session(signer, tickers, recorder, channels, url, refresh, queue)
            except SequenceGap as gap:
                recorder.log("gap", sid=gap.sid, expected=gap.expected, got=gap.got)
            except Exception as exc:
                recorder.log("disconnect", reason=repr(exc))
            queue.put_nowait(recorder.take())
            backoff = 1.0 if time.monotonic() - started > 60 else min(backoff * 2, max_backoff)
            await asyncio.sleep(backoff)
    finally:
        queue.put_nowait(recorder.take())
        queue.put_nowait(None)
        with contextlib.suppress(Exception):
            await asyncio.shield(writer)  # every queued batch reaches disk
        recorder.log("stop")
