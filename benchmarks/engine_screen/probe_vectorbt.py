"""vectorbt 1.0.0 probe: wide (time x instrument) matrix built from a trade tape; next-print entry;
settlement as a synthetic exit row at the payoff; Kalshi fee via (a) precomputed fixed_fees array and
(b) a numba order_func computing ceil_to_cent(0.07*C*P*(1-P)) at order time. 1M-event speed + param scan."""
from pathlib import Path
import sys, time, json, math
sys.path.insert(0, str(Path(__file__).parent))
from common import *  # noqa

import numpy as np
import pandas as pd
import vectorbt as vbt
from numba import njit
from vectorbt.portfolio import nb as pnb
from vectorbt.portfolio.enums import NoOrder


def wide_from_tape(df, settle_ts=None, settlement=None):
    px = df.pivot_table(index="ts", columns="ticker", values="price", aggfunc="last")
    if settle_ts is not None:
        px.loc[settle_ts] = np.nan
    px = px.sort_index()
    close = px.ffill()
    if settle_ts is not None:
        for t, v in settlement.items():
            close.loc[settle_ts, t] = v  # settlement = synthetic final print at the payoff
    return px, close


@njit
def kfee_nb(size, price):
    return math.ceil(round(0.07 * abs(size) * price * (1.0 - price) * 100.0, 9)) / 100.0


@njit
def order_func_nb(c, size_arr, price_arr):
    s = size_arr[c.i, c.col]
    if s == 0.0 or np.isnan(s):
        return NoOrder
    p = price_arr[c.i, c.col]
    return pnb.order_nb(size=s, price=p, fixed_fees=kfee_nb(s, p))


def probe():
    df = tape()
    px, close = wide_from_tape(df, SETTLE_TS, SETTLEMENT)
    # next A print strictly after the signal
    entry_ts = px.index[(px.index > SIGNAL_TS) & px[A].notna().values][0]
    size = pd.DataFrame(np.nan, index=px.index, columns=px.columns)  # NaN = no order (0 still validates price)
    size.loc[entry_ts, A] = QTY
    size.loc[SETTLE_TS, A] = -QTY  # settlement modelled as an exit at the payoff price
    price = close.copy()
    fixed = pd.DataFrame(np.vectorize(lambda s, p: kalshi_fee(s, p) if s == s and s else 0.0)(size.values, price.values), index=px.index, columns=px.columns)
    out = {"entry_ts": str(entry_ts), "reference": reference(), "index_len": len(px)}
    pf = vbt.Portfolio.from_orders(close=close, size=size, price=price, fixed_fees=fixed,
                                   init_cash=START_CASH, cash_sharing=True, group_by=True)
    out["from_orders_fixed_fee_array"] = {
        "final_value": float(pf.final_value()), "pnl": round(float(pf.final_value()) - START_CASH, 6),
        "orders": pf.orders.records_readable[["Timestamp", "Column", "Size", "Price", "Fees", "Side"]].astype(str).to_dict("records")}
    pf2 = vbt.Portfolio.from_order_func(close, order_func_nb, size.values, price.values,
                                        init_cash=START_CASH, cash_sharing=True, group_by=True)
    out["from_order_func_numba_fee"] = {
        "final_value": float(pf2.final_value()), "pnl": round(float(pf2.final_value()) - START_CASH, 6),
        "orders": pf2.orders.records_readable[["Timestamp", "Column", "Size", "Price", "Fees", "Side"]].astype(str).to_dict("records")}
    # what happens without the settlement row: position is only marked to the last print (0.75)
    px3, close3 = wide_from_tape(df)
    size3 = pd.DataFrame(np.nan, index=px3.index, columns=px3.columns); size3.loc[entry_ts, A] = QTY
    fixed3 = pd.DataFrame(np.vectorize(lambda s, p: kalshi_fee(s, p) if s == s and s else 0.0)(size3.values, close3.values), index=px3.index, columns=px3.columns)
    pf3 = vbt.Portfolio.from_orders(close=close3, size=size3, price=close3, fixed_fees=fixed3,
                                    init_cash=START_CASH, cash_sharing=True, group_by=True)
    # binary settlement at $0: can an exit be booked at price 0.0?
    try:
        c4 = pd.DataFrame({"X": [0.5, 0.0]}); s4 = pd.DataFrame({"X": [10.0, -10.0]})
        pf4 = vbt.Portfolio.from_orders(close=c4, size=s4, price=c4, init_cash=START_CASH)
        out["exit_at_price_0"] = float(pf4.final_value())
    except Exception as e:
        out["exit_at_price_0"] = "REJECTED: " + repr(e)
    c5 = pd.DataFrame({"X": [0.5, 0.0]}); s5 = pd.DataFrame({"X": [10.0, np.nan]})
    pf5 = vbt.Portfolio.from_orders(close=c5, size=s5, price=c5, init_cash=START_CASH)
    out["hold_and_mark_close_0"] = float(pf5.final_value())
    out["no_settlement_row_mark_to_last_print"] = round(float(pf3.final_value()) - START_CASH, 6)
    return out


