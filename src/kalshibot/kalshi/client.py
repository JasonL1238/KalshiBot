"""Thin synchronous client for the Kalshi Trade API v2.

Design notes:
- Public market-data endpoints need no auth; portfolio/trading endpoints do.
- List endpoints are cursor-paginated; `_paginate` hides that behind a generator.
- GETs are retried with exponential backoff on 429 (rate limited), 5xx, and dropped
  connections. Nothing else is ever retried: a resent order could fill twice.
- Kalshi moves settled markets older than a cutoff into `/historical/*` endpoints.
  `historical_cutoff()` tells you where the split is, so a full backfill reads both.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from itertools import batched
from typing import Any
from urllib.parse import urlparse

import httpx

from kalshibot import __version__
from kalshibot.kalshi.auth import KalshiSigner
from kalshibot.kalshi.models import OrderBook

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
ORDERBOOKS_MAX = 100  # markets per GET /markets/orderbooks


class KalshiClient:
    def __init__(
        self,
        base_url: str,
        signer: KalshiSigner | None = None,
        *,
        timeout: float = 20.0,
        max_retries: int = 8,
        min_interval: float = 0.1,
    ):
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"User-Agent": f"kalshibot/{__version__}"},
        )
        self._path_prefix = urlparse(base_url).path  # "/trade-api/v2", part of what gets signed
        self._signer = signer
        self._max_retries = max_retries
        self._min_interval = min_interval
        self._last_request = 0.0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> KalshiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- plumbing -------------------------------------------------------------------

    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        auth: bool = False,
    ) -> dict[str, Any]:
        if auth and self._signer is None:
            raise RuntimeError(f"{method} {path} requires credentials; see .env.example")
        params = {k: v for k, v in (params or {}).items() if v is not None}

        retries = self._max_retries if method == "GET" else 0
        for attempt in range(retries + 1):
            self._throttle()
            headers = self._signer.headers(method, self._path_prefix + path) if auth else None
            try:
                resp = self._http.request(method, path, params=params, json=json,
                                          headers=headers)
            except httpx.TransportError:
                if attempt == retries:
                    raise
                time.sleep(min(2**attempt * 0.5, 30))
                continue
            if resp.status_code not in RETRYABLE_STATUS or attempt == retries:
                break
            retry_after = resp.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after else min(2**attempt * 0.5, 30))

        resp.raise_for_status()
        return resp.json()

    def _paginate(
        self, path: str, key: str, params: dict[str, Any], *, auth: bool = False, limit: int = 1000,
        next_key: str = "cursor",
    ) -> Iterator[dict[str, Any]]:
        cursor = None
        while True:
            page = self._request("GET", path, params={**params, "limit": limit, "cursor": cursor},
                                 auth=auth)
            yield from page.get(key) or []
            cursor = page.get(next_key)
            if not cursor:
                return

    # ---- exchange & reference data (public) -----------------------------------------

    def exchange_status(self) -> dict[str, Any]:
        return self._request("GET", "/exchange/status")

    def get_series(self, series_ticker: str) -> dict[str, Any]:
        return self._request("GET", f"/series/{series_ticker}")["series"]

    def get_all_series(self) -> list[dict[str, Any]]:
        """Every series in one response: category, fee_type, fee_multiplier, ..."""
        return self._request("GET", "/series")["series"]

    def get_fee_changes(self) -> list[dict[str, Any]]:
        """Logged per-series fee changes (coverage: docs/RESEARCH.md §1)."""
        return self._request("GET", "/series/fee_changes",
                             params={"show_historical": "true"})["series_fee_change_arr"]

    def iter_events(self, *, status: str | None = None) -> Iterator[dict[str, Any]]:
        """All events, including settled ones (there is no /historical/events)."""
        return self._paginate("/events", "events", {"status": status}, limit=200)

    def get_event(self, event_ticker: str, *, with_markets: bool = True) -> dict[str, Any]:
        return self._request("GET", f"/events/{event_ticker}",
                             params={"with_nested_markets": with_markets})

    def get_market(self, ticker: str) -> dict[str, Any]:
        return self._request("GET", f"/markets/{ticker}")["market"]

    def iter_markets(
        self,
        *,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = None,
        min_close_ts: int | None = None,
        max_close_ts: int | None = None,
        mve_filter: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """status: unopened | open | closed | settled; mve_filter: only | exclude (combos)."""
        return self._paginate("/markets", "markets", {
            "series_ticker": series_ticker,
            "event_ticker": event_ticker,
            "status": status,
            "min_close_ts": min_close_ts,
            "max_close_ts": max_close_ts,
            "mve_filter": mve_filter,
        })

    # ---- live market data (public) --------------------------------------------------

    def get_orderbook(self, ticker: str, *, depth: int | None = None) -> OrderBook:
        payload = self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        return OrderBook.from_api(ticker, payload)

    def get_orderbooks_payload(self, tickers: list[str]) -> dict[str, Any]:
        """The raw response with full books for up to `ORDERBOOKS_MAX` markets, all from the
        same instant."""
        if len(tickers) > ORDERBOOKS_MAX:
            raise ValueError(f"at most {ORDERBOOKS_MAX} tickers per request")
        return self._request("GET", "/markets/orderbooks", params={"tickers": tickers})

    def get_orderbooks(self, tickers: list[str]) -> dict[str, OrderBook]:
        """Full books, parsed; one request (one instant) per `ORDERBOOKS_MAX` markets."""
        books: dict[str, OrderBook] = {}
        for chunk in batched(tickers, ORDERBOOKS_MAX, strict=False):
            for entry in self.get_orderbooks_payload(list(chunk)).get("orderbooks") or []:
                books[entry["ticker"]] = OrderBook.from_api(entry["ticker"], entry)
        return books

    def iter_incentive_programs(self) -> Iterator[dict[str, Any]]:
        """Every incentive program Kalshi serves, each for one market and window."""
        return self._paginate("/incentive_programs", "incentive_programs", {},
                              next_key="next_cursor")

    def iter_trades(
        self, *, ticker: str | None = None, min_ts: int | None = None, max_ts: int | None = None
    ) -> Iterator[dict[str, Any]]:
        return self._paginate("/markets/trades", "trades",
                              {"ticker": ticker, "min_ts": min_ts, "max_ts": max_ts})

    def get_candlesticks(
        self, ticker: str, *, start_ts: int, end_ts: int, period_interval: int = 60,
        series_ticker: str | None = None,
    ) -> list[dict[str, Any]]:
        """OHLC of yes_bid, yes_ask, and trade price. period_interval is in minutes: 1, 60,
        or 1440. Live markets need `series_ticker`; markets settled before
        `historical_cutoff()` are only served without it, from the historical archive."""
        path = (f"/series/{series_ticker}/markets/{ticker}/candlesticks" if series_ticker
                else f"/historical/markets/{ticker}/candlesticks")
        params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval}
        return self._request("GET", path, params=params)["candlesticks"]

    # ---- historical archive (public) ------------------------------------------------

    def historical_cutoff(self) -> dict[str, Any]:
        return self._request("GET", "/historical/cutoff")

    def iter_historical_markets(
        self, *, series_ticker: str | None = None, event_ticker: str | None = None,
        mve_filter: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        return self._paginate("/historical/markets", "markets", {
            "series_ticker": series_ticker, "event_ticker": event_ticker, "mve_filter": mve_filter,
        })

    def iter_historical_trades(
        self, *, ticker: str | None = None, min_ts: int | None = None, max_ts: int | None = None
    ) -> Iterator[dict[str, Any]]:
        return self._paginate("/historical/trades", "trades",
                              {"ticker": ticker, "min_ts": min_ts, "max_ts": max_ts})

    # ---- portfolio (authenticated) --------------------------------------------------

    def get_balance(self) -> dict[str, Any]:
        return self._request("GET", "/portfolio/balance", auth=True)

    def iter_positions(self) -> Iterator[dict[str, Any]]:
        return self._paginate("/portfolio/positions", "market_positions", {}, auth=True,
                              limit=200)
