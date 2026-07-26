from tests.test_auth import register
from tests.test_nodes import heartbeat, login, register_node


def test_placement_requires_session(client):
    assert client.get("/placement/chunk-1").status_code == 401


def test_placement_returns_up_to_replication_factor_nodes(client):
    register(client)
    login(client)
    for i in range(5):
        register_node(client, address=f"node-{i}:9000")

    body = client.get("/placement/chunk-1").json()

    assert len(body) == 3  # default replication factor
    assert len({n["address"] for n in body}) == 3


def test_placement_excludes_down_nodes(client):
    from datetime import datetime, timedelta, timezone

    from coordinator.db import SessionLocal
    from coordinator.models import Node
    from shared.placement import DOWN_GRACE_PERIOD, SUSPECT_THRESHOLD

    register(client)
    login(client)
    register_node(client, address="stale:9000")
    register_node(client, address="fresh-a:9000")
    register_node(client, address="fresh-b:9000")

    db = SessionLocal()
    try:
        stale = db.query(Node).filter_by(address="stale:9000").first()
        stale.last_heartbeat_at = (
            datetime.now(timezone.utc) - SUSPECT_THRESHOLD - DOWN_GRACE_PERIOD - timedelta(seconds=1)
        )
        db.commit()
    finally:
        db.close()

    body = client.get("/placement/chunk-1").json()

    assert "stale:9000" not in {n["address"] for n in body}


def test_placement_fails_when_below_write_quorum(client):
    register(client)
    login(client)

    response = client.get("/placement/chunk-1")

    assert response.status_code == 503  # 0 nodes < default write quorum of 2


def test_placement_degrades_below_replication_factor_but_above_quorum(client):
    register(client)
    login(client)
    register_node(client, address="a:9000")
    register_node(client, address="b:9000")

    response = client.get("/placement/chunk-1")

    assert response.status_code == 200
    assert len(response.json()) == 2  # below the default RF of 3, at the default W of 2


def test_placement_is_deterministic_for_the_same_chunk(client):
    register(client)
    login(client)
    for i in range(4):
        register_node(client, address=f"node-{i}:9000")

    first = client.get("/placement/same-chunk").json()
    second = client.get("/placement/same-chunk").json()

    assert first == second
