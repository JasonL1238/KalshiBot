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

The whole hot path (decode → book → decide → sign), and whether the book should move to
Rust, is measured in the next section.

## Hot path (M4 component benchmarks and P1)

Run on 2026-10-19 on a cloud VM: Linux 6.18 (Firecracker guest), Intel Xeon @ 2.10 GHz,
4 vCPUs, CPython 3.13.16 unless noted. Package versions: `[bench]` extra as installed
then (orjson 3.13.0, msgspec 0.22.0, uvloop 0.22.1, picows 2.3.1, aiohttp 3.14.5, httpx
0.28.1, h2 4.4.1, websockets 17.2, cryptography 50.0.2 on OpenSSL 4.0.3). Each script in
`benchmarks/` prints its own platform header and reproduces its table.

### Method

- **Synthetic feed.** `harness.Stream` writes Kalshi's WebSocket formats (RESEARCH §1):
  snapshots for 50 markets 5–100 levels deep per side, then deltas (85%), the trades
  behind some of them (14%), and our fills (1%), as compact JSON. Median frames: delta 174
  bytes, trade 233, fill 407, snapshot 1,399. Recorded frames replace these once the
  recorder has them. The frames omit `sending_ts_ms`.
- **Open loop, corrected for coordinated omission.** Every item has a scheduled time,
  and latency runs from that time to the item being done. An item queued behind a slow
  one is charged for the wait, which closed-loop timing hides. In-process benchmarks
  spin until each item is due. Network benchmarks replay from a separate process
  (`harness.Sender`), with bursts sent in one write.
- **Schedules.** Both have the same mean of 2,000 frames/s. *Steady* is Poisson. *Bursty*
  is Poisson at 1,000/s plus 200 frames at once every 200 ms. In a burst, the last frame
  waits for the 199 before it, so bursty percentiles mostly measure how fast a candidate
  drains a burst. Orders use their own schedules (below).
- **Fair comparison.** Candidates run in rotated rounds, each first equally often, after
  an untimed warm-up, and samples are pooled. Sender, client and order server are pinned
  to separate CPUs. Tests check that decoders agree, that feeds rebuild the same books,
  and that every replayed frame arrives byte for byte.
- **Host noise.** On this VM the hypervisor steals 0.4–0.8% of CPU time, in stalls of up
  to tens of ms: a pure spin loop loses about 2% of wall time to gaps of up to 8 ms. Every
  table carries a harness floor (a no-op) or the sender's own lag. **Read p50, CPU per
  item and p99; p99.9 and max above ~2 ms are mostly the host.** Percentiles with fewer
  than ten samples beyond them print as —. For the tail columns, rerun on dedicated
  hardware (bare metal, or the M4 Pro used for the book).

### Decoding (`decode.py`)

ns per frame, decode plus reading the fields the next stage needs (median of 7
interleaved batches):

| decoder | snapshot | delta | trade | fill |
|---|--:|--:|--:|--:|
| json (stdlib) | 9,994 | 2,879 | 3,167 | 4,143 |
| orjson | 6,117 | 768 | 910 | 1,361 |
| msgspec untyped | 6,389 | 712 | 930 | 1,281 |
| **msgspec typed structs (chosen)** | 5,943 | 463 | 521 | 784 |

Open loop, bursty schedule, 40,000 frames, µs (harness floor: p50 4.7, p99 138):

| decoder | p50 | p99 | p99.9 |
|---|--:|--:|--:|
| json (stdlib) | 38.5 | 1,379 | 25,120 |
| orjson | 18.5 | 2,533 | 40,925 |
| msgspec untyped | 16.3 | 465 | 1,827 |
| **msgspec typed structs** | 13.9 | 466 | 1,163 |

Typed structs are fastest on every message type: 1.7× orjson on deltas and 6× stdlib
json. They also validate the schema while decoding, and with `gc=False` they are
objects the collector never tracks (see GC). The stdlib and orjson p99.9 values are
host stalls that landed in their rounds.

### Signing (`sign.py`)

