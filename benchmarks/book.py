"""Which per-side bid structure should the shared order book use?

Run: python benchmarks/book.py

Each op is apply(price, delta) followed by best(); prices are fixed-point units. The
workloads (synthetic until recorded WebSocket deltas exist, see docs/EXECUTION.md):
- book churn: joins, partial and full cancels, and trades that sweep the best level;
- below-touch churn: one level deep in the book is added and removed over and over;
- gap: the best level empties with the next level 98 cents below.
Memory is measured per side with 40 resting levels, and again after the below-touch churn.
"""

from __future__ import annotations

import heapq
import random
import statistics
import time
import tracemalloc
from array import array

from sortedcontainers import SortedDict

from kalshibot.kalshi.models import PRICE_SCALE, BookSide


class ArrayBitmap:
    """The same bitmap, with sizes in a dense 10,001-slot array (the first design)."""

    def __init__(self):
        self.size = array("q", bytes(8 * (PRICE_SCALE + 1)))
        self.words, self.summary = [0] * ((PRICE_SCALE >> 6) + 1), 0

    def apply(self, p, d):
        old = self.size[p]
        self.size[p] = old + d
        if (old == 0) != (old + d == 0):
            w, bit = p >> 6, 1 << (p & 63)
            if old == 0:
                self.words[w] |= bit
                self.summary |= 1 << w
            else:
                self.words[w] &= ~bit
                if not self.words[w]:
                    self.summary &= ~(1 << w)

    def best(self):
        if not self.summary:
            return None
        w = self.summary.bit_length() - 1
        return (w << 6) | (self.words[w].bit_length() - 1)


class ScanLadder:
    """Size array plus a best index that scans downward when the best level empties."""

    def __init__(self):
        self.size, self.top = array("q", bytes(8 * (PRICE_SCALE + 1))), 0

    def apply(self, p, d):
        s = self.size
        s[p] += d
        if d > 0 and p > self.top:
            self.top = p
        elif p == self.top and s[p] == 0:
            while self.top > 0 and s[self.top] == 0:
                self.top -= 1

    def best(self):
        return self.top or None


class Sorted:
    def __init__(self):
        self.d = SortedDict()

    def apply(self, p, d):
        v = self.d.get(p, 0) + d
        if v:
            self.d[p] = v
        else:
            self.d.pop(p, None)

    def best(self):
        return self.d.peekitem(-1)[0] if self.d else None


class LazyHeap:
    def __init__(self):
        self.d, self.h = {}, []

    def apply(self, p, d):
        old = self.d.get(p, 0)
        if old + d:
            self.d[p] = old + d
            if not old:
                heapq.heappush(self.h, -p)
        else:
            self.d.pop(p, None)

    def best(self):
        while self.h and -self.h[0] not in self.d:
            heapq.heappop(self.h)
        return -self.h[0] if self.h else None


class DictMax:
    def __init__(self):
        self.d = {}

    def apply(self, p, d):
        v = self.d.get(p, 0) + d
        if v:
            self.d[p] = v
        else:
            self.d.pop(p, None)

    def best(self):
        return max(self.d) if self.d else None


CANDIDATES = {"dict + price bitmap (chosen)": BookSide, "dense array + bitmap": ArrayBitmap,
              "array + downward scan": ScanLadder, "lazy-deletion heap": LazyHeap,
              "SortedDict": Sorted, "dict + max()": DictMax}


