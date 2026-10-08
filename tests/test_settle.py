from datetime import UTC, datetime

import duckdb
import pandas as pd
import pytest

from kalshibot.kalshi.fee_history import FeeSchedule
from kalshibot.kalshi.fees import DIRECT_MEMBER, FCM_MEMBER, fee
from kalshibot.research.backtest.settle import (
    BINARY_PAYOFF_KNOWN,
    charged_fee,
    daily_returns,
    settle,
    yes_payoff,
)

T = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
END = datetime(2026, 1, 1, tzinfo=UTC)
FILLS = pd.DataFrame([
    # intent, ticker, side, time, price, count (size units), maker
    (1, "KXHIGHNY-A", "yes", T("2025-12-01"), 4000, 10_000, False),  # 100 YES @ $0.40, wins
    (2, "KXHIGHNY-A", "no", T("2025-12-01"), 3000, 1_000, True),     # 10 NO @ $0.30, loses
    (3, "KXHIGHNY-B", "yes", T("2025-12-20"), 2000, 1_000, False),   # open at window end
], columns=["intent_id", "ticker", "side", "fill_time", "fill_price", "fill_count", "maker"])
MARKETS = pd.DataFrame([("KXHIGHNY-A", "yes", 10_000, T("2025-12-02")),
                        ("KXHIGHNY-B", None, None, None)],
                       columns=["ticker", "result", "settlement_value", "settlement_ts"])


def run(bids=None):
    schedule = FeeSchedule([], {"KXHIGHNY": ("quadratic", "1", "Climate and Weather")})
    return settle(FILLS, MARKETS, lambda t: t.split("-")[0], schedule, END, FCM_MEMBER,
                  bids).set_index("intent_id")


def test_settled_fills_pay_out_net_of_dated_fees():
    out = run()
    # 2025 taker terms: 0.07 × 100 × 0.4 × 0.6 = $1.68, rounded up per fill to the cent.
    assert out.loc[1, "entry_fee"] == 1_680_000
    assert out.loc[1, "pnl"] == 100_000_000 - 40_000_000 - 1_680_000
    assert out.loc[2, "entry_fee"] == 0 and out.loc[2, "pnl"] == -3_000_000  # maker, lost


def test_open_positions_are_marked_at_the_bid_less_the_exit_fee():
    out = run(pd.DataFrame([("KXHIGHNY-B", "yes", 5000)], columns=["ticker", "side", "bid"]))
    assert out.loc[3, "status"] == "marked"
    assert out.loc[3, "entry_fee"] == 120_000  # 0.07 × 10 × 0.2 × 0.8 = $0.112 → $0.12
    assert out.loc[3, "exit_fee"] == 180_000  # selling at the bid: $0.175 → $0.18
    assert out.loc[3, "pnl"] == 5_000_000 - 2_000_000 - 120_000 - 180_000
    unmarked = run().loc[3]
    assert unmarked.status == "marked_at_zero" and unmarked.pnl == -2_000_000 - 120_000


def test_daily_returns_use_peak_capital_on_the_full_calendar():
    out = run().reset_index()
    r = daily_returns(out, datetime(2025, 11, 30, tzinfo=UTC), END)
    peak = 40_000_000 + 3_000_000  # fills 1 and 2 are open together on Dec 1
    # fills 1 and 2 settle at midnight Dec 2, which counts on Dec 1
    assert r.loc[T("2025-12-01")] == pytest.approx((58_320_000 - 3_000_000) / peak)
    assert len(r) == 32 and r.loc[T("2025-11-30")] == 0  # quiet days count, as zeros
    assert r.sum() * peak == pytest.approx(out["pnl"].sum())  # a mark at END counts too
    marked = out.loc[out["intent_id"] == 3, "pnl"].item()  # open at END, marked at zero
    assert r.loc[T("2025-12-31")] == pytest.approx(marked / peak)
    with pytest.raises(ValueError):  # a close outside the calendar would be lost
        daily_returns(out, datetime(2025, 11, 30, tzinfo=UTC), datetime(2025, 12, 15, tzinfo=UTC))


def test_scalar_markets_pay_their_value_and_conflicting_results_count_as_unknown():
    markets = pd.DataFrame([("KXP-S", "scalar", 4_000, T("2025-12-02")),
                            ("KXP-C", "yes", 0, T("2025-12-02"))],  # result and value disagree
                           columns=["ticker", "result", "settlement_value", "settlement_ts"])
    fills = pd.DataFrame([(1, "KXP-S", "yes", T("2025-12-01"), 3_000, 100, True),
                          (2, "KXP-S", "no", T("2025-12-01"), 5_000, 100, True),
                          (3, "KXP-C", "yes", T("2025-12-01"), 3_000, 100, True)],
                         columns=FILLS.columns)
    schedule = FeeSchedule([], {"KXP": ("quadratic", "1", "Sports")})
    out = settle(fills, markets, lambda t: t.split("-")[0], schedule, END, FCM_MEMBER)
    out = out.set_index("intent_id")
    assert out.loc[1, "pnl"] == 400_000 - 300_000 and out.loc[2, "pnl"] == 600_000 - 500_000
    assert out.loc[3, "status"] == "marked_at_zero"


def test_fills_after_settlement_are_refused():
    late = FILLS.assign(fill_time=T("2025-12-03"))
    with pytest.raises(ValueError):
        settle(late, MARKETS, lambda t: t.split("-")[0],
               FeeSchedule([], {"KXHIGHNY": ("quadratic", "1", "Climate and Weather")}), END,
               FCM_MEMBER)


def test_fees_charge_the_costliest_plausible_regime():
    # 2026-03-01 is inside the uncertain rounding switch; for a direct member, cent
    # rounding ($0.17) costs more than balance rounding ($0.168)
    schedule = FeeSchedule([], {"KXHIGHNY": ("quadratic", "1", "Climate and Weather")})
    ts = datetime(2026, 3, 1, tzinfo=UTC)
    fees = {fee(4_000, 1_000, buy=True, terms=t, precision=DIRECT_MEMBER)
            for t in schedule.plausible("KXHIGHNY", ts)}
    assert fees == {168_000, 170_000}
    assert charged_fee(schedule, "KXHIGHNY", ts, 4_000, 1_000, buy=True, maker=False,
                       precision=DIRECT_MEMBER) == 170_000


def test_the_sql_payoff_rule_matches_yes_payoff_on_binary_results():
    markets = pd.DataFrame({"result": ["yes", "yes", "no", "no", "no", "scalar", None],
                            "settlement_value": [10_000, 0, 0, None, 10_000, 4_000, None]})
    sql = duckdb.sql(f"SELECT {BINARY_PAYOFF_KNOWN} AS ok FROM markets").df()["ok"].fillna(False)
    binary = markets["result"].isin(["yes", "no"])
    assert (sql[binary] == yes_payoff(markets)[binary].notna()).all()
