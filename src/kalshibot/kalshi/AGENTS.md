# kalshi/: exchange API

Thin, dependency-light access to Kalshi Trade API v2. Research, data collection, and
execution all use it.

| File | What |
|------|------|
| `auth.py` | `KalshiSigner`: signs `timestamp_ms + METHOD + path` (Ed25519 or RSA-PSS) |
| `client.py` | `KalshiClient`: sync REST with pagination, throttle, and 429/5xx backoff |
| `models.py` | `OrderBook`: bids-only book; asks are derived (`yes_ask = 1 - best_no_bid`) |
| `fees.py` | `fee()` (vectorized single fill) and `OrderFees` (per-order rounding accumulator); the home of the fee model |

## Rules for code here

- New code parses prices and sizes from Kalshi's strings into integers, never through
  float (`int(0.57 * 10000) == 5699`). `models.py` and `to_float` still use floats until
  the M1 fixed-point book replaces them.
- Market data always comes from prod (`config.MARKET_DATA_URL`).
- Nothing in this package places orders yet. When it does, rule 7 applies (root
  `AGENTS.md`).

API facts (signing, units, historical cutoff, rate limits):
[docs/RESEARCH.md §1](../../../docs/RESEARCH.md).