| key type | µs per signed request | burst of 20 orders: p50 | p99 |
|---|--:|--:|--:|
| **Ed25519 (chosen)** | 41.7 | 70 µs | 1.2 ms |
| RSA-2048 PSS | 441.4 | 453 µs | 8.8 ms |

The schedule was Poisson 200/s plus 20 orders at once every 500 ms. RSA costs 10.6× as
much, so a 20-order requote spends 8.8 ms on signatures alone. **The trading key should
be Ed25519**, Kalshi's default since 2026-10-01.

### Event loop (`loop.py`)

A minimal protocol reader isolates the loop. "Callback" consumes each frame in
`data_received`, as picows does; "queue hop" adds the `asyncio.Queue` wakeup that
websockets and aiohttp put between the socket and the caller.

| loop / consumer | CPU µs/frame steady | bursty | bursty p50 µs | bursty p99 µs |
|---|--:|--:|--:|--:|
| asyncio / callback | 36.0 | 24.6 | 142 | 1,700 |
| asyncio / queue hop | 49.9 | 33.8 | 294 | 1,799 |
| **uvloop / callback** | 29.0 | 19.2 | 136 | 945 |
| uvloop / queue hop | 43.3 | 29.0 | 292 | 2,123 |

uvloop saves 5–7 µs of CPU per frame (about 20%). The queue hop costs more than the loop
choice: it adds 9–14 µs per frame and doubles the bursty p50. **Use uvloop, with a
callback-style client.**

### WebSocket client (`ws_clients.py`)

websockets is read the way `kalshi/ws.py` reads it, as str. aiohttp also delivers str.
picows hands over bytes, which both JSON decoders accept directly.

| client / loop | CPU µs/frame steady | bursty | steady p50 µs | bursty p50 µs | bursty p99 µs |
|---|--:|--:|--:|--:|--:|
| websockets / asyncio (incumbent) | 67.0 | 45.7 | 95.6 | 458 | 3,513 |
| websockets / uvloop | 58.1 | 40.5 | 86.0 | 515 | 2,713 |
| aiohttp / asyncio | 57.7 | 36.1 | 85.0 | 195 | 2,263 |
| aiohttp / uvloop | 44.5 | 28.2 | 71.3 | 179 | 1,338 |
| picows / asyncio | 36.9 | 24.8 | 62.5 | 136 | 3,062 |
| **picows / uvloop (chosen)** | 29.0 | 19.4 | 56.0 | 120 | 1,346 |

picows on uvloop uses 43% of the incumbent's CPU per frame. It drains bursts 3.8×
faster at p50: websockets' default `max_queue=16` stops reading mid-burst. picows has
the same per-frame cost as the minimal reader above, so the client adds almost nothing.
Caveat: picows delivers frames, not messages, so a production listener must join
continuation frames.

### Order HTTP client (`http_clients.py`)

Orders were POSTed to a local TLS server (HTTP/1.1 and HTTP/2 by ALPN), triggered open
loop at Poisson 200/s plus 10 at once every 250 ms. Pools were warm, and all clients ran
on uvloop. 9,996 orders per client.

| client | p50 µs | p99 µs | CPU µs/order |
|---|--:|--:|--:|
| httpx HTTP/1.1 | 2,479 | 34,692 | 1,698 |
| httpx HTTP/2 | 2,187 | 17,810 | 1,231 |
| **aiohttp HTTP/1.1 (chosen)** | 795 | 5,857 | 430 |

httpx costs 2.9–4× aiohttp's CPU per order. HTTP/2's multiplexing doesn't recover that
cost. A trap: without `sniffio` installed, httpcore retries a failed import on every
async request, about 0.2 ms each (the `[bench]` extra installs it). **Send orders with
aiohttp** on a warm pool. Re-measure HTTP/2 against Kalshi itself in the round-trip
benchmark, since CloudFront's handling of HTTP/2 can't be simulated locally.

### Garbage collector (`gc_pauses.py`)

| heap held | one full collection | after `gc.freeze()` | json dicts kept: full collection every | structs kept |
|---|--:|--:|--:|--:|
| interpreter only (52k tracked objects) | 10.6 ms | 0.0 ms | 6.5 s | never |
| + 1M dicts (2.05M tracked) | 366 ms | 0.0 ms | 257 s | never |

