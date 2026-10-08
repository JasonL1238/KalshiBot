"""NautilusTrader 1.230.0 probe: BinaryOption instruments, TradeTick tape, custom FeeModel,
native expiration + settlement_prices, next-print fill, plus 1M-tick speed test."""
from pathlib import Path
import sys, time, json
sys.path.insert(0, str(Path(__file__).parent))
from common import *  # noqa

import pandas as pd
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import FeeModel, LatencyModel
from nautilus_trader.config import LoggingConfig, StrategyConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import TradeTick, InstrumentClose
from nautilus_trader.model.enums import InstrumentCloseType, CurrencyType
from nautilus_trader.model.objects import Currency
from nautilus_trader.backtest.models import BestPriceFillModel
from nautilus_trader.model.enums import AccountType, OmsType, AssetClass, AggressorSide, OrderSide
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue, TradeId
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.persistence.wranglers import TradeTickDataWrangler
from nautilus_trader.trading.strategy import Strategy

KALSHI = Venue("KALSHI")
USD4 = Currency("USDK", 4, 0, "USD with 1e-4 precision", CurrencyType.FIAT)
Currency.register(USD4)
CUR = USD


def make_instrument(ticker: str, exp_ns: int) -> BinaryOption:
    return BinaryOption(
        instrument_id=InstrumentId(Symbol(ticker), KALSHI),
        raw_symbol=Symbol(ticker),
        asset_class=AssetClass.ALTERNATIVE,
        currency=CUR,
        price_precision=4,           # $0.0001 ticks (sub-penny)
        size_precision=2,            # 0.01-contract units
        price_increment=Price.from_str("0.0001"),
        size_increment=Quantity.from_str("0.01"),
        activation_ns=0,
        expiration_ns=exp_ns,
        ts_event=0,
        ts_init=0,
        outcome="Yes",
    )


class KalshiFee(FeeModel):
    """Per-fill nonlinear fee: ceil_to_cent(0.07*C*P*(1-P)). Python subclass of a Cython cpdef."""
    calls = []

    def get_commission(self, order, fill_qty, fill_px, instrument):
        f = kalshi_fee(float(fill_qty), float(fill_px))
        KalshiFee.calls.append((str(instrument.id), float(fill_qty), float(fill_px), f))
        return Money(f, CUR)


class Cfg(StrategyConfig, frozen=True):
    target: str
    signal_ns: int
    qty: str = "10"


class NextPrintBuyer(Strategy):
    def __init__(self, config: Cfg):
        super().__init__(config)
        self.armed = False
        self.done = False
        self.n_ticks = 0

    def on_start(self):
        for inst in self.cache.instruments():
            self.subscribe_trade_ticks(inst.id)
        if self.config.signal_ns > 0:
            self.clock.set_time_alert_ns("signal", self.config.signal_ns, self._on_signal)

    def _on_signal(self, event):
        self.armed = True

    def on_trade_tick(self, tick: TradeTick):
        self.n_ticks += 1
        if self.armed and not self.done and tick.instrument_id.symbol.value == self.config.target:
            self.submit_order(self.order_factory.market(
                tick.instrument_id, OrderSide.BUY, Quantity(float(self.config.qty), 2)))
            self.done = True


def ticks_from_tape(df: pd.DataFrame, insts: dict) -> list:
    out = []
    for i, r in enumerate(df.itertuples()):
        inst = insts[r.ticker]
        out.append(TradeTick(
            inst.id,
            Price(r.yes_price / 10_000, 4),
            Quantity(r.count / 100, 2),
            AggressorSide.BUYER if r.taker_side == "yes" else AggressorSide.SELLER,
            TradeId(str(i)),
            r.ts.value, r.ts.value,
        ))
    return out


def build_engine(insts, settlement, latency=None, fill_model=None):
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(log_level="ERROR")))
    engine.add_venue(
        venue=KALSHI,
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        starting_balances=[Money(START_CASH, CUR)],
        base_currency=CUR,
        fee_model=KalshiFee(),
        latency_model=latency,
        fill_model=fill_model,
        settlement_prices=settlement,
    )
    for inst in insts.values():
        engine.add_instrument(inst)
    return engine


