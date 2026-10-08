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
  `limit` ≤ 1000, `cursor`). Responses say `status: "active"` but the filter value is `open`.
- `GET /series/{t}`, `GET /events/{t}`, `GET /markets/{t}`
- `GET /markets/{t}/orderbook?depth=`, plus **`GET /markets/orderbooks?tickers=A&tickers=B`**
  (≤ 100 at once, all from the same instant)
- `GET /markets/trades`, `GET /series/{s}/markets/{t}/candlesticks` (1, 60, or 1440 min)
- `GET /historical/cutoff`, `/historical/markets`, `/historical/trades`,
  `/historical/markets/{t}/candlesticks`

### Historical data ✅
- Settled markets older than the cutoff (currently **2026-08-04**) disappear from live
  endpoints and move to `/historical/*`. A full backfill reads both (see `data/backfill.py`).
- History goes back to the start of each series (NYC highs: Aug 2021) at trade level.
- **No historical orderbook depth anywhere.** You only get book data by recording it
  yourself, so run the snapshot collector early and keep it running.
- Open dataset: [Jon-Becker/prediction-market-analysis](https://github.com/jon-becker/prediction-market-analysis),
  ~36 GiB of Kalshi + Polymarket trades in Parquet.

### Data formats ✅
- Prices are fixed-point dollar strings (`"0.5600"`), up to 4 decimals. Integer-cent
  fields were removed in early 2026; old tutorials using them are broken.
- Quantities are `*_fp` strings (`"148.34"`): **fractional contracts** exist.
- Each market has `price_ranges` (`{start, end, step}`) defining legal tick sizes; some
  markets have sub-penny ticks near 0 and 1. Snap order prices to `step`.
- The orderbook returns **bids only** for YES and NO, ascending. Best YES ask = 1 − best NO bid.

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
- The fee model (formula, maker shares by `fee_type`, rounding) lives in
  `kalshibot.kalshi.fees`.
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
