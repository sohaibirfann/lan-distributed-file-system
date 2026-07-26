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

    assert body == {
        "replication_factor": 3,
        "write_quorum": 2,
        "registered_node_count": 0,
        "under_replicated": True,
        "eligible_node_count": 0,
        "write_available": False,
    }


def test_under_replicated_clears_once_enough_nodes_register(client):
    from tests.test_nodes import register_node

    register(client)
    client.post("/login", json={"username": "alice", "password": "hunter22"})
    for i in range(3):
        register_node(client, address=f"node-{i}:9000")

    body = client.get("/config/replication").json()

    assert body["registered_node_count"] == 3
    assert body["under_replicated"] is False
    assert body["eligible_node_count"] == 3
    assert body["write_available"] is True


def test_registered_count_can_hide_an_active_outage(client):
    # A node that's down is still "registered", so under_replicated alone can
    # read healthy while writes are failing — eligible_node_count and
    # write_available are what actually reflect that.
    from datetime import datetime, timedelta, timezone

    from coordinator.db import SessionLocal
    from coordinator.models import Node
    from shared.placement import DOWN_GRACE_PERIOD, SUSPECT_THRESHOLD
    from tests.test_nodes import register_node

    register(client)
    client.post("/login", json={"username": "alice", "password": "hunter22"})
    for i in range(3):
        register_node(client, address=f"node-{i}:9000")

    db = SessionLocal()
    try:
        for node in db.query(Node).limit(2):
            node.last_heartbeat_at = (
                datetime.now(timezone.utc) - SUSPECT_THRESHOLD - DOWN_GRACE_PERIOD - timedelta(seconds=5)
            )
        db.commit()
    finally:
        db.close()

    body = client.get("/config/replication").json()

    assert body["registered_node_count"] == 3
    assert body["under_replicated"] is False  # this alone would look fine
    assert body["eligible_node_count"] == 1
    assert body["write_available"] is False  # write_quorum is 2


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
            "registered_node_count": 0,
            "under_replicated": True,
            "eligible_node_count": 0,
            "write_available": False,
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
