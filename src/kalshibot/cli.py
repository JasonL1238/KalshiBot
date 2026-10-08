"""Command-line entry point: `kalshibot --help`."""

from __future__ import annotations

import time

import typer

from kalshibot.config import MARKET_DATA_URL, data_dir, load_data_key
from kalshibot.data.backfill import backfill_markets, markets_path
from kalshibot.data.snapshot import snapshot_series, write_snapshot
from kalshibot.kalshi import KalshiClient, KalshiSigner

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
        typer.echo(f"{'ticker':<28} {'contract':<16} {'bid':>6} {'ask':>6} {'spread':>7}")
        open_markets = list(client.iter_markets(series_ticker=series, status="open"))
        books = client.get_orderbooks([m["ticker"] for m in open_markets])

        def fmt(x: float | None) -> str:
            return "-" if x is None else f"{x:.2f}"

        for m in open_markets:
            book = books.get(m["ticker"])
            if book is None:
                continue
            typer.echo(f"{m['ticker']:<28} {m.get('yes_sub_title', ''):<16} "
                       f"{fmt(book.best_yes_bid):>6} {fmt(book.best_yes_ask):>6} "
                       f"{fmt(book.spread):>7}")


@app.command()
def backfill(series: str = typer.Option("KXHIGHNY", help="Series ticker to backfill")):
    """Download every settled market in a series (historical + recent) to Parquet."""
    with public_client() as client:
        cutoff = client.historical_cutoff().get("market_settled_ts")
        typer.echo(f"Historical/live cutoff: {cutoff}")
        df = backfill_markets(client, series)
    if df.empty:
        typer.echo("No settled markets found.")
        raise typer.Exit(1)
    path = markets_path(data_dir(), series)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    typer.echo(f"Wrote {len(df):,} markets across {df['event_ticker'].nunique():,} events "
               f"({df['close_time'].min():%Y-%m-%d} to {df['close_time'].max():%Y-%m-%d}) "
               f"-> {path}")


@app.command()
def snapshot(
    series: str = typer.Option("KXHIGHNY", help="Series ticker to snapshot"),
    every: int = typer.Option(0, help="Repeat every N seconds (0 = run once)"),
    depth: int = typer.Option(10, help="Orderbook levels to keep per side"),
):
    """Record orderbook snapshots for every open market in a series."""
    with public_client() as client:
        while True:
            df = snapshot_series(client, series, depth=depth)
            path = write_snapshot(df, data_dir(), series)
            typer.echo(f"{len(df)} markets -> {path}")
            if every <= 0:
                break
            time.sleep(every)


@app.command()
def whoami():
    """Check the prod data key from .env by fetching your balance (the key must be read-only)."""
    key = load_data_key()
    with KalshiClient(MARKET_DATA_URL, KalshiSigner.from_pem(key.key_id, key.pem)) as client:
        balance = client.get_balance()
    typer.echo(f"[prod data key] authenticated OK. Balance: {balance}")


if __name__ == "__main__":
    app()
