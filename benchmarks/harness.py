"""Shared pieces of the M4 execution benchmarks: schedules, open-loop timing, synthetic
Kalshi frames, a replay sender, and a local order server. The method they implement
(open loop, coordinated-omission correction, interleaved rounds, host noise) is in
docs/EXECUTION.md.

Nothing here talks to Kalshi. Keys are generated per run, and every socket is loopback.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.metadata
import json
import math
import multiprocessing
import os
import platform
import random
import socket
import ssl
import statistics
import struct
import subprocess
import sys
import tempfile
import time
from array import array
from collections.abc import Awaitable, Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from kalshibot.kalshi.models import PRICE_SCALE

clock = time.monotonic_ns  # CLOCK_MONOTONIC: one time base across processes

PRICE_GRID = 100  # 1 cent, in price units
ORDER_PATH = "/trade-api/v2/portfolio/events/orders"
ORDER = {"ticker": "KXSYN000-26OCT20-B50.5", "side": "bid", "count": "1.00",
         "price": "0.5600", "time_in_force": "immediate_or_cancel",
         "self_trade_prevention_type": "taker_at_cross"}  # a V2 order body (RESEARCH §1)
VERIFY = False  # tests set this: WebSocket replays then check each payload against its frame
WARMUP = 1000  # items each candidate runs untimed before its rounds


# ---- platform -------------------------------------------------------------------------

def _cpu_model() -> str:
    if sys.platform == "darwin":
        return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                              capture_output=True, text=True).stdout.strip()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "missing"


# CPUs for the replay's processes, kept apart where the OS can pin (Linux, 3+ CPUs). Read
# when the parent imports this, unpinned; children get their CPU as an argument.
_ALLOWED = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else []
CPUS = (dict(zip(("sender", "client", "server"), _ALLOWED[:3], strict=True))
        if len(_ALLOWED) >= 3 else {})


def header(title: str, packages: Sequence[str] = ()) -> None:
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    print(title)
    print(f"platform: {platform.platform()}, {_cpu_model()}, {os.cpu_count()} CPUs")
    print(f"python: {platform.python_implementation()} {platform.python_version()}"
          f"{'' if gil else ' (free-threaded, GIL off)'}")
    if packages:
        print("packages: " + ", ".join(f"{p} {_version(p)}" for p in packages))
    print("replay processes pinned to CPUs " + ", ".join(f"{k} {v}" for k, v in CPUS.items())
          if CPUS else "replay processes not pinned (needs Linux and 3 CPUs)")
    print()


def _pin(cpu: int | None) -> set[int] | None:
    """Pin this process to `cpu` (None: leave it); returns the previous CPU set."""
    if cpu is None:
        return None
    before = os.sched_getaffinity(0)
    os.sched_setaffinity(0, {cpu})
    return before


def _affinity() -> set[int] | None:
    return os.sched_getaffinity(0) if hasattr(os, "sched_getaffinity") else None


@contextmanager
def _pinned(role: str):
    before = _pin(CPUS.get(role))
    try:
        yield
    finally:
        if before:
            os.sched_setaffinity(0, before)


# ---- schedules, percentiles, rounds ------------------------------------------------------

@dataclass(frozen=True)
class Pattern:
    rate: float  # Poisson background arrivals per second
    burst_every: float = 0.0  # seconds between bursts; 0 means no bursts
    burst_size: int = 0  # items that arrive at the same instant in each burst

    @property
    def mean_rate(self) -> float:
        """Arrivals per second, bursts included."""
        return self.rate + (self.burst_size / self.burst_every if self.burst_every else 0)

    def faster(self, factor: float) -> Pattern:
        """The same shape compressed in time."""
        return Pattern(self.rate * factor, self.burst_every / factor, self.burst_size)

    def __str__(self) -> str:
        bursts = (f" + {self.burst_size} at once every {self.burst_every * 1000:g} ms"
                  if self.burst_every else "")
        return f"Poisson {self.rate:g}/s{bursts}"


# The feed's schedules: the same mean rate, steady or with half the frames in bursts.
PATTERNS = {"steady": Pattern(2000), "bursty": Pattern(1000, burst_every=0.2, burst_size=200)}


def arrivals(n: int, pattern: Pattern, seed: int = 1) -> list[int]:
    """`n` nondecreasing arrival times (ns after the start). A burst's items share one."""
    rng, out, t = random.Random(seed), [], 0.0
    next_burst = pattern.burst_every or float("inf")
    while len(out) < n:
        t += rng.expovariate(pattern.rate)
        while next_burst <= t and len(out) < n:
            out.extend([round(next_burst * 1e9)] * min(pattern.burst_size, n - len(out)))
            next_burst += pattern.burst_every
        if len(out) < n:
            out.append(round(t * 1e9))
    return out


