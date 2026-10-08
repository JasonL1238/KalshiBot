"""zipline-reloaded 3.1.1 probe: custom minute bundle on the exchange_calendars '24/7' calendar,
prints aggregated to 1-minute OHLCV bars, custom CommissionModel, settlement via synthetic bar,
precision checks (ohlc_ratio), zero-price settlement check, minute-loop speed."""
from pathlib import Path
import os, sys, time, json, math, warnings
ROOT = str(Path(__file__).parent / "out" / "zipline_root")
os.environ["ZIPLINE_ROOT"] = ROOT
os.makedirs(ROOT, exist_ok=True)
sys.path.insert(0, str(Path(__file__).parent))
from common import *  # noqa
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from zipline import run_algorithm
from zipline.api import symbol, order, set_commission, set_slippage, record, get_datetime
from zipline.data.bundles import register, ingest
from zipline.data.bcolz_minute_bars import BcolzMinuteBarWriter
from zipline.finance.commission import CommissionModel
from zipline.finance.slippage import FixedSlippage
from zipline.assets import Equity
from zipline.utils.calendar_utils import get_calendar

CAL = get_calendar("24/7")
FEES = []


def minute_frames(df, settle=True):
    out = {}
    for t in (A, B):
        sub = df[df.ticker == t].copy()
        sub["minute"] = sub.ts.dt.floor("min")
        g = sub.groupby("minute")
        m = pd.DataFrame({"open": g.price.first(), "high": g.price.max(), "low": g.price.min(),
                          "close": g.price.last(), "volume": g["count"].sum()})  # volume in 0.01-contract units (uint32)
        if settle:
            m.loc[SETTLE_TS] = [SETTLEMENT[t]] * 4 + [1]
        out[t] = m.sort_index()
    return out


def make_ingest(frames, ratio):
    def _ingest(environ, asset_db_writer, minute_bar_writer, daily_bar_writer, adjustment_writer,
                calendar, start_session, end_session, cache, show_progress, output_dir):
        syms = list(frames)
        first = min(f.index.min() for f in frames.values()).tz_convert(None).normalize()
        last = max(f.index.max() for f in frames.values()).tz_convert(None).normalize()
        eq = pd.DataFrame({"symbol": syms, "asset_name": syms, "exchange": "KALSHI",
                           "start_date": first, "end_date": last, "auto_close_date": last + pd.Timedelta(days=1)})
        asset_db_writer.write(equities=eq, exchanges=pd.DataFrame({"exchange": ["KALSHI"], "country_code": ["US"]}))
        w = minute_bar_writer
        if ratio is not None:  # hack: re-create writer on same dir with a finer price ratio than the default 1000
            w = BcolzMinuteBarWriter(minute_bar_writer._rootdir, calendar, start_session, end_session,
                                     minutes_per_day=1440, default_ohlc_ratio=ratio)
        w.write([(sid, frames[s]) for sid, s in enumerate(syms)])
        sessions = calendar.sessions_in_range(first, last)
        daily = []
        for sid, s in enumerate(syms):
            d = frames[s].resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
            d.index = d.index.tz_convert(None)
            d = d.reindex(sessions).ffill().fillna(0)
            daily.append((sid, d))
        daily_bar_writer.write(daily)
        adjustment_writer.write()
    return _ingest


class KalshiCommission(CommissionModel):
    allowed_asset_types = (Equity,)

    def calculate(self, order_, transaction):
        f = kalshi_fee(abs(transaction.amount), transaction.price)
        FEES.append((str(transaction.dt), transaction.amount, transaction.price, f))
        return f


def run(bundle, frames, qty=QTY, end="2026-10-05"):
    FEES.clear()
    state = {"done": False, "fill": None, "b_at_settle": None}

    def initialize(ctx):
        ctx.a = symbol(A)
        ctx.b = symbol(B)
        set_commission(us_equities=KalshiCommission())
        set_slippage(us_equities=FixedSlippage(spread=0.0))

    def handle_data(ctx, data):
        now = get_datetime()
        if not state["done"] and now >= SIGNAL_TS:
            order(ctx.a, qty)  # fills at the first later bar of A with volume > 0 (= next print minute)
            state["done"] = True
        if now == SETTLE_TS:
            state["b_at_settle"] = float(data.current(ctx.b, "close"))
            state["a_at_settle"] = float(data.current(ctx.a, "close"))
            state["pos_a"] = ctx.portfolio.positions[ctx.a].amount

    t0 = time.perf_counter()
    perf = run_algorithm(start=pd.Timestamp("2026-10-03"), end=pd.Timestamp(end), initialize=initialize,
                         handle_data=handle_data, capital_base=START_CASH, data_frequency="minute",
                         bundle=bundle, trading_calendar=CAL,
                         benchmark_returns=pd.Series(0.0, index=CAL.sessions_in_range("2026-10-03", end).tz_localize("UTC")))
    dt = time.perf_counter() - t0
    txns = [t for day in perf.transactions for t in day]
    return {"final_portfolio_value": float(perf.portfolio_value.iloc[-1]),
            "pnl": round(float(perf.portfolio_value.iloc[-1]) - START_CASH, 6),
            "transactions": [{"dt": str(t["dt"]), "amount": t["amount"], "price": t["price"]} for t in txns],
            "commissions": list(FEES), "state": state, "run_s": round(dt, 2)}


