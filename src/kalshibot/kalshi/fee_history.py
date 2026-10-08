"""Which fee terms applied to a series at a given time.

Precedence, per series and instant:
1. Kalshi's fee-change log (`GET /series/fee_changes`): the latest entry at or before
   the instant.
2. From the log's start, a series the log never touches: its current `fee_type` and
   `fee_multiplier` from the series table.
3. Otherwise (before the log's start, or before a logged series' first entry):
   `fee_history.json`, built from the archived fee schedules (the 2021 taker rate, the
   2022–2026 index discount, the 2024 election waivers, the May–Sept 2025 maker lists).

The 2026 rounding change applies on top. Each component (rounding, taker rate, maker
fee, waiver) carries every value it could plausibly have had at that instant. `terms`
gives the best estimate; `plausible` gives every combination, so research can charge the
costliest one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from itertools import product
from pathlib import Path

HISTORY = json.loads((Path(__file__).with_name("fee_history.json")).read_text())
TAKER = Fraction(7, 100)
MAKER_SHARE = {
    "quadratic": Fraction(0),
    "quadratic_with_maker_fees": Fraction(1, 4),
    "quadratic_with_combo_maker_fees": Fraction(1, 2),
}


def _t(text: str) -> datetime:
    return datetime.fromisoformat(text)


@dataclass(frozen=True)
class FeeTerms:
    taker: Fraction  # coefficient on contracts × P × (1 − P) for takers
    maker: Fraction  # the same for makers (0: makers pay nothing)
    maker_flat: Fraction  # $ per contract for makers (the 2025 flat fee)
    rounding: str  # "cent": each fill rounded up to $0.01; "balance": the 2026 method
    uncertain: bool = False  # other regimes were possible here (see FeeSchedule.plausible)

    @classmethod
    def from_series(cls, fee_type: str, multiplier: str | float, rounding: str) -> FeeTerms:
        """From a series' `fee_type` and `fee_multiplier` (0 is a waiver)."""
        if fee_type not in MAKER_SHARE:
            raise ValueError(f"unsupported fee_type {fee_type!r}")
        m = Fraction(str(multiplier))
        if m < 0:
            raise ValueError(f"fee_multiplier must be >= 0, got {multiplier!r}")
        return cls(TAKER * m, TAKER * m * MAKER_SHARE[fee_type], Fraction(0), rounding)


Maker = tuple[Fraction, Fraction]  # (coefficient, flat $ per contract)


class FeeSchedule:
    """`log`: (series_ticker, scheduled_ts, fee_type, fee_multiplier) rows from
    fee_changes.parquet. `current`: series_ticker -> (fee_type, fee_multiplier, category)
    from series.parquet. Both are required; `Loader.fee_schedule()` builds them."""

    def __init__(self, log: list[tuple[str, datetime, str, str]],
                 current: dict[str, tuple[str, str, str]]):
        self._log: dict[str, list[tuple[datetime, str, str]]] = {}
        for series, ts, fee_type, mult in sorted(log, key=lambda r: r[1]):
            self._log.setdefault(series, []).append((ts, fee_type, mult))
        self._current = current
        self._log_start = _t(HISTORY["log_start"])

    def terms(self, series: str, ts: datetime) -> FeeTerms:
        rounding, takers, makers, waived = self._components(series, ts)
        uncertain = max(map(len, (rounding, takers, makers, waived))) > 1
        return _assemble(rounding[0], takers[0], makers[0], waived[0], uncertain)

    def plausible(self, series: str, ts: datetime) -> set[FeeTerms]:
        """Every combination of the components' plausible values at `ts`."""
        parts = self._components(series, ts)
        uncertain = max(map(len, parts)) > 1
        return {_assemble(*combo, uncertain) for combo in product(*parts)}

    def _components(self, series: str, ts: datetime):
        rounding = _rounding(ts)
        entries = [e for e in self._log.get(series, []) if e[0] <= ts]
        if entries:  # the log is authoritative: only rounding can be in doubt
            _, fee_type, mult = entries[-1]
            base = FeeTerms.from_series(fee_type, mult, rounding[0])
            return rounding, [base.taker], [(base.maker, base.maker_flat)], [False]
        current = self._current.get(series)
        if ts >= self._log_start and current and series not in self._log:
            base = FeeTerms.from_series(current[0], current[1], rounding[0])
            return rounding, [base.taker], [(base.maker, base.maker_flat)], [False]
        makers = _maker(series, ts)
        if current and series not in self._log and ts >= _t(HISTORY["maker"][-1]["from"]):
            # Between the last archived list and the log's start, the series' current
            # state is as plausible as the list's.
            now = FeeTerms.from_series(current[0], current[1], rounding[0])
            makers = list(dict.fromkeys([*makers, (now.maker, now.maker_flat)]))
        category = current[2] if current else None
        return (rounding, _taker(series, ts, logged=series in self._log), makers,
                _waiver(series, category, ts))


