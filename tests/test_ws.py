"""WebSocket feed and the recorders: WebSocket frames against a local mock of Kalshi's WS
server, REST snapshots against a mock transport."""

import asyncio
import json

import httpx
import pyarrow.parquet as pq
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from typer.testing import CliRunner
from websockets.asyncio.server import serve

from kalshibot import cli
from kalshibot.data import record as record_module
from kalshibot.data.record import Recorder, record, record_snapshots
from kalshibot.kalshi.auth import KalshiSigner
from kalshibot.kalshi.ws import BookFeed, Connection, SequenceGap


def snapshot(sid, seq, ticker="T"):
    return {"type": "orderbook_snapshot", "sid": sid, "seq": seq,
            "msg": {"market_ticker": ticker, "yes_dollars_fp": [["0.4000", "10.00"]],
                    "no_dollars_fp": [["0.5500", "5.00"]]}}


def delta(sid, seq, price, amount, side="yes", ticker="T"):
    return {"type": "orderbook_delta", "sid": sid, "seq": seq,
            "msg": {"market_ticker": ticker, "price_dollars": price, "delta_fp": amount,
                    "side": side, "ts_ms": 1}}


def test_feed_applies_snapshots_and_deltas():
    feed = BookFeed()
    feed.on_message(snapshot(1, 1))
    feed.on_message(delta(1, 2, "0.4100", "3.00"))
    feed.on_message(delta(1, 3, "0.4000", "-10.00"))
    book = feed.books["T"]
    assert (book.best_yes_bid, book.yes_bid_size, book.best_yes_ask) == (4100, 300, 4500)
    assert feed.on_message({"type": "subscribed", "id": 1, "msg": {"sid": 1}}) is None


@pytest.mark.parametrize("messages", [
    [snapshot(1, 1), delta(1, 3, "0.4100", "1.00")],  # seq 2 missing
    [delta(1, 1, "0.4100", "1.00")],  # delta before any snapshot
])
def test_feed_raises_on_broken_streams(messages):
    feed = BookFeed()
    with pytest.raises(SequenceGap):
        for m in messages:
            feed.on_message(m)


def test_subscriptions_are_chunked():
    sent = []

    class FakeWS:
        async def send(self, text):
            sent.append(json.loads(text))

    conn = Connection(FakeWS())
    asyncio.run(conn.subscribe(["orderbook_delta"], [f"T{i}" for i in range(1201)]))
    assert [len(c["params"]["market_tickers"]) for c in sent] == [500, 500, 201]
    assert [c["id"] for c in sent] == [1, 2, 3] and conn.requested[3][0] == "T1000"


def test_recorder_batches_and_writes_atomically(tmp_path):
    rec = Recorder(tmp_path, flush_frames=2)
    assert rec.add(1_791_000_000_000_000_000, "a") is None
    batch = rec.add(1_791_000_000_100_000_000, "b")
    path = rec.write(batch)
    assert pq.read_table(path).to_pylist()[1] == {"recv_ns": 1_791_000_000_100_000_000,
                                                   "frame": "b"}
    assert rec.buffer == [] and not list(tmp_path.rglob("*.tmp"))
    assert rec.add(1_791_000_000_200_000_000, "c") is None
    rec.last_flush -= rec.flush_seconds  # a minute later, the batch is due with no new frame
    assert rec.due() == [(1_791_000_000_200_000_000, "c")]


class MockKalshi:
    """Answers subscribes with acks, then plays a scripted stream."""

    def __init__(self, script):
        self.script, self.subscribes, self.keys = script, [], []

    async def __call__(self, ws):
        self.keys.append(ws.request.headers.get("KALSHI-ACCESS-KEY"))
        async for raw in ws:
            cmd = json.loads(raw)
            self.subscribes.append(cmd["params"]["market_tickers"])
            await ws.send(json.dumps({"id": cmd["id"], "type": "subscribed",
                                      "msg": {"channel": "orderbook_delta", "sid": 7}}))
            if len(self.subscribes) == 1:
                for m in self.script:
                    await ws.send(json.dumps(m))


async def drive(job, until, tmp_path):
    """Run the recorder coroutine `job` until `until()` holds, then cancel it. Cancelling
    drops every frame the recorder has not yet handled, so `until` must test the
    recorder's side (its log, its frames), never the server's or the clock's, and must
    imply everything the test then asserts."""
    task = asyncio.create_task(job)
    try:
        async with asyncio.timeout(30):
            while not until():
                if task.done():
                    task.result()  # a recorder only ends early by raising; surface it
                await asyncio.sleep(0.02)
    except TimeoutError:
        pytest.fail(f"`until` never held; coverage log: {coverage(tmp_path)}")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def run_recorder(tmp_path, server, tickers, until, refresh=60.0, flush_frames=100_000):
    """Run `record` against `server` until `until()` holds (`drive`), and return the
    coverage events."""
    async def main():
        async with serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            signer = KalshiSigner("key-1", ed25519.Ed25519PrivateKey.generate())
            await drive(record(signer, tickers, tmp_path, ["orderbook_delta"],
                               url=f"ws://127.0.0.1:{port}", refresh=refresh,
                               flush_frames=flush_frames), until, tmp_path)

    asyncio.run(main())
    return coverage(tmp_path)


