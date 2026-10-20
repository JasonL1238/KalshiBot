"""H8, deep resting orders that catch sweeps (docs/STRATEGIES.md): a burst of YES buying
can push a thin in-play book far past where it settles back, and a NO bid resting well
below the last trade is paid for absorbing it.

While a game is live, every `horizon` on a fixed clock (multiples of it since the UTC
epoch), each of its two team markets gets a NO bid for one contract at `depth` under the
NO price of its last trade. The next tick's bid replaces it, so one bid per contract
rests at a time.

- Universe: the game-winner series in `LEAGUES`, in events that show exactly two markets,
  mutually exclusive, at the decision.
- Live: from the game's listed start until its league's cap (how Kalshi lists starts:
  docs/RESEARCH.md §1, Data formats). A listing counts only if `latest_expiration_time`
  less one of its league's offsets (`League.offsets`, whole 24-hour days) lands on the
  game's Eastern date (the event ticker's YYMONDD). Its start is then the ticker's own
  time where it gives one (YYMONDDHHMM, US Eastern), and that landing point otherwise.
  Listings with no offset landing (a 730-day expiration) or no date in the ticker get no
  bids; their count is logged.
- Reference: the market's last instant (one created_time, block trades aside) in the
  `max_hold` before the tick, priced where it ended: its highest YES price if takers
  bought YES, its lowest if they sold, the middle of its range if both. No print, no bid.
- Limit: PRICE_SCALE − reference − depth, rounded down to whole cents; none under 2¢.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, NamedTuple

import pandas as pd

from kalshibot.kalshi.models import PRICE_SCALE, SIZE_SCALE
from kalshibot.strategies.base import INTENT_COLUMNS, MarketData

log = logging.getLogger(__name__)
PRICE_CENT = PRICE_SCALE // 100
MIN_LIMIT = 2 * PRICE_CENT
EASTERN = "America/New_York"
TICKER = r"^[^-]+-(\d\d[A-Z]{3}\d\d)(\d{4})?"  # an event ticker's game date, and time if given


class League(NamedTuple):
    cap: timedelta  # how long after its listed start a game is live
    offsets: tuple[int, ...]  # whole days from start to latest_expiration_time; each is tried


# The universe of H8 v2's discovery trial (docs/STRATEGIES.md H8).
LEAGUES = {
    "KXMLBGAME": League(timedelta(hours=5), (14, 3)),
    "KXNCAAMBGAME": League(timedelta(hours=3), (14, 2)),
    "KXNHLGAME": League(timedelta(hours=3, minutes=30), (14, 2)),
    "KXWNBAGAME": League(timedelta(hours=3), (14, 2)),
}


class DeepBids:
    family = "H8"
    max_hold = timedelta(days=15)
    min_edge = 300  # 3¢ a contract: what Tier 1 can't see (queues, latency, stale quotes) costs

    def configs(self) -> dict[str, dict[str, Any]]:
        return {"NO 20¢, 10 s": {"maker": True, "horizon": timedelta(seconds=10),
                                 "depth": 20 * PRICE_CENT}}

    def intents(self, data: MarketData, start: datetime, end: datetime,
                config: dict[str, Any]) -> pd.DataFrame:
        every, depth = config["horizon"], config["depth"]
        listed = data.markets(end).filter(
            f"series_ticker IN ({', '.join(repr(s) for s in LEAGUES)})").project(
            "ticker, event_ticker, series_ticker, created_time, latest_expiration_time, "
            "mutually_exclusive").df()
        live, unregistered = _live(listed, start, end, every)  # the query reads `live`
        if len(unregistered):
            log.warning("H8: no registered start for %d listings, by series: %s",
                        len(unregistered), unregistered.value_counts().to_dict())
        if live.empty:
            return pd.DataFrame(columns=INTENT_COLUMNS)
        # Millions of bids: each names its market by its row in `live`, so the frame
        # holds one string per ticker
        chosen = data.trades(start - self.max_hold, end).query("t", f"""
            WITH ticks AS (
                SELECT market, ticker,
                       unnest(range(first_tick, stop,
                                    INTERVAL '{every.total_seconds()} seconds'))
                       AS decision_time
                FROM live),
             instants AS (
                SELECT ticker, created_time,
                       CASE WHEN bool_and(taker_side = 'yes') THEN max(yes_price)
                            WHEN bool_and(taker_side = 'no') THEN min(yes_price)
                            ELSE (max(yes_price) + min(yes_price)) / 2 END AS reference
                FROM t
                WHERE ticker IN (SELECT ticker FROM live)
                  AND NOT coalesce(is_block_trade, FALSE)
                GROUP BY ticker, created_time),
             bids AS (
                SELECT k.market, k.ticker, k.decision_time,
                       floor(({PRICE_SCALE} - i.reference - {depth}) / {PRICE_CENT})::BIGINT
                       * {PRICE_CENT} AS "limit"
                FROM ticks k ASOF JOIN instants i
                  ON k.ticker = i.ticker AND k.decision_time > i.created_time
                WHERE i.created_time >= k.decision_time
                                        - INTERVAL '{self.max_hold.total_seconds()} seconds')
            SELECT market, decision_time, "limit" FROM bids WHERE "limit" >= {MIN_LIMIT}
            ORDER BY decision_time, ticker""").df()
        return pd.DataFrame({
            "intent_id": range(len(chosen)),
            "ticker": live["ticker"].to_numpy()[chosen["market"].to_numpy()], "side": "no",
            "decision_time": chosen["decision_time"], "count": SIZE_SCALE,
            "limit": chosen["limit"]}, columns=INTENT_COLUMNS)


def _game_day(markets: pd.DataFrame) -> pd.Series:
    """The date in each market's event ticker (US Eastern, naive); NaT without one."""
    return pd.to_datetime(markets["event_ticker"].str.extract(TICKER)[0], format="%y%b%d",
                          errors="coerce")


