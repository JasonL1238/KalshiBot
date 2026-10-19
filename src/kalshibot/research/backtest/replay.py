"""Tier 2, event-driven: the trade tape replayed through NautilusTrader's matching engine.

Tier 1 (`fills.py`) decides fills by the rules in docs/BACKTESTING.md (Fill models);
here NautilusTrader decides taker fills by its own, on a top of book driven by the trade
tape: an order is processed when it arrives (`latency` after its decision), after every
print at that instant (status events at the decision and at arrival make this
independent of how busy the tape is), and fills at its market's last print, however old,
since the book shows the last print on both sides: up to that print's size, the rest one
tick worse. With no print yet, it is rejected. A limit order fills only within its
limit, and whatever it can't fill on arrival is canceled. An order arriving after its
market settled, or after `window_end`, is dropped. `settle.settle` prices these fills
exactly like Tier-1 fills, so the gap between the tiers is fill-model uncertainty alone
(docs/BACKTESTING.md).

Makers aren't replayed: on a book made of prints, a resting bid at the last price fills
completely at the next event of any kind, so maker fills here would be noise. Their
Tier 2 waits for recorded order books.

Each Kalshi market is one BinaryOption quoted in YES dollars ($0.0001 ticks, 0.01-contract
lots). Buying NO is selling YES: the same payoff, and by the fee's symmetry the same fee.
The FeeModel charges `settle.charged_fee` at the engine clock's time, i.e. the fill's.
Settlement is an InstrumentClose at the market's payoff (`settle.yes_payoff`):
BinaryOption doesn't expire natively (engine screen, BACKTESTING.md).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import FeeModel, LatencyModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.data import InstrumentClose, InstrumentStatus, TradeTick
from nautilus_trader.model.enums import (
    AccountType,
    AggressorSide,
    AssetClass,
    CurrencyType,
    InstrumentCloseType,
    LiquiditySide,
    MarketStatusAction,
    OmsType,
    OrderSide,
    TimeInForce,
)
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, Symbol, TradeId, Venue
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Currency, Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy

from kalshibot.kalshi.fee_history import FeeSchedule
from kalshibot.kalshi.models import MICROS, PRICE_SCALE, SIZE_SCALE
from kalshibot.research.backtest.fills import LATENCY, no_fills, tape_order
from kalshibot.research.backtest.settle import charged_fee, settled, yes_payoff

KALSHI = Venue("KALSHI")
MICRO_USD = Currency("USDU", 6, 0, "US dollar, micro-dollar precision", CurrencyType.FIAT)
Currency.register(MICRO_USD, overwrite=True)
PREFIX = "I-"  # client order ids are PREFIX + intent_id; settlement legs are the engine's


class _KalshiFee(FeeModel):
    def __init__(self, fee_of: Callable[[str, int, int, int, bool], int]):
        super().__init__()
        self.fee_of, self.clock = fee_of, None

    def get_commission(self, order, fill_qty, fill_px, instrument):
        if not order.client_order_id.value.startswith(PREFIX):  # settlement pays no fee
            return Money(0, MICRO_USD)
        yes = round(fill_px.as_double() * PRICE_SCALE)
        micros = self.fee_of(instrument.id.symbol.value, self.clock.timestamp_ns(),
                             yes if order.side == OrderSide.BUY else PRICE_SCALE - yes,
                             round(fill_qty.as_double() * SIZE_SCALE),
                             order.liquidity_side == LiquiditySide.MAKER)
        return Money(micros / MICROS, MICRO_USD)


class _Submit(Strategy):
    """Submits each batch of orders at its decision time, and cancels what is left of a
    limit order just after it arrives: on a book of prints, NautilusTrader leaves an
    unfilled IOC remainder resting until a later print trades through it."""

    def __init__(self, batches: list[tuple[int, list[Callable[[Strategy], object]]]],
                 cancels: list[tuple[int, ClientOrderId]]):
        super().__init__()
        self.batches, self.cancels = batches, cancels

    def on_start(self):
        for n, (ts, orders) in enumerate(self.batches):
            self.clock.set_time_alert_ns(f"intents-{n}", ts, lambda _, o=orders: [
                self.submit_order(make(self)) for make in o], allow_past=True)
        for n, (ts, oid) in enumerate(self.cancels):
            self.clock.set_time_alert_ns(f"cancel-{n}", ts, lambda _, i=oid: self._cancel(i),
                                         allow_past=True)

    def _cancel(self, oid: ClientOrderId):
        order = self.cache.order(oid)
        if order is not None and order.is_open:
            self.cancel_order(order)


def _instrument(ticker: str) -> BinaryOption:
    return BinaryOption(InstrumentId(Symbol(ticker), KALSHI), Symbol(ticker),
                        AssetClass.ALTERNATIVE, MICRO_USD, 4, 2, Price(1 / PRICE_SCALE, 4),
                        Quantity(0.01, 2), 0, 0, 0, 0, outcome="Yes")


def _order(row, instrument: BinaryOption):
    side = OrderSide.BUY if row.side == "yes" else OrderSide.SELL
    qty, oid = Quantity(row.count / SIZE_SCALE, 2), ClientOrderId(f"{PREFIX}{row.intent_id}")
    if pd.isna(row.limit):
        return lambda s: s.order_factory.market(instrument.id, side, qty, client_order_id=oid)
    yes = row.limit if row.side == "yes" else PRICE_SCALE - row.limit
    return lambda s: s.order_factory.limit(instrument.id, side, qty,
                                           Price(yes / PRICE_SCALE, 4),
                                           time_in_force=TimeInForce.IOC, client_order_id=oid)


def replay(intents: pd.DataFrame, trades: pd.DataFrame, markets: pd.DataFrame,
           series_of: Callable[[str], str], schedule: FeeSchedule, window_end: datetime,
           precision: int, *, latency: timedelta = LATENCY
           ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fill taker `intents` (`strategies.base.INTENT_COLUMNS`) on `trades` (trade_id, ticker,
    created_time, yes_price, count, taker_side).

    Returns (fills as `fills.FILL_COLUMNS`, engine PnL): the second is NautilusTrader's own
    realized PnL in micro-dollars per market that settled before `window_end`, for the
    accounting cross-check against `settle.settle`."""
    delay = int(latency.total_seconds() * 1e9)
    end_ns = pd.Timestamp(window_end).value
    arrival = pd.to_datetime(intents["decision_time"], utc=True) + latency
    closes = intents["ticker"].map(markets.set_index("ticker")["settlement_ts"])
    intents = intents[(arrival < window_end) & ~(arrival >= closes).fillna(False).astype(bool)]
    if intents.empty:  # nothing can reach a market that is still open
        return no_fills()
    tickers = sorted(set(intents["ticker"]))
    instruments = {t: _instrument(t) for t in tickers}
    m = markets[markets["ticker"].isin(tickers)]
    done = m.assign(paid=yes_payoff(m))[settled(m, window_end)]

    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    fees = _KalshiFee(lambda ticker, ts, price, count, is_maker: charged_fee(
        schedule, series_of(ticker), pd.Timestamp(ts, tz="UTC").to_pydatetime(warn=False),
        price, count, buy=True, maker=is_maker, precision=precision))
    engine.add_venue(KALSHI, OmsType.NETTING, AccountType.CASH, base_currency=MICRO_USD,
                     starting_balances=[Money(1e9, MICRO_USD)], fee_model=fees,
                     latency_model=LatencyModel(0, delay + 1),  # after the instant's prints
                     settlement_prices={instruments[r.ticker].id: r.paid / PRICE_SCALE
                                        for r in done.itertuples()},
                     trade_execution=True, bar_execution=False)
    for instrument in instruments.values():
        engine.add_instrument(instrument)
    tape = tape_order(trades[trades["ticker"].isin(tickers)])
    data = [TradeTick(instruments[r.ticker].id, Price(r.yes_price / PRICE_SCALE, 4),
                      Quantity(r.count / SIZE_SCALE, 2),
                      AggressorSide.BUYER if r.taker_side == "yes" else AggressorSide.SELLER,
                      TradeId(str(n)), ts, ts)
            for n, r in enumerate(tape.itertuples())
            if (ts := pd.Timestamp(r.created_time).value) < end_ns]
    for r in done.itertuples():
        ts = pd.Timestamp(r.settlement_ts).value
        data.append(InstrumentClose(instruments[r.ticker].id, Price(r.paid / PRICE_SCALE, 4),
                                    InstrumentCloseType.CONTRACT_EXPIRED, ts, ts))
    cancels = []
    for r in intents.itertuples():  # submit at the decision, process the moment it arrives
        decided = pd.Timestamp(r.decision_time).value
        steps = (1, 3) if pd.notna(r.limit) else (1,)  # limits: then cancel what is left
        data += [InstrumentStatus(instruments[r.ticker].id, MarketStatusAction.TRADING, ts, ts)
                 for ts in (decided, *(decided + delay + k for k in steps))]
        if pd.notna(r.limit):
            cancels.append((decided + delay + 2, ClientOrderId(f"{PREFIX}{r.intent_id}")))
    engine.add_data(data)
    fees.clock = engine.kernel.clock

    batches = [(pd.Timestamp(decided).value, [_order(r, instruments[r.ticker])
                                              for r in group.itertuples()])
               for decided, group in intents.groupby("decision_time", sort=True)]
    engine.add_strategy(_Submit(batches, cancels))
    engine.run(end=window_end)

    report = engine.trader.generate_fills_report()
    ids = {f"{PREFIX}{i}": i for i in intents["intent_id"]}
    report = report[report.index.isin(ids)] if len(report) else pd.DataFrame(
        columns=["instrument_id", "last_px", "last_qty", "ts_event", "liquidity_side"])
    intent = report.index.map(ids).to_numpy()
    side = intents.set_index("intent_id")["side"].reindex(intent).to_numpy()
    yes = (report["last_px"].astype(float) * PRICE_SCALE).round().astype("int64").to_numpy()
    fills = pd.DataFrame({
        "intent_id": intent,
        "ticker": [str(i).rsplit(".", 1)[0] for i in report["instrument_id"]],
        "side": side,
        "fill_time": pd.to_datetime(report["ts_event"].to_numpy(), utc=True),
        "fill_price": np.where(side == "yes", yes, PRICE_SCALE - yes),
        "fill_count": (report["last_qty"].astype(float) * SIZE_SCALE).round()
        .astype("int64").to_numpy(),
        "maker": (report["liquidity_side"].astype(str) == "MAKER").to_numpy(),
    })
    # a position closed and reopened keeps its earlier realized PnL in a snapshot
    positions = engine.cache.positions() + engine.cache.position_snapshots()
    pnl = pd.DataFrame([(p.instrument_id.symbol.value,
                         round(float(p.realized_pnl.as_decimal()) * MICROS))
                        for p in positions], columns=["ticker", "pnl"])
    engine.dispose()
    pnl = pnl[pnl["ticker"].isin(done["ticker"])].groupby("ticker", as_index=False)["pnl"].sum()
    return fills.sort_values(["fill_time", "intent_id"], ignore_index=True), pnl