def quantiles(ns: Sequence[int]) -> dict[str, float]:
    """p50, p99, p99.9 and max in microseconds (nearest rank); NaN when empty."""
    s = sorted(ns)
    if not s:
        return dict.fromkeys(("p50", "p99", "p99.9", "max"), math.nan)

    def q(p: float) -> float:
        return s[max(0, math.ceil(len(s) * p) - 1)] / 1000

    return {"p50": q(0.5), "p99": q(0.99), "p99.9": q(0.999), "max": s[-1] / 1000}


def table_head(first: str, width: int = 34) -> str:
    return f"{first:<{width}}{'n':>8}" + "".join(
        f"{k:>11}" for k in ("mean µs", "p50", "p99", "p99.9", "max"))


def row(name: str, ns: Sequence[int], width: int = 34) -> str:
    """A table row. A percentile needs 10 samples beyond it, else it prints as —."""
    qs, n = quantiles(ns), len(ns)
    shown = [statistics.fmean(ns) / 1000 if n else math.nan, qs["p50"],
             qs["p99"] if n >= 1000 else math.nan, qs["p99.9"] if n >= 10_000 else math.nan,
             qs["max"]]
    return f"{name:<{width}}{n:>8,}" + "".join(
        f"{'—':>11}" if math.isnan(v) else f"{v:>11.1f}" for v in shown)


def burst_ends(offsets: Sequence[int]) -> list[int]:
    """The index of each burst's last item: a burst drains by the time it is done."""
    return [i for i in range(1, len(offsets)) if offsets[i] == offsets[i - 1]
            and (i + 1 == len(offsets) or offsets[i + 1] != offsets[i])]


def default_rounds(candidates: int) -> int:
    """At least 4 rounds, and a multiple of the candidates so that rotating the order puts
    each first equally often."""
    return candidates * math.ceil(4 / candidates)


def rotated(names: Sequence[str], r: int) -> list[str]:
    k = r % len(names)
    return [*names[k:], *names[:k]]


# ---- in-process timing -------------------------------------------------------------------

def open_loop(fn: Callable, items: Sequence, offsets: Sequence[int]) -> array:
    """Run `fn(item)` for each item at its scheduled offset (ns), spinning between items.
    Returns each item's latency (ns) from its intended start to its finish, so queueing
    behind a slow item counts. Spinning allocates no GC-tracked objects."""
    n = len(items)
    lat = array("q", bytes(8 * n))
    t0 = clock() + 2_000_000
    for i in range(n):
        due = t0 + offsets[i]
        while clock() < due:
            pass
        fn(items[i])
        lat[i] = clock() - due
    return lat


def noop(_) -> None:
    """The harness floor: `open_loop` of this measures the measurement (and host noise)."""


def compare(candidates: dict[str, Callable], items: Sequence, offsets: Sequence[int],
            rounds: int | None = None) -> dict[str, array]:
    """Open-loop latencies per candidate, pooled over interleaved rounds that each replay
    an equal slice of the schedule, after one untimed warm-up pass per candidate."""
    names, rounds = list(candidates), rounds or default_rounds(len(candidates))
    for fn in candidates.values():
        for item in items[:WARMUP]:
            fn(item)
    out = {name: array("q") for name in names}
    per = len(offsets) // rounds
    for r in range(rounds):
        part, base = items[r * per:(r + 1) * per], offsets[r * per]
        times = [o - base for o in offsets[r * per:(r + 1) * per]]
        for name in rotated(names, r):
            out[name].extend(open_loop(candidates[name], part, times))
    return out


