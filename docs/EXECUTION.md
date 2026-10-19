# Execution

How orders reach Kalshi as fast and as safely as possible. M4 benchmarks the options
and M5 builds the engine on the winner. Both write their results and decisions here.

## Order book structure (decided in M1)

`kalshi.models.BookSide` holds one side of a market's bids: a dict of sizes plus a
two-level price bitmap, so finding the best level takes O(1) `int.bit_length` calls.
Measured with `python benchmarks/book.py` (macOS 15.7, Apple M4 Pro, CPython 3.13.5). The
workloads are synthetic until recorded WebSocket deltas exist. The script prints their
shape: the best price moves on 18% of churn ops, and books hold a median of 13, 24, and
103 levels.

| ns per update+best | churn 1¢ | churn 0.1¢ | churn, ~100-level book | below-touch churn | best empties over 98¢ gap | KiB/side 40 lvls | KiB after churn |
|---|--:|--:|--:|--:|--:|--:|--:|
| **dict + price bitmap (chosen)** | 246 | 238 | 234 | 293 | 285 | 4.7 | 1.6 |
| dense array + bitmap | 193 | 189 | 190 | 237 | 233 | 85.5 | 84.5 |
| array + downward scan | 930 | 232 | 864 | 79 | 162,726 | 83.2 | 83.2 |
| lazy-deletion heap | 181 | 175 | 169 | 122 | 139 | 4.0 | 3,907.5 |
| SortedDict | 396 | 396 | 381 | 456 | 452 | 4.3 | 1.9 |
| dict + max() | 232 | 290 | 755 | 101 | 100 | 2.4 | 0.3 |

The chosen structure is 44–65 ns slower per update than the dense array, and 57–171 ns
slower than the heap, in CPython. It wins on the properties that matter at scale:
- Memory and worst case stay bounded together. The heap's stale entries grew to 3.8 MiB
  per side under below-touch churn. The dense array uses 18× the memory (85.5 vs
  4.7 KiB per side), which adds up when holding many books (recording, cross-market
  scans).
- The scan ladder takes ~0.16 ms when the best empties across a gap. `dict + max()`
  triples its cost on a ~100-level book (755 ns).
- It lists levels best-first without sorting, which snapshots and depth checks need.
- Tens of nanoseconds are noise next to a network round trip measured in milliseconds.

M4 measured the whole hot path (decode → book → decide → sign) and kept the book in
Python for now; see Hot path.

## Hot path (measured in M4)

Run on 2026-10-19 on a cloud VM: Linux 6.18 (Firecracker guest), Intel Xeon @ 2.10 GHz,
4 vCPUs, CPython 3.13.16 (3.14.6 and 3.14.6t for the GC rows that say so). Package
versions: `[bench]` extra as installed then (orjson 3.13.0, msgspec 0.22.0, uvloop
0.22.1, picows 2.3.1, aiohttp 3.14.5, httpx 0.28.1, h2 4.4.1, websockets 17.2,
cryptography 50.0.2 on OpenSSL 4.0.3). Each script in `benchmarks/` prints its own
platform header and reproduces its table. Every script ran three times; tables give the
median of the three runs.

### Method

- **Synthetic feed.** `harness.Stream` writes Kalshi's WebSocket formats (RESEARCH §1):
  snapshots for 50 markets 5–100 levels deep per side, then deltas (85%), the trades
  behind some of them (14%), and our fills (1%), as compact JSON. Median frames: delta
  174 bytes, trade 233, fill 407, snapshot 1,399. Recorded frames replace these once the
  recorder has them.
- **Open loop, corrected for coordinated omission.** Every item has a scheduled time,
  and latency runs from that time to the item being done. An item queued behind a slow
  one is charged for the wait, which closed-loop timing hides. In-process benchmarks
  spin until each item is due. Network benchmarks replay from a separate process
  (`harness.Sender`), with bursts sent in one write.
- **Schedules.** Both have the same mean of 2,000 frames/s. *Steady* is Poisson.
  *Bursty* is Poisson at 1,000/s plus 200 frames at once every 200 ms, so half the
  frames arrive in bursts and wait for the ones ahead of them. Orders use their own
  schedules (below).
