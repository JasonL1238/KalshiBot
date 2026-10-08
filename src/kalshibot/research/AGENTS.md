# research/: which strategies have real edge

Section 1 of the repo. Method and pass rules: [docs/BACKTESTING.md](../../../docs/BACKTESTING.md).
Hypotheses and verdicts: [docs/STRATEGIES.md](../../../docs/STRATEGIES.md).

| File | What |
|------|------|
| `calendar.py` | Research windows (discovery / confirmation / lockbox / forward) and per-hypothesis confirmation starts |
| `ledger.py` | Append-only trial ledger (`ledger/<family>.jsonl`, committed) plus trial returns; the single lockbox unlock |
| `load.py` | `Loader`: the only way research reads market data (one trade tape, post-hoc masking, lockbox refusal) and fee terms (`fee_schedule()`) |
| `calibration.py` | Taker calibration by price paid (the favorite–longshot table; the M2 positive control) |
| `backtest/` | Tier 1 (`fills.py`, `settle.py`: every verdict) and the Tier-2 engines (model uncertainty) |
| `backtest/fills.py` | Conservative fill rules from the trade tape (taker: next same-side print; maker: strict trade-through); the intent and fill schemas |
| `backtest/settle.py` | Net PnL per fill (costliest plausible fee regime; settlement value, including scalar results, or a mark at window end) and daily returns on one calendar |
| `backtest/replay.py` | Tier 2: NautilusTrader replays the tape and fills taker intents its own way; returns fills plus its own PnL |
| `backtest/bars.py` | Tier 2: vectorbt fills taker intents at 1-minute bar closes; returns fills plus its own PnL |
| `validation/` | Anti-overfitting statistics and the measurements behind the gates |
| `validation/null.py` | Market-calibrated null: inputs from de-vigged mids (multiplicative/Shin), cluster-coherent outcome draws, recentered PnL, same-day and cross-day dependence and the widening they imply, p-values |
| `validation/stats.py` | PSR/DSR, effective trial count (ONC base step), PBO via CSCV, stationary-bootstrap p-value, Romano–Wolf step-down, Holm |
| `validation/gauntlet.py` | The pre-registered decision procedure: discovery selection, the confirmation test, Holm across families; diagnostics |
| `validation/synthetic.py` | Synthetic worlds with a known truth (boundary null for YES or NO buyers, planted edges, same-day shocks that can last for days) |
| `validation/conformance.py` | Size and power of every candidate procedure on those worlds; the grid behind the gate choice |

## Rules for code here

- Read data only through `Loader`. Never read `data/` Parquet directly; that bypasses
  the calendar and look-ahead masking.
- Never import `kalshibot.execution` (the hygiene test enforces it).
- Record every evaluation with `ledger.record_trial` before looking at its result.
  In a worktree, set `KALSHIBOT_LEDGER` to the main checkout's `ledger/`.