def per_call_ns(candidates: dict[str, Callable], items: Sequence, repeats: int = 7,
                ) -> dict[str, float]:
    """Mean ns per call over `items`, the median of `repeats` batches, with candidates
    interleaved so that a noisy stretch of the host hits them alike."""
    times: dict[str, list[float]] = {name: [] for name in candidates}
    for _ in range(repeats):
        for name, fn in candidates.items():
            start = time.perf_counter_ns()
            for item in items:
                fn(item)
            times[name].append((time.perf_counter_ns() - start) / len(items))
    return {name: statistics.median(t) for name, t in times.items()}


class Steal:
    """The share of CPU time the hypervisor took from this VM while the block ran (Linux
    /proc/stat `steal`; n/a elsewhere). Tails on a host that steals are partly the host's."""

    def __init__(self):
        self.share: float | None = None

    @staticmethod
    def _read() -> list[int] | None:
        try:
            return [int(x) for x in Path("/proc/stat").read_text().split("\n")[0].split()[1:]]
        except (OSError, ValueError):
            return None

    def __enter__(self) -> Steal:
        self._start = self._read()
        return self

    def __exit__(self, *exc) -> None:
        end = self._read()
        if self._start and end:
            d = [b - a for a, b in zip(self._start, end, strict=False)]
            self.share = d[7] / max(1, sum(d[:8]))

    def __str__(self) -> str:
        return "n/a" if self.share is None else f"{self.share:.1%}"


# ---- synthetic Kalshi frames -------------------------------------------------------------

def dollars(units: int) -> str:
    """Price units as Kalshi's dollar string: 5600 -> "0.5600"."""
    return f"{units // PRICE_SCALE}.{units % PRICE_SCALE:04d}"


def _size(units: int) -> str:
    sign, units = ("-", -units) if units < 0 else ("", units)
    return f"{sign}{units // 100}.{units % 100:02d}"


def _uuid(rng: random.Random) -> str:
    h = f"{rng.getrandbits(128):032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


