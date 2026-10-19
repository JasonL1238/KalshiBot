"""Should the hot path freeze or disable CPython's cyclic garbage collector?

Run: python benchmarks/gc_pauses.py   (stdlib only; adds the typed path if msgspec is in)

Two measurements:
1. What one full collection costs, with a heap of long-lived containers standing in for
   what a trading process holds (market metadata, positions, history), and what it costs
   once `gc.freeze()` has moved that heap out of every collection.
2. The feed's per-message work (decode plus book update), replayed open loop with bursts
   in this process, under each GC setting:
   - default: generational collection at the interpreter's thresholds;
   - freeze: `gc.freeze()` after setup;
   - disable: `gc.disable()` for the replay (the feed makes no cycles, so nothing leaks
     that a `gc.collect()` in a quiet moment wouldn't free).
   The paths: stdlib `json` into dicts and `kalshi.ws.BookFeed`, keeping nothing or
   keeping every message (a session tape held in memory, the worst case for the
   collector); and, if msgspec is installed, `decode.Message` structs (gc=False: never
   tracked) into `decode.StructFeed`, keeping every message. Settings run in interleaved
   rounds; gc.callbacks time each collector pause.
Runs on any CPython from 3.13, including free-threaded builds.
"""

from __future__ import annotations

import gc
import json
import sys
import time
from array import array

import harness

from kalshibot.kalshi.ws import BookFeed

try:
    import decode
except ImportError:  # no msgspec: the dict paths alone
    decode = None

PATTERN = harness.Pattern(1000, burst_every=0.2, burst_size=200)
SETTINGS = ("default", "freeze", "disable")


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
    """Collector pauses seen through gc.callbacks while installed."""

    def __init__(self) -> None:
        self.ns, self.full, self._start = array("q"), 0, 0

    def __call__(self, phase: str, info: dict) -> None:
        if phase == "start":
            self._start = time.perf_counter_ns()
        else:
            self.ns.append(time.perf_counter_ns() - self._start)
            self.full += info["generation"] == 2

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
        return sorted(times)[repeats // 2]

    gc.collect()
    thawed = median_ms()
    gc.freeze()
    frozen = median_ms()
    gc.unfreeze()
    return thawed, frozen


def run(setting: str, path: tuple, frames: list[bytes], offsets: list[int]
        ) -> tuple[array, Pauses]:
    make, decoder, keeps = path
    gc.collect()
    feed, tape = make(), []
    if setting == "freeze":
        gc.freeze()
    elif setting == "disable":
        gc.disable()

    def step(frame: bytes) -> None:
        msg = decoder(frame)
        feed.on_message(msg)
        if keeps:
            tape.append(msg)

    pauses = Pauses()
    gc.callbacks.append(pauses)
    try:
        lat = harness.open_loop(step, frames, offsets)
    finally:
        gc.callbacks.remove(pauses)
        gc.enable()
        gc.unfreeze()
    return lat, pauses


def main(n: int = 40_000, speed: float = 1.0, heaps: tuple[int, ...] = (0, 1_000_000),
         rounds: int = 4) -> None:
    harness.header("gc_pauses: garbage-collector settings under burst replay", ("msgspec",))
    print(f"gc thresholds {gc.get_threshold()}")
    stream = harness.Stream(n // rounds)
    offsets = harness.arrivals(n // rounds, PATTERN.faster(speed))
    for size in heaps:
        heap = long_lived_heap(size)  # held alive for the collector to walk
        thawed, frozen = full_collection_ms()
        print(f"\nlong-lived heap of {size:,} dicts: one full collection takes {thawed:.1f} ms, "
              f"{frozen:.1f} ms after gc.freeze()\n")
        for name, path in paths().items():
            lat = {s: array("q") for s in SETTINGS}
            pauses = {s: Pauses() for s in SETTINGS}
            with harness.Steal() as steal:
                for r in range(rounds):
                    for s in SETTINGS[r % 3:] + SETTINGS[:r % 3]:
                        ns, p = run(s, path, stream.frames, offsets)
                        lat[s].extend(ns)
                        pauses[s].ns.extend(p.ns)
                        pauses[s].full += p.full
            print(f"{name}, {size:,}-dict heap, {PATTERN.faster(speed)}, "
                  f"{rounds} × {n // rounds:,} frames, host CPU steal {steal}")
            print(harness.table_head("gc setting", 12) + "   collector pauses")
            for s in SETTINGS:
                print(harness.row(s, lat[s], 12) + f"   {pauses[s]}")
            print()
        del heap
    sys.stdout.flush()


if __name__ == "__main__":
    main()