def book_churn(n: int, grid: int, deep: bool = False, seed: int = 1) -> list[tuple[int, int]]:
    """A book that behaves like a live one. Orders join around a slowly drifting fair
    price, cancel in part or in full, and trades take (and often sweep) the best level.
    `deep` adds 90 levels resting far below the action that are never touched, so the book
    holds ~100 levels like a liquid market's."""
    rng, book, ops, fair, floor = random.Random(seed), {}, [], 5000, 1000

    def emit(price, delta):
        book[price] = book.get(price, 0) + delta
        if not book[price]:
            del book[price]
        ops.append((price, delta))

    for price in range(100, floor, 10) if deep else ():
        emit(price, 10_000)
    emit(fair, 50_000)
    while len(ops) < n:
        if rng.random() < 0.05:
            fair = min(8000, max(2000, fair + rng.choice((-grid, grid))))
        active = [p for p in book if p >= floor]
        if not active:
            emit(fair, 50_000)
            continue
        best, r = max(active), rng.random()
        if r < 0.40:  # join at or just below fair
            emit(fair - grid * rng.randint(0, 4), rng.randint(100, 20_000))
        elif r < 0.60:  # partial cancel of an active level
            price = rng.choice(active)
            if book[price] > 1:
                emit(price, -rng.randint(1, book[price] - 1))
        elif r < 0.75:  # full cancel of one of the top five levels
            emit(price := rng.choice(sorted(active, reverse=True)[:5]), -book[price])
        elif r < 0.90:  # trade at the best; 40% of the time it sweeps the level
            emit(best, -(book[best] if rng.random() < 0.4 else rng.randint(1, book[best])))
        elif r < 0.95:  # a better bid above fair
            emit(min(9_999, fair + grid), rng.randint(100, 20_000))
        else:  # resting order below the action
            emit(max(floor, fair - grid * rng.randint(10, 40)), rng.randint(100, 5_000))
    return ops


def below_touch_churn(n: int) -> list[tuple[int, int]]:
    return [(5000, 50_000)] + [op for _ in range(n // 2) for op in ((3000, 100), (3000, -100))]


def wide_gap(n: int) -> list[tuple[int, int]]:
    return [(1, 5)] + [op for _ in range(n // 2) for op in ((9900, 100), (9900, -100))]


def shape(ops) -> str:
    """Share of ops that move the best price, and the number of resting levels."""
    side, last, changes, levels = BookSide(), None, 0, []
    for p, d in ops:
        side.apply(p, d)
        changes += side.best() != last
        last = side.best()
        levels.append(len(side.levels()))
    return (f"best moves {changes / len(ops):.0%}, levels min {min(levels)} / "
            f"median {statistics.median(levels):.0f}")


def ns_per_op(cls, ops, repeats: int = 5) -> float:
    def once():
        side = cls()
        start = time.perf_counter_ns()
        for p, d in ops:
            side.apply(p, d)
            side.best()
        return (time.perf_counter_ns() - start) / len(ops)
    return statistics.median(once() for _ in range(repeats))


def kib_per_side(cls, ops, sides: int = 50) -> float:
    tracemalloc.start()
    held = [cls() for _ in range(sides)]
    for side in held:
        for p, d in ops:
            side.apply(p, d)
    used = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    return used / sides / 1024


def check_agreement(ops) -> None:
    sides = [cls() for cls in CANDIDATES.values()]
    for p, d in ops:
        for s in sides:
            s.apply(p, d)
        assert len({s.best() for s in sides}) == 1, (p, d)


WORKLOADS = {"churn 1¢": lambda n: book_churn(n, 100),
             "churn 0.1¢": lambda n: book_churn(n, 10),
             "churn, ~100-level book": lambda n: book_churn(n, 100, deep=True),
             "below-touch churn": below_touch_churn,
             "best empties over 98¢ gap": wide_gap}

if __name__ == "__main__":
    workloads = {name: make(200_000) for name, make in WORKLOADS.items()}
    for ops in workloads.values():
        check_agreement(ops[:20_000])
    for name, ops in workloads.items():
        print(f"{name}: {shape(ops)}")
    print()
    resting = [(5000 - 100 * i, 1000) for i in range(40)]
    print(f"{'structure':<28}" + "".join(f"{w:>27}" for w in workloads)
          + f"{'KiB/side 40 lvls':>18}{'KiB after churn':>17}   (ns per update+best)")
    for name, cls in CANDIDATES.items():
        speeds = "".join(f"{ns_per_op(cls, ops):>27.0f}" for ops in workloads.values())
        churned = kib_per_side(cls, workloads["below-touch churn"])
        mem = f"{kib_per_side(cls, resting):>18.1f}{churned:>17.1f}"
        print(f"{name:<28}{speeds}{mem}")