def probe(fill_model=None, settle=True):
    KalshiFee.calls = []
    df = tape()
    insts = {t: make_instrument(t, SETTLE_TS.value) for t in (A, B)}
    engine = build_engine(insts, {insts[A].id: SETTLEMENT[A], insts[B].id: SETTLEMENT[B]}, fill_model=fill_model)
    data = ticks_from_tape(df, insts)
    if settle:
        # BinaryOption is NOT in ENGINE_EXPIRING_INSTRUMENT_CLASSES, so expiration_ns alone does nothing.
        # Workaround: inject InstrumentClose(CONTRACT_EXPIRED) at settlement; the matching engine then
        # closes open positions at venue settlement_prices[instrument_id].
        for t in (A, B):
            data.append(InstrumentClose(insts[t].id, Price(SETTLEMENT[t], 4), InstrumentCloseType.CONTRACT_EXPIRED,
                                        SETTLE_TS.value, SETTLE_TS.value))
    engine.add_data(data)
    strat = NextPrintBuyer(Cfg(target=A, signal_ns=SIGNAL_TS.value))
    engine.add_strategy(strat)
    # run past the last tick to settlement time so the expiration timer fires
    engine.run(end=(SETTLE_TS + pd.Timedelta(hours=1)).to_pydatetime())
    acct = engine.cache.account_for_venue(KALSHI)
    final = float(acct.balance_total(CUR))
    fills = engine.trader.generate_order_fills_report()
    pos = engine.trader.generate_positions_report()
    out = {
        "final_cash": final,
        "pnl": round(final - START_CASH, 6),
        "fee_calls": KalshiFee.calls,
        "fills": fills[["instrument_id", "side", "filled_qty", "avg_px", "ts_last"]].astype(str).to_dict("records") if len(fills) else [],
        "positions": pos[["instrument_id", "realized_pnl", "ts_closed"]].astype(str).to_dict("records") if len(pos) else [],
        "reference": reference(),
    }
    engine.dispose()
    return out


def speed():
    big = big_tape_cached()
    tickers = sorted(big.ticker.unique())
    insts = {t: make_instrument(t, 0) for t in tickers}
    t0 = time.perf_counter()
    ticks = []
    for t in tickers:
        sub = big[big.ticker == t]
        w = TradeTickDataWrangler(insts[t])
        d = pd.DataFrame({
            "price": sub.yes_price.values / 10_000,
            "quantity": sub["count"].values / 100,
            "side": ["BUY" if s == "yes" else "SELL" for s in sub.taker_side.values],
            "trade_id": sub.index.astype(str),
        }, index=pd.DatetimeIndex(sub.ts.values, tz="UTC"))
        ticks.extend(w.process(d))
    t_prep = time.perf_counter() - t0
    engine = build_engine(insts, {})
    strat = NextPrintBuyer(Cfg(target=tickers[0], signal_ns=int(big.ts.iloc[len(big)//2].value)))
    engine.add_strategy(strat)
    t1 = time.perf_counter()
    engine.add_data(ticks)  # sorts
    t_add = time.perf_counter() - t1
    t2 = time.perf_counter()
    engine.run()
    t_run = time.perf_counter() - t2
    n = strat.n_ticks
    engine.dispose()
    return {"n_events": len(ticks), "ticks_seen_by_strategy": n, "prep_s": round(t_prep, 2),
            "add_data_s": round(t_add, 2), "run_s": round(t_run, 2),
            "events_per_s_run": round(len(ticks) / t_run)}


VARIANTS = {
    "default_fill_USD": dict(cur="USD", fill=None, settle=True),
    "bestprice_fill_USD": dict(cur="USD", fill="best", settle=True),
    "default_fill_USD4": dict(cur="USD4", fill=None, settle=True),
    "bestprice_fill_USD4": dict(cur="USD4", fill="best", settle=True),
    "no_close_event_USD4": dict(cur="USD4", fill="best", settle=False),
}

if __name__ == "__main__":
    import subprocess
    if "--variant" in sys.argv:
        v = VARIANTS[sys.argv[sys.argv.index("--variant") + 1]]
        CUR = USD if v["cur"] == "USD" else USD4
        r = probe(fill_model=BestPriceFillModel() if v["fill"] == "best" else None, settle=v["settle"])
        print("RESULT" + json.dumps(r, default=str))
    elif "--speed" in sys.argv:
        r = speed()
        print("RESULT" + json.dumps(r, default=str))
    else:
        res = {}
        for name in list(VARIANTS) + ["speed"]:
            args = ["--variant", name] if name != "speed" else ["--speed"]
            p = subprocess.run([sys.executable, __file__] + args, capture_output=True, text=True)
            line = [l for l in p.stdout.splitlines() if l.startswith("RESULT")]
            res[name] = json.loads(line[0][6:]) if line else {"error": p.stderr[-2000:]}
            v = res[name]
            print(name, json.dumps({k: v.get(k) for k in ("final_cash", "pnl", "fee_calls", "fills", "positions", "error")} if name != "speed" else v, default=str))
        print("reference", reference())
        json.dump(res, open(OUT + "/out_nautilus.json", "w"), indent=1, default=str)