- **Fair comparison.** Candidates run in rotated rounds, each first equally often, after
  an untimed warm-up, and samples are pooled. Sender, client and order server are pinned
  to separate CPUs.
- **Host noise.** The hypervisor steals 0.3–0.9% of CPU time, in stalls of up to tens of
  ms: in these runs, the harness floor (a no-op) reached 40 ms and the sender's own lag
  43 ms. Each stall queues every item due during it. Across the three runs, most CPU and
  p50 figures varied within ±10%, but a noisy run reached +80% (decoding trades and
  fills in run 1) and some p99s varied up to 9×. Every comparison below holds in each
  run, not just in the medians. **Compare on CPU per item and p50**, use p99 only where
  the gap is large, and don't use p99.9 or max at all. For the tails, rerun on dedicated
  hardware (bare metal, or the M4 Pro used for the book). Percentiles with fewer than
  ten samples beyond them print as —.

### Decoding (`decode.py`)

ns per frame, decode plus reading the fields the next stage needs (median of 7
interleaved batches per run):

| decoder | snapshot | delta | trade | fill | bursty open loop: mean µs | p50 µs |
|---|--:|--:|--:|--:|--:|--:|
| json (stdlib) | 10,437 | 3,109 | 3,441 | 4,208 | 228 | 36.4 |
| orjson | 6,621 | 848 | 985 | 1,301 | 99 | 17.1 |
| msgspec untyped | 6,735 | 797 | 992 | 1,251 | 79 | 14.0 |
| **msgspec typed structs (chosen)** | 6,099 | 485 | 678 | 805 | 67 | 14.0 |

Typed structs are fastest on every message type: 1.7× orjson on deltas, and 6.4× stdlib
json. They also validate the schema while decoding, and with `gc=False` they are objects
the collector never tracks (see GC). The open-loop columns are 40,000 frames on the
bursty schedule. The harness floor there is mean 38 µs and p50 4.8 µs, so host stalls
carry much of each mean.

### Signing (`sign.py`)

| key type | µs per signed request, hot loop | after 1 ms idle | after decoding 100 frames | open loop: p50 µs | p99 µs |
|---|--:|--:|--:|--:|--:|
| **Ed25519 (chosen)** | 39.2 | 76.8 | 52.5 | 65 | 1,040 |
| RSA-2048 PSS | 414.8 | 450.5 | 394.3 | 446 | 8,469 |

The open loop covers every order on a schedule of Poisson 200/s plus 20 orders at once
every 500 ms. RSA costs 10.6× as much in a hot loop, so a 20-order requote spends about
8 ms on signatures alone. A trading process signs only now and then. A 1 ms idle gap
before a signature adds about 37 µs on either key (Ed25519 twice its hot-loop cost);
decoding in between costs less. **The trading key should be Ed25519**, Kalshi's default
for new keys (RESEARCH §1).

### Event loop (`loop.py`)

A minimal protocol reader isolates the loop. "Callback" consumes each frame in
`data_received`, as picows does; "queue hop" adds the `asyncio.Queue` wakeup that
websockets and aiohttp put between the socket and the caller.

| loop / consumer | CPU µs/frame steady | bursty | bursty p50 µs |
|---|--:|--:|--:|
| asyncio / callback | 35.9 | 24.2 | 139 |
| asyncio / queue hop | 49.9 | 33.4 | 291 |
| **uvloop / callback** | 27.4 | 19.1 | 136 |
| uvloop / queue hop | 42.2 | 26.9 | 262 |

uvloop saves 5–9 µs of CPU per frame (15–24%). The queue hop costs more than the choice
of loop: it adds 8–15 µs per frame and doubles the bursty p50. **Use uvloop with a
callback-style client.**

### WebSocket client (`ws_clients.py`)

websockets is read the way `kalshi/ws.py` reads it, as str. aiohttp also delivers str.
picows hands over bytes, which both JSON decoders accept directly.

