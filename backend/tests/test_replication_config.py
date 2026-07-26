import importlib

import pytest
from fastapi.testclient import TestClient

from tests.test_auth import NAMESPACE_PASSPHRASE, register


def test_replication_config_requires_session(client):
    assert client.get("/config/replication").status_code == 401


def test_replication_config_defaults(client):
    register(client)
    client.post("/login", json={"username": "alice", "password": "hunter22"})

    body = client.get("/config/replication").json()

    assert body == {"replication_factor": 3, "write_quorum": 2}


def _fresh_client(tmp_path, monkeypatch, **env):
    db_path = tmp_path / "coordinator.db"
    monkeypatch.setenv("COORDINATOR_DB_PATH", str(db_path))
    monkeypatch.setenv("NAMESPACE_PASSPHRASE", NAMESPACE_PASSPHRASE)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import coordinator.app as app_module
    import coordinator.db as db_module
    import coordinator.models as models_module

    importlib.reload(db_module)
    importlib.reload(models_module)
    importlib.reload(app_module)
    return app_module


def test_replication_config_reads_from_environment(tmp_path, monkeypatch):
    app_module = _fresh_client(tmp_path, monkeypatch, REPLICATION_FACTOR="5", WRITE_QUORUM="3")

    with TestClient(app_module.app) as c:
        register(c)
        c.post("/login", json={"username": "alice", "password": "hunter22"})
        assert c.get("/config/replication").json() == {
            "replication_factor": 5,
            "write_quorum": 3,
        }


def test_write_quorum_exceeding_replication_factor_fails_startup(tmp_path, monkeypatch):
    app_module = _fresh_client(tmp_path, monkeypatch, REPLICATION_FACTOR="2", WRITE_QUORUM="3")

    with pytest.raises(RuntimeError):
        with TestClient(app_module.app):
            pass


def test_replication_factor_below_two_fails_startup(tmp_path, monkeypatch):
    app_module = _fresh_client(tmp_path, monkeypatch, REPLICATION_FACTOR="1")

    with pytest.raises(RuntimeError):
        with TestClient(app_module.app):
            pass


@pytest.mark.parametrize("write_quorum", ["0", "-5"])
def test_write_quorum_below_one_fails_startup(tmp_path, monkeypatch, write_quorum):
    # W=0 would commit a write that no replica acknowledged.
    app_module = _fresh_client(tmp_path, monkeypatch, WRITE_QUORUM=write_quorum)

    with pytest.raises(RuntimeError):
        with TestClient(app_module.app):
            pass


@pytest.mark.parametrize("env", [{"REPLICATION_FACTOR": "abc"}, {"WRITE_QUORUM": ""}])
def test_non_numeric_config_fails_with_a_clear_error(tmp_path, monkeypatch, env):
    app_module = _fresh_client(tmp_path, monkeypatch, **env)

    with pytest.raises(RuntimeError, match="whole number"):
        with TestClient(app_module.app):
            pass
