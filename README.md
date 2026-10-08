# KalshiBot

A research and trading system for [Kalshi](https://kalshi.com) prediction markets. It has
two parts:

- **Research.** Which strategies have real edge after fees, in which Kalshi
  categories? Strategies are tested on point-in-time data at prices you could actually
  have traded at, with established open-source backtest engines. The results then have
  to pass pre-registered anti-overfitting tests. Method:
  [docs/BACKTESTING.md](docs/BACKTESTING.md). Verdicts: [docs/STRATEGIES.md](docs/STRATEGIES.md).
- **Execution.** The fastest safe way to trade those strategies, chosen by benchmarks.
  See [docs/EXECUTION.md](docs/EXECUTION.md).

Status and plan: [docs/ROADMAP.md](docs/ROADMAP.md). Setup, code map, and contributor
rules: [AGENTS.md](AGENTS.md).

```bash
kalshibot markets --series KXHIGHNY      # live top of book for one series
kalshibot backfill reference             # every series, event, and non-combo market -> Parquet
kalshibot backfill trades --start YYYY-MM-DD --end YYYY-MM-DD   # trade tape by UTC day
```

Market data uses Kalshi's public endpoints and needs no account. Recording live order
books needs a read-only prod key in `.env` (see `.env.example`). Run the recorder in a
terminal multiplexer so it outlives the window; a failure stays on screen:

```bash
tmux new -d -s record "$PWD/.venv/bin/kalshibot record; echo exited \$?; read"
tail -f data/raw/ws/coverage.jsonl   # expect "connect", then "subscribed" events
```

Background on the venue: [docs/RESEARCH.md](docs/RESEARCH.md).
