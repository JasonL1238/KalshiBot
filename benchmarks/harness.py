"""Shared pieces of the M4 execution benchmarks (docs/EXECUTION.md).

- `header()`: the platform line every script prints first.
- `PATTERNS`, `arrivals()`: open-loop arrival schedules, with and without bursts.
- `open_loop()`: replays a schedule against a function in this process.
- `Stream`: synthetic Kalshi WebSocket frames (snapshot, delta, trade, fill).
- `Sender`: a separate process that writes frames to one TCP connection on a schedule,
  optionally after a WebSocket handshake, so network replays are open loop too.
- `OrderServer`: a local TLS order endpoint (HTTP/1.1 and HTTP/2) in its own process.

Every latency is measured from the item's *intended* time on the schedule, never from
when the code under test got to it. A slow item therefore delays, and is charged to,
every item queued behind it, which closed-loop timing hides (coordinated omission).

Nothing here talks to Kalshi. Keys are generated per run, and every socket is loopback.
"""

from __future__ import annotations

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

clock = time.monotonic_ns  # CLOCK_MONOTONIC: one time base across processes

PRICE_GRID = 100  # 1 cent, in price units
ORDER_PATH = "/trade-api/v2/portfolio/events/orders"


# ---- platform -------------------------------------------------------------------------

def cpu_model() -> str:
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


def header(title: str, packages: Sequence[str] = ()) -> None:
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    print(title)
    print(f"platform: {platform.platform()}, {cpu_model()}, {os.cpu_count()} CPUs")
    print(f"python: {platform.python_implementation()} {platform.python_version()}"
          f"{'' if gil else ' (free-threaded, GIL off)'}")
    if packages:
        print("packages: " + ", ".join(f"{p} {version(p)}" for p in packages))
    print()


def version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "missing"


def pin(cpu: int) -> set[int] | None:
    """Pin this process to one CPU where the OS allows it (Linux), to keep the sender,
    server, and client off each other's cores. Returns the previous CPU set."""
    if not hasattr(os, "sched_setaffinity") or (os.cpu_count() or 1) <= cpu:
        return None
    before = os.sched_getaffinity(0)
    os.sched_setaffinity(0, {cpu})
    return before


@contextmanager
def pinned(cpu: int):
    before = pin(cpu)
    try:
        yield
    finally:
        if before:
            os.sched_setaffinity(0, before)


# ---- schedules and percentiles ----------------------------------------------------------

@dataclass(frozen=True)
class Pattern:
    rate: float  # Poisson background arrivals per second
    burst_every: float = 0.0  # seconds between bursts; 0 means no bursts
    burst_size: int = 0  # items that arrive at the same instant in each burst

    def faster(self, factor: float) -> Pattern:
        """The same shape compressed in time (used by the tests to run quickly)."""
        return Pattern(self.rate * factor, self.burst_every / factor, self.burst_size)

    def __str__(self) -> str:
        bursts = (f" + {self.burst_size} at once every {self.burst_every * 1000:g} ms"
                  if self.burst_every else "")
        return f"Poisson {self.rate:g}/s{bursts}"


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


def row(name: str, ns: Sequence[int], width: int = 34) -> str:
    qs = quantiles(ns)
    return f"{name:<{width}}" + "".join(f"{v:>11.1f}" for v in qs.values())


def table_head(first: str, width: int = 34) -> str:
    return f"{first:<{width}}" + "".join(f"{k:>11}" for k in ("p50 µs", "p99", "p99.9", "max"))


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


def noop(_) -> None:
    """The harness floor: `open_loop` of this measures the measurement (and host noise)."""


def compare(candidates: dict[str, Callable], items: Sequence, offsets: Sequence[int],
            rounds: int = 4) -> dict[str, array]:
    """Open-loop latencies per candidate, pooled over `rounds` interleaved runs of an equal
    slice of the schedule, so a noisy stretch of the host hits every candidate alike."""
    out = {name: array("q") for name in candidates}
    per = len(offsets) // rounds
    for r in range(rounds):
        part, base = items[r * per:(r + 1) * per], offsets[r * per]
        times = [o - base for o in offsets[r * per:(r + 1) * per]]
        names = list(candidates)
        for name in names[r % len(names):] + names[:r % len(names)]:
            out[name].extend(open_loop(candidates[name], part, times))
    return out


class Steal:
    """The share of CPU time the hypervisor took from this VM while the block ran (Linux
    /proc/stat `steal`; None elsewhere). Tails on a host that steals are the host's."""

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

