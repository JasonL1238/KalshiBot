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

Every script in `benchmarks/` but book.py ran three times at commit 8cea023 on one host:
a cloud VM, Linux 6.18 (Firecracker guest), Intel Xeon @ 2.10 GHz, 4 vCPUs, CPython
3.13.16, with gc_pauses also once on 3.14.6 and 3.14.6t. The command and the raw outputs,
each with its commit, platform and package versions, are in `runs/m4-benchmarks/rerun/`
(`rerun.md`). Tables give the median of the three runs. The same command's earlier runs
at e20d5df, before the merge reviews changed the scripts, stay in `runs/m4-benchmarks/`.

### Method

- **Synthetic feed.** `harness.Stream` writes Kalshi's WebSocket formats (RESEARCH §1):
  snapshots for 50 markets 5–100 levels deep per side, then deltas (85%), the trades
  behind some of them (14%), and our fills (1%), as compact JSON. Median frames: delta
  174 bytes, trade 233, fill 407, snapshot 1,399. Recorded frames replace these in a
  later M4 step (ROADMAP M4).
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
- **Host noise.** The hypervisor steals 0.2–0.6% of CPU time, in stalls of up to over
  100 ms. Each output prints the harness floor (a no-op) or the sender's own lag next to
  its rows (their max reached 133 ms once), and CPU steal per table. **What holds in
  every run: each table's chosen row beats the others on its measure, and P1 beats the
  incumbent on CPU per frame and on every p50.** Other figures are medians of the three
  runs, with per-run ranges where they matter. Read p99.9 and max as host-bound here;
  the tails come from the us-east-2 run (ROADMAP M4).

### Decoding (`decode.py`)

ns per frame, decode plus reading the fields the next stage needs (median of 7
interleaved batches per run), and the bursty open loop (40,000 frames; harness floor
mean 29 µs, p50 3.6 µs):

| decoder | snapshot | delta | trade | fill | bursty mean µs | p50 µs |
|---|--:|--:|--:|--:|--:|--:|
| json (stdlib) | 10,411 | 2,986 | 3,173 | 4,183 | 210 | 33.1 |
| orjson | 6,401 | 820 | 938 | 1,320 | 80 | 14.4 |
| msgspec untyped | 6,633 | 773 | 924 | 1,222 | 74 | 13.0 |
| **msgspec typed structs (chosen)** | 6,115 | 490 | 536 | 788 | 55 | 10.6 |

Typed structs are fastest on every message type in every run: at the median, 1.7×
orjson on deltas and 6.1× stdlib json. They also validate the schema while decoding,
and with `gc=False` they are objects the collector never tracks (see GC).

### Signing (`sign.py`)

| key type | µs per signed request: hot loop | after 1 ms idle | after decoding 100 frames | open loop, all orders: p50 µs | p99 µs |
|---|--:|--:|--:|--:|--:|
| **Ed25519 (chosen)** | 38.6 | 79.5 | 49.6 | 49 | 979 |
| RSA-2048 PSS | 396.4 | 430.0 | 395.4 | 411 | 7,974 |

The open loop pools every order of a schedule of Poisson 200/s plus 20 orders at once
every 500 ms. RSA costs 10.3× as much in a hot loop, so a 20-order requote spends about
8 ms on signatures. Across the runs, an Ed25519 signature cost 1.1–1.6× its hot-loop
time between other work, and 1.5–2.2× after an idle gap (the gap added 19–69 µs on
either key). **The trading key should be Ed25519.**

### Event loop and WebSocket client (`ws_clients.py`)

websockets is read the way `kalshi/ws.py` reads it, as str. aiohttp also delivers str.
picows hands over bytes, which both JSON decoders accept directly. The bare reader
parses frames itself, so it measures the loop alone: "callback" consumes each frame in
`data_received`, as picows does; "queue hop" adds the `asyncio.Queue` wakeup that
websockets and aiohttp put between the socket and the caller.

| client / loop | CPU µs/frame steady | bursty | steady p50 µs | bursty p50 µs |
|---|--:|--:|--:|--:|
| websockets / asyncio (incumbent) | 63.1 | 45.6 | 83.3 | 745 |
| websockets / uvloop | 54.8 | 38.9 | 75.4 | 696 |
| aiohttp / asyncio | 55.3 | 37.2 | 72.0 | 196 |
| aiohttp / uvloop | 41.0 | 28.3 | 60.7 | 183 |
| picows / asyncio | 35.1 | 24.1 | 55.5 | 133 |
| **picows / uvloop (chosen)** | 27.1 | 19.2 | 49.5 | 124 |
| bare reader, callback / asyncio | 37.1 | 26.4 | 57.5 | 145 |
| bare reader, callback / uvloop | 27.9 | 19.7 | 50.8 | 138 |
| bare reader, queue hop / asyncio | 50.2 | 34.8 | 68.8 | 293 |
| bare reader, queue hop / uvloop | 39.6 | 28.4 | 58.8 | 293 |

