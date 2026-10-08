# QuantConnect LEAN probe: custom PythonData trade tape (two instruments), always-open UTC exchange hours,
# custom FeeModel ceil_to_cent(0.07*C*P*(1-P)), next-print market buy, settlement via a synthetic settlement row.
from AlgorithmImports import *
import math, json, time as _time

SIGNAL = datetime(2026, 10, 3, 23, 0, 0)


def kalshi_fee(c, p):
    return math.ceil(round(0.07 * abs(c) * p * (1.0 - p) * 100.0, 9)) / 100.0


class KalshiTrade(PythonData):
    def get_source(self, config, date, is_live):
        return SubscriptionDataSource(f"/Lean/Data/kalshi/{config.symbol.value.lower()}.csv",
                                      SubscriptionTransportMedium.LOCAL_FILE, FileFormat.CSV)

    def reader(self, config, line, date, is_live):
        if not line or line[0] == "t":
            return None
        ts, px, qty, side, settle = line.split(",")
        d = KalshiTrade()
        d.symbol = config.symbol
        fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in ts else "%Y-%m-%dT%H:%M:%S"
        d.time = datetime.strptime(ts, fmt)
        d.end_time = d.time
        d.value = float(px)
        d["contracts"] = float(qty)
        d["settle"] = int(settle)
        return d


class KalshiFeeModel(FeeModel):
    calls = []

    def get_order_fee(self, parameters):
        sec = parameters.security
        o = parameters.order
        f = kalshi_fee(float(o.absolute_quantity), float(sec.price))
        KalshiFeeModel.calls.append([str(sec.symbol.value), float(o.absolute_quantity), float(sec.price), f])
        return OrderFee(CashAmount(f, "USD"))


class KalshiProbe(QCAlgorithm):
    def initialize(self):
        import os; self.mode = os.environ.get("PROBE_MODE", "probe")
        self.set_time_zone(TimeZones.UTC)
        self.set_cash(1000)
        self.set_brokerage_model(BrokerageName.DEFAULT, AccountType.CASH)
        props = SymbolProperties("Kalshi binary", "USD", 1, 0.0001, 0.01, "")  # tick $0.0001, lot 0.01 contracts
        hours = SecurityExchangeHours.always_open(TimeZones.UTC)
        if self.mode == "speed":
            self.set_start_date(2025, 1, 1)
            self.set_end_date(2025, 1, 19)
            tickers = ["KXBIG-0", "KXBIG-1"]
        else:
            self.set_start_date(2026, 10, 3)
            self.set_end_date(2026, 10, 5)
            tickers = ["KXTEST-A", "KXTEST-B"]
        self.syms = []
        for t in tickers:
            sec = self.add_data(KalshiTrade, t, props, hours, Resolution.SECOND, False, 1)
            sec.set_fee_model(KalshiFeeModel())
            self.syms.append(sec.symbol)
        self.a, self.b = self.syms
        self.armed = False
        self.done = False
        self.n = 0
        self.fills = []
        self.t0 = _time.time()
        self.weekend_events = 0

    def on_data(self, data):
        self.n += 1
        if self.mode == "speed":
            if self.n == 500_000 and data.contains_key(self.a):
                self.market_order(self.a, 10)
            return
        if self.time.weekday() >= 5:
            self.weekend_events += 1
        if not self.armed and self.time >= SIGNAL:
            self.armed = True
        if self.mode == "zero_check" and not self.done and data.contains_key(self.b):
            self.market_order(self.b, 10)
            self.done = True
        if self.mode in ("probe", "frac") and self.armed and not self.done and data.contains_key(self.a):
            self.market_order(self.a, 10 if self.mode == "probe" else 1.5)
            self.done = True
        for s in self.syms:
            if data.contains_key(s) and int(data[s]["settle"]) == 1 and self.portfolio[s].invested:
                self.liquidate(s)  # settlement: exit at payoff price from the synthetic settlement row

    def on_order_event(self, e):
        if e.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED, OrderStatus.INVALID):
            self.fills.append({"time": str(self.time), "sym": e.symbol.value, "status": str(e.status),
                               "qty": float(e.fill_quantity), "px": float(e.fill_price),
                               "fee": float(e.order_fee.value.amount), "msg": e.message})

    def on_end_of_algorithm(self):
        out = {"mode": self.mode, "final_value": float(self.portfolio.total_portfolio_value),
               "cash": float(self.portfolio.cash), "fills": self.fills, "fee_calls": KalshiFeeModel.calls[:10],
               "on_data_calls": self.n, "weekend_on_data": self.weekend_events,
               "wall_s_since_init": round(_time.time() - self.t0, 2)}
        self.log("PROBE_RESULT " + json.dumps(out))
