from datetime import datetime, timedelta, timezone

from shared.placement import DOWN_GRACE_PERIOD, SUSPECT_THRESHOLD
from tests.test_auth import register
from tests.test_files import make_file_body
from tests.test_nodes import login, register_node


def mark_down(address: str) -> None:
    from coordinator.db import SessionLocal
    from coordinator.models import Node

    db = SessionLocal()
    try:
        node = db.query(Node).filter_by(address=address).first()
        node.last_heartbeat_at = (
            datetime.now(timezone.utc) - SUSPECT_THRESHOLD - DOWN_GRACE_PERIOD - timedelta(seconds=5)
        )
        db.commit()
    finally:
        db.close()


def mark_suspect(address: str) -> None:
    from coordinator.db import SessionLocal
    from coordinator.models import Node

    db = SessionLocal()
    try:
        node = db.query(Node).filter_by(address=address).first()
        node.last_heartbeat_at = datetime.now(timezone.utc) - SUSPECT_THRESHOLD - timedelta(seconds=5)
        db.commit()
    finally:
        db.close()


def test_replication_health_requires_session(client):
    assert client.get("/replication/health").status_code == 401


def test_fully_replicated_chunk_is_not_reported(client):
    register(client)
    login(client)
    node_ids = [register_node(client, address=f"n{i}:9000").json()["id"] for i in range(3)]
    client.post("/files", json=make_file_body(chunk_sizes=(100,), node_ids=node_ids))

    assert client.get("/replication/health").json() == []


def test_chunk_with_a_down_replica_is_under_replicated(client):
    register(client)
    login(client)
    node_ids = [register_node(client, address=f"n{i}:9000").json()["id"] for i in range(3)]
    client.post("/files", json=make_file_body(chunk_sizes=(100,), node_ids=node_ids))

    mark_down("n0:9000")

    body = client.get("/replication/health").json()
    assert len(body) == 1
    assert body[0]["status"] == "under_replicated"
    assert set(body[0]["healthy_node_ids"]) == set(node_ids[1:])


def test_chunk_with_all_replicas_down_is_unavailable(client):
    register(client)
    login(client)
    node_ids = [register_node(client, address=f"n{i}:9000").json()["id"] for i in range(2)]
    client.post("/files", json=make_file_body(chunk_sizes=(100,), node_ids=node_ids))

    mark_down("n0:9000")
    mark_down("n1:9000")

    body = client.get("/replication/health").json()
    assert len(body) == 1
    assert body[0]["status"] == "unavailable"
    assert body[0]["healthy_node_ids"] == []


def test_suspect_node_still_counts_as_a_healthy_replica(client):
    # Repair only kicks in once a node is confirmed DOWN, not merely SUSPECT.
    register(client)
    login(client)
    node_ids = [register_node(client, address=f"n{i}:9000").json()["id"] for i in range(3)]
    client.post("/files", json=make_file_body(chunk_sizes=(100,), node_ids=node_ids))

    mark_suspect("n0:9000")

    assert client.get("/replication/health").json() == []


def test_health_reports_multiple_at_risk_chunks_independently(client):
    register(client)
    login(client)
    node_ids = [register_node(client, address=f"n{i}:9000").json()["id"] for i in range(3)]
    client.post("/files", json=make_file_body(chunk_sizes=(100, 200), node_ids=node_ids))

    mark_down("n0:9000")

    body = client.get("/replication/health").json()
    assert len(body) == 2
    assert {c["sequence_index"] for c in body} == {0, 1}


def test_health_evaluates_the_same_instant_for_every_chunk(client, monkeypatch):
    # If "now" were re-fetched per node/chunk instead of snapshotted once,
    # a node crossing a threshold mid-request could classify inconsistently
    # across chunks in the same response. Pinning the call count to exactly
    # one is what actually guarantees that can't happen.
    import coordinator.replication as app_module

    register(client)
    login(client)
    node_ids = [register_node(client, address=f"n{i}:9000").json()["id"] for i in range(3)]
    client.post("/files", json=make_file_body(chunk_sizes=(100, 200, 300), node_ids=node_ids))
    mark_down("n0:9000")

    real_datetime = app_module.datetime
    calls = []

    class SpyDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            calls.append(1)
            return real_datetime.now(tz)

    monkeypatch.setattr(app_module, "datetime", SpyDatetime)

    response = client.get("/replication/health")

    assert response.status_code == 200
    assert len(response.json()) == 3
    assert len(calls) == 1