| client / loop | CPU µs/frame steady | bursty | steady p50 µs | bursty p50 µs |
|---|--:|--:|--:|--:|
| websockets / asyncio (incumbent) | 63.9 | 45.2 | 88.1 | 466 |
| websockets / uvloop | 54.1 | 40.2 | 78.4 | 445 |
| aiohttp / asyncio | 53.0 | 37.4 | 72.2 | 199 |
| aiohttp / uvloop | 40.9 | 29.2 | 61.3 | 183 |
| picows / asyncio | 33.6 | 24.5 | 57.3 | 129 |
| **picows / uvloop (chosen)** | 27.1 | 19.1 | 50.3 | 116 |

picows on uvloop uses 42% of the incumbent's CPU per frame, and its bursty p50 is a
quarter of the incumbent's. That is the same per-frame CPU as the minimal reader above,
so the client adds almost nothing. websockets' burst latency points to flow control, not
CPU: aiohttp on asyncio uses less CPU than websockets on uvloop (37.4 vs 40.2 µs) yet
has under half its bursty p50 (199 vs 445 µs). The likely cause is websockets' default
`max_queue=16`, which stops reading from the socket mid-burst. Caveat: picows delivers
frames, not messages, so a production listener must join continuation frames.

### Order HTTP client (`http_clients.py`)

Orders were POSTed to a local TLS server (HTTP/1.1 and HTTP/2 by ALPN), triggered open
loop at Poisson 200/s plus 10 at once every 250 ms. Pools were warm, and all clients ran
on uvloop. 9,996 orders per client per run.

| client | p50 µs | p99 µs | CPU µs/order |
|---|--:|--:|--:|
| httpx HTTP/1.1 | 2,434 | 35,822 | 1,647 |
| httpx HTTP/2 | 2,224 | 17,715 | 1,249 |
| **aiohttp HTTP/1.1 (chosen)** | 785 | 3,417 | 423 |

httpx costs 3–3.9× aiohttp's CPU per order, and HTTP/2's multiplexing doesn't recover
that cost. One trap: without `sniffio` installed, httpcore retries a failed import on
every async request. A one-off check of sequential requests put it at about 0.2 ms each
(1.11 vs 0.89 ms); the `[bench]` extra installs sniffio. **Send orders with aiohttp** on
a warm pool. Re-measure HTTP/2 against Kalshi itself in the round-trip benchmark, since
CloudFront's handling of HTTP/2 can't be simulated locally.

### Garbage collector (`gc_pauses.py`)

| interpreter, heap held | one full collection | after `gc.freeze()` | full collection every (json dicts kept) |
|---|--:|--:|--:|
| 3.13, interpreter only (52k tracked objects) | 8.9 ms | 0.0 ms | 6.5 s |
| 3.13, + 1M dicts (2.05M tracked) | 384 ms | 0.0 ms | 257 s |
| 3.14, + 1M dicts (3.06M tracked), one run | 400 ms | 0.0 ms | 191 s |
| 3.14t free-threaded, + 1M dicts (3.07M tracked), one run | 164 ms | 86 ms | 192 s |

The interval assumes 2,000 msgs/s with every decoded message kept (a tape, a history).
CPython collects in full once its long-lived containers have grown by a quarter. A kept
dict message adds 1.0 tracked container on 3.13 and 2.0 on 3.14. So a process that keeps
dicts stalls for a whole-heap collection every few minutes, and with a large heap that
takes 0.4 s. `gc=False` structs keep nothing the collector tracks, so they never move
it.