- uvloop cuts CPU per frame for every client in every run; at the median by 5–14 µs,
  13–26% depending on the client.
- The queue hop costs 8–13 µs per frame and doubles the bursty p50; on bursty p50 it
  matters more than the loop choice.
- picows on uvloop costs what the bare reader does, so the client adds nothing
  measurable. It uses 42–43% of the incumbent's CPU per frame, and its bursty p50 is a
  sixth of the incumbent's.
- websockets' burst latency points to flow control, not CPU. In every run, bursty,
  aiohttp on asyncio used less CPU than websockets on uvloop (37.2 vs 38.9 µs at the
  median) yet had under a third of its p50. The likely cause is websockets' default
  `max_queue=16`, which stops reading from the socket mid-burst.

**Use uvloop with picows.** picows delivers frames, not messages, so a production
listener must join continuation frames.

### Order HTTP client (`http_clients.py`)

Orders were POSTed to a local TLS server (HTTP/1.1 and HTTP/2 by ALPN), triggered open
loop at Poisson 200/s plus 10 at once every 250 ms. Pools were warm, and all clients ran
on uvloop. 9,996 orders per client per run.

| client | p50 µs | p99 µs | CPU µs/order |
|---|--:|--:|--:|
| httpx HTTP/1.1 | 2,248 | 31,537 | 1,617 |
| httpx HTTP/2 | 2,072 | 15,325 | 1,213 |
| **aiohttp HTTP/1.1 (chosen)** | 756 | 2,992 | 411 |

At the median httpx costs 3.0–3.9× aiohttp's CPU per order, and HTTP/2's multiplexing
doesn't recover that cost. One trap: without `sniffio` installed, httpcore retries a
failed import on every async request; the `[bench]` extra installs it. **Send orders
with aiohttp** on a warm pool. Re-measure HTTP/2 against Kalshi itself in the round-trip
benchmark, since CloudFront's handling of HTTP/2 can't be simulated locally.

### Garbage collector (`gc_pauses.py`)

| interpreter, heap held | one full collection | after `gc.freeze()` | full collection every (json dicts kept) | longest full pause while the heap grows by half (≥200k containers) | the same after freeze |
|---|--:|--:|--:|--:|--:|
| 3.13, interpreter only (52k tracked) | 9.2 ms | 0.0 ms | 6.5 s | none | none |
| 3.13, + 1M dicts (2.05M tracked) | 362 ms | 0.0 ms | 257 s | 428 ms | 34 ms (3 of them) |
| 3.14, + 1M dicts (3.06M tracked), one run | 388 ms | 0.0 ms | 191 s | 464 ms | 73 ms (5 of them) |
| 3.14t free-threaded, + 1M dicts (3.07M tracked), one run | 157 ms | 81 ms | 192 s | 194 ms | none |

- **When full collections come.** CPython collects in full once its long-lived
  containers have grown by a quarter since the last full collection. 3.14.6 behaves as
  3.13 here: its growth test ran 764 collections, one of them full. A process that keeps
  its decoded dict messages (a tape, a history) therefore stalls for a whole-heap
  collection every few minutes. With a large heap that takes 0.36–0.46 s. A kept dict
  message adds 1.0 tracked container on 3.13 and 2.0 on 3.14; `gc=False` structs keep
  nothing the collector tracks, so they never move it.
- **What freeze does.** It takes the heap out of every collection, but kept containers
  still accumulate. After freeze, the quarter is measured against the unfrozen
  survivors, which are few, so full collections come more often but walk only what was
  kept since: 3 of up to 34 ms instead of one of 428 ms on 3.13, and in total 101 ms
  instead of 468 ms. On 3.14t, freeze only halves an explicit full collection (157 to
  81 ms), though its growth test then triggered none.
- **In the replays,** each starting right after a full collection, the only pauses were
  young ones: 44 per 40,000 kept dict messages, at most 0.34 ms, and none for structs.
  The settings differed only by noise.

**Call `gc.freeze()` once startup state is loaded, keep the hot path on `gc=False`
structs, and leave the collector enabled** (disabling it lets any accidental cycle
leak). Free-threading brings nothing to this single-threaded hot path, and a forced
full collection there stays costly after freeze.

### P1: the pure-Python hot path (`hot_path.py`)

Two stacks consume the same replay, keep every book, and run one decision rule: an IOC
buy at the ask when a market's ask first drops a tick below its first snapshot's. It
fired 135 times per 10,000 frames over 50 markets, 286 over the reconnect's 500. Each
order is signed, serialized and POSTed to the local TLS server without blocking the
feed, and both stacks must make identical decisions.
- **Incumbent:** asyncio, websockets via `kalshi.ws.Connection`, json, `BookFeed`, httpx.
- **P1:** the chosen row of each table above: uvloop, picows, msgspec structs into
  `StructFeed` (the same `OrderBook`), Ed25519, aiohttp, and `gc.freeze()`.

µs, 4 × 10,000 frames per stack per run:

