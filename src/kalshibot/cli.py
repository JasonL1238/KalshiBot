"""Command-line entry point: `kalshibot --help`."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from datetime import date
from typing import Annotated

import typer

from kalshibot.config import MARKET_DATA_URL, data_dir, load_data_key
from kalshibot.data import backfill as bf
from kalshibot.data.record import record as record_frames
from kalshibot.kalshi import KalshiClient, KalshiSigner
from kalshibot.kalshi.models import PRICE_SCALE
from kalshibot.research import ledger, run
from kalshibot.research.load import Loader
from kalshibot.strategies import FAMILIES

app = typer.Typer(no_args_is_help=True, help="Kalshi research & trading toolkit.")


@app.command()
def markets(series: str = typer.Option("KXHIGHNY", help="Series ticker, e.g. KXHIGHNY")):
    """Show open markets in a series with their live top of book."""
    with bf.public_clients(1)() as client:
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
    with bf.public_clients(1)() as client:
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
        bf.public_clients(workers),
        bf.kalshi_dir(data_dir()), date.fromisoformat(start), date.fromisoformat(end),
        workers=workers)
    typer.echo(f"wrote {len(paths)} day files")


research_app = typer.Typer(no_args_is_help=True, help="Evaluate a hypothesis family, "
                            "recorded in the trial ledger (docs/BACKTESTING.md, Pipeline).")
app.add_typer(research_app, name="research")
Family = Annotated[str, typer.Argument(help=f"One of {', '.join(FAMILIES)}")]


def _family(name: str):
    if name not in FAMILIES:
        raise typer.BadParameter(f"unknown family {name!r}; known: {', '.join(FAMILIES)}")
    return FAMILIES[name], Loader(family=name)


@research_app.command("discover")
def research_discover(family: Family):
    """Every configuration on discovery, each recorded in the trial ledger."""
    returns = run.discover(*_family(family))
    for trial in ledger.trials(family)[-returns.shape[1]:]:
        typer.echo(f"{trial['trial']['name']}: {trial['summary']}")


@research_app.command("candles")
def research_candles(
    family: Family,
    workers: int = typer.Option(2, help="Markets fetched in parallel, sharing the read budget"),
):
    """The 1-minute candles the family's confirmation test prices its null from."""
    strategy, loader = _family(family)
    typer.echo(f"wrote {run.fetch_candles(strategy, loader, workers)} candle files")


@research_app.command("confirm")
def research_confirm(family: Family):
    """The family's one confirmation test (it can't be repeated), then its Tier-2 report."""
    strategy, loader = _family(family)
    verdict = run.confirm(strategy, loader)
    power = ledger.confirmation(family)["summary"]["power"]
    typer.echo(f"{verdict.config}: p = {verdict.p_value:.4g} (null {verdict.p_null:.4g}, "
               f"bootstrap {verdict.p_boot:.4g}); power at "
               f"{power['edge'] / (PRICE_SCALE // 100):g}¢: {power['power']:.2f}; the rest is "
               f"in {ledger.ledger_dir() / f'{family}.jsonl'}")
    try:
        typer.echo(f"Tier 2: {run.report(strategy, loader)}")
    except Exception:
        typer.echo(f"The verdict is recorded; its Tier-2 report failed. Rerun it with "
                   f"`kalshibot research report {family}`.", err=True)
        raise


@research_app.command("report")
def research_report(family: Family):
    """The Tier-2 report on the family's confirmation again (it can't change the verdict)."""
    typer.echo(run.report(*_family(family)))


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
        with bf.public_clients(1)() as client:
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
