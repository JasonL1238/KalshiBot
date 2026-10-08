from dataclasses import fields

from kalshibot.config import REPO_ROOT, DataKey, data_dir


def test_config_only_knows_a_read_only_data_key():
    """Configuration never carries a key that can trade (rule 7); see .env.example."""
    assert {f.name for f in fields(DataKey)} == {"key_id", "pem"}


def test_data_dir_resolves_relative_to_repo(monkeypatch):
    monkeypatch.setenv("DATA_DIR", "somewhere")
    assert data_dir() == REPO_ROOT / "somewhere"
    monkeypatch.setenv("DATA_DIR", "/abs/path")
    assert str(data_dir()) == "/abs/path"
