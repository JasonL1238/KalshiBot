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

P1's stage costs bound what a Rust book could save; P2 measures it (Hot path, below:
P1 is the pure-Python hot path, P2 the book in Rust, P3 NautilusTrader).

## Hot path (measured in M4)

Every script in `benchmarks/` but book.py ran three times at commit e20d5df on one host:
a cloud VM, Linux 6.18 (Firecracker guest), Intel Xeon @ 2.10 GHz, 4 vCPUs, CPython
3.13.16, with gc_pauses also once on 3.14.6 and 3.14.6t. The command and the raw
outputs, each with its platform header and package versions, are in
`runs/m4-benchmarks/` (`plan.md`). Tables give the median of the three runs.

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
  frames arrive in bursts and wait for the ones ahead of them. P1 adds a *reconnect*:
  steady traffic over 500 markets, with every market's snapshot again at one instant
  mid-replay, as after a resubscribe. Orders use their own schedules (below).
- **Fair comparison.** Candidates run in rotated rounds, each first equally often, after
  an untimed warm-up, and samples are pooled. Sender, client and order server are pinned
  to separate cores, as the guest OS reports them (a VM can't see its host's).
- **Host noise.** The hypervisor steals 0.2–0.8% of CPU time, in stalls of up to
  hundreds of ms. Each output prints the harness floor (a no-op) or the sender's own lag
  next to its rows; their max reached 373 ms once. Across the three runs, most CPU and
  p50 figures varied within ±10%, and some p99s and p99.9s several-fold. Every
  comparison below holds in each run. **Compare on CPU per item and p50**, use p99 only
  where the gap is large, and read p99.9 and max as host-bound. Rerun on dedicated
  hardware for the tails.

### Decoding (`decode.py`)

ns per frame, decode plus reading the fields the next stage needs (median of 7
interleaved batches per run), and the bursty open loop (40,000 frames; harness floor
mean 32 µs, p50 4.2 µs):

| decoder | snapshot | delta | trade | fill | bursty mean µs | p50 µs |
|---|--:|--:|--:|--:|--:|--:|
| json (stdlib) | 12,158 | 3,464 | 3,378 | 4,358 | 238 | 37.0 |
| orjson | 7,100 | 1,066 | 965 | 1,440 | 88 | 16.8 |
| msgspec untyped | 7,483 | 1,049 | 988 | 1,347 | 78 | 14.6 |
| **msgspec typed structs (chosen)** | 6,974 | 559 | 570 | 845 | 76 | 13.8 |

Typed structs are fastest on deltas, trades and fills in every run, and on snapshots in
two of three: 1.9× orjson on deltas, and 6.2× stdlib json. They also validate the schema
while decoding, and with `gc=False` they are objects the collector never tracks (see
GC).

### Signing (`sign.py`)

| key type | µs per signed request: hot loop | after 1 ms idle | after decoding 100 frames | open loop, all orders: p50 µs | p99 µs |
|---|--:|--:|--:|--:|--:|
| **Ed25519 (chosen)** | 40.1 | 79.1 | 52.9 | 65 | 1,048 |
| RSA-2048 PSS | 412.1 | 480.9 | 421.2 | 462 | 8,951 |

The open loop pools every order of a schedule of Poisson 200/s plus 20 orders at once
every 500 ms. RSA costs 10.3× as much in a hot loop, so a 20-order requote spends about
8 ms on signatures. An Ed25519 signature between other work costs 1.3× the hot loop, and
after an idle gap 2×; the gap adds 40–70 µs on either key. **The trading key should be
Ed25519.**

### Event loop and WebSocket client (`ws_clients.py`)

websockets is read the way `kalshi/ws.py` reads it, as str. aiohttp also delivers str.
picows hands over bytes, which both JSON decoders accept directly. The bare reader
parses frames itself, so it measures the loop alone: "callback" consumes each frame in
`data_received`, as picows does; "queue hop" adds the `asyncio.Queue` wakeup that
websockets and aiohttp put between the socket and the caller.

| client / loop | CPU µs/frame steady | bursty | steady p50 µs | bursty p50 µs |
|---|--:|--:|--:|--:|
| websockets / asyncio (incumbent) | 70.4 | 49.0 | 96.9 | 831 |
| websockets / uvloop | 59.1 | 42.2 | 84.3 | 787 |
| aiohttp / asyncio | 59.9 | 41.3 | 82.7 | 222 |
| aiohttp / uvloop | 45.2 | 31.7 | 68.9 | 206 |
| picows / asyncio | 38.1 | 27.2 | 61.4 | 147 |
| **picows / uvloop (chosen)** | 29.1 | 21.1 | 54.8 | 134 |
| bare reader, callback / asyncio | 44.2 | 27.9 | 66.1 | 156 |
| bare reader, callback / uvloop | 29.3 | 21.2 | 56.2 | 149 |
| bare reader, queue hop / asyncio | 55.4 | 36.9 | 80.2 | 335 |
| bare reader, queue hop / uvloop | 44.4 | 30.1 | 69.1 | 333 |

