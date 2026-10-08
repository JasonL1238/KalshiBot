"""backtrader 1.9.78.123 probe: each trade print as a degenerate OHLC bar (O=H=L=C=price), two feeds,
custom CommInfoBase fee, settlement as a synthetic final bar at the payoff, speed on 1M prints."""
from pathlib import Path
import sys, time, json, math
sys.path.insert(0, str(Path(__file__).parent))
from common import *  # noqa

import pandas as pd
import backtrader as bt


class KalshiComm(bt.CommInfoBase):
    params = (("stocklike", True), ("commtype", bt.CommInfoBase.COMM_FIXED), ("percabs", True))
    calls = []

    def _getcommission(self, size, price, pseudoexec):
        f = kalshi_fee(abs(size), price)
        if pseudoexec:
            KalshiComm.calls.append((size, price, f))
        return f


def feed_df(sub: pd.DataFrame, settle_px=None):
    d = pd.DataFrame({
        "open": sub.price.values, "high": sub.price.values, "low": sub.price.values, "close": sub.price.values,
        "volume": sub.contracts.values, "openinterest": 0.0,
    }, index=pd.DatetimeIndex(sub.ts.dt.tz_convert(None).values, name="datetime"))
    if settle_px is not None:  # synthetic settlement print
        d.loc[SETTLE_TS.tz_convert(None)] = [settle_px] * 4 + [0.0, 0.0]
    return d


class NextPrintBuyer(bt.Strategy):
    params = (("signal", None), ("qty", QTY))

    def __init__(self):
        self.armed = False
        self.done = False
        self.closed = False
        self.log = []
        self.n = 0

    def next(self):
        self.n += 1
        dt = self.datetime.datetime(0)
        if self.p.signal is not None and not self.armed and dt >= self.p.signal:
            self.armed = True
        if self.armed and not self.done:
            # decided on first event after the signal; market order fills at the NEXT bar of data A = next A print
            self.buy(data=self.datas[0], size=self.p.qty)
            self.done = True

    def notify_order(self, order):
        if order.status == order.Completed:
            self.log.append({"dt": str(bt.num2date(order.executed.dt)), "data": order.data._name,
                             "size": order.executed.size, "price": order.executed.price,
                             "comm": order.executed.comm})


def run_small(with_settle_bar=True):
    KalshiComm.calls = []
    df = tape()
    cerebro = bt.Cerebro(stdstats=False)
    for t in (A, B):
        sub = df[df.ticker == t]
        cerebro.adddata(bt.feeds.PandasData(dataname=feed_df(sub, SETTLEMENT[t] if with_settle_bar else None)), name=t)
    cerebro.addstrategy(NextPrintBuyer, signal=SIGNAL_TS.tz_convert(None).to_pydatetime())
    cerebro.broker.setcash(START_CASH)
    cerebro.broker.addcommissioninfo(KalshiComm())
    cerebro.broker.set_coc(False)
    res = cerebro.run()
    s = res[0]
    return {"with_settle_bar": with_settle_bar, "final_value": cerebro.broker.getvalue(),
            "pnl": round(cerebro.broker.getvalue() - START_CASH, 6), "cash": cerebro.broker.getcash(),
            "fills": s.log, "next_calls": s.n}


def speed(n_limit=None):
    big = big_tape_cached()
    if n_limit:
        big = big.iloc[:n_limit]
    big = big.assign(price=big.yes_price / 10_000, contracts=big["count"] / 100)
    t0 = time.perf_counter()
    cerebro = bt.Cerebro(stdstats=False)
    tickers = sorted(big.ticker.unique())
    for t in tickers:
        cerebro.adddata(bt.feeds.PandasData(dataname=feed_df(big[big.ticker == t])), name=t)
    cerebro.addstrategy(NextPrintBuyer, signal=big.ts.iloc[len(big) // 2].tz_convert(None).to_pydatetime())
    cerebro.broker.setcash(START_CASH)
    cerebro.broker.addcommissioninfo(KalshiComm())
    res = cerebro.run()
    dt = time.perf_counter() - t0
    return {"n_events": len(big), "next_calls": res[0].n, "total_s": round(dt, 2), "events_per_s": round(len(big) / dt)}


if __name__ == "__main__":
    out = {"reference": reference()}
    out["with_settle_bar"] = run_small(True)
    out["no_settle_bar"] = run_small(False)
    print(json.dumps(out, indent=1, default=str))
    if "--speed" in sys.argv:
        n = int(sys.argv[sys.argv.index("--speed") + 1])
        out["speed"] = speed(n)
        print(json.dumps(out["speed"], indent=1))
    json.dump(out, open(f"{OUT}/out_backtrader{'_speed' if '--speed' in sys.argv else ''}.json", "w"), indent=1, default=str)
