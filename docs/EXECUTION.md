# Execution

How orders reach Kalshi as fast and as safely as possible. M4 benchmarks the options
and M5 builds the engine on the winner. Both write their results and decisions here.

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