def _assemble(rounding: str, taker: Fraction, maker: Maker, waived: bool,
              uncertain: bool) -> FeeTerms:
    if waived:
        return FeeTerms(Fraction(0), Fraction(0), Fraction(0), rounding, uncertain)
    return FeeTerms(taker, maker[0], maker[1], rounding, uncertain)


def _in(ts: datetime, windows: list[list[str]]) -> bool:
    return any(_t(a) <= ts < _t(b) for a, b in windows)


def _rounding(ts: datetime) -> list[str]:
    r = HISTORY["rounding"]
    best = "cent" if ts < _t(r["cent_until"]) else "balance"
    return [best, "balance" if best == "cent" else "cent"] if _in(ts, r["uncertain"]) else [best]


def _latest(rules: list[dict], ts: datetime) -> dict | None:
    live = [r for r in rules if _t(r["from"]) <= ts]
    return max(live, key=lambda r: _t(r["from"])) if live else None


def _taker(series: str, ts: datetime, logged: bool) -> list[Fraction]:
    """KX-prefixed index series the log later moves off the discount had it (certain);
    other KX index series may or may not have."""
    best = Fraction(_latest(HISTORY["taker"], ts)["rate"])
    later = [Fraction(r["rate"]) for r in HISTORY["taker"] if _in(ts, r.get("uncertain", []))]
    rates = list(dict.fromkeys([best, *later]))
    index = HISTORY["index_taker"]
    if _t(index["from"]) <= ts < _t(index["until"]):
        kx = series.startswith(tuple(index["uncertain_prefixes"]))
        if series.startswith(tuple(index["prefixes"])) or (kx and logged):
            return [Fraction(index["rate"])]
        if kx:
            return list(dict.fromkeys([Fraction(index["rate"]), *rates]))
    return rates


def _maker_of(rule: dict | None, series: str, ts: datetime) -> Maker:
    if rule is None:
        return Fraction(0), Fraction(0)
    dropped = {s for cut, names in rule.get("except_after", {}).items()
               if ts >= _t(cut) for s in names}
    if series not in rule["series"] or series in dropped:
        return Fraction(0), Fraction(0)
    rate = Fraction(rule["rate"])
    return (Fraction(0), rate) if rule["kind"] == "flat" else (rate, Fraction(0))


def _maker(series: str, ts: datetime) -> list[Maker]:
    best = _maker_of(_latest(HISTORY["maker"], ts), series, ts)
    later = [_maker_of(r, series, _t(r["from"])) for r in HISTORY["maker"]
             if _in(ts, r.get("uncertain", []))]
    return list(dict.fromkeys([best, *later]))


def _waiver(series: str, category: str | None, ts: datetime) -> list[bool]:
    """Named series are waived with certainty inside a waiver's dates. Other series in its
    category, and every member near an unpinned start or end, may or may not have been."""
    applies, unsure = False, False
    for w in HISTORY["waivers"]:
        named = series in w["named"] or series.startswith(tuple(w["named_prefixes"]))
        if not (named or category == w["category"]):
            continue
        inside = _t(w["from"]) <= ts < _t(w["until"])
        applies |= inside and named
        unsure |= (inside and not named) or _in(ts, w["uncertain"])
    return [applies, not applies] if unsure else [applies]
