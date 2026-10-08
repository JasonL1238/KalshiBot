# kalshi/: exchange API

Thin, dependency-light access to Kalshi Trade API v2. Research, data collection, and
execution all use it.

| File | What |
|------|------|
| `auth.py` | `KalshiSigner`: signs `timestamp_ms + METHOD + path` (Ed25519 or RSA-PSS) |
| `client.py` | `KalshiClient`: sync REST (markets, events, series, fee changes, trades, candles, books) with pagination, throttle, and backoff on 429/5xx/dropped connections (GET only) |
| `models.py` | Fixed-point units (`price_units`, `size_units`; `on_tick` checks a price against a market's tick grid) and the mutable `OrderBook` (a dict + price bitmap `BookSide` per side; asks derived from the other side's bids) |
| `ws.py` | WebSocket: `connect_feed` (signed), `Connection.subscribe` (chunked), `BookFeed` (books from snapshots/deltas, `SequenceGap` on missing seq) |
| `fees.py` | `fee()` (vectorized single fill) and `OrderFees` (per-order rounding accumulator), given `FeeTerms` |
| `fee_history.py` | `FeeSchedule.terms(series, time)`: the fee terms in force (Kalshi's change log, else the series' current terms from the log's start, else `fee_history.json` from cited archived schedules); `plausible()`: every regime that could have applied |

## Rules for code here

- Prices and sizes are integers parsed from Kalshi's strings with `price_units` /
  `size_units`, never through float (`int(0.57 * 10000) == 5699`).
- Market data always comes from prod (`config.MARKET_DATA_URL`).
- Nothing in this package places orders yet. When it does, rule 7 applies (root
  `AGENTS.md`).

API facts (signing, units, historical cutoff, rate limits):
[docs/RESEARCH.md §1](../../../docs/RESEARCH.md).
