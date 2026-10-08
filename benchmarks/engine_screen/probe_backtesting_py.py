"""backtesting.py 0.6.6 probe: single-instrument engine. Each print = degenerate OHLC bar; B is only
available as an extra (non-tradable) column; commission=callable(size, price); trade_on_close to fill at
the print; settlement as a synthetic final bar + finalize_trades; speed on 1M prints."""
from pathlib import Path
import sys, time, json, math, warnings
sys.path.insert(0, str(Path(__file__).parent))
from common import *  # noqa
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from backtesting import Backtest, Strategy

FEE_LOG = []


def kalshi_commission(order_size, price):
    f = kalshi_fee(abs(order_size), price)
    FEE_LOG.append((order_size, price, f))
    return f


def bars(sub, settle_px=None, n_settle=1):
    d = pd.DataFrame({"Open": sub.price.values, "High": sub.price.values, "Low": sub.price.values,
                      "Close": sub.price.values, "Volume": sub.contracts.values},
                     index=pd.DatetimeIndex(sub.ts))
    if settle_px is not None:
        for k in range(n_settle):  # backtesting.py never calls next() on / closes at the final bar
            d.loc[SETTLE_TS + pd.Timedelta(seconds=k)] = [settle_px] * 4 + [0.0]
    return d


class NextPrintBuyer(Strategy):
    signal_ns = 0
    qty = QTY

    def init(self):
        self.done = False

    def next(self):
        ts = self.data.index[-1]
        if not self.done and ts.value > self.signal_ns:
            self.buy(size=self.qty)  # trade_on_close=True -> filled at this bar's close = this print
            self.done = True


def run_small(trade_on_close=True, settle=True, n_settle=1):
    FEE_LOG.clear()
    df = tape()
    data = bars(df[df.ticker == A], SETTLEMENT[A] if settle else None, n_settle)
    # B can only ride along as an extra column (forward-filled onto A's timeline); it is not tradable
    b = df[df.ticker == B].set_index("ts").price
    data["B_last"] = b.reindex(data.index.union(b.index)).ffill().reindex(data.index).values
    bt = Backtest(data, NextPrintBuyer, cash=START_CASH, commission=kalshi_commission,
                  trade_on_close=trade_on_close, finalize_trades=True)
    st = bt.run(signal_ns=SIGNAL_TS.value)
    tr = st._trades[["Size", "EntryPrice", "ExitPrice", "EntryTime", "ExitTime", "PnL", "Commission"]] if len(st._trades) else pd.DataFrame()
    return {"trade_on_close": trade_on_close, "settle_bar": settle, "n_settle_bars": n_settle, "final_equity": float(st["Equity Final [$]"]),
            "pnl": round(float(st["Equity Final [$]"]) - START_CASH, 6), "trades": tr.astype(str).to_dict("records"),
            "fee_calls": list(FEE_LOG)}


def frac_size_check():
    df = tape()
    data = bars(df[df.ticker == A], SETTLEMENT[A])

    class Frac(Strategy):
        def init(self): pass
        def next(self):
            if len(self.data) == 2:
                try:
                    self.buy(size=1.5)
                except Exception as e:  # noqa
                    raise
    try:
        Backtest(data, Frac, cash=START_CASH, finalize_trades=True).run()
        return "accepted"
    except Exception as e:
        return "REJECTED: " + repr(e)[:200]


def zero_settle_check():
    df = tape()
    data = bars(df[df.ticker == B], 0.0, 2)

    class Hold(Strategy):
        def init(self): pass
        def next(self):
            if len(self.data) == 2:
                self.buy(size=10)
    try:
        st = Backtest(data, Hold, cash=START_CASH, commission=kalshi_commission, trade_on_close=True,
                      finalize_trades=True).run()
        return {"final_equity": float(st["Equity Final [$]"]), "trades": st._trades[["EntryPrice", "ExitPrice", "PnL"]].astype(str).to_dict("records")}
    except Exception as e:
        return "ERROR: " + repr(e)[:300]


def speed(n):
    big = big_tape_cached()
    k0 = big[big.ticker == "KXBIG-0"]
    # single instrument only: replicate to reach n bars by using both tickers' prints as one series
    sub = big.iloc[:n].assign(price=big.yes_price.iloc[:n] / 10_000, contracts=big["count"].iloc[:n] / 100)
    data = bars(sub)
    data = data[~data.index.duplicated()]
    t0 = time.perf_counter()
    bt = Backtest(data, NextPrintBuyer, cash=1e9, commission=kalshi_commission, trade_on_close=True, finalize_trades=True)
    st = bt.run(signal_ns=int(data.index[len(data) // 2].value))
    dt = time.perf_counter() - t0
    return {"n_bars": len(data), "run_s": round(dt, 2), "bars_per_s": round(len(data) / dt)}


if __name__ == "__main__":
    out = {"reference": reference()}
    out["trade_on_close_settle"] = run_small(True, True)
    out["trade_on_close_two_settle_bars"] = run_small(True, True, 2)
    out["next_open_settle"] = run_small(False, True)
    out["trade_on_close_no_settle_bar"] = run_small(True, False)
    out["fractional_size_1.5"] = frac_size_check()
    out["settle_at_zero_B"] = zero_settle_check()
    print(json.dumps(out, indent=1, default=str))
    out["speed"] = speed(1_000_000)
    print(json.dumps(out["speed"], indent=1))
    json.dump(out, open(OUT + "/out_backtesting_py.json", "w"), indent=1, default=str)