def _price(units: int) -> str:
    return f"{units // 10_000}.{units % 10_000:04d}"


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
        return (self.fair if side == "yes" else 10_000 - self.fair) - PRICE_GRID

    def snapshot(self) -> dict:
        body = {"market_ticker": self.ticker}
        for side, levels in self.sides.items():
            if levels:
                body[f"{side}_dollars_fp"] = [[_price(p), _size(s)]
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
        yes = best if side == "yes" else 10_000 - best
        trade = {"trade_id": _uuid(rng), "market_ticker": self.ticker,
                 "yes_price_dollars": _price(yes), "no_price_dollars": _price(10_000 - yes),
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
        return {"market_ticker": self.ticker, "price_dollars": _price(price),
                "delta_fp": _size(size), "side": side, "ts_ms": ts_ms}


class Stream:
    """A replayable WebSocket session in Kalshi's formats (docs/RESEARCH.md §1): snapshots
    for every market, then book deltas, the trades that cause some of them, and our own
    fills. Books start 10-100 levels deep per side. Frames are compact JSON bytes."""

    SIDS = {"orderbook_snapshot": 1, "orderbook_delta": 1, "trade": 2, "fill": 3}

    def __init__(self, n: int, markets: int = 50, depth: int = 100, seed: int = 1):
        rng = random.Random(seed)
        self.markets = [_Market(rng, f"KXSYN{i:03d}-26OCT20-B{50 + i}.5",
                                rng.randint(10, depth)) for i in range(markets)]
        seq = dict.fromkeys(self.SIDS.values(), 0)
        ts_ms, self.messages = 1_792_000_000_000, []

        def emit(kind: str, body: dict) -> None:
            sid = self.SIDS[kind]
            seq[sid] += 1
            self.messages.append({"type": kind, "sid": sid, "seq": seq[sid], "msg": body})

        for m in self.markets:
            emit("orderbook_snapshot", m.snapshot())
        while len(self.messages) < n:
            ts_ms += rng.randint(0, 3)
            if rng.random() < 0.01:
                m = rng.choice(self.markets)
                emit("fill", self.fill(rng, m, ts_ms))
                continue
            for kind, body in rng.choice(self.markets).step(ts_ms):
                emit(kind, body)
        del self.messages[n:]
        self.frames = [json.dumps(m, separators=(",", ":")).encode() for m in self.messages]

    @staticmethod
    def fill(rng: random.Random, m: _Market, ts_ms: int) -> dict:
        size = m.lot()
        return {"trade_id": _uuid(rng), "order_id": _uuid(rng), "market_ticker": m.ticker,
                "is_taker": rng.random() < 0.5, "side": rng.choice(("yes", "no")),
                "yes_price_dollars": _price(m.fair), "count_fp": _size(size),
                "fee_cost": "0.0700", "action": rng.choice(("buy", "sell")), "ts": ts_ms // 1000,
                "client_order_id": _uuid(rng), "post_position_fp": _size(size),
                "purchased_side": "yes"}


def ws_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """An unmasked server-to-client WebSocket frame."""
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 1 << 16:
        head = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    return head + payload


# ---- the replay sender -------------------------------------------------------------------

def _recv_until(sock: socket.socket, end: bytes) -> bytes:
    data = b""
    while end not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("peer closed during the handshake")
        data += chunk
    return data


def _accept_websocket(sock: socket.socket) -> None:
    request = _recv_until(sock, b"\r\n\r\n").decode("latin-1")
    key = next(line.split(":", 1)[1].strip() for line in request.split("\r\n")
               if line.lower().startswith("sec-websocket-key:"))
    accept = base64.b64encode(hashlib.sha1(
        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
    sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                  f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())


def _send(pipe, frames: list[bytes], offsets: list[int], websocket: bool, cpu: int) -> None:
    pin(cpu)
    server = socket.create_server(("127.0.0.1", 0))
    pipe.send(server.getsockname()[1])
    while True:  # one replay per accepted connection, until the parent says stop
        if not pipe.poll(None) or pipe.recv() != "go":
            return
        sock, _ = server.accept()
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if websocket:
            _accept_websocket(sock)
        wire = [ws_frame(f) for f in frames] if websocket else frames
        groups: list[tuple[int, bytes]] = []
        for off, data in zip(offsets, wire, strict=True):
            if groups and groups[-1][0] == off:
                groups[-1] = (off, groups[-1][1] + data)
            else:
                groups.append((off, data))
        t0 = clock() + 300_000_000
        start = str(t0).encode()
        sock.sendall(ws_frame(start) if websocket else struct.pack("!q", t0))
        lags = array("q")
        for off, data in groups:
            due = t0 + off
            while clock() < due:  # spin: a sleeping sender wakes up late
                pass
            sock.sendall(data)
            lags.append(clock() - due)
        if websocket:
            sock.sendall(ws_frame(struct.pack("!H", 1000), opcode=0x8))
        sock.settimeout(5)
        try:
            while sock.recv(65536):
                pass
        except OSError:
            pass
        sock.close()
        pipe.send(lags.tobytes())


def _recv(pipe, proc, timeout: float = 120):
    """The next message from a child process, or an error if it died or hung."""
    deadline = time.monotonic() + timeout
    while not pipe.poll(0.1):
        if not proc.is_alive() or time.monotonic() > deadline:
            raise RuntimeError(f"{proc.name} exited ({proc.exitcode}) or hung")
    return pipe.recv()


class Sender:
    """A process that replays `frames` at `offsets` to each client that connects, as
    WebSocket text frames or raw bytes. The first frame (or 8 raw bytes) carries t0, the
    schedule's start on `clock()`. Bursts go out in one write. Use as a context manager;
    call `replay()` before each client connects and `lag()` after it is done."""

    def __init__(self, frames: list[bytes], offsets: list[int], websocket: bool = True,
                 cpu: int = 0):
        ctx = multiprocessing.get_context("spawn")
        self._pipe, child = ctx.Pipe()
        self._proc = ctx.Process(target=_send, args=(child, frames, offsets, websocket, cpu),
                                 daemon=True)
        self._proc.start()
        self.port = _recv(self._pipe, self._proc)
        self.url = f"ws://127.0.0.1:{self.port}/"

    def replay(self) -> None:
        self._pipe.send("go")

    def lag(self) -> array:
        """How late each write finished against its schedule (ns): the sender's own share."""
        out = array("q")
        out.frombytes(_recv(self._pipe, self._proc))
        return out

    def __enter__(self) -> Sender:
        return self

    def __exit__(self, *exc) -> None:
        self._pipe.send("stop")
        self._proc.join(5)
        if self._proc.is_alive():
            self._proc.kill()


class Recorder:
    """Client side of a replay. The first message carries t0; item i is then charged from
    t0 + offsets[i]. `frame(payload)` stamps messages that are done when they arrive, in
    order; `hit(i)` stamps item i whenever it finishes (an order's response, say)."""

    def __init__(self, offsets: Sequence[int]):
        self.offsets, self.t0, self.i = offsets, 0, 0
        self.lat = array("q", bytes(8 * len(offsets)))
        self.cpu_ns = 0  # the client's CPU time for the replay, if it measures its own

    @property
    def done(self) -> bool:
        return self.i == len(self.offsets)

    def frame(self, payload: bytes | str) -> None:
        now = clock()
        if not self.t0:
            self.t0 = int(payload)
            return
        self.lat[self.i] = now - self.t0 - self.offsets[self.i]
        self.i += 1

    def hit(self, i: int) -> None:
        self.lat[i] = clock() - self.t0 - self.offsets[i]
        self.i += 1


def run_on(loop: str, main: Callable[[], Awaitable]):
    """Run `main()` to completion on a new event loop: "asyncio" (stdlib) or "uvloop"."""
    if loop == "uvloop":
        import uvloop
        return uvloop.run(main())
    import asyncio
    return asyncio.run(main())


def replay(candidates: dict[str, Callable[[str, Recorder], None]], frames: list[bytes],
           offsets: list[int], rounds: int = 4, websocket: bool = True,
           ) -> tuple[dict[str, array], array, dict[str, float]]:
    """Each candidate connects to a `Sender` and consumes one replay of `frames`, `rounds`
    times in rotating order; latencies are pooled per candidate. Also returns the sender's
    own lag, and each candidate's CPU µs per item (its process time over the replay, or
    what it put in `Recorder.cpu_ns`). The client runs pinned to CPU 1, the sender to 0."""
    out, lags, names = {name: array("q") for name in candidates}, array("q"), list(candidates)
    cpu = dict.fromkeys(candidates, 0)
    with pinned(1), Sender(frames, offsets, websocket) as sender:
        for r in range(rounds):
            for name in names[r % len(names):] + names[:r % len(names)]:
                rec = Recorder(offsets)
                sender.replay()
                start = time.process_time_ns()
                candidates[name](sender.url, rec)
                cpu[name] += rec.cpu_ns or time.process_time_ns() - start
                assert rec.done, f"{name} stopped after {rec.i} of {len(offsets)} items"
                out[name].extend(rec.lat)
                lags.extend(sender.lag())
    return out, lags, {name: ns / 1000 / len(out[name]) for name, ns in cpu.items()}


def replay_table(first: str, lat: dict[str, array], lag: array, cpu: dict[str, float],
                 lag_name: str = "(sender lag)") -> str:
    lines = [table_head(first) + f"{'CPU µs/item':>13}", row(lag_name, lag)]
    lines += [row(name, ns) + f"{cpu[name]:>13.1f}" for name, ns in lat.items()]
    return "\n".join(lines)


# ---- a local TLS order endpoint ------------------------------------------------------------

def self_signed(directory: Path) -> tuple[Path, Path]:
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
    "order_id": "00000000-0000-0000-0000-000000000000", "client_order_id": "x",
    "ticker": "KXSYN000-26OCT20-B50.5", "side": "bid", "status": "executed",
    "count": "1.00", "price": "0.5600", "fill_count": "1.00", "remaining_count": "0.00",
    "time_in_force": "immediate_or_cancel", "ts_ms": 1792000000000}}).encode()