def _listed_start(markets: pd.DataFrame) -> pd.Series:
    """Each market's listed start (module docstring, Live); NaT without a registered one.
    Raises if two of a league's offsets give one listing a start."""
    parts = markets["event_ticker"].str.extract(TICKER)
    given = pd.to_datetime(parts[0] + parts[1], format="%y%b%d%H%M", errors="coerce")
    rows = markets.assign(
        offset=markets["series_ticker"].map({s: lg.offsets for s, lg in LEAGUES.items()}),
        day=_game_day(markets), given=given.dt.tz_localize(
            EASTERN, ambiguous="NaT", nonexistent="NaT").dt.tz_convert("UTC")).explode("offset")
    lands = rows["latest_expiration_time"] - pd.to_timedelta(rows["offset"], unit="D")
    on_day = lands.dt.tz_convert(EASTERN).dt.tz_localize(None).dt.normalize() == rows["day"]
    start = rows["given"].fillna(lands)[on_day]
    if (twice := start.index.duplicated()).any():
        raise ValueError("two of a league's offsets give these listings a start: "
                         f"{list(markets.loc[start.index[twice], 'event_ticker'].unique())}")
    return start.reindex(markets.index)


def _live(markets: pd.DataFrame, start: datetime, end: datetime,
          every: timedelta) -> tuple[pd.DataFrame, pd.Series]:
    """Each market's ticks in [start, end) as (ticker, first_tick, stop exclusive, market:
    its row): every multiple of `every` from its listed start until its league's cap,
    while its event, mutually exclusive, shows exactly two markets (those created before
    the tick). Also the series of each exclusive listing without a registered start
    whose game day (as a UTC midnight; a listing without one, its creation) is in the
    window, so a window cut in parts counts each once."""
    step = pd.Timedelta(every)
    m = markets[markets["mutually_exclusive"].eq(True)].sort_values(
        ["event_ticker", "created_time", "ticker"], ignore_index=True)
    nth = m.groupby("event_ticker").cumcount().to_numpy()
    created = m.set_index("event_ticker")["created_time"]
    listing = {n: created[nth == n].reindex(m["event_ticker"]).set_axis(m.index)
               for n in (1, 2)}  # when the event's second and third markets were created
    begins = _listed_start(m)
    first = begins.clip(lower=pd.Timestamp(start)).dt.ceil(step)
    first = first.where(first > listing[1], listing[1].dt.floor(step) + step)
    stop = (begins + m["series_ticker"].map({s: lg.cap for s, lg in LEAGUES.items()})
            ).clip(upper=pd.Timestamp(end))
    third = listing[2].dt.floor(step) + step  # a tick at the third listing still sees two
    stop = stop.where(~(stop > third), third)
    when = _game_day(m).dt.tz_localize("UTC").fillna(m["created_time"])
    unregistered = m.loc[begins.isna() & (when >= pd.Timestamp(start))
                         & (when < pd.Timestamp(end)), "series_ticker"]
    keep = (nth < 2) & (first < stop)
    live = pd.DataFrame({"ticker": m["ticker"], "first_tick": first, "stop": stop})[keep]
    return live.reset_index(drop=True).assign(market=lambda f: f.index), unregistered
