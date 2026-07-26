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


def test_concurrent_first_registration_does_not_500(client, monkeypatch):
    # Force _find_node to say "no existing row" once, with a conflicting row
    # already underneath it — as if two requests' lookups both missed it.
    import coordinator.app as app_module
    from coordinator.db import SessionLocal
    from coordinator.schemas import NodeRegisterRequest

    register(client)
    login(client)

    db = SessionLocal()
    try:
        acct = db.query(app_module.Account).filter_by(username="alice").first()
        body = NodeRegisterRequest(
            address="race:9000", capacity_budget_bytes=10 * 1024**3, free_disk_bytes=5 * 1024**3
        )
        db.add(app_module.Node(owner_account_id=acct.id, address=body.address, **{
            "capacity_budget_bytes": 1, "free_disk_bytes": 1, "used_bytes": 0,
        }))
        db.commit()

        real_find_node = app_module._find_node
        calls = []

        def fake_find_node(db, owner_account_id, address):
            calls.append(1)
            return None if len(calls) == 1 else real_find_node(db, owner_account_id, address)

        monkeypatch.setattr(app_module, "_find_node", fake_find_node)

        result = app_module.register_node(body, acct, db)
        assert result.free_disk_bytes == 5 * 1024**3
    finally:
        db.close()


def test_two_owners_can_register_nodes_at_the_same_address(client):
    register(client, username="alice")
    register(client, username="bob", password="hunter33")

    login(client, "alice", "hunter22")
    first = register_node(client)
    login(client, "bob", "hunter33")
    second = register_node(client)

    assert first.json()["id"] != second.json()["id"]