class _OrderProtocol:
    """Answers every request on one TLS connection with `ORDER_ACK` (201): HTTP/2 when ALPN
    picked it (the `h2` state machine), else HTTP/1.1 with keep-alive and pipelining."""

    H1_ACK = (b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\n"
              b"Content-Length: %d\r\n\r\n" % len(ORDER_ACK)) + ORDER_ACK

    def connection_made(self, transport) -> None:
        self.transport, self.buf = transport, b""
        self.h2 = None
        if transport.get_extra_info("ssl_object").selected_alpn_protocol() == "h2":
            from h2.config import H2Configuration
            from h2.connection import H2Connection
            self.h2 = H2Connection(H2Configuration(client_side=False, header_encoding=None))
            self.h2.initiate_connection()
            transport.write(self.h2.data_to_send())

    def data_received(self, data: bytes) -> None:
        if self.h2 is None:
            self._h1(data)
            return
        from h2.events import DataReceived, StreamEnded
        conn = self.h2
        for event in conn.receive_data(data):
            if isinstance(event, DataReceived):
                conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
            elif isinstance(event, StreamEnded):
                conn.send_headers(event.stream_id, [(":status", "201"),
                                                    ("content-type", "application/json"),
                                                    ("content-length", str(len(ORDER_ACK)))])
                conn.send_data(event.stream_id, ORDER_ACK, end_stream=True)
        self.transport.write(conn.data_to_send())

    def _h1(self, data: bytes) -> None:
        buf, out = self.buf + data, []
        while (end := buf.find(b"\r\n\r\n")) >= 0:
            head = buf[:end].lower()
            at = head.find(b"content-length:")
            length = int(head[at + 15:].split(b"\r\n", 1)[0]) if at >= 0 else 0
            if len(buf) < end + 4 + length:
                break
            buf = buf[end + 4 + length:]
            out.append(self.H1_ACK)
        self.buf = buf
        if out:
            self.transport.write(b"".join(out))

    def connection_lost(self, exc) -> None:
        pass

    def eof_received(self) -> None:
        pass


