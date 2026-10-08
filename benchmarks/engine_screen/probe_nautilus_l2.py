"""NautilusTrader L2 + queue-position probe: L2_MBP book from OrderBookDelta, resting buy limit at the
best bid behind 5 contracts, then sell-aggressor trades at the bid. Compares queue_position on/off
and a latency model. Run each variant in its own process (nautilus logging is per-process)."""
from pathlib import Path
import sys, json, subprocess
sys.path.insert(0, str(Path(__file__).parent))
from common import *  # noqa
import pandas as pd
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import LatencyModel
from nautilus_trader.config import LoggingConfig, StrategyConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import TradeTick, OrderBookDelta, BookOrder
from nautilus_trader.model.enums import (AccountType, OmsType, AssetClass, AggressorSide, OrderSide, BookType,
                                         BookAction, RecordFlag)
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue, TradeId
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy

V = Venue("KALSHI")
T0 = pd.Timestamp("2026-10-03 12:00:00", tz="UTC").value
S = 1_000_000_000


class Cfg(StrategyConfig, frozen=True):
    iid: str


class Passive(Strategy):
    def __init__(self, config):
        super().__init__(config)
        self.placed = False
        self.fills = []

    def on_start(self):
        self.iid = InstrumentId.from_str(self.config.iid)
        self.subscribe_order_book_deltas(self.iid)
        # place 10s after the book snapshot (bid 0.40 x 5 already resting ahead of us)
        self.clock.set_time_alert_ns("place", T0 + 10 * S, self._place)

    def _place(self, event):
        self.submit_order(self.order_factory.limit(self.iid, OrderSide.BUY, Quantity(4, 2), Price(0.40, 4)))
        self.placed = True

    def on_order_filled(self, ev):
        self.fills.append({"ts": str(pd.Timestamp(ev.ts_event, tz="UTC")), "qty": float(ev.last_qty), "px": float(ev.last_px)})


def run(queue_position: bool, latency_s: int):
    inst = BinaryOption(InstrumentId(Symbol(A), V), Symbol(A), AssetClass.ALTERNATIVE, USD, 4, 2,
                        Price.from_str("0.0001"), Quantity.from_str("0.01"), 0, 0, 0, 0, outcome="Yes")
    eng = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(log_level="ERROR")))
    eng.add_venue(V, OmsType.NETTING, AccountType.CASH, [Money(1000, USD)], base_currency=USD,
                  book_type=BookType.L2_MBP, queue_position=queue_position,
                  latency_model=LatencyModel(latency_s * S) if latency_s else None)
    eng.add_instrument(inst)
    iid = inst.id
    data = [OrderBookDelta.clear(iid, 0, T0, T0),
            OrderBookDelta(iid, BookAction.ADD, BookOrder(OrderSide.BUY, Price(0.40, 4), Quantity(5, 2), 0), 0, 1, T0, T0),
            OrderBookDelta(iid, BookAction.ADD, BookOrder(OrderSide.SELL, Price(0.42, 4), Quantity(5, 2), 0), RecordFlag.F_LAST, 2, T0, T0)]
    # sell-aggressor prints at our bid level: 3, then 3, then 3 contracts (0.40)
    for k, q in enumerate([3, 3, 3, 3]):
        ts = T0 + (k + 1) * 60 * S
        data.append(TradeTick(iid, Price(0.40, 4), Quantity(q, 2), AggressorSide.SELLER, TradeId(f"t{k}"), ts, ts))
    eng.add_data(data)
    st = Passive(Cfg(iid=str(iid)))
    eng.add_strategy(st)
    eng.run()
    return {"queue_position": queue_position, "latency_s": latency_s, "fills": st.fills}


if __name__ == "__main__":
    if "--one" in sys.argv:
        q, l = sys.argv[sys.argv.index("--one") + 1].split(",")
        print("RESULT" + json.dumps(run(q == "1", int(l))))
    else:
        res = []
        for q, l in (("0", "0"), ("1", "0"), ("0", "90"), ("1", "90")):
            p = subprocess.run([sys.executable, __file__, "--one", f"{q},{l}"], capture_output=True, text=True)
            line = [x for x in p.stdout.splitlines() if x.startswith("RESULT")]
            r = json.loads(line[0][6:]) if line else {"error": p.stderr[-1500:]}
            res.append(r); print(r)
        json.dump(res, open(OUT + "/out_nautilus_l2.json", "w"), indent=1)