The interval assumes 2,000 msgs/s. CPython collects in full once its long-lived
containers have grown by a quarter. A process that keeps decoded dict messages (a tape,
a history) therefore stalls for a whole-heap collection every few minutes, and with a
large heap that collection takes a third of a second. `gc=False` structs keep nothing
the collector tracks, so they never move it. In the replays (each starts right after a
full collection), the only pauses were young-generation ones, 44 per 40,000 dict
messages kept and 4 for structs. They took at most 0.2–0.4 ms, apart from one 8.3 ms
outlier under freeze, which can't slow a young collection, so most likely a host stall. Default, freeze and disable differed
only by host noise. **Call `gc.freeze()` once startup state is loaded, keep the hot path
on `gc=False` structs, and leave the collector enabled** (disabling it lets any
accidental cycle leak). On CPython 3.14 a kept dict message holds 2.0 tracked
containers, not 1.0, which halves the interval.

### P1: the pure-Python hot path (`hot_path.py`)

Two stacks consume the same replay, keep every book, and run one decision rule: an IOC
buy at the ask when a market's ask first drops a tick below its snapshot's. That fired
135 times per 10,000 frames. Each order is signed, serialized and POSTed to the local
TLS server without blocking the feed, and both stacks must make identical decisions.
- **Incumbent:** asyncio, websockets via `kalshi.ws.Connection`, json, `BookFeed`, httpx.
- **P1:** uvloop, picows, msgspec structs into `StructFeed` (the same `OrderBook`), aiohttp,
  and `gc.freeze()`.

4 × 10,000 frames per stack (540 orders), µs:

| bursty schedule | incumbent p50 | p99 | P1 p50 | p99 |
|---|--:|--:|--:|--:|
| frame → processed | 1,226 | 6,164 | **257** | 2,741 |
| frame → signed order | 1,465 | — | **401** | — |
| frame → ack (loopback TLS) | 9,128 | — | **2,633** | — |
| CPU per frame | 103.5 | | **43.3** | |

| steady schedule | incumbent p50 | P1 p50 |
|---|--:|--:|
| frame → processed | 146 | **80** |
| frame → signed order | 296 | **224** |
| frame → ack (loopback TLS) | 3,044 | **1,151** |
| CPU per frame | 127.6 | **60.1** |

Stage costs per frame, P1, p50 (bursty / steady): decode 1.2 / 2.4 µs, book 5.3 /
10.5 µs (its p99, about 50 µs, is the snapshots), decide + sign 127 / 140 µs per
order. Of each frame's 43 µs of CPU, decode and book take under 7 µs. The order POSTs take
about 6 µs (aiohttp's 430 µs for each of 1.35 orders per 100 frames). Transport and loop
take the remaining ~30 µs. Signing in context costs about 3× the 42 µs microbenchmark,
and decode and book double between bursts. Code that runs only now and then, between
idle waits, finds its caches cold.

### Recommendation for M4's decision

- **Components for M5:** uvloop, picows with a callback listener, msgspec typed
  `gc=False` structs, the existing `OrderBook`/`BookSide`, Ed25519, aiohttp on a warm
  pool, and `gc.freeze()` after startup. Together they cut CPU per frame 2.4× and
  tick-to-order p50 3.7× under bursts.
- **Don't move the book to Rust now.** P1 spends 5 µs of a frame's 43 µs in the book,
  and 1–2 µs in decoding. The rest is transport, loop, and signing (127 µs per order).
  A Rust book would save under 5 µs per frame, against an order round trip of
  milliseconds: 12–17 ms from Azure East US; from us-east-2 not yet measured.
- **What would change that:** burst drain. A 200-frame burst takes about 9 ms to drain
  in P1, against about 21 ms for the incumbent, and the last frame waits the whole time.
  If recorded bursts are that large and the us-east-2 round trip is around 1 ms, a
  native feed handler (Rust, or NautilusTrader's) that drains a burst in well under a
  millisecond would pay. Two measurements settle it: replay recorded bursts through P1
  in place of synthetic ones, and measure the order round trip from us-east-2 (the
  user's part of M4).

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