class _Market:
    """One market's resting bids on both sides, kept uncrossed (yes + no < $1)."""

    def __init__(self, rng: random.Random, ticker: str, depth: int):
        self.rng, self.ticker = rng, ticker
        self.fair = rng.randrange(15, 86) * PRICE_GRID
        self.sides: dict[str, dict[int, int]] = {"yes": {}, "no": {}}
        for side in ("yes", "no"):
            for k in range(rng.randint(depth // 2, depth)):
                price = self.top(side) - PRICE_GRID * (1 + k)
                if price > 0:
                    self.sides[side][price] = self.lot()

    def lot(self) -> int:
        rng = self.rng
        whole = 100 * rng.choice((1, 1, 2, 5, 10, 10, 25, 50, 100, 250, 1000))
        return whole if rng.random() < 0.9 else whole + rng.randint(1, 99)

    def top(self, side: str) -> int:
        """The highest price a new bid on `side` may take without crossing."""
        return (self.fair if side == "yes" else PRICE_SCALE - self.fair) - PRICE_GRID

    def snapshot(self) -> dict:
        body = {"market_ticker": self.ticker}
        for side, levels in self.sides.items():
            if levels:
                body[f"{side}_dollars_fp"] = [[dollars(p), _size(s)]
                                              for p, s in sorted(levels.items())]
        return body

    def step(self, ts_ms: int) -> list[tuple[str, dict]]:
        """One book event: a join, a cancel, or a trade (which takes the best bid on one side
        and may sweep it). Returns (type, msg) pairs."""
        rng, side = self.rng, self.rng.choice(("yes", "no"))
        levels = self.sides[side]
        if rng.random() < 0.03:  # fair value moves a tick; bids that would now cross leave
            self.fair = min(9000, max(1000, self.fair + rng.choice((-PRICE_GRID, PRICE_GRID))))
            out = [("orderbook_delta", self.take(s, p, self.sides[s][p], ts_ms))
                   for s in ("yes", "no") for p in sorted(self.sides[s]) if p > self.top(s)]
            if out:
                return out
        r = rng.random()
        if r < 0.55 or not levels:  # join at or a few ticks below the touch
            price = self.top(side) - PRICE_GRID * rng.choice((0, 0, 1, 1, 2, 3, 5, 8, 13, 21))
            if price <= 0:
                return []
            size = self.lot()
            levels[price] = levels.get(price, 0) + size
            return [("orderbook_delta", self.delta(price, size, side, ts_ms))]
        if r < 0.83:  # cancel part or all of a level near the top
            price = rng.choice(sorted(levels)[-6:])
            size = levels[price] if rng.random() < 0.5 else rng.randint(1, levels[price])
            return [("orderbook_delta", self.take(side, price, size, ts_ms))]
        best = max(levels)  # a trade: the taker sells into `side`'s best bid
        size = levels[best] if rng.random() < 0.4 else rng.randint(1, levels[best])
        yes = best if side == "yes" else PRICE_SCALE - best
        trade = {"trade_id": _uuid(rng), "market_ticker": self.ticker,
                 "yes_price_dollars": dollars(yes), "no_price_dollars": dollars(PRICE_SCALE - yes),
                 "count_fp": _size(size), "taker_side": "no" if side == "yes" else "yes",
                 "ts": ts_ms // 1000}
        return [("trade", trade), ("orderbook_delta", self.take(side, best, size, ts_ms))]

    def take(self, side: str, price: int, size: int, ts_ms: int) -> dict:
        levels = self.sides[side]
        levels[price] -= size
        if not levels[price]:
            del levels[price]
        return self.delta(price, -size, side, ts_ms)

    def delta(self, price: int, size: int, side: str, ts_ms: int) -> dict:
        return {"market_ticker": self.ticker, "price_dollars": dollars(price),
                "delta_fp": _size(size), "side": side, "ts_ms": ts_ms}


class Stream:
    """A replayable WebSocket session in Kalshi's formats (docs/RESEARCH.md §1): snapshots
    for every market, then book deltas, the trades that cause some of them, and our own
    fills. Books start 5-100 levels deep per side. Book messages carry a contiguous `seq`
    per subscription; trades and fills carry none, the case a decoder must accept. Frames
    are compact JSON bytes."""

    SIDS = {"orderbook_snapshot": 1, "orderbook_delta": 1, "trade": 2, "fill": 3}

    def __init__(self, n: int, markets: int = 50, depth: int = 100, seed: int = 1):
        if n < markets:
            raise ValueError(f"{n} frames can't hold {markets} markets' snapshots")
        rng = random.Random(seed)
        self.markets = [_Market(rng, f"KXSYN{i:03d}-26OCT20-B{50 + i}.5",
                                rng.randint(10, depth)) for i in range(markets)]
        seq = 0
        ts_ms, self.messages = 1_792_000_000_000, []

        def emit(kind: str, body: dict) -> None:
            nonlocal seq
            msg = {"type": kind, "sid": self.SIDS[kind]}
            if kind.startswith("orderbook"):
                seq += 1
                msg["seq"] = seq
            self.messages.append({**msg, "msg": body})

        for m in self.markets:
            emit("orderbook_snapshot", m.snapshot())
        while len(self.messages) < n:
            ts_ms += rng.randint(0, 3)
            m = rng.choice(self.markets)
            if rng.random() < 0.01:
                emit("fill", self.fill(rng, m, ts_ms))
                continue
            before = {side: dict(levels) for side, levels in m.sides.items()}, m.fair
            events = m.step(ts_ms)
            if len(self.messages) + len(events) > n:  # it won't fit: undo it, end on fills
                m.sides, m.fair = before
                continue
            for kind, body in events:
                emit(kind, body)
        self.frames = [json.dumps(m, separators=(",", ":")).encode() for m in self.messages]

    @staticmethod
    def fill(rng: random.Random, m: _Market, ts_ms: int) -> dict:
        size = m.lot()
        return {"trade_id": _uuid(rng), "order_id": _uuid(rng), "market_ticker": m.ticker,
                "is_taker": rng.random() < 0.5, "side": rng.choice(("yes", "no")),
                "yes_price_dollars": dollars(m.fair), "count_fp": _size(size),
                "fee_cost": "0.0700", "action": rng.choice(("buy", "sell")), "ts": ts_ms // 1000,
                "client_order_id": _uuid(rng), "post_position_fp": _size(size),
                "purchased_side": "yes"}


# ---- helper processes ----------------------------------------------------------------------

class _Child:
    """A helper process on a pipe, pinned to its role's CPU. `target(pipe, cpu, *args)`
    pins itself, reports (port, its CPU set) when ready, and stops on "stop"."""

    def __init__(self, role: str, target: Callable, *args):
        ctx = multiprocessing.get_context("spawn")
        self._pipe, child = ctx.Pipe()
        cpu = CPUS.get(role)
        self._proc = ctx.Process(target=target, args=(child, cpu, *args), daemon=True)
        self._proc.start()
        self.port, affinity = self._recv()
        if cpu is not None and affinity != {cpu}:
            self._proc.kill()
            raise RuntimeError(f"{role} runs on CPUs {affinity}, not {cpu}")

    def _recv(self, timeout: float = 120):
        """The child's next message; an error if it died or hung instead."""
        deadline = time.monotonic() + timeout
        while not self._pipe.poll(0.1):
            if not self._proc.is_alive() or time.monotonic() > deadline:
                raise RuntimeError(f"{self._proc.name} exited ({self._proc.exitcode}) or hung")
        return self._pipe.recv()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        try:
            self._pipe.send("stop")
        except OSError:  # the child is already gone
            pass
        self._proc.join(5)
        if self._proc.is_alive():
            self._proc.kill()


def _ws_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """An unmasked server-to-client WebSocket frame."""
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 1 << 16:
        head = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    return head + payload


def _accept_websocket(sock: socket.socket) -> None:
    request = b""
    while b"\r\n\r\n" not in request:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("peer closed during the handshake")
        request += chunk
    key = next(line.split(":", 1)[1].strip() for line in request.decode("latin-1").split("\r\n")
               if line.lower().startswith("sec-websocket-key:"))
    accept = base64.b64encode(hashlib.sha1(
        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
    sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                  f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())


def _send(pipe, cpu: int | None, frames: list[bytes], offsets: list[int],
          websocket: bool) -> None:
    _pin(cpu)
    server = socket.create_server(("127.0.0.1", 0))
    pipe.send((server.getsockname()[1], _affinity()))
    wire = [_ws_frame(f) for f in frames] if websocket else frames
    while (msg := pipe.recv()) != "stop":  # ("go", limit): one replay per connection
        limit = msg[1]
        sock, _ = server.accept()
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if websocket:
            _accept_websocket(sock)
        groups: list[tuple[int, bytes]] = []  # a burst goes out in one write
        for off, data in zip(offsets[:limit], wire[:limit], strict=True):
            if groups and groups[-1][0] == off:
                groups[-1] = (off, groups[-1][1] + data)
            else:
                groups.append((off, data))
        t0 = clock() + 100_000_000  # time for the client to finish connecting
        sock.sendall(_ws_frame(str(t0).encode()) if websocket else struct.pack("!q", t0))
        lags = array("q")
        for off, data in groups:
            due = t0 + off
            while clock() < due:  # spin: a sleeping sender wakes up late
                pass
            sock.sendall(data)
            lags.append(clock() - due)
        try:  # end the stream, so the client needn't time out; it may have gone already
            if websocket:
                sock.sendall(_ws_frame(struct.pack("!H", 1000), opcode=0x8))
            sock.shutdown(socket.SHUT_WR)
            sock.settimeout(5)
            while sock.recv(65536):
                pass
        except OSError:
            pass
        sock.close()
        pipe.send(lags.tobytes())


class Sender(_Child):
    """A process that replays `frames` at `offsets` to each client that connects, as
    WebSocket text frames or raw bytes. The first frame (or 8 raw bytes) carries t0, the
    schedule's start on `clock()`; then the frames go out, bursts in one write, and the
    stream ends (a close frame, then EOF). Call `replay()` before each client connects
    and `lag()` once it is done."""

    def __init__(self, frames: list[bytes], offsets: list[int], websocket: bool = True):
        super().__init__("sender", _send, frames, offsets, websocket)
        self.url = f"ws://127.0.0.1:{self.port}/"

    def replay(self, limit: int | None = None) -> None:
        """Serve the next connection the first `limit` frames (all if None)."""
        self._pipe.send(("go", limit))

    def lag(self) -> array:
        """How late each write finished against its schedule (ns): the sender's own share."""
        out = array("q")
        out.frombytes(self._recv())
        return out


class Recorder:
    """Client side of a replay. The first message carries t0; item i is then charged from
    t0 + offsets[i]. `frame(payload)` stamps messages that are done when they arrive, in
    order; `hit(i)` stamps item i whenever it finishes (an order's response, say).
    `cpu_ns` is the client's CPU time from t0 to the last item. With `expected` frames,
    each payload must match its frame. `warmup` marks an untimed replay."""

    def __init__(self, offsets: Sequence[int], expected: Sequence[bytes] | None = None,
                 warmup: bool = False):
        self.offsets, self.expected, self.warmup = offsets, expected, warmup
        self.t0, self.i, self.cpu_ns = 0, 0, 0
        self.lat = array("q", bytes(8 * len(offsets)))

    @property
    def done(self) -> bool:
        return self.i == len(self.offsets)

    def start(self, t0: int) -> None:
        self.t0, self._cpu = t0, time.process_time_ns()

    def frame(self, payload: bytes | str) -> None:
        now = clock()
        if not self.t0:
            self.start(int(payload))
            return
        if self.expected is not None:
            got = payload.encode() if isinstance(payload, str) else bytes(payload)
            assert got == self.expected[self.i], f"frame {self.i} arrived altered"
        self.lat[self.i] = now - self.t0 - self.offsets[self.i]
        self._next()

    def hit(self, i: int) -> None:
        self.lat[i] = clock() - self.t0 - self.offsets[i]
        self._next()

    def _next(self) -> None:
        self.i += 1
        if self.done:
            self.cpu_ns = time.process_time_ns() - self._cpu


def run_on(loop: str, main: Callable[[], Awaitable]):
    """Run `main()` to completion on a new event loop: "asyncio" (stdlib) or "uvloop"."""
    if loop == "uvloop":
        import uvloop  # the [bench] extra; gc_pauses runs without it
        return uvloop.run(main())
    return asyncio.run(main())


def on_loop(loop: str, consumer: Callable[[str, Recorder], Awaitable]):
    """A `replay` candidate that runs `consumer(url, recorder)` on `loop`."""
    return lambda url, sink: run_on(loop, lambda: consumer(url, sink))


@dataclass
class Replay:
    lat: dict[str, array]  # per candidate, ns from each item's scheduled time
    cpu: dict[str, float]  # per candidate, client CPU µs per item
    lag: array  # the sender's lateness per write (ns), over every round

    def table(self, first: str, lag_name: str = "(sender lag, per write)") -> str:
        lines = [table_head(first) + f"{'CPU µs/item':>13}", row(lag_name, self.lag)]
        lines += [row(name, ns) + f"{self.cpu[name]:>13.1f}" for name, ns in self.lat.items()]
        return "\n".join(lines)


def replay(candidates: dict[str, Callable[[str, Recorder], None]], frames: list[bytes],
           offsets: list[int], rounds: int | None = None, websocket: bool = True) -> Replay:
    """Each candidate connects to a `Sender` and consumes a replay of `frames` at
    `offsets`: once untimed as warm-up (the first `WARMUP` frames), then `rounds` times in
    rotating order, pooled. The client side runs pinned apart from the sender."""
    names, rounds = list(candidates), rounds or default_rounds(len(candidates))
    lat, cpu, lags = {name: array("q") for name in names}, dict.fromkeys(names, 0), array("q")
    warmup = min(WARMUP, len(offsets))
    with _pinned("client"), Sender(frames, offsets, websocket) as sender:
        for r in range(-1, rounds):
            for name in rotated(names, max(r, 0)):
                rec = Recorder(offsets[:warmup] if r < 0 else offsets,
                               frames if VERIFY and websocket else None, warmup=r < 0)
                sender.replay(warmup if r < 0 else None)
                candidates[name](sender.url, rec)
                assert rec.done, f"{name} stopped after {rec.i} of {len(rec.offsets)} items"
                lag = sender.lag()
                if r >= 0:
                    lat[name].extend(rec.lat)
                    cpu[name] += rec.cpu_ns
                    lags.extend(lag)
    return Replay(lat, {name: ns / 1000 / len(lat[name]) for name, ns in cpu.items()}, lags)


def report_ws_replays(first: str, candidates: dict[str, Callable], n: int, speed: float,
                      rounds: int | None = None) -> None:
    """Replay a synthetic stream to WebSocket candidates under each of `PATTERNS`."""
    rounds = rounds or default_rounds(len(candidates))
    stream = Stream(n // rounds)
    for label, pattern in PATTERNS.items():
        pattern = pattern.faster(speed)
        offsets = arrivals(n // rounds, pattern)
        with Steal() as steal:
            result = replay(candidates, stream.frames, offsets, rounds)
        print(f"open-loop replay, {label} ({pattern}), {rounds} × {n // rounds:,} frames "
              f"each, host CPU steal {steal}")
        print(result.table(first))
        print()


# ---- a local TLS order endpoint ------------------------------------------------------------

def _self_signed(directory: Path) -> tuple[Path, Path]:
    """A throwaway certificate for 127.0.0.1 (never a real key)."""
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


ORDER_ACK = json.dumps({"order": {
    **ORDER, "order_id": "00000000-0000-0000-0000-000000000000", "client_order_id": "x",
    "status": "executed", "fill_count": "1.00", "remaining_count": "0.00",
    "ts_ms": 1792000000000}}).encode()


def _order_protocol() -> type[asyncio.Protocol]:
    """The order server's protocol (imports h2, which only the server process needs).
    Answers every request on a TLS connection with `ORDER_ACK` (201): HTTP/2 when ALPN
    picked it, else HTTP/1.1 with keep-alive and pipelining."""
    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.events import DataReceived, StreamEnded

    h1_ack = (b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\n"
              b"Content-Length: %d\r\n\r\n" % len(ORDER_ACK)) + ORDER_ACK
    h2_headers = [(":status", "201"), ("content-type", "application/json"),
                  ("content-length", str(len(ORDER_ACK)))]

    class OrderProtocol(asyncio.Protocol):
        def connection_made(self, transport) -> None:
            self.transport, self.buf, self.h2 = transport, b"", None
            if transport.get_extra_info("ssl_object").selected_alpn_protocol() == "h2":
                self.h2 = H2Connection(H2Configuration(client_side=False, header_encoding=None))
                self.h2.initiate_connection()
                transport.write(self.h2.data_to_send())

        def data_received(self, data: bytes) -> None:
            if self.h2 is None:
                self.h1(data)
                return
            for event in self.h2.receive_data(data):
                if isinstance(event, DataReceived):
                    self.h2.acknowledge_received_data(event.flow_controlled_length,
                                                      event.stream_id)
                elif isinstance(event, StreamEnded):
                    self.h2.send_headers(event.stream_id, h2_headers)
                    self.h2.send_data(event.stream_id, ORDER_ACK, end_stream=True)
            self.transport.write(self.h2.data_to_send())

        def h1(self, data: bytes) -> None:
            buf, acks = self.buf + data, 0
            while (end := buf.find(b"\r\n\r\n")) >= 0:
                head = buf[:end].lower()
                at = head.find(b"content-length:")
                length = int(head[at + 15:].split(b"\r\n", 1)[0]) if at >= 0 else 0
                if len(buf) < end + 4 + length:
                    break
                buf, acks = buf[end + 4 + length:], acks + 1
            self.buf = buf
            if acks:
                self.transport.write(h1_ack * acks)

    return OrderProtocol


def _serve(pipe, cpu: int | None, cert: str, key: str) -> None:
    import uvloop

    _pin(cpu)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key)
    ctx.set_alpn_protocols(["h2", "http/1.1"])
    protocol = _order_protocol()

    async def main():
        loop = asyncio.get_running_loop()
        stop = loop.create_future()
        loop.add_reader(pipe.fileno(), lambda: stop.done() or stop.set_result(None))
        server = await loop.create_server(protocol, "127.0.0.1", 0, ssl=ctx)
        pipe.send((server.sockets[0].getsockname()[1], _affinity()))
        await stop
        server.close()

    uvloop.run(main())


class OrderServer(_Child):
    """A local HTTPS server (uvloop, its own process) that acknowledges every request with
    a canned order. `ssl_context()` trusts its throwaway certificate."""

    def __init__(self):
        self._dir = tempfile.TemporaryDirectory()
        self.cert, key = _self_signed(Path(self._dir.name))
        super().__init__("server", _serve, str(self.cert), str(key))
        self.url = f"https://127.0.0.1:{self.port}{ORDER_PATH}"

    def ssl_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self.cert))

    def __exit__(self, *exc) -> None:
        super().__exit__(*exc)
        self._dir.cleanup()
