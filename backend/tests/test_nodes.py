from datetime import datetime, timedelta, timezone

import pytest

from shared.placement import NodeState

from tests.test_auth import register


def login(client, username="alice", password="hunter22"):
    return client.post("/login", json={"username": username, "password": password})


def register_node(client, address="192.168.1.10:9000", capacity_gb=10, free_disk_gb=10, used_gb=0):
    gb = 1024**3
    return client.post(
        "/nodes/register",
        json={
            "address": address,
            "capacity_budget_bytes": capacity_gb * gb,
            "free_disk_bytes": free_disk_gb * gb,
            "used_bytes": used_gb * gb,
        },
    )


@pytest.mark.parametrize(
    "address",
    [
        "not a host",
        "http://example.com:80",
        "n1:9000/../admin",
        "127.0.0.1:80\r\nX-Injected: y",
        "host:99999",
        "host:0",
        "host",
        ":9000",
    ],
)
def test_registration_rejects_malformed_addresses(client, address):
    register(client)
    login(client)

    assert register_node(client, address=address).status_code == 422


def test_node_registration_requires_session(client):
    response = register_node(client)
    assert response.status_code == 401


def test_node_registration_succeeds_for_authenticated_owner(client):
    register(client)
    login(client)

    response = register_node(client)

    assert response.status_code == 201
    body = response.json()
    assert body["address"] == "192.168.1.10:9000"
    assert body["state"] == NodeState.UP.value


def test_re_registering_same_owner_and_address_updates_the_same_node(client):
    register(client)
    login(client)

    first = register_node(client, free_disk_gb=10)
    second = register_node(client, free_disk_gb=5)

    assert first.json()["id"] == second.json()["id"]
    assert second.json()["free_disk_bytes"] == 5 * 1024**3


def test_racing_registration_conflicts_instead_of_looping(client, monkeypatch):
    # Both requests' lookups miss, so both try to insert. The loser must get a
    # clean 409 rather than retrying forever or duplicating the row.
    import coordinator.app as app_module
    from coordinator.db import SessionLocal
    from coordinator.models import Node
    from coordinator.schemas import NodeRegisterRequest
    from fastapi import HTTPException

    register(client)
    login(client)

    db = SessionLocal()
    try:
        acct = db.query(app_module.Account).filter_by(username="alice").first()
        db.add(
            Node(
                owner_account_id=acct.id,
                address="race:9000",
                capacity_budget_bytes=1,
                free_disk_bytes=1,
                used_bytes=0,
            )
        )
        db.commit()

        monkeypatch.setattr(app_module, "_find_node", lambda *a, **k: None)

        body = NodeRegisterRequest(
            address="race:9000",
            capacity_budget_bytes=10 * 1024**3,
            free_disk_bytes=5 * 1024**3,
            used_bytes=0,
        )
        try:
            app_module.register_node(body, acct, db)
            raise AssertionError("expected a 409, not a successful registration")
        except HTTPException as err:
            assert err.status_code == 409

        db.rollback()
        assert db.query(Node).filter_by(address="race:9000").count() == 1
    finally:
        db.close()


def test_second_owner_cannot_claim_an_address_already_registered(client):
    # One row per machine, otherwise two replicas could land on one box and
    # the ring would count its capacity twice.
    register(client, username="alice")
    register(client, username="bob", password="hunter33")

    login(client, "alice", "hunter22")
    assert register_node(client).status_code == 201

    login(client, "bob", "hunter33")
    assert register_node(client).status_code == 409


def heartbeat(client, address="192.168.1.10:9000", free_disk_gb=8, used_gb=2):
    gb = 1024**3
    return client.post(
        "/nodes/heartbeat",
        json={"address": address, "free_disk_bytes": free_disk_gb * gb, "used_bytes": used_gb * gb},
    )


def test_heartbeat_requires_session(client):
    assert heartbeat(client).status_code == 401


def test_heartbeat_for_unregistered_node_is_404(client):
    register(client)
    login(client)

    assert heartbeat(client).status_code == 404


def test_heartbeat_updates_usage_and_free_disk(client):
    register(client)
    login(client)
    register_node(client, free_disk_gb=10, used_gb=0)

    response = heartbeat(client, free_disk_gb=7, used_gb=3)

    assert response.status_code == 200
    body = response.json()
    assert body["free_disk_bytes"] == 7 * 1024**3
    assert body["used_bytes"] == 3 * 1024**3


