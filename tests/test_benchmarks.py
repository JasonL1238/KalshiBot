"""Keep the benchmark scripts runnable and their measurements honest: candidates must agree
on every workload, the harness must charge queueing to the items queued, and each script
must run end to end at a small scale. Scripts other than book.py need the [bench] extra."""

import importlib
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import book  # noqa: E402
import harness  # noqa: E402

from kalshibot.kalshi.ws import BookFeed  # noqa: E402


def bench(name: str):
    """A benchmark script, or a skip when the [bench] extra is missing."""
    for package in ("msgspec", "orjson", "uvloop", "picows", "aiohttp", "h2"):
        pytest.importorskip(package)
    return importlib.import_module(name)


def test_book_candidates_agree_on_every_workload():
    for make in book.WORKLOADS.values():
        book.check_agreement(make(4_000))


def test_arrivals_keep_rate_and_bursts():
    pattern = harness.Pattern(1000, burst_every=0.1, burst_size=50)
    times = harness.arrivals(20_000, pattern)
    assert times == sorted(times) and len(times) == 20_000
    span = times[-1] / 1e9
    assert 20_000 / span == pytest.approx(1000 + 50 / 0.1, rel=0.05)
    assert times.count(round(0.1 * 1e9)) == 50  # a burst shares one instant


def test_quantiles_use_nearest_rank():
    q = harness.quantiles(list(range(1, 1001)))  # 1..1000 ns
    assert (q["p50"], q["p99"], q["p99.9"], q["max"]) == (0.5, 0.99, 0.999, 1.0)


def test_open_loop_charges_queueing_to_the_items_behind():
    """Coordinated-omission correction: one slow item delays every item due during it,
    and each is charged from its own intended time."""
    def work(slow: bool) -> None:
        end = time.perf_counter_ns() + (5_000_000 if slow else 0)
        while time.perf_counter_ns() < end:
            pass

    offsets = [0] + [1_000_000 * k for k in range(1, 5)]  # 4 items due during the slow one
    lat = harness.open_loop(work, [True, False, False, False, False], offsets)
    assert lat[0] >= 5_000_000
    for k in range(1, 5):  # each waited until 5 ms, so it is charged ~(5 - k) ms
        assert lat[k] >= 5_000_000 - offsets[k]


def test_stream_frames_rebuild_the_generators_books():
    stream = harness.Stream(5_000, markets=10)
    feed = BookFeed()
    for frame in stream.frames:
        feed.on_message(json.loads(frame))
    for m in stream.markets:
        ob = feed.books[m.ticker]
        assert dict(ob.yes.levels()) == m.sides["yes"]
        assert dict(ob.no.levels()) == m.sides["no"]
        assert ob.spread is None or ob.spread > 0
    assert {msg["type"] for msg in stream.messages} == {
        "orderbook_snapshot", "orderbook_delta", "trade", "fill"}


def test_decoders_agree_and_typed_feed_matches_book_feed():
    decode = bench("decode")
    stream = harness.Stream(4_000, markets=20)
    decode.check_agreement(stream.frames + harness.Stream(100, markets=100, seed=2).frames)
    dicts, structs = BookFeed(), decode.StructFeed()
    for frame in stream.frames:
        assert dicts.on_message(json.loads(frame)) == structs.on_message(
            decode.typed_decoder.decode(frame))
    for ticker, ob in dicts.books.items():
        assert ob.yes.levels() == structs.books[ticker].yes.levels()
        assert ob.no.levels() == structs.books[ticker].no.levels()


def test_signatures_verify():
    bench("sign").check_signatures()


SMALL = {  # script -> main() arguments for a run of a few seconds
    "decode": {"n": 2_000, "speed": 20},
    "sign": {"n": 400, "speed": 10},
    "loop": {"n": 800, "speed": 10, "rounds": 2},
    "ws_clients": {"n": 800, "speed": 10, "rounds": 2},
    "http_clients": {"n": 200, "speed": 1, "rounds": 2},
    "gc_pauses": {"n": 800, "speed": 10, "heaps": (0, 10_000), "rounds": 2},
    "hot_path": {"n": 2_000, "speed": 5, "rounds": 2},
}


@pytest.mark.parametrize("script", SMALL)
def test_script_runs_end_to_end(script, capsys):
    bench(script).main(**SMALL[script])
    out = capsys.readouterr().out
    assert out.startswith(f"{script}") and "platform: " in out and "python: " in out
    assert "p99.9" in out
