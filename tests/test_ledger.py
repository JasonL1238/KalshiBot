import pandas as pd
import pytest

from kalshibot.research import ledger


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "ledger").mkdir()
    monkeypatch.setenv("KALSHIBOT_LEDGER", str(tmp_path / "ledger"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def test_trials_are_appended_with_their_returns(env):
    r = pd.Series([0.01, -0.02], index=pd.to_datetime(["2025-12-01", "2025-12-02"], utc=True))
    a = ledger.record_trial("H1", {"threshold": 0.1}, "discovery", "abc", r, {"sharpe": 0.1})
    b = ledger.record_trial("H1", {"threshold": 0.2}, "discovery", "abc", r * 2, {"sharpe": 0.1})
    assert [t["trial_id"] for t in ledger.trials("H1")] == [a, b]
    assert ledger.trial_returns("H1")[b].tolist() == [0.02, -0.04]
    before = (env / "ledger" / "H1.jsonl").read_text()
    ledger.record_trial("H1", {"threshold": 0.3}, "discovery", "abc", r, {})
    assert (env / "ledger" / "H1.jsonl").read_text().startswith(before)  # append-only


def test_lockbox_opens_exactly_once_for_named_survivors(env):
    assert ledger.lockbox_survivors() is None
    ledger.unlock_lockbox(["H1", "H4"], "market-calibrated null p-value")
    assert ledger.lockbox_survivors() == {"H1", "H4"}
    with pytest.raises(RuntimeError):
        ledger.unlock_lockbox(["H2"], "again")


@pytest.mark.parametrize("content", ["", '{"survivors": []}\n', '{"a": 1}\n{"b": 2}\n'])
def test_a_malformed_unlock_never_opens_the_lockbox(env, content):
    (env / "ledger" / "lockbox.jsonl").write_text(content)
    with pytest.raises(RuntimeError):
        ledger.lockbox_survivors()


def test_no_ledger_means_no_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("KALSHIBOT_LEDGER", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError):
        ledger.record_trial("H1", {}, "discovery", "x", pd.Series(dtype=float), {})