- uvloop saves 6–15 µs of CPU per frame, 14–34% depending on the client.
- The queue hop costs 9–15 µs per frame and doubles the bursty p50; on bursty p50 it
  matters more than the loop choice.
- picows on uvloop costs what the bare reader does, so the client adds almost nothing.
  It uses 41–43% of the incumbent's CPU per frame, and its bursty p50 is a sixth of the
  incumbent's.
- websockets' burst latency points to flow control, not CPU. aiohttp on asyncio uses
  less CPU than websockets on uvloop (41.3 vs 42.2 µs) yet has under a third of its
  bursty p50. The likely cause is websockets' default `max_queue=16`, which stops
  reading from the socket mid-burst.

**Use uvloop with picows.** picows delivers frames, not messages, so a production
listener must join continuation frames.

### Order HTTP client (`http_clients.py`)

Orders were POSTed to a local TLS server (HTTP/1.1 and HTTP/2 by ALPN), triggered open
loop at Poisson 200/s plus 10 at once every 250 ms. Pools were warm, and all clients ran
on uvloop. 9,996 orders per client per run.

| client | p50 µs | p99 µs | CPU µs/order |
|---|--:|--:|--:|
| httpx HTTP/1.1 | 2,720 | 37,932 | 1,815 |
| httpx HTTP/2 | 2,337 | 16,673 | 1,314 |
| **aiohttp HTTP/1.1 (chosen)** | 812 | 3,221 | 447 |

httpx costs 2.9–4.1× aiohttp's CPU per order, and HTTP/2's multiplexing doesn't recover
that cost. One trap: without `sniffio` installed, httpcore retries a failed import on
every async request. A one-off check of sequential requests put it at about 0.2 ms each
(1.11 vs 0.89 ms); the `[bench]` extra installs sniffio. **Send orders with aiohttp** on
a warm pool. Re-measure HTTP/2 against Kalshi itself in the round-trip benchmark, since
CloudFront's handling of HTTP/2 can't be simulated locally.

### Garbage collector (`gc_pauses.py`)

| interpreter, heap held | one full collection | after `gc.freeze()` | full collection every (json dicts kept) | longest full pause while the heap grows by half (≥200k containers) | the same after freeze |
|---|--:|--:|--:|--:|--:|
| 3.13, interpreter only (52k tracked) | 9.4 ms | 0.0 ms | 6.5 s | none | none |
| 3.13, + 1M dicts (2.05M tracked) | 421 ms | 0.0 ms | 257 s | 454 ms | 39 ms (3 of them) |
| 3.14, + 1M dicts (3.06M tracked), one run | 459 ms | 0.0 ms | 191 s | 532 ms | 93 ms (5 of them) |
| 3.14t free-threaded, + 1M dicts (3.07M tracked), one run | 163 ms | 86 ms | 192 s | 202 ms | none |

- **When full collections come.** CPython collects in full once its long-lived
  containers have grown by a quarter since the last full collection. 3.14.6 behaves as
  3.13 here: its growth test ran 764 collections, one of them full. A process that keeps
  its decoded dict messages (a tape, a history) therefore stalls for a whole-heap
  collection every few minutes. With a large heap that takes 0.4–0.5 s. A kept dict
  message adds 1.0 tracked container on 3.13 and 2.0 on 3.14; `gc=False` structs keep
  nothing the collector tracks, so they never move it.
- **What freeze does.** It takes the heap out of every collection, but kept containers
  still accumulate. After freeze, the quarter is measured against the unfrozen
  survivors, which are few, so full collections come more often but walk only what was
  kept since: 3 of up to 39 ms instead of one of 454 ms on 3.13, and in total 111 ms
  instead of 495 ms. On 3.14t, freeze only halves an explicit full collection (163 to
  86 ms), though its growth test then triggered none.
- **In the replays,** each starting right after a full collection, the only pauses were
  young ones: 44 per 40,000 kept dict messages, at most 0.25 ms with the large heap and
  0.7 ms without, and none for structs. The settings differed only by noise.

**Call `gc.freeze()` once startup state is loaded, keep the hot path on `gc=False`
structs, and leave the collector enabled** (disabling it lets any accidental cycle
leak). Free-threading brings nothing to this single-threaded hot path, and a forced full
collection there stays costly after freeze.

### P1: the pure-Python hot path (`hot_path.py`)

Two stacks consume the same replay, keep every book, and run one decision rule: an IOC
buy at the ask when a market's ask first drops a tick below its first snapshot's. It
fired 135 times per 10,000 frames over 50 markets, 286 over the reconnect's 500. Each
order is signed, serialized and POSTed to the local TLS server without blocking the
feed, and both stacks must make identical decisions.
- **Incumbent:** asyncio, websockets via `kalshi.ws.Connection`, json, `BookFeed`,
  httpx.