def speed():
    big = big_tape_cached()
    big["price"] = big.yes_price / 10_000
    t0 = time.perf_counter()
    big = big.drop_duplicates(["ts", "ticker"], keep="last")
    px = big.pivot(index="ts", columns="ticker", values="price")
    close = px.ffill().bfill()
    t_pivot = time.perf_counter() - t0
    n_rows, n_cols = close.shape
    size = np.full(close.shape, np.nan); size[n_rows // 2, 0] = 10.0; size[-1, 0] = -10.0
    price = close.values
    fixed = np.zeros(close.shape); fixed[n_rows // 2, 0] = kalshi_fee(10, price[n_rows // 2, 0]); fixed[-1, 0] = kalshi_fee(10, price[-1, 0])
    # warm up numba
    vbt.Portfolio.from_order_func(close.iloc[:100], order_func_nb, size[:100], price[:100], init_cash=START_CASH)
    vbt.Portfolio.from_orders(close=close.iloc[:100], size=size[:100], price=price[:100], fixed_fees=fixed[:100], init_cash=START_CASH)
    t1 = time.perf_counter()
    pf = vbt.Portfolio.from_orders(close=close, size=size, price=price, fixed_fees=fixed, init_cash=START_CASH)
    fv = pf.final_value().sum()
    t_orders = time.perf_counter() - t1
    t2 = time.perf_counter()
    pf2 = vbt.Portfolio.from_order_func(close, order_func_nb, size, price, init_cash=START_CASH)
    fv2 = pf2.final_value().sum()
    t_func = time.perf_counter() - t2
    # parameter scan: 20 different entry rows for instrument 0 at once (columns = params)
    K = 20
    c0 = np.repeat(close.values[:, :1], K, axis=1)
    sz = np.full(c0.shape, np.nan)
    rows = np.linspace(1000, n_rows - 1000, K).astype(int)
    sz[rows, np.arange(K)] = 10.0; sz[-1, :] = -10.0
    fx = np.zeros_like(c0); fx[rows, np.arange(K)] = np.vectorize(kalshi_fee)(10.0, c0[rows, 0]); fx[-1, :] = kalshi_fee(10, c0[-1, 0])
    t3 = time.perf_counter()
    pf3 = vbt.Portfolio.from_orders(close=c0, size=sz, price=c0, fixed_fees=fx, init_cash=START_CASH)
    fv3 = pf3.final_value()
    t_scan = time.perf_counter() - t3
    return {"wide_shape": [n_rows, n_cols], "pivot_s": round(t_pivot, 2), "from_orders_s": round(t_orders, 3),
            "from_order_func_s": round(t_func, 3), "param_scan_cols": K, "param_scan_rows": n_rows,
            "param_scan_s": round(t_scan, 3), "cells_per_s_scan": round(c0.size / t_scan)}


if __name__ == "__main__":
    res = {"probe": probe()}
    print(json.dumps(res["probe"], indent=1, default=str))
    res["speed"] = speed()
    print(json.dumps(res["speed"], indent=1))
    json.dump(res, open(OUT + "/out_vectorbt.json", "w"), indent=1, default=str)
