"""Thin synchronous client for the Kalshi Trade API v2.

Design notes:
- Public market-data endpoints need no auth; portfolio/trading endpoints do.
- List endpoints are cursor-paginated; `_paginate` hides that behind a generator.
- 429 (rate limited) and 5xx responses are retried with exponential backoff.
- Kalshi moves settled markets older than a cutoff into `/historical/*` endpoints.
  `historical_cutoff()` tells you where the split is, so a full backfill reads both.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import httpx

from kalshibot import __version__
from kalshibot.kalshi.auth import KalshiSigner
from kalshibot.kalshi.models import OrderBook

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class KalshiClient:
    def __init__(
        self,
        base_url: str,
        signer: KalshiSigner | None = None,
        *,
        timeout: float = 20.0,
        max_retries: int = 5,
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

        for attempt in range(self._max_retries + 1):
            self._throttle()
            headers = self._signer.headers(method, self._path_prefix + path) if auth else None
            resp = self._http.request(method, path, params=params, json=json, headers=headers)
            if resp.status_code not in RETRYABLE_STATUS or attempt == self._max_retries:
                break
            retry_after = resp.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after else min(2**attempt * 0.5, 30))

        resp.raise_for_status()
        return resp.json()

    def _paginate(
        self, path: str, key: str, params: dict[str, Any], *, auth: bool = False, limit: int = 1000
    ) -> Iterator[dict[str, Any]]:
        cursor = None
        while True:
            page = self._request("GET", path, params={**params, "limit": limit, "cursor": cursor},
                                 auth=auth)
            yield from page.get(key) or []
            cursor = page.get("cursor")
            if not cursor:
                return

    # ---- exchange & reference data (public) -----------------------------------------

    def exchange_status(self) -> dict[str, Any]:
        return self._request("GET", "/exchange/status")

    def get_series(self, series_ticker: str) -> dict[str, Any]:
        return self._request("GET", f"/series/{series_ticker}")["series"]

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
    ) -> Iterator[dict[str, Any]]:
        """status: unopened | open | closed | settled"""
        return self._paginate("/markets", "markets", {
            "series_ticker": series_ticker,
            "event_ticker": event_ticker,
            "status": status,
            "min_close_ts": min_close_ts,
            "max_close_ts": max_close_ts,
        })

    # ---- live market data (public) --------------------------------------------------

    def get_orderbook(self, ticker: str, *, depth: int | None = None) -> OrderBook:
        payload = self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        return OrderBook.from_api(ticker, payload)

    def get_orderbooks(self, tickers: list[str]) -> dict[str, OrderBook]:
        """Full books for up to 100 markets in one request, all from the same instant."""
        books: dict[str, OrderBook] = {}
        for i in range(0, len(tickers), 100):
            payload = self._request("GET", "/markets/orderbooks",
                                    params={"tickers": tickers[i:i + 100]})
            for entry in payload.get("orderbooks") or []:
                books[entry["ticker"]] = OrderBook.from_api(entry["ticker"], entry)
        return books

    def iter_trades(
        self, *, ticker: str | None = None, min_ts: int | None = None, max_ts: int | None = None
    ) -> Iterator[dict[str, Any]]:
        return self._paginate("/markets/trades", "trades",
                              {"ticker": ticker, "min_ts": min_ts, "max_ts": max_ts})

    def get_candlesticks(
        self, series_ticker: str, ticker: str, *, start_ts: int, end_ts: int,
        period_interval: int = 60,
    ) -> list[dict[str, Any]]:
        """period_interval is in minutes: 1, 60, or 1440."""
        return self._request(
            "GET", f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        )["candlesticks"]

    # ---- historical archive (public) ------------------------------------------------

    def historical_cutoff(self) -> dict[str, Any]:
        return self._request("GET", "/historical/cutoff")

    def iter_historical_markets(
        self, *, series_ticker: str | None = None, event_ticker: str | None = None
    ) -> Iterator[dict[str, Any]]:
        return self._paginate("/historical/markets", "markets",
                              {"series_ticker": series_ticker, "event_ticker": event_ticker})

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
