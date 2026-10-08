from datetime import UTC, datetime
from fractions import Fraction

from kalshibot.kalshi.fee_history import FeeSchedule, FeeTerms

T = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731
LOG = [("KXINX", T(2026, 7, 3, 17), "quadratic", "1"),
       ("KXBTCY", T(2026, 2, 26), "quadratic", "0"),
       ("KXMLBHR", T(2026, 8, 7, 4, 55), "quadratic", "0.5")]
CURRENT = {  # series -> (fee_type, fee_multiplier, category), as in series.parquet
    "KXHIGHNY": ("quadratic", "1", "Climate and Weather"),
    "KXINX": ("quadratic", "1", "Financials"),
    "KXINXW": ("quadratic", "1", "Financials"),
    "KXBTCY": ("quadratic", "0", "Crypto"),
    "KXMLBHR": ("quadratic", "0.5", "Sports"),
    "PRES": ("quadratic", "1", "Elections"),
    "CONTROLH": ("quadratic", "1", "Elections"),
    "PRESPARTYFL": ("quadratic", "1", "Elections"),
    "SENATEPA": ("quadratic", "1", "Elections"),
    "PRESINDEXD": ("quadratic", "1", "Financials"),
    "KXBTCMAXY": ("quadratic", "1", "Crypto"),  # on the Sept 2025 maker list, not today
}
S = FeeSchedule(LOG, CURRENT)


def takers(series, ts):
    return {t.taker for t in S.plausible(series, ts)}


def test_taker_rate_history():
    assert S.terms("KXHIGHNY", T(2021, 7, 1)).taker == Fraction(14, 100)
    assert takers("KXHIGHNY", T(2021, 7, 22)) == {Fraction(14, 100), Fraction(7, 100)}
    assert S.terms("KXHIGHNY", T(2023, 1, 1)) == FeeTerms(Fraction(7, 100), Fraction(0),
                                                           Fraction(0), "cent")


def test_index_discount_until_the_logged_change():
    assert S.terms("INXD", T(2023, 1, 1)).taker == Fraction(35, 1000)
    assert takers("KXINX", T(2026, 7, 3, 16)) == {Fraction(35, 1000)}  # logged off it later
    assert takers("KXINXW", T(2025, 3, 1)) == {Fraction(35, 1000), Fraction(7, 100)}  # unknown
    assert S.terms("KXINX", T(2026, 7, 3, 18)).taker == Fraction(7, 100)  # the log wins
    assert S.terms("INXD", T(2022, 9, 1)).taker == Fraction(7, 100)  # before the discount
    assert takers("INXD", T(2026, 3, 15)) == {Fraction(35, 1000)}  # only rounding varies


def test_election_waivers_by_series():
    oct_2024 = T(2024, 10, 20)
    assert S.terms("PRES", oct_2024).taker == 0 and not S.terms("PRES", oct_2024).uncertain
    assert S.terms("CONTROLH", oct_2024).taker == 0
    assert S.terms("PRESPARTYFL", oct_2024).taker == 0
    assert takers("SENATEPA", oct_2024) == {0, Fraction(7, 100)}  # related, but not named
    assert takers("PRESINDEXD", oct_2024) == {Fraction(7, 100)}  # a finance series
    assert S.terms("PRES", T(2024, 12, 15)).taker == 0  # 2024-election waiver: series PRES
    assert S.terms("CONTROLH", T(2024, 12, 15)).taker == Fraction(7, 100)
    assert takers("PRES", T(2025, 1, 25)) == {0, Fraction(7, 100)}  # end date not pinned


def test_maker_fee_lists_and_kinds():
    assert S.terms("KXCPI", T(2025, 6, 1)).maker_flat == Fraction(25, 10_000)
    assert S.terms("KXCPI", T(2025, 8, 1)).maker == Fraction(175, 10_000)
    assert S.terms("KXNBAGAME", T(2025, 6, 1)).maker_flat == 0  # added June 5
    assert S.terms("KXNBAGAME", T(2025, 6, 6)).maker_flat == Fraction(25, 10_000)
    assert S.terms("KXATPMATCH", T(2025, 7, 10)).maker > 0
    assert S.terms("KXATPMATCH", T(2025, 7, 20)).maker == 0  # dropped after July 12
    assert S.terms("KXNFLSPREAD", T(2025, 9, 17, 12)).maker == 0  # live 09/18 3am ET
    assert S.terms("KXNFLSPREAD", T(2025, 9, 18, 8)).maker > 0
    assert S.terms("KXHIGHNY", T(2025, 11, 1)).maker == 0


def test_current_state_after_the_log_starts():
    assert S.terms("KXBTCMAXY", T(2025, 9, 25)).uncertain  # list says maker; today says not
    assert S.terms("KXBTCMAXY", T(2025, 11, 1)).maker == 0  # unlogged: current state
    assert S.terms("KXBTCY", T(2026, 3, 1)).taker == 0
    assert takers("KXBTCY", T(2026, 3, 15)) == {0}
    assert S.terms("KXMLBHR", T(2026, 9, 1)) == FeeTerms.from_series("quadratic", "0.5",
                                                                     "balance")


def test_rounding_regime():
    assert S.terms("KXHIGHNY", T(2025, 12, 1)).rounding == "cent"
    assert S.terms("KXHIGHNY", T(2026, 6, 1)).rounding == "balance"
    assert {t.rounding for t in S.plausible("KXHIGHNY", T(2026, 4, 1))} == {"cent", "balance"}
    assert not S.terms("KXHIGHNY", T(2026, 9, 1)).uncertain
