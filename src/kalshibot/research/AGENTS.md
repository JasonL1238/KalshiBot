# research/: which strategies have real edge

Section 1 of the repo. Method and pass rules: [docs/BACKTESTING.md](../../../docs/BACKTESTING.md).
Hypotheses and verdicts: [docs/STRATEGIES.md](../../../docs/STRATEGIES.md).

| File | What |
|------|------|
| `calendar.py` | Research windows (discovery / confirmation / lockbox / forward) and per-hypothesis confirmation starts |
| `ledger.py` | Append-only trial ledger (`ledger/<family>.jsonl`, committed) plus trial returns; the single lockbox unlock |
| `load.py` | `Loader`: the only way research reads market data (one trade tape, post-hoc masking, lockbox refusal) and fee terms (`fee_schedule()`) |

## Rules for code here

- Read data only through `Loader`. Never read `data/` Parquet directly; that bypasses
  the calendar and look-ahead masking.
- Never import `kalshibot.execution` (the hygiene test enforces it).
- Record every evaluation with `ledger.record_trial` before looking at its result.
  In a worktree, set `KALSHIBOT_LEDGER` to the main checkout's `ledger/`.