- **P1:** the chosen row of each table above: uvloop, picows, msgspec structs into
  `StructFeed` (the same `OrderBook`), Ed25519, aiohttp, and `gc.freeze()`.

µs, 4 × 10,000 frames per stack per run:

| | incumbent steady | bursty | reconnect | **P1 steady** | **bursty** | **reconnect** |
|---|--:|--:|--:|--:|--:|--:|
| frame → processed, p50 | 141 | 1,245 | 189 | 82 | 264 | 97 |
| frame → processed, p99 | 1,597 | 5,901 | 92,099 | 844 | 2,856 | 69,786 |
| frame → processed, p99.9 | 14,914 | 8,980 | 140,610 | 7,669 | 3,704 | 84,610 |
| burst → its last frame done, p50 | | 4,212 | 93,780 | | 1,906 | 75,876 |
| frame → signed order, p50 | 302 | 1,590 | 299 | 233 | 393 | 206 |
| frame → ack (loopback TLS), p50 | 3,091 | 8,996 | 3,057 | 1,163 | 2,755 | 1,000 |
| CPU per frame | 129.6 | 106.2 | 175.1 | 61.7 | 45.8 | 81.6 |
| stage: decode, p50 / mean | 8.0 / 9.7 | 5.2 / 7.7 | 8.3 / 10.3 | 2.4 / 3.5 | 1.2 / 2.6 | 2.7 / 4.1 |
| stage: book, p50 / mean | 10.5 / 12.8 | 6.4 / 9.8 | 11.4 / 28.5 | 11.0 / 13.6 | 5.3 / 10.7 | 12.4 / 28.2 |
| stage: decide + sign, per order, p50 | 143 | 131 | 117 | 139 | 123 | 111 |

Bursts are 200 frames, and the reconnect's is the 500 snapshots, one per replay. The
p99.9s swung between runs: P1's was 3.6, 3.7 and 20 ms bursty, and 2.5–145 ms steady.
- **Against the incumbent,** P1 cuts CPU per frame 2.1× steady and 2.3× bursty, and
  tick-to-order 1.3× steady and 4.0× bursty. It drains a 200-frame burst in 1.9 ms
  (9.5 µs a frame) against 4.2 ms.
- **Where a bursty frame's 45.8 µs of CPU goes:**
  - picows on uvloop: 21.1 µs (`ws_clients.py`);
  - decode and book: 2.6 and 10.7 µs (means);
  - deciding, signing and POSTing 1.35 orders per 100 frames: about 7.7 µs;
  - the rest, about 4 µs: the stack's own overhead and its timing.
- **Decide + sign** costs 110–140 µs per order, 2.8–3.5× signing's hot-loop figure, well
  above `sign.py`'s in-context figure. Deciding and encoding the body add a few µs, and
  the rest of the gap is unexplained.
- **Steady costs more per frame than bursty:** each frame wakes the process alone, and
  decode and book double.
- **A reconnect is the worst case measured.** 500 snapshots at once take P1 76 ms to
  drain (152 µs a snapshot, nearly all of it building books from up to hundreds of price
  and size strings each), and every frame behind them waits.

### Provisional reading, from P1 alone

The decision rule: Python holds if the hot path's p99.9 under bursts is small against
the end-to-end p99.9 (the order round trip), and a native stage is worth its cost only
where it moves the end-to-end p99.9. This reading is provisional until the Linux
us-east-2 run gives both on the trading host.

- **Under the synthetic bursts, Python holds.** P1's bursty p99.9 is 3.7 ms (20 ms in
  one run of three, on this noisy host), against the round trips in Hosting (Azure East
  US costed; us-east-2 not yet measured).
- **A Rust book's saving inside a delta burst is about 1 ms.** Decode and book are about
  6.5 µs of the 9.5 µs a burst frame costs, so a native book would bring a 200-frame
  burst's last frame from 1.9 ms toward 1 ms.
- **The reconnect is where it could matter.** Its 76 ms drain, and 85 ms p99.9, are
  mostly book building, which native code likely does in a fraction of the time. That
  exceeds any round trip we expect.

What M4 still needs before deciding:
- P2: the book in Rust via PyO3, and a native-strategy upper bound (the whole hot path
  native);
- P3: NautilusTrader's native book and engine;
- recorded frames in place of synthetic ones, snapshot bursts included;
- threads vs processes for the feed, the strategy and the order path;
- free-threading on 3.14t, with a run that asserts the GIL stays off;
- the priority limiter (orders and cancels ahead of everything else);
- round trips, from the user's host and us-east-2;
- the full ranking of P1–P3 under one schedule.

M5 keeps one book feed: `decode.StructFeed` is P1's prototype of `kalshi.ws.BookFeed`
for typed messages, and folds into kalshi/ws.py with whichever stack wins.

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
