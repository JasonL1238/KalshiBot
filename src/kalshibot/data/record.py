"""Record raw Kalshi frames for later replay (books, trades, latency studies).

Every frame is stored with its local receive time (ns since the epoch), in Parquet under
`<out>/date=YYYY-MM-DD/`; a separate writer task does the disk work, so receiving and
timestamping never wait on it. `<out>/coverage.jsonl` logs what loaders need to know
where data is missing; in both logs, an `error` at `write` marks frames lost from its
`first_ns` to its `last_ns`.

- `record` (needs the read-only key): WebSocket frames, verbatim. The log has connect,
  each acknowledged subscription (sid, channel, tickers), gap, disconnect, error, and
  stop. Data is missing between a gap/disconnect/stop and the next subscription; a
  `connect` with no preceding gap/disconnect/stop (the process was killed or the
  machine lost power) ends the earlier run's coverage at its last stored frame. The
  market list is re-read every `refresh` seconds, so markets listed while connected are
  subscribed too.
- `record_snapshots` (keyless, until the WebSocket recorder runs): every `every`
  seconds, the full books of a few series' open markets by REST, one frame per request
  of up to `ORDERBOOKS_MAX` markets, whole events together where they fit: the tickers
  asked for, when the call was made, and the response, from one instant between then
  and the receive time (a span that includes the client's wait for the read budget).
  The log has start (series, every), listed (the market count, first and when it
  changes), error (where, and the first ticker of a failed request), and stop. A frame
  covers its own instant only; after a failed listing, the last list stands, so an
  empty book need not mean an open market.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from kalshibot.kalshi.auth import KalshiSigner
from kalshibot.kalshi.client import ORDERBOOKS_MAX, RETRYABLE_STATUS, KalshiClient
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
        return self.due()

    def due(self) -> list[tuple[int, str]] | None:
        """The batch to write if a flush is due (enough frames, or time), else None."""
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
    """Write batches in order, off the event loop, until it receives None. A batch that
    fails to write is logged (its frames' first and last receive times) and dropped; if
    even that can't be logged, the writer fails, and `_recording` stops the recorder."""
    while (batch := await queue.get()) is not None:
        try:
            await asyncio.to_thread(recorder.write, batch)
        except Exception as exc:
            recorder.log("error", where="write", frames=len(batch), first_ns=batch[0][0],
                         last_ns=batch[-1][0], reason=repr(exc))


@contextlib.asynccontextmanager
async def _recording(out: Path, **options):
    """(the recorder, the queue its batches go to disk through); on the way out, every
    buffered frame reaches disk (or the log says it didn't) and the log says stop. If the
    writer fails, the recorder stops, raising the writer's error."""
    recorder, queue = Recorder(out, **options), asyncio.Queue()
    writer, main = asyncio.create_task(_writer(recorder, queue)), asyncio.current_task()

    def stop_if_failed(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            main.cancel()

    writer.add_done_callback(stop_if_failed)
    try:
        yield recorder, queue
    finally:
        queue.put_nowait(recorder.take())
        queue.put_nowait(None)
        try:
            await asyncio.shield(writer)
        finally:
            recorder.log("stop")


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
    backoff = 1.0
    async with _recording(out, flush_frames=flush_frames) as (recorder, queue):
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


def open_markets(client: KalshiClient, series: list[str] | None) -> list[dict]:
    """The open non-combo markets of `series` (every series when None): what a recorder
    captures."""
    return [m for s in (series or [None])
            for m in client.iter_markets(series_ticker=s, status="open", mve_filter="exclude")]


def _requests(markets: list[dict]) -> list[list[str]]:
    """Tickers packed into requests of at most `ORDERBOOKS_MAX`, keeping each event in one
    request where it fits, so its books come from one instant."""
    events: dict[str, list[str]] = {}
    for m in markets:
        events.setdefault(m["event_ticker"], []).append(m["ticker"])
    requests: list[list[str]] = []
    for tickers in events.values():
        for part in batched(tickers, ORDERBOOKS_MAX, strict=False):
            fit = next((r for r in requests if len(r) + len(part) <= ORDERBOOKS_MAX), None)
            if fit is None:
                requests.append(list(part))
            else:
                fit.extend(part)
    return requests


def _snapshot(client: KalshiClient, tickers: list[str]) -> tuple[int, str]:
    """(receive time, frame) for one request (the module docstring)."""
    sent_ns = time.time_ns()
    response = client.get_orderbooks_payload(tickers)
    return time.time_ns(), json.dumps({"tickers": tickers, "sent_ns": sent_ns,
                                       "response": response}, separators=(",", ":"))


async def record_snapshots(make_client: Callable[[], KalshiClient], series: list[str],
                           out: Path, every: float) -> None:
    """Snapshot books by REST until cancelled (module docstring). Each round lists the
    series' open markets afresh, so a 15-minute market is snapshot from the round after
    it opens. A failed request is logged and skipped, and `make_client` shouldn't retry:
    a late snapshot is worth little, and the next round reads again. A rate limit or
    server error ends the round, so the wait for the next one is the backoff."""
    if not series:
        raise ValueError("record_snapshots needs series: every open market is too many "
                         "requests per round")
    requests: list[list[str]] | None = None
    with make_client() as client:
        async with _recording(out) as (recorder, queue):
            recorder.log("start", series=series, every=every)
            while True:
                started = time.monotonic()
                try:
                    listed = _requests(await asyncio.to_thread(open_markets, client, series))
                    if requests is None or sum(map(len, listed)) != sum(map(len, requests)):
                        recorder.log("listed", markets=sum(map(len, listed)))
                    requests = listed
                except Exception as exc:
                    recorder.log("error", where="listing", reason=repr(exc))
                for tickers in requests or []:
                    try:
                        recv_ns, frame = await asyncio.to_thread(_snapshot, client, tickers)
                    except Exception as exc:
                        recorder.log("error", where="books", first=tickers[0], reason=repr(exc))
                        if (isinstance(exc, httpx.HTTPStatusError)
                                and exc.response.status_code in RETRYABLE_STATUS):
                            break
                        continue
                    if batch := recorder.add(recv_ns, frame):
                        queue.put_nowait(batch)
                if batch := recorder.due():  # an outage must not hold frames in memory
                    queue.put_nowait(batch)
                await asyncio.sleep(max(0.0, every - (time.monotonic() - started)))