def _serve(pipe, cert: str, key: str, cpu: int) -> None:
    import asyncio

    import uvloop

    pin(cpu)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key)
    ctx.set_alpn_protocols(["h2", "http/1.1"])

    async def main():
        loop = asyncio.get_running_loop()
        stop = loop.create_future()
        loop.add_reader(pipe.fileno(), lambda: stop.done() or stop.set_result(None))
        server = await loop.create_server(_OrderProtocol, "127.0.0.1", 0, ssl=ctx)
        pipe.send(server.sockets[0].getsockname()[1])
        await stop
        server.close()

    uvloop.run(main())


class OrderServer:
    """A local HTTPS server (`_OrderProtocol` on uvloop, in its own process) that answers
    every request with a canned order acknowledgement. `ssl_context()` trusts its
    certificate."""

    def __init__(self, cpu: int = 2):
        self._dir = tempfile.TemporaryDirectory()
        self.cert, key = self_signed(Path(self._dir.name))
        ctx = multiprocessing.get_context("spawn")
        self._pipe, child = ctx.Pipe()
        self._proc = ctx.Process(target=_serve, args=(child, str(self.cert), str(key), cpu),
                                 daemon=True)
        self._proc.start()
        self.port = _recv(self._pipe, self._proc)
        self.url = f"https://127.0.0.1:{self.port}{ORDER_PATH}"

    def ssl_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self.cert))

    def __enter__(self) -> OrderServer:
        return self

    def __exit__(self, *exc) -> None:
        self._pipe.send("stop")
        self._proc.join(5)
        if self._proc.is_alive():
            self._proc.kill()
        self._dir.cleanup()
