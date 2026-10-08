# benchmarks/engine_screen/: the backtest-engine screen

The probes behind the engine-screen table in
[docs/BACKTESTING.md](../../docs/BACKTESTING.md), kept only as its record. They are not
maintained: ruff skips them, and the rejected engines aren't in the repo's dependencies.

They are removed in the commit right after the one that adds them, together with this
directory's ruff exclude (pyproject.toml), its `.gitignore` line, and its row in
`benchmarks/AGENTS.md`; that commit puts the adding commit's hash in BACKTESTING.md.

| File | What |
|------|------|
| `common.py` | The shared two-market weekend tape, reference fill and PnL, 1M-trade speed tape |
| `probe_nautilus.py` | NautilusTrader 1.230.0: BinaryOption, fee model, settlement, speed |
| `probe_nautilus_l2.py` | NautilusTrader L2 book with queue position and latency |
| `probe_hftbacktest.py` | hftbacktest 2.4.4: trades-only fills, synthetic book, speed |
| `probe_vectorbt.py` | vectorbt 1.0.0: wide matrices, fees, settlement row, speed |
| `probe_backtrader.py` | backtrader 1.9.78.123 |
| `probe_backtesting_py.py` | Backtesting.py 0.6.6 |
| `probe_zipline.py` | zipline-reloaded 3.1.1 on the 24/7 calendar |
| `probe_lean.py` | LEAN algorithm (mounted as `main.py` in the LEAN container) |

Run a probe with the engine installed at the listed version: `python probe_<engine>.py`
(add `--speed` where the script accepts it). Outputs go to `out/` (gitignored). LEAN ran
in Docker (`quantconnect/lean:18171`, linux/arm64) on per-ticker CSVs exported from
`common.py` (`ts,price,contracts,side,settle`); that launcher wasn't kept, so the LEAN
row needs manual setup to reproduce.
