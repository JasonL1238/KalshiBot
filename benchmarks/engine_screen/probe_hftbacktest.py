"""hftbacktest 2.4.4 probe: two assets, trade tape (+ optional synthetic BBO), next-print market buy,
strategy-side Kalshi fee ledger, post-hoc binary settlement, 1M-event speed test."""
from pathlib import Path
import sys, time, json, math
sys.path.insert(0, str(Path(__file__).parent))
from common import *  # noqa

import numpy as np
import pandas as pd
from numba import njit
from hftbacktest import (BacktestAsset, HashMapMarketDepthBacktest, event_dtype,
                         EXCH_EVENT, LOCAL_EVENT, TRADE_EVENT, DEPTH_EVENT, BUY_EVENT, SELL_EVENT,
                         GTC, MARKET, LIMIT)
from hftbacktest.order import IOC, FILLED

TICK = 0.0001
LOT = 0.01
FLAGS = EXCH_EVENT | LOCAL_EVENT


def asset_events(df, synth_bbo: bool) -> np.ndarray:
    """Build hftbacktest event array for one ticker. If synth_bbo, emit a synthetic 1-tick-wide
    book (bid = p-tick, ask = p, huge size) at each trade timestamp, BEFORE the trade, deleting old levels."""
    rows = []
    prev_bid = prev_ask = None
    for r in df.itertuples():
        ts = int(r.ts.value)
        p = r.yes_price / 10_000
        if synth_bbo:
            if prev_ask is not None:
                rows.append((FLAGS | DEPTH_EVENT | SELL_EVENT, ts, ts, prev_ask, 0.0, 0, 0, 0.0))
                rows.append((FLAGS | DEPTH_EVENT | BUY_EVENT, ts, ts, prev_bid, 0.0, 0, 0, 0.0))
            prev_ask, prev_bid = p, round(p - TICK, 4)
            rows.append((FLAGS | DEPTH_EVENT | SELL_EVENT, ts, ts, prev_ask, 1e9, 0, 0, 0.0))
            rows.append((FLAGS | DEPTH_EVENT | BUY_EVENT, ts, ts, prev_bid, 1e9, 0, 0, 0.0))
        side = BUY_EVENT if r.taker_side == "yes" else SELL_EVENT
        rows.append((FLAGS | TRADE_EVENT | side, ts, ts, p, r.count / 100, 0, 0, 0.0))
    return np.array(rows, dtype=event_dtype)


def make_asset(ev, latency_ns=0):
    return (BacktestAsset()
            .data([ev])
            .linear_asset(1.0)
            .constant_order_latency(latency_ns, latency_ns)
            .risk_adverse_queue_model()
            .no_partial_fill_exchange()
            .trading_value_fee_model(0.0, 0.0)  # only linear fee models exist; Kalshi fee computed in strategy
            .tick_size(TICK)
            .lot_size(LOT)
            .last_trades_capacity(10_000))


@njit
def next_print_buyer(hbt, signal_ts, qty, use_market):
    # returns (fill_px, fill_qty, kalshi_fee_ledger, n_feeds)
    armed = False
    done = False
    fill_px = np.nan
    fill_qty = 0.0
    fee = 0.0
    fill_exch_ts = 0
    n = 0
    while hbt.wait_next_feed(True, 10**18) in (2, 3):
        n += 1
        now = hbt.current_timestamp
        if not armed and now >= signal_ts:
            armed = True
        trades = hbt.last_trades(0)
        if armed and not done and len(trades) > 0:
            last = trades[len(trades) - 1]
            if last.local_ts >= signal_ts:
                px = hbt.depth(0).best_ask if use_market else last.px
                if use_market:
                    hbt.submit_buy_order(0, 1, px, qty, IOC, MARKET, True)
                else:
                    hbt.submit_buy_order(0, 1, last.px, qty, GTC, LIMIT, True)
                done = True
        hbt.clear_last_trades(0)
        hbt.clear_last_trades(1)
        o = hbt.orders(0).get(1)
        if o is not None and fill_qty == 0.0 and o.exec_qty > 0:
            fill_qty = o.exec_qty
            fill_px = o.exec_price
            fill_exch_ts = o.exch_timestamp
            # strategy-side Kalshi fee ledger: ceil_to_cent(0.07*C*P*(1-P))
            fee = math.ceil(round(0.07 * fill_qty * fill_px * (1.0 - fill_px) * 100.0, 9)) / 100.0
    return fill_px, fill_qty, fee, n, fill_exch_ts