@pytest.fixture
def buffered(monkeypatch):
    """Every frame a recorder has buffered, in order."""
    original, frames_seen = Recorder.add, []

    def spying_add(self, recv_ns, frame):
        frames_seen.append(frame)
        return original(self, recv_ns, frame)

    monkeypatch.setattr(Recorder, "add", spying_add)
    return frames_seen


def coverage(tmp_path):
    log = tmp_path / "coverage.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def frames(tmp_path):
    return [r["frame"] for f in sorted(tmp_path.rglob("*.parquet"))
            for r in pq.read_table(f).to_pylist()]


def test_record_logs_a_gap_keeps_every_frame_and_stops_cleanly(tmp_path):
    server = MockKalshi([snapshot(7, 1), delta(7, 2, "0.4100", "1.00"),
                         delta(7, 4, "0.4200", "1.00")])

    def gapped():
        return "gap" in [e["event"] for e in coverage(tmp_path)]

    events = run_recorder(tmp_path, server, lambda: ["T"], gapped)
    kinds = [e["event"] for e in events]
    assert kinds[:3] == ["connect", "subscribed", "gap"] and kinds[-1] == "stop"
    assert events[1]["tickers"] == ["T"] and events[2]["expected"] == 3
    assert len(frames(tmp_path)) >= 4 and server.keys[0] == "key-1"


def test_markets_listed_while_connected_get_subscribed(tmp_path):
    server = MockKalshi([snapshot(7, 1)])
    universe = [["T"], ["T", "NEW"]]
    calls = []

    def tickers():
        calls.append(1)
        return universe[min(len(calls) - 1, 1)]

    def subscribed():
        return [e["tickers"] for e in coverage(tmp_path) if e["event"] == "subscribed"]

    run_recorder(tmp_path, server, tickers, lambda: ["NEW"] in subscribed(), refresh=0.05)
    assert server.subscribes == subscribed() == [["T"], ["NEW"]]


def test_cancel_writes_buffered_frames_and_logs_stop(tmp_path, buffered):
    server = MockKalshi([snapshot(7, 1), delta(7, 2, "0.4100", "1.00")])
    events = run_recorder(tmp_path, server, lambda: ["T"], lambda: len(buffered) >= 3)
    assert events[-1]["event"] == "stop"
    assert frames(tmp_path) == buffered  # ack + snapshot + delta, all buffered at cancel


def test_slow_disk_writes_do_not_stall_receive_timestamps(tmp_path, monkeypatch):
    import time as _time

    original = record_module.Recorder.write

    def slow_write(self, batch):
        _time.sleep(0.4)
        return original(self, batch)

    monkeypatch.setattr(record_module.Recorder, "write", slow_write)

    class Steady(MockKalshi):
        async def __call__(self, ws):
            await ws.recv()
            await ws.send(json.dumps(snapshot(7, 1)))
            for seq in range(2, 80):
                await ws.send(json.dumps(delta(7, seq, "0.4100", "1.00")))
                await asyncio.sleep(0.01)
            await ws.wait_closed()  # closing would make the recorder reconnect and replay

    server = Steady([])

    def written():
        return len(frames(tmp_path)) >= 70

    run_recorder(tmp_path, server, lambda: ["T"], written, flush_frames=10)
    stamps = [r["recv_ns"] for f in sorted(tmp_path.rglob("*.parquet"))
              for r in pq.read_table(f).to_pylist()]
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert max(gaps) < 200_000_000  # < 0.2 s, though each write takes 0.4 s


def test_requests_keep_each_event_together_where_it_fits():
    markets = ([{"event_ticker": "E", "ticker": f"E{i}"} for i in range(150)]
               + [{"event_ticker": "F", "ticker": f"F{i}"} for i in range(60)]
               + [{"event_ticker": "G", "ticker": "G0"}])
    sizes = [len(r) for r in record_module._requests(markets)]
    assert sizes == [100, 51, 60]  # E splits, F gets its own request, G fills E's tail
    assert record_module._requests(markets)[1][-1] == "G0"