def main():
    df = tape()
    out = {"reference": reference()}
    frames = minute_frames(df)
    for name, ratio in (("kalshi_default_ratio", None), ("kalshi_ratio_1e4", 10_000)):
        register(name, make_ingest(frames, ratio), calendar_name="24/7", minutes_per_day=1440,
                 start_session=pd.Timestamp("2026-10-03"), end_session=pd.Timestamp("2026-10-05"))
        try:
            ingest(name, show_progress=False)
            out[name] = run(name, frames)
        except Exception as e:
            import traceback
            out[name] = {"error": traceback.format_exc()[-1500:]}
        print(name, json.dumps(out[name], default=str))
    # fractional order
    try:
        out["fractional_order_1.5"] = run("kalshi_ratio_1e4", frames, qty=1.5)["transactions"]
    except Exception as e:
        out["fractional_order_1.5"] = "ERROR " + repr(e)[:300]
    print("fractional", out["fractional_order_1.5"])
    # NYSE calendar check: would weekend tape be usable?
    nyse = get_calendar("XNYS")
    out["nyse_sessions_in_tape_window"] = [str(s) for s in nyse.sessions_in_range("2026-10-03", "2026-10-04")]
    json.dump(out, open(OUT + "/out_zipline.json", "w"), indent=1, default=str)
    return out


def speed(days=10):
    """Minute loop speed: 2 assets x every minute of `days` 24/7 sessions (1440 bars/day/asset)."""
    rng = np.random.default_rng(1)
    idx = pd.date_range("2025-01-01", periods=1440 * days, freq="min", tz="UTC")
    frames = {}
    for t in ("SPA", "SPB"):
        p = np.clip(0.5 + np.cumsum(rng.choice([-0.0001, 0, 0.0001], len(idx))), 0.01, 0.99)
        frames[t] = pd.DataFrame({"open": p, "high": p, "low": p, "close": p, "volume": rng.integers(1, 100, len(idx))}, index=idx)
    end = (idx[-1].tz_convert(None)).normalize()
    register("speed", make_ingest(frames, 10_000), calendar_name="24/7", minutes_per_day=1440,
             start_session=pd.Timestamp("2025-01-01"), end_session=end)
    t0 = time.perf_counter(); ingest("speed", show_progress=False); t_ing = time.perf_counter() - t0
    n = {"k": 0}

    def initialize(ctx):
        ctx.s = [symbol("SPA"), symbol("SPB")]

    def handle_data(ctx, data):
        n["k"] += 1
        data.current(ctx.s[0], "close")

    t1 = time.perf_counter()
    run_algorithm(start=pd.Timestamp("2025-01-01"), end=end, initialize=initialize, handle_data=handle_data,
                  capital_base=1e6, data_frequency="minute", bundle="speed", trading_calendar=CAL,
                  benchmark_returns=pd.Series(0.0, index=CAL.sessions_in_range("2025-01-01", end).tz_localize("UTC")))
    dt = time.perf_counter() - t1
    bars = 2 * len(idx)
    return {"days": days, "minutes": len(idx), "asset_minute_bars": bars, "handle_data_calls": n["k"],
            "ingest_s": round(t_ing, 2), "run_s": round(dt, 2), "asset_bars_per_s": round(bars / dt),
            "extrapolated_1M_asset_bars_s": round(1_000_000 / (bars / dt), 1)}


if __name__ == "__main__":
    out = main()
    if "--speed" in sys.argv:
        out["speed"] = speed(int(sys.argv[sys.argv.index("--speed") + 1]))
        print("speed", out["speed"])
        json.dump(out, open(OUT + "/out_zipline.json", "w"), indent=1, default=str)
