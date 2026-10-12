import inspect
from dataclasses import fields

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from typer.testing import CliRunner

from kalshibot import cli
from kalshibot.config import REPO_ROOT, DataKey, data_dir
from kalshibot.kalshi.client import read_only


def test_config_only_knows_a_read_only_data_key():
    """Configuration never carries a key that can trade (rule 7); see .env.example."""
    assert {f.name for f in fields(DataKey)} == {"key_id", "pem"}


def test_data_dir_resolves_relative_to_repo(monkeypatch):
    monkeypatch.setenv("DATA_DIR", "somewhere")
    assert data_dir() == REPO_ROOT / "somewhere"
    monkeypatch.setenv("DATA_DIR", "/abs/path")
    assert str(data_dir()) == "/abs/path"


def test_only_read_scopes_count_as_read_only():
    assert read_only(["read"]) and read_only(["read", "read::portfolio_balance"])
    for scopes in (["read", "write"], ["read", "write::trade"], ["write::transfer"], []):
        assert not read_only(scopes)


@pytest.fixture
def data_key(monkeypatch, mock_client):
    """The .env key, a fresh Ed25519 key, and what Kalshi lists: the key `id` with `scopes`
    (beside another key of the account that can trade)."""
    pem = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    monkeypatch.setattr(cli, "load_data_key", lambda: DataKey("mine", pem))
    listed = {"id": "mine", "scopes": ["read"]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert "KALSHI-ACCESS-SIGNATURE" in request.headers
        if request.url.path.endswith("/api_keys"):
            return httpx.Response(200, json={"api_keys": [
                {"api_key_id": "other", "name": "trading", "scopes": ["read", "write"]},
                {"api_key_id": listed["id"], "name": "data", "scopes": listed["scopes"]}]})
        return httpx.Response(200, json={"balance": 0})

    monkeypatch.setattr(cli, "KalshiClient", lambda url, signer: mock_client(handler,
                                                                             signer=signer))
    return listed


def test_a_read_only_key_reads_and_records(data_key, monkeypatch):
    assert CliRunner().invoke(cli.app, ["whoami"]).output.startswith("[prod data key] read-only")
    got = []
    monkeypatch.setattr(cli, "record_frames", lambda signer, *a: got.append(signer) or _done())
    assert CliRunner().invoke(cli.app, ["record"]).exit_code == 0
    assert got[0].key_id == "mine"


async def _done() -> None:
    return None


@pytest.mark.parametrize("command", [["whoami"], ["record"]])
def test_a_key_that_can_act_on_the_account_is_refused(data_key, monkeypatch, command):
    data_key["scopes"] = ["read", "write"]
    monkeypatch.setattr(cli, "record_frames", lambda *a: pytest.fail("recorded"))
    out = CliRunner().invoke(cli.app, command)
    assert out.exit_code != 0 and "not read-only" in out.output


def test_a_key_kalshi_does_not_list_is_refused(data_key):
    data_key["id"] = "someone else's"
    out = CliRunner().invoke(cli.app, ["whoami"])
    assert out.exit_code != 0 and "Couldn't confirm" in out.output


def test_commands_load_the_data_key_only_through_the_check():
    callers = [p.relative_to(REPO_ROOT) for p in (REPO_ROOT / "src").rglob("*.py")
               if "load_data_key(" in p.read_text()]
    assert sorted(map(str, callers)) == ["src/kalshibot/cli.py", "src/kalshibot/config.py"]
    assert inspect.getsource(cli).count("load_data_key()") == 1  # in _read_only_signer