def run_small(synth_bbo, use_market, latency_ns=0):
    df = tape()
    evA = asset_events(df[df.ticker == A], synth_bbo)
    evB = asset_events(df[df.ticker == B], synth_bbo)
    hbt = HashMapMarketDepthBacktest([make_asset(evA, latency_ns), make_asset(evB, latency_ns)])
    fill_px, fill_qty, fee, n, fts = next_print_buyer(hbt, SIGNAL_TS.value, float(QTY), use_market)
    sv = hbt.state_values(0)
    pos, bal, eng_fee = sv.position, sv.balance, sv.fee
    hbt.close()
    # post-hoc binary settlement: cash + position * payoff - strategy-side fee
    pnl = bal + pos * SETTLEMENT[A] - fee
    return {"synth_bbo": synth_bbo, "order": "MARKET/IOC" if use_market else "LIMIT/GTC@print",
            "latency_ns": latency_ns, "fill_px": fill_px, "fill_qty": fill_qty, "position": pos,
            "engine_balance": bal, "engine_fee": eng_fee, "kalshi_fee_ledger": fee,
            "pnl_after_posthoc_settlement": round(pnl, 6), "feeds": n, "order_exch_ts": str(pd.Timestamp(fts, tz="UTC")) if fts else None}


@njit
def count_loop(hbt):
    n = 0
    while hbt.wait_next_feed(False, 10**18) == 2:
        n += 1
        hbt.clear_last_trades(0)
        hbt.clear_last_trades(1)
    return n


def speed():
    big = big_tape_cached()
    out = {}
    tickers = sorted(big.ticker.unique())
    t0 = time.perf_counter()
    evs = []
    for t in tickers:
        sub = big[big.ticker == t]
        ev = np.zeros(len(sub), dtype=event_dtype)
        ev["ev"] = FLAGS | TRADE_EVENT | np.where(sub.taker_side.values == "yes", BUY_EVENT, SELL_EVENT)
        ev["exch_ts"] = sub.ts.values.astype("datetime64[ns]").astype(np.int64)
        ev["local_ts"] = ev["exch_ts"]
        ev["px"] = sub.yes_price.values / 10_000
        ev["qty"] = sub["count"].values / 100
        evs.append(ev)
    out["prep_s"] = round(time.perf_counter() - t0, 2)
    # warm up JIT on small data
    small = run_small(True, True)
    df = tape()
    w = HashMapMarketDepthBacktest([make_asset(asset_events(df[df.ticker == A], False)), make_asset(asset_events(df[df.ticker == B], False))])
    count_loop(w); w.close()  # JIT warm-up excluded from timing
    hbt = HashMapMarketDepthBacktest([make_asset(e) for e in evs])
    t1 = time.perf_counter()
    n = count_loop(hbt)
    dt = time.perf_counter() - t1
    hbt.close()
    out.update({"n_events": int(sum(len(e) for e in evs)), "feeds_seen_count_loop": int(n), "count_loop_run_s": round(dt, 3),
                "count_loop_events_per_s": round(n / dt)})
    hbt = HashMapMarketDepthBacktest([make_asset(e) for e in evs])
    t1 = time.perf_counter()
    _, _, _, n2, _ = next_print_buyer(hbt, int(big.ts.iloc[500_000].value), 10.0, False)
    dt2 = time.perf_counter() - t1
    hbt.close()
    out.update({"strategy_loop_feeds": int(n2), "strategy_loop_run_s": round(dt2, 3), "strategy_loop_events_per_s": round(n2 / dt2)})
    return out


if __name__ == "__main__":
    res = {"reference": reference()}
    for synth in (False, True):
        for mkt in (True, False):
            key = f"synth_bbo={synth},market={mkt}"
            try:
                res[key] = run_small(synth, mkt)
            except Exception as e:  # noqa
                res[key] = {"error": repr(e)}
            print(key, res[key])
    res["synth_bbo_market_latency_5min"] = run_small(True, True, latency_ns=5 * 60 * 10**9)
    print("latency 5min", res["synth_bbo_market_latency_5min"])
    res["speed"] = speed()
    print("speed", res["speed"])
    print("reference", res["reference"])
    json.dump(res, open(OUT + "/out_hftbacktest.json", "w"), indent=1, default=str)
