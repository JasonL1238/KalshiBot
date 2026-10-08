"""Runtime configuration from environment variables, with `.env` at the repo root as fallback.

`.env` only ever holds a **read-only** prod key: it signs market-data WebSocket
connections and read-only account calls. A key that can trade (prod or demo) never goes
in configuration; order commands take a key path argument from the user. Reading `.env`
never modifies `os.environ`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

# Public market data (prices, orderbooks, history) is always read from production:
# the demo exchange has fake markets with no real price discovery.
MARKET_DATA_URL = "https://external-api.kalshi.com/trade-api/v2"

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str) -> str | None:
    return os.getenv(name) or dotenv_values(REPO_ROOT / ".env").get(name) or None


def data_dir() -> Path:
    """`DATA_DIR`, with relative paths resolved against the repo root (default `data`)."""
    path = Path(_env("DATA_DIR") or "data").expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


@dataclass(frozen=True)
class DataKey:
    """The read-only prod API key. Loaded only by commands that sign requests."""

    key_id: str
    pem: bytes


def load_data_key() -> DataKey:
    key_id = _env("KALSHI_DATA_KEY_ID")
    if raw := _env("KALSHI_DATA_KEY"):
        pem = raw.encode()
    elif path := _env("KALSHI_DATA_KEY_PATH"):
        file = Path(path).expanduser()
        if not file.exists():
            raise SystemExit(f"KALSHI_DATA_KEY_PATH points to a missing file: {file}")
        pem = file.read_bytes()
    else:
        pem = None
    if not (key_id and pem):
        raise SystemExit("No data key configured: copy .env.example to .env and fill it in.")
    return DataKey(key_id=key_id, pem=pem)
