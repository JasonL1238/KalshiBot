# data/: acquisition

Gets market data onto disk under `config.data_dir()` (default: the repo's gitignored
top-level data directory).

| File | What |
|------|------|
| `backfill.py` | Kalshi reference tables (series, events, non-combo markets, fee changes) and the trade tape by UTC day → `raw/kalshi/` (`kalshibot backfill reference` / `trades`) |
| `record.py` | `kalshibot record`: raw WS frames + receive ns → `raw/ws/date=…/*.parquet`; coverage log (connect, subscribed sid/channel/tickers, gap, disconnect, error, stop) → `raw/ws/coverage.jsonl` |

## Invariants

- Book history exists only if we record it ([RESEARCH §1](../../../docs/RESEARCH.md)).
- Raw tables keep everything, including columns only known after settlement. What
  research may see when is decided in `research/load.py` (post-hoc columns appear once
  `settlement_ts < as_of`).
