"""Keep the benchmark scripts runnable and their measurements honest: the harness must
charge queueing to the items queued, decoders and feeds must agree, every replayed frame
must arrive intact, and each script must run end to end at a small scale. Scripts that
import a [bench] package skip without it."""

import faulthandler
import importlib
import json
import sys
import time
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))

import book  # noqa: E402
import harness  # noqa: E402

from kalshibot.kalshi.ws import BookFeed  # noqa: E402

# The [bench] extra's packages; each imports under its distribution's name.
BENCH = {Requirement(r).name for r in tomllib.loads((ROOT / "pyproject.toml").read_text())[
    "project"]["optional-dependencies"]["bench"]}


def bench(name: str):
    """A benchmark script, or a skip when it needs a missing [bench] package."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as e:
        if e.name.split(".")[0] not in BENCH:
            raise
        pytest.skip(f"{name} needs {e.name} (the [bench] extra)")


def test_book_candidates_agree_on_every_workload():
    for make in book.WORKLOADS.values():
        book.check_agreement(make(4_000))


def test_arrivals_keep_rate_and_bursts():
    pattern = harness.Pattern(1000, burst_every=0.1, burst_size=50)
    times = harness.arrivals(20_000, pattern)
    assert times == sorted(times) and len(times) == 20_000
    assert 20_000 / (times[-1] / 1e9) == pytest.approx(pattern.mean_rate, rel=0.05)
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
    """Every frame applies (no negative size, no seq gap), no book ever crosses, and the
    books end as the generator left them."""
    stream = harness.Stream(5_000, markets=10)
    feed = BookFeed()
    for frame in stream.frames:
        if (ticker := feed.on_message(json.loads(frame))) is not None:
            spread = feed.books[ticker].spread
            assert spread is None or spread > 0
    for m in stream.markets:
        ob = feed.books[m.ticker]
        assert dict(ob.yes.levels()) == m.sides["yes"]
        assert dict(ob.no.levels()) == m.sides["no"]
    assert len(stream.frames) == 5_000
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


SMALL = {  # script -> main() arguments for a run of a few seconds
    "decode": {"n": 2_000, "speed": 20},
    "sign": {"n": 400, "speed": 10},
    "ws_clients": {"n": 1_200, "speed": 10},
    "http_clients": {"n": 300, "speed": 1},
    "gc_pauses": {"n": 800, "speed": 10, "heaps": (0, 10_000)},
    "hot_path": {"n": 4_000, "speed": 5, "reconnect_markets": 100},
}


@pytest.mark.parametrize("script", SMALL)
def test_script_runs_end_to_end(script, capsys, monkeypatch):
    module = bench(script)
    monkeypatch.setattr(harness, "VERIFY", True)  # replays check each frame's payload
    # a hung replay fails, with stacks (pytest captures sys.stderr, which has no fileno)
    faulthandler.dump_traceback_later(300, exit=True, file=sys.__stderr__)
    try:
        module.main(**SMALL[script])
    finally:
        faulthandler.cancel_dump_traceback_later()
    out = capsys.readouterr().out
    assert out.startswith(f"{script}") and "platform: " in out and "python: " in out
    assert "mean µs" in out and "p99.9" in out
