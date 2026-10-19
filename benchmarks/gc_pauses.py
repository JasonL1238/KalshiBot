"""Should the hot path freeze or disable CPython's cyclic garbage collector?

Run: python benchmarks/gc_pauses.py   (needs kalshibot's core imports: cryptography,
httpx, websockets; adds the typed path if msgspec is installed. On a build without
wheels for the rest, e.g. free-threaded: PYTHONPATH=src:benchmarks)

1. What one full collection costs with a heap of long-lived containers standing in for
   what a trading process holds (market metadata, positions, history), and once
   `gc.freeze()` has moved that heap out of every collection. CPython collects in full
   once the long-lived containers have grown by a quarter since the last full
   collection, so the script also estimates how often that comes at the replay's rate.
2. The feed's per-message work (decode plus book update), replayed open loop with bursts
   in this process, under each setting:
   - default: generational collection at the interpreter's thresholds;
   - freeze: `gc.freeze()` after setup;
   - disable: `gc.disable()` for the replay (the feed makes no cycles, so nothing leaks
     that a `gc.collect()` in a quiet moment wouldn't free).
   The paths: stdlib `json` into dicts and `kalshi.ws.BookFeed`, keeping nothing or
   keeping every message (a session tape held in memory, the worst case for the
   collector); and `decode.Message` structs (gc=False: never tracked) into
   `decode.StructFeed`, keeping every message. Each replay starts right after a full
   collection, so with a large heap no full collection fits inside one: part 1 prices
   that risk. Settings run in interleaved rounds; gc.callbacks time each pause.
"""

from __future__ import annotations

import gc
import json
import statistics
import sysconfig
import time
from array import array

import harness

from kalshibot.kalshi.ws import BookFeed

try:
    import decode
except ImportError:  # no msgspec: the dict paths alone
    decode = None

PATTERN = harness.PATTERNS["bursty"]
SETTINGS = ("(harness floor)", "default", "freeze", "disable")
FREE_THREADED = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


def paths() -> dict[str, tuple]:
    """name -> (make a fresh feed, decode a frame, keep each message?)."""
    out = {"json + BookFeed, keeps nothing": (BookFeed, json.loads, False),
           "json + BookFeed, keeps every message": (BookFeed, json.loads, True)}
    if decode is not None:
        out["msgspec structs + StructFeed, keeps every message"] = (
            decode.StructFeed, decode.typed_decoder.decode, True)
    return out


def long_lived_heap(n: int) -> list[dict]:
    """`n` dicts holding a list and a dict each: 3n GC-tracked containers."""
    return [{"ticker": f"KXSYN{i:07d}", "fields": [i, str(i)], "meta": {"i": i}}
            for i in range(n)]


class Pauses:
    """Collector pauses seen through gc.callbacks while installed. The free-threaded
    collector isn't generational: it reports generation 0, but every pass is full."""

    def __init__(self) -> None:
        self.ns, self.full, self._start = array("q"), 0, 0

    def __call__(self, phase: str, info: dict) -> None:
        if phase == "start":
            self._start = time.perf_counter_ns()
        else:
            self.ns.append(time.perf_counter_ns() - self._start)
            self.full += FREE_THREADED or info["generation"] == 2

    def __str__(self) -> str:
        if not self.ns:
            return "none"
        return (f"{len(self.ns)} ({self.full} full), total {sum(self.ns) / 1e6:.1f} ms, "
                f"max {max(self.ns) / 1000:.0f} µs")


def full_collection_ms(repeats: int = 5) -> tuple[float, float]:
    """Median ms of `gc.collect()` with the current heap, then with it frozen."""
    def median_ms() -> float:
        times = []
        for _ in range(repeats):
            start = time.perf_counter_ns()
            gc.collect()
            times.append((time.perf_counter_ns() - start) / 1e6)
        return statistics.median(times)

    gc.collect()
    thawed = median_ms()
    gc.freeze()
    frozen = median_ms()
    gc.unfreeze()
    return thawed, frozen


def stepper(path: tuple):
    """A fresh feed's per-frame function, and the tape it keeps."""
    make, decoder, keeps = path
    feed, tape = make(), []

    def step(frame: bytes) -> None:
        msg = decoder(frame)
        feed.on_message(msg)
        if keeps:
            tape.append(msg)
    return step, tape


def kept_per_message(path: tuple, frames: list[bytes]) -> float:
    """GC-tracked containers the path keeps alive per message once its books exist: the
    growth over the second half of the frames."""
    step, _ = stepper(path)
    half = len(frames) // 2
    for f in frames[:half]:
        step(f)
    gc.collect()
    before = len(gc.get_objects())
    for f in frames[half:]:
        step(f)
    gc.collect()
    return (len(gc.get_objects()) - before) / (len(frames) - half)


def run(setting: str, path: tuple, frames: list[bytes], offsets: list[int],
        pauses: Pauses) -> array:
    gc.collect()
    step, _ = stepper(path)
    if setting == "(harness floor)":
        step = harness.noop
    elif setting == "freeze":
        gc.freeze()
    elif setting == "disable":
        gc.disable()
    gc.callbacks.append(pauses)
    try:
        return harness.open_loop(step, frames, offsets)
    finally:
        gc.callbacks.remove(pauses)
        gc.enable()
        gc.unfreeze()


def main(n: int = 40_000, speed: float = 1.0, heaps: tuple[int, ...] = (0, 1_000_000),
         rounds: int | None = None) -> None:
    harness.header("gc_pauses: garbage-collector settings under burst replay", ("msgspec",))
    print(f"gc thresholds {gc.get_threshold()}")
    rounds = rounds or harness.default_rounds(len(SETTINGS))
    pattern = PATTERN.faster(speed)
    stream = harness.Stream(n // rounds)
    offsets = harness.arrivals(n // rounds, pattern)
    for size in heaps:
        heap = long_lived_heap(size)  # held alive for the collector to walk
        thawed, frozen = full_collection_ms()
        tracked = len(gc.get_objects())
        print(f"\nlong-lived heap of {size:,} dicts ({tracked:,} tracked objects in all): one "
              f"full collection takes {thawed:.1f} ms, {frozen:.1f} ms after gc.freeze()")
        for name, path in paths().items():
            kept = kept_per_message(path, stream.frames)
            if kept >= 0.01:
                every = tracked / 4 / (kept * pattern.mean_rate)
                every_s = f"{every:,.0f}" if every >= 10 else f"{every:.2f}"
                print(f"  {name}: {kept:.1f} tracked containers kept per message, so at "
                      f"{pattern.mean_rate:,.0f} msgs/s a full collection about every {every_s} s")
        print()
        for name, path in paths().items():
            lat = {s: array("q") for s in SETTINGS}
            pauses = {s: Pauses() for s in SETTINGS}
            with harness.Steal() as steal:
                for r in range(rounds):
                    for s in harness.rotated(SETTINGS, r):
                        lat[s].extend(run(s, path, stream.frames, offsets, pauses[s]))
            print(f"{name}, {size:,}-dict heap, {pattern}, {rounds} × {n // rounds:,} frames, "
                  f"host CPU steal {steal}")
            print(harness.table_head("gc setting", 16) + "   collector pauses")
            for s in SETTINGS:
                print(harness.row(s, lat[s], 16) + f"   {pauses[s]}")
            print()
        del heap


if __name__ == "__main__":
    main()