| | incumbent steady | bursty | reconnect | **P1 steady** | **bursty** | **reconnect** |
|---|--:|--:|--:|--:|--:|--:|
| frame → processed, p50 | 124 | 1,102 | 154 | 69 | 248 | 81 |
| frame → processed, p99 | 1,242 | 5,535 | 82,234 | 527 | 2,686 | 65,382 |
| frame → processed, p99.9 | 2,606 | 9,336 | 116,259 | 2,204 | 3,695 | 84,237 |
| burst → its last frame done, p50 | | 3,817 | 83,669 | | 1,834 | 73,216 |
| frame → signed order, p50 | 271 | 1,440 | 255 | 204 | 402 | 184 |
| frame → ack (loopback TLS), p50 | 2,758 | 7,915 | 2,642 | 1,023 | 2,573 | 899 |
| CPU per frame | 120.1 | 97.4 | 160.4 | 55.4 | 42.4 | 74.5 |
| stage: decode, p50 / mean | 7.2 / 8.8 | 4.8 / 6.9 | 7.4 / 9.3 | 1.9 / 3.0 | 1.1 / 2.4 | 2.2 / 3.5 |
| stage: book, p50 / mean | 9.7 / 11.1 | 5.8 / 9.0 | 10.4 / 26.6 | 10.3 / 13.0 | 5.4 / 10.4 | 11.6 / 26.1 |
| stage: decide + sign, per order, p50 | 133 | 117 | 101 | 122 | 119 | 101 |

Bursts are 200 frames, and the reconnect's is the 500 snapshots, one per replay.
- **Against the incumbent,** P1 cuts CPU per frame 2.2× steady and 2.3× bursty, and
  tick-to-order p50 1.3× steady and 3.6× bursty. It drains a 200-frame burst in 1.8 ms
  (9.2 µs a frame) against 3.8 ms.
- **Where a bursty frame's 42.4 µs of CPU goes** (means over every frame, isolated ones
  included, so they exceed the 9.2 µs a frame costs inside a burst):
  - picows on uvloop: 19.2 µs (`ws_clients.py`);
  - decode and book: 2.4 and 10.4 µs;
  - deciding, signing and POSTing 1.35 orders per 100 frames: about 7.2 µs;
  - the rest, about 3 µs: the stack's own overhead and its timing.
- **The book stage is mostly parsing.** Timed apart in a closed loop (`hot_path.py`
  prints it first), turning dollar and size strings into units is 65% of a delta's book
  stage (1.5 of 2.3 µs) and 64% of a snapshot's (87 of 136 µs); the rest builds or
  updates the `OrderBook`. Closed loop and hot, these are shares, not in-replay costs.
- **Decide + sign** costs 100–135 µs per order, 2.6–3.5× signing's hot-loop figure,
  above what `sign.py`'s in-context runs show. Deciding and encoding the body add a few
  µs, and the rest of the gap is unexplained.
- **Steady costs more per frame than bursty:** each frame wakes the process alone, and
  decode and book take 1.7–1.9× as long.
- **A reconnect is the worst case measured.** 500 snapshots at once take P1 73 ms to
  drain (146 µs a snapshot, nearly all of it the book stage), and every frame behind
  them waits.
- **One book feed for M5.** `decode.StructFeed` is P1's prototype of `kalshi.ws.BookFeed`
  for typed messages, kept equal to it by the decoder-agreement test; it folds into
  kalshi/ws.py with whichever stack wins.

### Reading, from P1 alone

The decision rule: Python holds if the hot path's p99.9 under bursts is at most 10% of
the end-to-end p99.9 (the order round trip), and a native stage is worth its cost only
where it brings a failing hot path under that line. On this host the rule is **not
decidable**, and the reading stays provisional until the us-east-2 run:
- P1's bursty p99.9 was 3.2, 3.7 and 4.0 ms across the three runs, and 3.6, 3.7 and 20 ms
  in the earlier runs at e20d5df: host-bound (Method).
- The round trip's p99.9 isn't measured. Azure East US's typical 12–17 ms (Hosting) is
  only a lower bound on it, so the 10% line is at least 1.2–1.7 ms, which P1's bursty
  p99.9 doesn't clear on that bound alone.
- The reconnect fails the rule: P1's p99.9 through it is 84 ms.
- In the first e20d5df run, P1's bursty p99.9 (20.2 ms) lost to the incumbent's (9.0 ms)
  with normal steal (0.5%) and sender lag (p99.9 1.9 ms). That is unexplained; in every
  re-run P1 won bursty (3.2–4.0 against 8.0–9.4 ms), and lost once through the reconnect
  (241 against 137 ms).

What a native book could save, from the p50s above: at most about 1 ms on a 200-frame
burst's last frame (the book stage's 5.4 µs × 200), and only if parsing moves native
too, since parsing is two thirds of the stage. Decode is msgspec's C code already. On
the reconnect the same stage is most of the 73 ms, which is where P2 (the book, with its
fixed-point parsing, in Rust) would be measured first. The remaining M4 work, P2 and P3
among it, is ROADMAP's M4 item.

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
