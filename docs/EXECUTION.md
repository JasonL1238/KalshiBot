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

M4 measures the whole hot path (decode → book → decide → sign) under replayed bursts and
decides whether the book moves to Rust, where each of these costs nanoseconds.

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
