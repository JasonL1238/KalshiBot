import pytest

from kalshibot.config import data_dir


def pytest_collection_modifyitems(config, items):
    """Skip `data` tests without local data (e.g. in CI) and `slow` tests unless -m asks."""
    raw = data_dir() / "raw"
    skips = {}
    if not raw.exists():
        skips["data"] = pytest.mark.skip(reason=f"no local data under {raw}")
    if "slow" not in (config.getoption("markexpr") or ""):
        skips["slow"] = pytest.mark.skip(reason="slow; run with -m slow")
    for item in items:
        for marker, skip in skips.items():
            if item.get_closest_marker(marker):
                item.add_marker(skip)
