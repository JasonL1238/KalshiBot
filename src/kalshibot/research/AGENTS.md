# research/: which strategies have real edge

Section 1 of the repo. Method and pass rules: [docs/BACKTESTING.md](../../../docs/BACKTESTING.md).
Hypotheses and verdicts: [docs/STRATEGIES.md](../../../docs/STRATEGIES.md).

| File | What |
|------|------|
| `calendar.py` | Research windows (discovery / confirmation / lockbox / forward), the families confirmed forward (`FORWARD`) and withdrawn (`WITHDRAWN`), and each hypothesis's confirmation window (`confirmation_window`) |
| `ledger.py` | Append-only trial ledger (`ledger/<family>.jsonl`, committed: trials, the confirmation's start marker, and reports on it) plus trial returns; the single lockbox unlock |
| `load.py` | `Loader`: the only way research reads market data (one trade tape, candles, post-hoc masking, lockbox refusal), fee terms (`fee_schedule()`), the data hash trials record (`fingerprint()`), when the tables were fetched (`fetched()`), and the memory-capped DuckDB connection it and the data tests use (`connect()`) |
| `calibration.py` | Taker calibration by price paid (the favorite–longshot table; the M2 positive control) |
| `quotes.py` | The null's prices and draws: mids from 1-minute candles, for every market of an exclusive event or the contract alone, searched for a day at a time (`needed`, `mids`); strike ladders read off Kalshi's tickers |
| `run.py` | The research runner, evaluating a month of decisions at a time (`CHUNK`): `discover` (every configuration, recorded), `fetch_candles` (what the null prices from), `confirm` (the family's one confirmation test, with its power and strata), `report` (Tier 2 on it) |
| `backtest/` | Tier 1 (`fills.py`, `settle.py`: every verdict) and the Tier-2 engines (model uncertainty) |
| `backtest/fills.py` | Fill rules from the trade tape (taker: next same-side print; maker: strict trade-through, one fill per sweep) and how late an order can fill (`reach`); the fill schema; the order the Tier-2 engines read the tape in |
| `backtest/settle.py` | Net PnL per fill (costliest plausible fee regime, its own rounding or the fee rate itself; settlement value, including scalar results, or a mark at window end) and daily returns on one calendar |
| `backtest/replay.py` | Tier 2: NautilusTrader replays the tape and fills taker intents its own way; returns fills plus its own PnL |
| `backtest/bars.py` | Tier 2: vectorbt fills taker intents at 1-minute bar closes; returns fills plus its own PnL |
| `validation/` | Anti-overfitting statistics and the measurements behind the gates |
| `validation/null.py` | Market-calibrated null: inputs from de-vigged mids (multiplicative/Shin), cluster-coherent outcome draws, recentered PnL, same-day and cross-day dependence and the widening they imply (for a resting bid's fills: their covariance on one outcome, flat cross-day weights, and the widening's jackknife SE; for a taker's repeated decisions on one contract, their shared outcome's covariance, `Repeated`), p-values |
| `validation/stats.py` | PSR/DSR, effective trial count (ONC base step), PBO via CSCV, stationary-bootstrap p-value, Romano–Wolf step-down, Holm |
| `validation/gauntlet.py` | The pre-registered decision procedure (`Gates`): discovery selection, the dependence sample, the confirmation test and its power (a sample with maker fills gets the Maker null, `Positions.from_frame` refusing maker fills outside its bound; a taker's repeated decisions, by each position's `contract` and `exclusive`, get theirs, `_repeated`), Holm across families (forward ones at their own α/12); diagnostics |
| `validation/synthetic.py` | Synthetic worlds with a known truth (boundary null for YES or NO buyers, planted edges, same-day shocks that can last for days; for resting bids, price paths swept at the boundary; for repeated taker decisions, snapshots of posterior truths through a contract's day) |
| `validation/conformance.py` | Size and power of every candidate procedure on those worlds (the grid behind the gate choice; `--only` runs rows by label prefix), a check of `gauntlet.power`, and the coupling ratios R and R̄ of repeated positions (makers, H1-shaped); the last rows are worlds held out from choosing the Maker null (`--only makers:` skips them) and the repeated-decision null (`--only "H1-shaped:"` skips them); the third table's cells are bold by `_calibration` (docs/BACKTESTING.md step 6) |

## Rules for code here

- Read data only through `Loader`. Never read `data/` Parquet directly; that bypasses
  the calendar and look-ahead masking.
- Never import `kalshibot.execution` (the hygiene test enforces it).
- Evaluate families only through `run.py`'s public functions, which record every
  evaluation in the ledger before returning it, run on the calendar's windows only, and
  allow one confirmation test per family. In a worktree, set `KALSHIBOT_LEDGER` to the
  main checkout's `ledger/`.
