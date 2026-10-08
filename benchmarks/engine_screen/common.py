"""Shared synthetic Kalshi-style tape, fee function and reference PnL for the engine screen.

Tape schema mirrors the KalshiBot trade tape:
  ticker, ts (UTC), yes_price (int, $0.0001 units), count (int, 0.01-contract units), taker_side
The tape deliberately lives on a weekend / overnight (Sat 2026-10-03 .. Sun 2026-10-04 UTC)
so that any engine which forces an exchange calendar will drop or reject it.
The fill print is sub-penny (0.4213) to test price precision.
"""
from __future__ import annotations

import math
from pathlib import Path
import numpy as np
import pandas as pd

OUT = str(Path(__file__).parent / "out")  # probe outputs (gitignored)
Path(OUT).mkdir(exist_ok=True)

FEE_RATE = 0.07
QTY = 10  # contracts to buy
START_CASH = 1000.0

A, B = "KXTEST-A", "KXTEST-B"

_ROWS = [
    # ticker, ts, yes_price(1e-4 $), count(1e-2 ct), taker_side
    (A, "2026-10-03 09:00:00", 3500, 500, "yes"),
    (B, "2026-10-03 10:00:00", 6000, 300, "no"),
    (A, "2026-10-03 11:30:00", 3600, 250, "no"),
    (B, "2026-10-03 13:00:00", 5800, 700, "yes"),
    (A, "2026-10-03 14:05:00", 3800, 1000, "yes"),
    (A, "2026-10-03 18:45:30", 3700, 150, "no"),
    (B, "2026-10-03 20:00:00", 5500, 200, "no"),
    (A, "2026-10-03 22:10:00", 3900, 300, "yes"),
    # ---- signal fires at 2026-10-03 23:00:00 ----
    (B, "2026-10-04 00:30:00", 5000, 900, "no"),   # B print between signal and A fill (must NOT be used)
    (A, "2026-10-04 01:15:00", 4213, 800, "yes"),  # <-- expected fill: next A print after signal
    (A, "2026-10-04 04:20:00", 4500, 1200, "yes"),
    (B, "2026-10-04 08:00:00", 4000, 500, "no"),
    (A, "2026-10-04 10:00:00", 5200, 400, "no"),
    (A, "2026-10-04 16:30:00", 6000, 2000, "yes"),
    (B, "2026-10-04 19:00:00", 3000, 1000, "no"),
    (A, "2026-10-04 21:00:00", 7500, 100, "yes"),
]

SIGNAL_TS = pd.Timestamp("2026-10-03 23:00:00", tz="UTC")
SETTLE_TS = pd.Timestamp("2026-10-05 00:00:00", tz="UTC")
SETTLEMENT = {A: 1.0, B: 0.0}  # A resolves YES, B resolves NO


def tape() -> pd.DataFrame:
    df = pd.DataFrame(_ROWS, columns=["ticker", "ts", "yes_price", "count", "taker_side"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["price"] = df["yes_price"] / 10_000.0
    df["contracts"] = df["count"] / 100.0
    return df


def write_csv(path: str) -> None:
    tape().to_csv(path, index=False)


def fee_exact_cents(contracts_x100: int, price_x1e4: int, rate_x100: int = 7) -> int:
    """Exact integer fee in cents: ceil(rate * C * P * (1-P) * 100)."""
    num = rate_x100 * contracts_x100 * price_x1e4 * (10_000 - price_x1e4)
    den = 100 * 100 * 10**8 // 100  # rate/100 * C/100 * P(1-P)/1e8 * 100 cents
    return -(-num // den)


def kalshi_fee(contracts: float, price: float, rate: float = FEE_RATE) -> float:
    """Float version used inside engines: ceil_to_cent(rate*C*P*(1-P))."""
    raw = rate * abs(contracts) * price * (1.0 - price)
    return math.ceil(round(raw * 100.0, 9)) / 100.0


def reference() -> dict:
    df = tape()
    a = df[(df.ticker == A) & (df.ts > SIGNAL_TS)].iloc[0]
    p = float(a.price)
    fee = kalshi_fee(QTY, p)
    assert abs(fee - fee_exact_cents(QTY * 100, int(a.yes_price)) / 100) < 1e-12
    pnl = QTY * (SETTLEMENT[A] - p) - fee
    return {"fill_ts": str(a.ts), "fill_px": p, "fee": fee, "pnl": round(pnl, 6),
            "final_cash": round(START_CASH + pnl, 6)}


def big_tape(n: int = 1_000_000, n_inst: int = 2, seed: int = 7) -> pd.DataFrame:
    """Synthetic 1M-trade tape for speed tests: random-walk prices in [0.01, 0.99]."""
    rng = np.random.default_rng(seed)
    ts = pd.Timestamp("2025-01-01", tz="UTC").value + np.cumsum(rng.integers(1, 3_000_000_000, n))  # ns, ~1.5s apart
    inst = rng.integers(0, n_inst, n)
    steps = rng.choice([-1, 0, 1], size=n, p=[0.3, 0.4, 0.3])
    px = np.empty(n, dtype=np.int64)
    cur = np.full(n_inst, 5000)
    for i in range(n):  # fine for 1M once; cached to parquet
        k = inst[i]
        cur[k] = min(9900, max(100, cur[k] + steps[i] * 100))
        px[i] = cur[k]
    cnt = rng.integers(1, 50, n) * 100
    side = np.where(rng.random(n) < 0.5, "yes", "no")
    return pd.DataFrame({
        "ticker": np.array([f"KXBIG-{k}" for k in range(n_inst)])[inst],
        "ts": pd.to_datetime(ts, utc=True),
        "yes_price": px, "count": cnt, "taker_side": side,
    })


def big_tape_cached(path: str = OUT + "/big_tape.parquet") -> pd.DataFrame:
    import os
    if os.path.exists(path):
        return pd.read_parquet(path)
    df = big_tape()
    df.to_parquet(path)
    return df


if __name__ == "__main__":
    print(tape())
    print(reference())
    write_csv(OUT + "/tape.csv")
    big_tape_cached()