In the replays, each starting right after a full collection, the only pauses on 3.13
were young-generation ones: 44 per 40,000 kept dict messages and 4 for structs, at most
0.15–0.6 ms except one of 1.8 ms. With the 1M-dict heap and dicts kept, default also had
the highest p50 in all three runs (88–94 µs, against 78–86 for freeze), a gap the
measured pauses don't explain. Otherwise the settings differed only by host noise. On
3.14t a full collection did land in one replay, a 12 ms pause (that table's larger tails
are host stalls, which one 12 ms pause can't produce). There freeze only halves a full
collection, though an unfrozen one is faster than on 3.14. **Call `gc.freeze()` once
startup state is loaded, keep the hot path on `gc=False` structs, and leave the
collector enabled** (disabling it lets any accidental cycle leak). Free-threading brings
nothing to this single-threaded hot path, and with freeze its collector is the weaker
one.

### P1: the pure-Python hot path (`hot_path.py`)

Two stacks consume the same replay, keep every book, and run one decision rule: an IOC
buy at the ask when a market's ask first drops a tick below its snapshot's. That fired
135 times per 10,000 frames. Each order is signed, serialized and POSTed to the local
TLS server without blocking the feed, and both stacks must make identical decisions.
- **Incumbent:** asyncio, websockets via `kalshi.ws.Connection`, json, `BookFeed`,
  httpx.
- **P1:** uvloop, picows, msgspec structs into `StructFeed` (the same `OrderBook`),
  aiohttp, and `gc.freeze()`.

p50 in µs, 4 × 10,000 frames (540 orders) per stack per run:

| | incumbent steady | incumbent bursty | **P1 steady** | **P1 bursty** |
|---|--:|--:|--:|--:|
| frame → processed | 134 | 1,180 | 75 | 262 |
| burst of 200 → its last frame processed | | 3,930 | | 1,860 |
| frame → signed order (tick-to-order) | 293 | 1,497 | 227 | 423 |
| frame → ack (loopback TLS) | 2,977 | 8,790 | 1,173 | 2,631 |
| CPU µs per frame | 124.5 | 102.1 | 57.4 | 43.0 |
| stage: decode | 7.4 | 4.9 | 2.2 | 1.2 |
| stage: book | 9.7 | 6.0 | 10.3 | 5.4 |
| stage: decide + sign, per order | 144 | 134 | 143 | 129 |

P1 cuts CPU per frame 2.2–2.4× and tick-to-order 1.3× (steady) to 3.5× (bursty). Bursty
frame → processed p99 is 5.8 ms for the incumbent and 3.1 ms for P1. A burst costs P1
9.3 µs per frame. P1's bursty CPU per frame adds up as follows:
- picows on uvloop: 19.1 µs (measured alone above);
- decode and book: 2.6 and 10.3 µs (means; snapshots weigh on the book);
- signing and POSTing 1.35 orders per 100 frames: about 7.4 µs;
- the rest, about 4 µs: the stack's own overhead and its timing.

Isolated wakeups cost more than bursts: on the steady schedule decode and book double.
Decide + sign costs about 130–145 µs per order, over 3× signing's hot-loop figure, even
mid-burst with no idle gap. Deciding and encoding the body add a few µs, and the rest is
unexplained (neither of `sign.py`'s contexts reproduces it).

### Recommendation for M4's decision

- **Components for M5:** uvloop, picows with a callback listener, msgspec typed
  `gc=False` structs, the existing `OrderBook`/`BookSide`, Ed25519, aiohttp on a warm
  pool, and `gc.freeze()` after startup.
- **Keep the book in Python for now.** The book stage takes a quarter of P1's CPU per
  frame. Inside a burst it is over half of the 9.3 µs a frame costs, and it includes
  parsing the price and size strings. A native book would bring a 200-frame burst's
  drain from about 1.9 ms toward 1 ms. That is worth having only if the order round trip
  from us-east-2 is around a millisecond, and only if real bursts are this large;
  against the milliseconds from elsewhere (Hosting) it is noise.
- **Next measurements, which settle it:** profile the book stage first (a price-string
  cache is the cheap Python fix to try). Then replay recorded bursts through P1 in place
  of the synthetic ones, and measure the order round trip from us-east-2 (the user's
  part of M4). Rust, or NautilusTrader's native book, becomes the M4 prototype to build
  only if both measurements say a millisecond matters.

## Hosting (decided in M4)

- Kalshi runs in **AWS us-east-2 (Ohio)** behind CloudFront. The trading host is
  chosen from measured round-trip times (M4), not cost. us-east-2 is the obvious
  candidate.
- Costed earlier (Oct 2026): Azure East US / East US 2 are about 12–17 ms from Kalshi.
  That's acceptable for collectors, but it adds latency to every order.
- Cost gotchas that apply on any cloud:
  - Log ingestion is the classic surprise bill. Never log order-book deltas to stdout,
    and set a daily cap.
  - A private network with no outbound route can't reach Kalshi; a VM needs a public
    IP or NAT.
  - Kalshi doesn't allowlist IPs, so changing outbound IPs is fine.
