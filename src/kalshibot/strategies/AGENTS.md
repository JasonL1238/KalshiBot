# strategies/: what each strategy does

Shared by research (which backtests strategies) and execution (which will run them).
Never import `kalshibot.research` or `kalshibot.execution` here (the hygiene test
enforces it). Hypotheses, pre-registrations and verdicts:
[docs/STRATEGIES.md](../../../docs/STRATEGIES.md).

| File | What |
|------|------|
| `base.py` | `Strategy` (family, `max_hold`, configurations, intents) and `MarketData` protocols; `INTENT_COLUMNS`, the intent schema every tier takes |
| `favorite_longshot.py` | H1: buy the favorite's side as a taker at the day's first print past a threshold |

`FAMILIES` (`__init__.py`) maps each family to its strategy; `kalshibot research` finds
families there.

## Rules for code here

- A strategy sees only what its `MarketData` shows at each decision time; never read
  files directly. The research runner shows it nothing after the window it decides in;
  within the window, each strategy's tests include a truncation test (its intents
  before t don't change when the window ends at t).
- Configurations are fixed at pre-registration: adding one later is a new trial in the
  family's ledger, not an edit.
