"""Command-line entry point: `kalshibot --help`."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from datetime import date
from typing import Annotated

import typer

from kalshibot.config import MARKET_DATA_URL, READ_BUDGET, data_dir, load_data_key
from kalshibot.data import backfill as bf
from kalshibot.data.record import record as record_frames
from kalshibot.kalshi import KalshiClient, KalshiSigner
from kalshibot.kalshi.models import PRICE_SCALE

app = typer.Typer(no_args_is_help=True, help="Kalshi research & trading toolkit.")


def public_client() -> KalshiClient:
    return KalshiClient(MARKET_DATA_URL)


@app.command()
def markets(series: str = typer.Option("KXHIGHNY", help="Series ticker, e.g. KXHIGHNY")):
    """Show open markets in a series with their live top of book."""
    with public_client() as client:
        info = client.get_series(series)
        typer.echo(f"{info['title']}  [{series}]  settles via: "
                   + ", ".join(s["name"] for s in info.get("settlement_sources", [])))
        typer.echo(f"{'ticker':<28} {'contract':<16} {'bid':>6} {'ask':>6} {'spread':>6}")
        open_markets = list(client.iter_markets(series_ticker=series, status="open"))
        books = client.get_orderbooks([m["ticker"] for m in open_markets])

        def fmt(units: int | None) -> str:
            return "-" if units is None else f"{units / PRICE_SCALE:.4f}"

        for m in open_markets:
            book = books.get(m["ticker"])
            if book is None:
                continue
            typer.echo(f"{m['ticker']:<28} {m.get('yes_sub_title', ''):<16} "
                       f"{fmt(book.best_yes_bid):>6} {fmt(book.best_yes_ask):>6} "
                       f"{fmt(book.spread):>6}")


backfill_app = typer.Typer(no_args_is_help=True,
                           help="Download Kalshi reference data and trades to Parquet.")
app.add_typer(backfill_app, name="backfill")


@backfill_app.command("reference")
def backfill_reference():
    """Series, fee changes, events, and every non-combo market (live + historical)."""
    out = bf.kalshi_dir(data_dir())
    with KalshiClient(MARKET_DATA_URL, min_interval=1 / READ_BUDGET) as client:
        for step in (bf.backfill_series, bf.backfill_fee_changes, bf.backfill_markets,
                     bf.backfill_events):
            typer.echo(f"{step.__name__} -> {step(client, out)}")


@backfill_app.command("trades")
def backfill_trades(
    start: str = typer.Option(..., help="First UTC day, YYYY-MM-DD"),
    end: str = typer.Option(..., help="Day after the last, YYYY-MM-DD (exclusive)"),
    workers: int = typer.Option(2, help="Days fetched in parallel, sharing the read budget"),
):
    """Every non-combo trade in [start, end), one Parquet file per finished UTC day.
    Resumable: days already on disk are skipped."""
    paths = bf.backfill_trades(
        lambda: KalshiClient(MARKET_DATA_URL, min_interval=workers / READ_BUDGET),
        bf.kalshi_dir(data_dir()), date.fromisoformat(start), date.fromisoformat(end),
        workers=workers)
    typer.echo(f"wrote {len(paths)} day files")


@app.command()
def record(
    channels: str = typer.Option("orderbook_delta,trade", help="Comma-separated WS channels"),
    series: Annotated[list[str] | None, typer.Option(
        help="Only these series (default: all open markets)")] = None,
):
    """Record raw WebSocket frames for open non-combo markets (needs the read-only data key).
    Runs until stopped; reconnects with backoff and logs gaps to raw/ws/coverage.jsonl."""
    key = load_data_key()

    def universe() -> list[str]:
        with public_client() as client:
            return [m["ticker"] for s in (series or [None])
                    for m in client.iter_markets(series_ticker=s, status="open",
                                                 mve_filter="exclude")]

    signer = KalshiSigner.from_pem(key.key_id, key.pem)

    async def main() -> None:
        task = asyncio.create_task(record_frames(signer, universe, data_dir() / "raw" / "ws",
                                                 channels.split(",")))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):  # stop cleanly: flush, log
            loop.add_signal_handler(sig, task.cancel)
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(main())


@app.command()
def whoami():
    """Check the prod data key from .env by fetching your balance (the key must be read-only)."""
    key = load_data_key()
    with KalshiClient(MARKET_DATA_URL, KalshiSigner.from_pem(key.key_id, key.pem)) as client:
        balance = client.get_balance()
    typer.echo(f"[prod data key] authenticated OK. Balance: {balance}")


if __name__ == "__main__":
    app()
