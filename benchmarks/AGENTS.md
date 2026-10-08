# benchmarks/: execution performance evidence

Each script reproduces a decision recorded in [docs/EXECUTION.md](../docs/EXECUTION.md)
(or, for `engine_screen/`, docs/BACKTESTING.md).
Results depend on the machine, so record platform and Python version next to any
numbers you publish. `tests/test_benchmarks.py` keeps `book.py` runnable.

| File | Decision it backs |
|------|-------------------|
| `book.py` | Order-book side structure: `kalshi.models.BookSide` (dict + price bitmap) |
| [engine_screen/](engine_screen/AGENTS.md) | The backtest-engine screen in [docs/BACKTESTING.md](../docs/BACKTESTING.md) (archived; see its AGENTS.md) |

When a decision is final, delete the losing candidates' code (cite the pre-deletion
commit in EXECUTION.md) unless the script is the record that justifies the winner.
