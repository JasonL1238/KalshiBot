# data/: acquisition

Gets market data onto disk under `config.data_dir()` (default: the repo's gitignored
top-level data directory).

| File | What |
|------|------|
| `backfill.py` | Every settled market in a series, from historical and live endpoints → `raw/markets/{series}.parquet` |
| `snapshot.py` | REST order-book snapshots, Hive-partitioned: `raw/snapshots/series=…/date=…/HHMMSS.parquet` |

## Invariants

- Book history exists only if we record it ([RESEARCH §1](../../../docs/RESEARCH.md)).
- Every derived row must say when its information became available (`available_at`).
  The M1 loaders will filter on it so backtests can't see the future.
