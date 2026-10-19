# KalshiBot: agent guide

What the project is: [README.md](README.md). Each `CLAUDE.md` is a symlink to the
`AGENTS.md` beside it, so edit only `AGENTS.md`. The repo assumes macOS or Linux.

## Rules

1. Never list an AI agent as author or co-author of a commit or PR. That means no
   `Co-authored-by` AI lines, `Claude-Session` lines, or "Generated with" lines.
   `.githooks/commit-msg` strips them and refuses AI author identities
   (patterns in `.githooks/ai-*.txt`).
2. Ignore engineering time and cost. Quality comes first.
3. Don't duplicate. Improve the existing module or doc instead of adding a parallel
   one, and delete whatever becomes dead. Each fact has one home. Agent docs link to
   `docs/` and never restate results.
4. Before working in a directory listed in the map, read its `AGENTS.md`. When you
   finish, update it. A new directory with its own conventions gets an `AGENTS.md`
   plus `ln -s AGENTS.md CLAUDE.md`, linked from its parent.
5. Every commit-sized step gets two independent adversarial reviews, one for
   correctness/rigor and one for leanness/structure. Fix material findings and
   re-review, up to 3 rounds, then escalate to the user.
6. Use subagents only when they save time or context, never for small tasks.
7. Agents never place orders, on demo or prod. Order commands are for the user to run.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
git config core.hooksPath .githooks     # once per clone
pytest -q && ruff check .               # `data` tests skip without DATA_DIR/raw;
                                        # `slow` tests run only when -m selects them
```

For parallel work in a git worktree, give the worktree its own venv and set `DATA_DIR`
to the main checkout's absolute `data/` path.

## Map

| Path | What |
|------|------|
| `src/kalshibot/cli.py` | `kalshibot` command (typer) |
| `src/kalshibot/config.py` | Env settings, `data_dir()`, API hosts |
| [src/kalshibot/kalshi/](src/kalshibot/kalshi/AGENTS.md) | Exchange API: signing, REST, fees, order book |
| [src/kalshibot/data/](src/kalshibot/data/AGENTS.md) | Data acquisition |
| [src/kalshibot/strategies/](src/kalshibot/strategies/AGENTS.md) | Strategy types shared by research and execution |
| [src/kalshibot/research/](src/kalshibot/research/AGENTS.md) | **Section 1:** research calendar, point-in-time loader, the research runner, backtests, validation |
| [benchmarks/](benchmarks/AGENTS.md) | Execution performance evidence |
| `tests/` | pytest; `test_repo_hygiene.py` checks attribution, the doc tree, ignores, declared dependencies, append-only ledgers, and layering |
| `runs/` | Committed outputs of long runs behind docs/ results (conformance reruns, method revisions); one subdirectory or file set per run, never edited after commit |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Plan of record and research calendar |
| [docs/RESEARCH.md](docs/RESEARCH.md) | Venue and API facts, reference repos |
| [docs/STRATEGIES.md](docs/STRATEGIES.md) | Hypotheses and verdicts |
| [docs/BACKTESTING.md](docs/BACKTESTING.md) | Backtest and validation method |
| [docs/EXECUTION.md](docs/EXECUTION.md) | Execution design and benchmarks |
| [docs/LEARNING.md](docs/LEARNING.md) | Reading list |

`data/` is gitignored. Kalshi's developer agreement forbids redistributing its data.
