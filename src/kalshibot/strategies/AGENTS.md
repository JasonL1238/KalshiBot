# strategies/: what each strategy does

Shared by research (which backtests strategies) and execution (which will run them).
Never import `kalshibot.research` or `kalshibot.execution` here (the hygiene test
enforces it). Hypotheses, pre-registrations and verdicts:
[docs/STRATEGIES.md](../../../docs/STRATEGIES.md).

| File | What |
|------|------|
| `base.py` | `Strategy` (family, `max_hold`, `min_edge`, configurations, intents) and `MarketData` protocols; `INTENT_COLUMNS`, the intent schema every tier takes |
| `favorite_longshot.py` | H1: buy the favorite's side as a taker at the day's first print past a threshold |
| `deep_bids.py` | H8: while a game winner is live, a NO bid on each team market a fixed depth under its last trade, requoted on a fixed clock; `LEAGUES` is the universe |

`FAMILIES` (`__init__.py`) maps each family to its strategy; `kalshibot research` finds
families there.

## Rules for code here

- A strategy sees only what its `MarketData` shows at each decision time; never read
  files directly. The research runner evaluates a window in parts (`research/run.py`'s
  `CHUNK`) and shows the strategy nothing after the part it decides in. Each strategy's
  tests include a truncation test (its intents before t don't change when the window
  ends at t) and a split test (a window split at a UTC midnight gives the halves'
  intents).
- Configurations are fixed at pre-registration: adding one later is a new trial in the
  family's ledger, not an edit. So are `max_hold` and `min_edge`: every trial records
  them, and `confirm` refuses if they changed since discovery.