def test_rest_snapshots_skip_a_bad_request_and_end_the_round_on_a_rate_limit(
        tmp_path, buffered, mock_client):
    listings, asked = [], []
    script = {1: 400, 3: 503}  # the nth books request fails so

    def handler(request):
        if request.url.path.endswith("/markets"):
            listings.append(1)
            if len(listings) == 2:  # the second listing fails: the first list stands
                return httpx.Response(500)
            return httpx.Response(200, json={"cursor": "", "markets": [
                {"event_ticker": e, "ticker": f"{e}{i}"}
                for e, n in (("A", 60), ("B", 60), ("C", 30)) for i in range(n)]})
        tickers = request.url.params.get_list("tickers")
        asked.append(tickers[0])
        if len(asked) in script:
            return httpx.Response(script[len(asked)])
        return httpx.Response(200, json={"orderbooks": [{"ticker": t} for t in tickers]})

    def snapshots():
        return [json.loads(f) for f in buffered]

    asyncio.run(drive(record_snapshots(lambda: mock_client(handler, max_retries=0), ["S"],
                                       tmp_path, every=0.01),
                      lambda: len(snapshots()) >= 3, tmp_path))
    events = coverage(tmp_path)
    assert events[0] == {"t": events[0]["t"], "event": "start", "series": ["S"], "every": 0.01}
    assert events[-1]["event"] == "stop"
    assert [e["markets"] for e in events if e["event"] == "listed"] == [150]
    assert [(e["where"], e.get("first")) for e in events if e["event"] == "error"][:3] == [
        ("books", "A0"), ("listing", None), ("books", "A0")]
    # round 1 skips the bad request; round 2 (on round 1's list) stops at the 503
    assert asked[:5] == ["A0", "B0", "A0", "A0", "B0"]
    assert [len(f["tickers"]) for f in snapshots()[:3]] == [60, 90, 60]  # A and C together
    assert frames(tmp_path) == buffered


def test_a_failed_write_is_logged_and_recording_goes_on(tmp_path, monkeypatch):
    original, calls = Recorder.write, []

    def flaky_write(self, batch):
        calls.append(len(batch))
        if len(calls) == 1:
            raise OSError("disk hiccup")
        return original(self, batch)

    monkeypatch.setattr(Recorder, "write", flaky_write)
    server = MockKalshi([snapshot(7, 1)] + [delta(7, s, "0.4100", "1.00") for s in range(2, 6)])
    events = run_recorder(tmp_path, server, lambda: ["T"], lambda: len(frames(tmp_path)) >= 3,
                          flush_frames=1)
    lost = [e for e in events if e["event"] == "error" and e["where"] == "write"]
    assert len(lost) == 1 and lost[0]["frames"] == 1 and lost[0]["first_ns"] == lost[0]["last_ns"]
    assert events[-1]["event"] == "stop"


def test_a_write_that_cannot_even_be_logged_stops_the_recorder(tmp_path, monkeypatch):
    def broken_write(self, batch):
        raise OSError("disk gone")

    def log_unless_write(self, event, **detail):
        if detail.get("where") == "write":
            raise OSError("disk gone")
        original_log(self, event, **detail)

    original_log = Recorder.log
    monkeypatch.setattr(Recorder, "write", broken_write)
    monkeypatch.setattr(Recorder, "log", log_unless_write)
    server = MockKalshi([snapshot(7, 1), delta(7, 2, "0.4100", "1.00")])

    async def main():
        async with serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            signer = KalshiSigner("key-1", ed25519.Ed25519PrivateKey.generate())
            async with asyncio.timeout(30):
                await record(signer, lambda: ["T"], tmp_path, ["orderbook_delta"],
                             url=f"ws://127.0.0.1:{port}", flush_frames=1)

    with pytest.raises(OSError, match="disk gone"):
        asyncio.run(main())
    assert coverage(tmp_path)[-1]["event"] == "stop"


def test_rest_recording_runs_without_retries(monkeypatch):
    seen = []

    async def fake(make_client, series, out, every):
        seen.append((make_client()._max_retries, series, every))

    monkeypatch.setattr(cli, "record_snapshots", fake)
    monkeypatch.setattr(cli, "_read_only_signer", lambda: pytest.fail("--rest loaded the key"))
    out = CliRunner().invoke(cli.app, ["record", "--rest", "--series", "S", "--every", "5"])
    assert out.exit_code == 0 and seen == [(0, ["S"], 5.0)]


def test_rest_recording_needs_series(monkeypatch):
    monkeypatch.setattr(cli, "record_snapshots", lambda *a: pytest.fail("recorded"))
    out = CliRunner().invoke(cli.app, ["record", "--rest"])
    assert out.exit_code != 0 and "--series" in out.output
