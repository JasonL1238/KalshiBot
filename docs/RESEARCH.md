# Research notes

Background research compiled 2026-10-03. Kalshi changes fast (fees, rate limits, key
types, and settlement sources all changed in 2026), so treat specifics as "true as of
this date" and re-check the [docs](https://docs.kalshi.com) and
[changelog](https://docs.kalshi.com/changelog) before relying on them.

Items marked ✅ were verified live against the API from this repo.

---

## 1. Kalshi API essentials

### Hosts
| | REST | WebSocket |
|-|------|-----------|
| Prod | `https://external-api.kalshi.com/trade-api/v2` ✅ | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` |
| Demo | `https://external-api.demo.kalshi.co/trade-api/v2` ✅ | `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2` |

The legacy hosts (`api.elections.kalshi.com`, `demo-api.kalshi.co`) still work. Demo is
a separate account at [demo.kalshi.co](https://demo.kalshi.co) with fake money and its own
API keys, so use it for all execution testing.

### Authentication
- Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (Unix **ms**),
  `KALSHI-ACCESS-SIGNATURE` = base64 signature of `timestamp + METHOD + path`.
- `path` includes `/trade-api/v2` and excludes the query string.
- **Ed25519 keys are the default since Oct 1, 2026**; RSA-2048 (RSA-PSS/SHA-256) is still
  supported. `kalshibot.kalshi.auth.KalshiSigner` handles both.
- WebSocket handshakes must be signed even for public channels (sign `GET /trade-api/ws/v2`).
- [docs: API keys](https://docs.kalshi.com/getting_started/api_keys)

### Public market data (no auth) ✅
- `GET /markets` (filters: `series_ticker`, `event_ticker`, `status` = open|closed|settled…,
  `mve_filter` = only|exclude for combo markets, `limit` ≤ 1000, `cursor`). Responses say
  `status: "active"` but the filter value is `open`.
- `GET /series` returns every series (~15k) in one response; `GET /series/{t}` one series.
- `GET /events` (`limit` ≤ 200) covers settled events too; there is no `/historical/events`.
- `GET /series/fee_changes?show_historical=true`: logged per-series fee changes. The log
  starts 2025-10-04 and records only the new value. Earlier fee history, with sources:
  `kalshi/fee_history.json`.
- `GET /markets/{t}/orderbook?depth=`, plus **`GET /markets/orderbooks?tickers=A&tickers=B`**
  (≤ 100 at once, all from the same instant)
- `GET /markets/trades` and `/historical/trades` take `min_ts` (inclusive) and `max_ts`
  (**exclusive**), in whole seconds.
- Candlesticks (1, 60, or 1440 min; OHLC of `yes_bid`, `yes_ask`, and trade price):
  `GET /series/{s}/markets/{t}/candlesticks` for live markets and
  `/historical/markets/{t}/candlesticks` for archived ones.

### Historical data ✅
- Each night at 00:00 UTC a cutoff advances by a day. Settled markets and trades older
  than it leave the live endpoints and are served only by `/historical/*`. Read the
  current cutoffs (`market_settled_ts`, `trades_created_ts`) from `GET /historical/cutoff`.
  `data/backfill.py` picks the archive per day.
- `/historical/markets` accepts `mve_filter=exclude` but returns 400 for `mve_filter=only`.
- History goes back to the start of each series (NYC highs: Aug 2021) at trade level.
- **No historical orderbook depth anywhere.** You only get book data by recording it
  yourself, so run the recorder early and keep it running.
- Open dataset: [Jon-Becker/prediction-market-analysis](https://github.com/jon-becker/prediction-market-analysis)
  (MIT repo). Downloaded 2026-10-08 from `https://s3.jbecker.dev/data.tar.zst`
  (36,020,641,508 bytes, ETag `18f290309fb5d12915d667fc5ee199e4-4294`, last-modified
  2026-02-05), extracted with `zstd -dc data.tar.zst | tar -xf -` into
  `data/external/becker/`. Kalshi: 72,134,741 trades (2021-06-30 → 2025-11-25 22:00:15
  UTC) and 7,682,445 markets. Its end date sets the research calendar's discovery seam.
  - Prices are integer cents. Some markets traded on a $0.001 grid (`deci_cent`) in late
    2025, and from 2025-10-09 those sub-cent trades are truncated, so YES + NO cents sum
    to 99.
  - It lacks whole markets. On the 15th of each month, 2021-07 → 2025-09, every Becker
    trade is in the API tape with identical fields, but 6,733 of 1,109,042 API trades
    (0.6%) are not in Becker. All of them belong to 3,714 markets Becker lacks entirely,
    mostly hourly crypto, FX, and index series (KXBTC, KXEURUSDH, KXNASDAQ100U, …) and
    POPVOTE. Research therefore reads the API tape for every date
    (`research/load.py`), and Becker serves as the independent cross-check.
  - `pytest -m data` pins the archive (count, last trade, so the calendar's seam) and
    reruns that cross-check. Fetch each sample day with
    `kalshibot backfill trades --start 2021-07-15 --end 2021-07-16` (and so on).

### Data formats ✅
- Prices are fixed-point dollar strings (`"0.5600"`), up to 4 decimals. Integer-cent
  fields were removed in early 2026; old tutorials using them are broken.
- Quantities are `*_fp` strings (`"148.34"`): **fractional contracts** exist.
- Each market has `price_ranges` (`{start, end, step}`) defining legal tick sizes; some
  markets have sub-penny ticks near 0 and 1. Snap order prices to `step`.
- The orderbook returns **bids only** for YES and NO, ascending. Best YES ask = 1 − best NO bid.
- WebSocket books ([docs](https://docs.kalshi.com/websockets/orderbook-updates)):
  `orderbook_snapshot` msgs carry `market_ticker` plus `yes_dollars_fp` / `no_dollars_fp`
  `[price, size]` string pairs, and a side's key is omitted when that side is empty.
  `orderbook_delta` msgs carry `price_dollars`, a signed `delta_fp`, `side`, and `ts_ms`.
  Every msg has `sid` and `seq`, which must be contiguous per subscription.

### Orders
- Use **V2** endpoints: `POST /portfolio/events/orders`. Legacy `/portfolio/orders` is dead.
- Required fields: `ticker`, `side` (`bid` = buy YES, `ask` = sell YES), `count` and `price`
  (strings), `time_in_force` (`good_till_canceled` | `immediate_or_cancel` | `fill_or_kill`),
  and `self_trade_prevention_type`. Optional: `post_only`, `reduce_only`, `client_order_id`,
  `expiration_time`.
- **No market orders**: send an aggressive IOC limit instead.
- Amend, decrease, cancel, batch create/cancel, and **order groups** (auto-cancel if fills
  exceed N contracts in a rolling 15s window, a built-in circuit breaker).
- [docs: create order v2](https://docs.kalshi.com/api-reference/orders/create-order-v2)

### Fees
- Fee mechanics live in `kalshibot.kalshi.fees`, and which terms applied when (maker
  shares by `fee_type`, dated regimes) in `kalshibot.kalshi.fee_history`.
- Read `fee_type` / `fee_multiplier` from `GET /series/{t}`, and upcoming changes from
  `GET /series/fee_changes`. Never hard-code them.
- ✅ `KXHIGHNY`: `fee_type=quadratic`, multiplier 1, so **makers pay nothing**.
- Check `fee_cost` on real fills, which also tells you the member precision ($0.0001
  direct vs $0.01 FCM).

### Rate limits
Token buckets, with separate read and write buckets. Most calls cost 10 tokens.

| Tier | Read | Write | How |
|------|------|-------|-----|
| Basic | 200 tok/s (~20 req/s) | 100 tok/s (~10 orders/s) | default |
| Advanced | 300 | 300 | self-serve upgrade via API |
| Expert → Prestige | 600 → 12,000 | 600 → 9,600 | trading volume |

429s come with no `Retry-After`; back off exponentially (the client does). Our client
defaults to ≤ 10 req/s.

### Operational gotchas
- **Weekly maintenance: Thursdays 3–5 AM ET.** Trading pauses; reconnect sockets after.
- **Exchange sharding:** weather is on shard 0, crypto on shard 2. Collateral must sit on the
  right shard (`/portfolio/intra_exchange_instance_transfer` or auto-rebalance).
- Official Python SDK (`kalshi_python_sync`) requires Python ≥ 3.13 and can lag the API.
  We use our own thin client instead, which is also more educational.
- API keys need a current **location attestation** for sports/elections trading (not weather).

### Rules that matter
- **Developer Agreement:** API use is for your own trading. Don't redistribute Kalshi data
  (keep `data/` out of git, and don't publish raw datasets or a public mirror). Backoff
  required. No spoofing or wash trading.
- **Eligibility:** 18+, KYC. Position accountability level for temperature contracts is
  $25,000 per strike, far above what you'll trade.

---

## 2. Weather markets specifics

- Series: `KXHIGHNY` (NYC/Central Park), plus other cities (Chicago, Miami, Austin, LA,
  Denver, Philadelphia…). Each daily event has ~6 mutually exclusive buckets
  (two tails + four 2°F ranges).
- **Settlement source changed Aug 14, 2026** ✅: from the NWS Daily Climate Report (CLI) to
  **The Weather Company** (weather.com/kalshi), same station (CLINYC = Central Park). Older
  help-center pages are stale; trust each market's `rules_primary`.
- ✅ Kalshi's settlement values match the free [IEM CLI archive](https://mesonet.agron.iastate.edu/json/cli.py?station=KNYC&year=2025)
  on 99.9% of 936 days (including all 49 TWC-era days), so IEM is a usable label source.
- Climate day = midnight to midnight **local standard time** (1:00 AM → 12:59 AM during DST).
- Expiration: first 7 or 8 AM ET after data release; settlement timer 300s.
- Bucket math with integer reporting: "63° to 64°" = P(62.5 ≤ T < 64.5); "71° or above"
  = P(T ≥ 70.5).
- ✅ Bucket mids in an event sum to more than $1 (observed 1.025): that's the overround.
- ✅ Of 1,880 NYC events, 98 have no YES winner and 64 have two or more. Check with
  `pd.read_parquet("data/raw/markets/KXHIGHNY.parquet")`, grouped by `event_ticker`,
  summing `result == "yes"`.

### Free weather data
| Source | What | Use |
|--------|------|-----|
| [NWS API](https://www.weather.gov/documentation/services-web-api) ✅ | Live point/grid forecasts | Live features |
| [Open-Meteo Previous Runs](https://open-meteo.com/en/docs/previous-runs-api) ✅ | Forecast as issued 1–7 days earlier, back to 2022 | Leak-free training features |
| [IEM CLI archive](https://mesonet.agron.iastate.edu/nws/clitable.php) ✅ | Official daily highs/lows | Labels |
| [IEM ASOS](https://mesonet.agron.iastate.edu/request/download.phtml) | Hourly/5-min station obs (METAR) | Same-day features |
| NOAA on AWS via [Herbie](https://herbie.readthedocs.io/) | Raw GFS/GEFS/HRRR/NBM model output | Ensemble spread, advanced features |

Open-Meteo is free for non-commercial use; check its terms if this ever becomes more
than a personal project. NOAA data is public domain.

---

## 3. Evidence about edge

Prior evidence and our own results: [STRATEGIES.md](STRATEGIES.md). Method and
pitfalls: [BACKTESTING.md](BACKTESTING.md).

## 4. Reference repos

Read these for ideas; don't install random bot repos (there have been
[key-stealing fake bot packages](https://safedep.io/malicious-polymarket-npm-crypto-wallet-drainer/)).

| Repo | Why look |
|------|----------|
| [Kalshi/kalshi-starter-code-python](https://github.com/Kalshi/kalshi-starter-code-python) | Official signing pattern (stale otherwise) |
| [rodlaf/KalshiMarketMaker](https://github.com/rodlaf/KalshiMarketMaker) | Avellaneda–Stoikov market making |
| [Weather-Capital-Markets/kalshi-weather](https://github.com/Weather-Capital-Markets/kalshi-weather) | Serious weather architecture (5s books, NBM GRIB ingestion) |
| [Nemesis-0/weather-prediction-market](https://github.com/Nemesis-0/weather-prediction-market) | Pre-registered point-in-time study with an honest null |
| [jon-becker/prediction-market-analysis](https://github.com/jon-becker/prediction-market-analysis) | Dataset + microstructure research |

Survivorship bias warning: "my bot made $X" posts are usually paper PnL, ignore fees, and
you only hear from the winners.
