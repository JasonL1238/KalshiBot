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
kalshibot backfill reference             # series, events, non-combo markets, fees, incentive programs
kalshibot backfill trades --start YYYY-MM-DD --end YYYY-MM-DD   # trade tape by UTC day
kalshibot research discover H1           # a family's configurations on discovery, logged
kalshibot research candles H1            # the quotes its confirmation test needs
kalshibot research confirm H1            # its one confirmation test (once its window ends)
kalshibot research report H1             # Tier 2 on that confirmation, again
```

Market data uses Kalshi's public endpoints and needs no account. Recording live order
books needs a read-only prod key in `.env` (see `.env.example`); until then,
`kalshibot record --rest --series KXBTCD --series KXBTC15M` snapshots full books keylessly
(the crypto series H4 and H11 need; add others as families need them).
Run the recorder on an always-on machine, in a terminal multiplexer so it outlives the
window; a failure stays on screen:

```bash
tmux new -d -s record "$PWD/.venv/bin/kalshibot record; echo exited \$?; read"
tail -f data/raw/ws/coverage.jsonl   # expect "connect", then "subscribed" events
```

Background on the venue: [docs/RESEARCH.md](docs/RESEARCH.md).