def test_heartbeat_does_not_affect_a_different_owners_node(client):
    register(client, username="alice")
    register(client, username="bob", password="hunter33")

    login(client, "alice", "hunter22")
    register_node(client)

    login(client, "bob", "hunter33")
    assert heartbeat(client).status_code == 404


def test_list_nodes_requires_session(client):
    assert client.get("/nodes").status_code == 401


def test_list_nodes_shows_every_owners_nodes(client):
    register(client, username="alice")
    register(client, username="bob", password="hunter33")

    login(client, "alice", "hunter22")
    register_node(client, address="alice-node:9000")

    login(client, "bob", "hunter33")
    register_node(client, address="bob-node:9000")

    response = client.get("/nodes")

    assert response.status_code == 200
    addresses = {n["address"] for n in response.json()}
    assert addresses == {"alice-node:9000", "bob-node:9000"}


def test_list_nodes_effective_capacity_is_capped_by_free_disk(client):
    register(client)
    login(client)
    register_node(client, capacity_gb=100, free_disk_gb=10)

    body = client.get("/nodes").json()

    assert body[0]["effective_capacity_bytes"] == 10 * 1024**3


def test_heartbeat_advances_last_heartbeat_but_not_registered_at(client):
    register(client)
    login(client)
    before = register_node(client).json()

    after = heartbeat(client).json()

    assert after["last_heartbeat_at"] > before["last_heartbeat_at"]
    assert after["registered_at"] == before["registered_at"]


def test_heartbeat_leaves_capacity_budget_alone(client):
    register(client)
    login(client)
    register_node(client, capacity_gb=10)

    assert heartbeat(client).json()["capacity_budget_bytes"] == 10 * 1024**3


def test_effective_capacity_tracks_free_disk_reported_by_heartbeat(client):
    register(client)
    login(client)
    register_node(client, capacity_gb=100, free_disk_gb=100)

    body = heartbeat(client, free_disk_gb=4, used_gb=1).json()

    assert body["effective_capacity_bytes"] == 4 * 1024**3


def test_timestamps_are_returned_as_utc(client):
    from datetime import datetime, timezone

    register(client)
    login(client)
    register_node(client)

    parsed = datetime.fromisoformat(client.get("/nodes").json()[0]["last_heartbeat_at"])

    assert parsed.tzinfo is not None
    assert datetime.now(timezone.utc) - parsed >= timedelta(0)


def test_stale_node_is_reported_suspect_then_down(client):
    from coordinator.db import SessionLocal
    from coordinator.models import Node
    from shared.placement import DOWN_GRACE_PERIOD, SUSPECT_THRESHOLD

    register(client)
    login(client)
    register_node(client)

    def state_after(age):
        db = SessionLocal()
        try:
            node = db.query(Node).first()
            node.last_heartbeat_at = datetime.now(timezone.utc) - age
            db.commit()
        finally:
            db.close()
        return client.get("/nodes").json()[0]["state"]

    assert state_after(timedelta(seconds=1)) == "up"
    assert state_after(SUSPECT_THRESHOLD + timedelta(seconds=5)) == "suspect"
    assert state_after(SUSPECT_THRESHOLD + DOWN_GRACE_PERIOD + timedelta(seconds=5)) == "down"


def test_heartbeat_brings_a_down_node_back_up(client):
    from coordinator.db import SessionLocal
    from coordinator.models import Node
    from shared.placement import DOWN_GRACE_PERIOD, SUSPECT_THRESHOLD

    register(client)
    login(client)
    register_node(client)

    db = SessionLocal()
    try:
        node = db.query(Node).first()
        node.last_heartbeat_at = (
            datetime.now(timezone.utc) - SUSPECT_THRESHOLD - DOWN_GRACE_PERIOD - timedelta(minutes=1)
        )
        db.commit()
    finally:
        db.close()
    assert client.get("/nodes").json()[0]["state"] == "down"

    assert heartbeat(client).json()["state"] == "up"


def test_draining_outranks_a_fresh_heartbeat(client):
    from coordinator.db import SessionLocal
    from coordinator.models import Node

    register(client)
    login(client)
    register_node(client)

    db = SessionLocal()
    try:
        db.query(Node).first().draining = True
        db.commit()
    finally:
        db.close()

    assert heartbeat(client).json()["state"] == "draining"
