"""The trial ledger: every evaluation is recorded before anyone looks at its result.

`ledger/<family>.jsonl` (committed, append-only, checked by the hygiene test) holds one
line per trial: config, window, data and code fingerprints, summary statistics. Each
trial's daily returns go to `data_dir()/research/trials/` for DSR, PBO, and effective-N.
`ledger/lockbox.jsonl` holds the single lockbox unlock: exactly one entry naming the
surviving families. Until it exists, the loader refuses lockbox data, and afterwards it
serves it only to those families.

The ledger always lives in the main checkout (found through git), so runs in a git
worktree append to the same files. `KALSHIBOT_LEDGER` overrides the location for tests.
Without a ledger directory nothing can be recorded, and runs refuse to start.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from kalshibot.config import REPO_ROOT, data_dir


def _main_checkout() -> Path:
    out = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                         cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    return Path(out.stdout.strip()).parent


def ledger_dir() -> Path:
    path = Path(os.getenv("KALSHIBOT_LEDGER") or _main_checkout() / "ledger")
    if not path.is_dir():
        raise RuntimeError(f"no trial ledger at {path}: create it in the main checkout "
                           "(mkdir ledger)")
    return path


def _commit() -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True,
                         text=True)
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "src"], cwd=REPO_ROOT,
                           capture_output=True, text=True).stdout.strip()
    return out.stdout.strip() + ("-dirty" if dirty else "")


def _append(path: Path, entry: dict[str, Any]) -> None:
    with path.open("a") as f:
        f.write(json.dumps(entry, sort_keys=True, default=str) + "\n")


def record_trial(family: str, config: dict[str, Any], window: str, data_hash: str,
                 returns: pd.Series, summary: dict[str, Any]) -> str:
    """Append one trial and store its daily returns; return the trial id."""
    ledger = ledger_dir()
    now = datetime.now(UTC).isoformat()
    body = json.dumps({"family": family, "config": config, "window": window,
                       "data_hash": data_hash, "time": now}, sort_keys=True, default=str)
    trial_id = hashlib.sha256(body.encode()).hexdigest()[:16]
    folder = data_dir() / "research" / "trials" / family
    folder.mkdir(parents=True, exist_ok=True)
    returns.rename("return").to_frame().to_parquet(folder / f"{trial_id}.parquet")
    _append(ledger / f"{family}.jsonl", {
        "trial_id": trial_id, "time": now, "family": family, "window": window,
        "config": config, "data_hash": data_hash, "commit": _commit(), "summary": summary})
    return trial_id


def trials(family: str) -> list[dict[str, Any]]:
    path = ledger_dir() / f"{family}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def trial_returns(family: str) -> pd.DataFrame:
    """Daily returns of every trial in the family, one column per trial id."""
    folder = data_dir() / "research" / "trials" / family
    cols = {t["trial_id"]: pd.read_parquet(folder / f"{t['trial_id']}.parquet")["return"]
            for t in trials(family)}
    return pd.DataFrame(cols).fillna(0.0)


def lockbox_survivors() -> set[str] | None:
    """The families allowed into the lockbox, or None while it is closed. Anything but a
    single well-formed unlock entry counts as closed."""
    try:
        path = ledger_dir() / "lockbox.jsonl"
    except RuntimeError:
        return None
    if not path.exists():
        return None
    lines = path.read_text().splitlines()
    if len(lines) != 1:
        raise RuntimeError(f"{path} must hold exactly one unlock entry, found {len(lines)}")
    entry = json.loads(lines[0])
    if not entry.get("survivors") or not entry.get("statistic"):
        raise RuntimeError(f"{path} lacks survivors or a statistic: {entry}")
    return set(entry["survivors"])


def unlock_lockbox(survivors: list[str], statistic: str) -> None:
    """Open the lockbox once, for every survivor together, with a pre-registered statistic."""
    path = ledger_dir() / "lockbox.jsonl"
    if path.exists():
        raise RuntimeError("the lockbox was already opened: " + path.read_text().strip())
    if not survivors or not statistic:
        raise ValueError("an unlock names the surviving families and the statistic")
    _append(path, {"time": datetime.now(UTC).isoformat(), "survivors": sorted(survivors),
                   "statistic": statistic, "commit": _commit()})
