from contextlib import ExitStack

from tests.test_auth import register
from tests.test_nodes import chunk_auth_headers, login, register_node
from tests.test_repair_execute import chunk_id_for, spawn_fake_node
from tests.test_replication_health import mark_draining


def test_drain_requires_session(client):
    assert client.post("/nodes/drain", json={"address": "a:9000"}).status_code == 401


def test_drain_rejects_a_node_owned_by_someone_else(client):
    register(client)
    login(client)
    register_node(client, address="a:9000")

    client.post("/register", json={
        "username": "bob", "password": "hunter22",
        "namespace_passphrase": "correct horse battery staple",
    })
    client.post("/login", json={"username": "bob", "password": "hunter22"})

    response = client.post("/nodes/drain", json={"address": "a:9000"})
    assert response.status_code == 404


def test_drain_rejects_an_unregistered_address(client):
    register(client)
    login(client)
    assert client.post("/nodes/drain", json={"address": "nobody:9000"}).status_code == 404


def test_re_registering_a_drained_node_brings_it_back(client):
    register(client)
    login(client)
    register_node(client, address="a:9000")
    client.post("/nodes/drain", json={"address": "a:9000"})

    register_node(client, address="a:9000")

    node = next(n for n in client.get("/nodes").json() if n["address"] == "a:9000")
    assert node["draining"] is False
    assert node["state"] == "up"


def test_drain_does_not_release_a_node_whose_other_copies_include_another_draining_node(
    client, tmp_path, monkeypatch
):
    # RF 3, chunk on a (being drained now) + d (already draining) + e (healthy),
    # with only one spare. d doesn't count toward RF either, so the true
    # permanent replica count after repair is 2 (e + spare), not 3 -- a must
    # NOT be released even though a naive "not DOWN" count would say otherwise.
    register(client)
    login(client)
    node_a = register_node(client, address="a:9000").json()
    node_d = register_node(client, address="d:9000").json()
    node_e = register_node(client, address="e:9000").json()
    register_node(client, address="spare1:9000")
    mark_draining("d:9000")

    data = b"chunk shared by two draining nodes"
    chunk_hash = chunk_id_for(data)
    client.post(
        "/files",
        json={
            "name": "f.bin",
            "size_bytes": len(data),
            "chunks": [
                {
                    "sequence_index": 0,
                    "hash": chunk_hash,
                    "size_bytes": len(data),
                    "node_ids": [node_a["id"], node_d["id"], node_e["id"]],
                }
            ],
        },
    )

    with ExitStack() as stack:
        fake_a = spawn_fake_node(stack, tmp_path, "a:9000", monkeypatch)
        fake_d = spawn_fake_node(stack, tmp_path, "d:9000", monkeypatch)
        fake_e = spawn_fake_node(stack, tmp_path, "e:9000", monkeypatch)
        fake_spare1 = spawn_fake_node(stack, tmp_path, "spare1:9000", monkeypatch)
        fake_a.put(f"/chunks/{chunk_hash}", content=data, headers=chunk_auth_headers())
        fake_d.put(f"/chunks/{chunk_hash}", content=data, headers=chunk_auth_headers())
        fake_e.put(f"/chunks/{chunk_hash}", content=data, headers=chunk_auth_headers())

        clients = {
            "a:9000": fake_a, "d:9000": fake_d, "e:9000": fake_e, "spare1:9000": fake_spare1,
        }
        import coordinator.nodes as coordinator_nodes_module
        import coordinator.replication as coordinator_replication_module

        monkeypatch.setattr(
            coordinator_replication_module, "_default_node_client", lambda address: clients[address]
        )
        monkeypatch.setattr(
            coordinator_nodes_module, "_default_node_client", lambda address: clients[address]
        )

        response = client.post("/nodes/drain", json={"address": "a:9000"})

        assert response.status_code == 200
        assert response.json() == {"remaining_chunks": 1}
        assert fake_a.get(f"/chunks/{chunk_hash}", headers=chunk_auth_headers()).status_code == 200  # not released
        assert fake_spare1.get(f"/chunks/{chunk_hash}", headers=chunk_auth_headers()).status_code == 200  # repaired onto the spare


def test_draining_node_reports_zero_remaining_once_a_spare_takes_over(client, tmp_path, monkeypatch):
    register(client)
    login(client)
    node_a = register_node(client, address="a:9000").json()
    node_b = register_node(client, address="b:9000").json()
    spare1 = register_node(client, address="spare1:9000").json()
    spare2 = register_node(client, address="spare2:9000").json()

    data = b"chunk on a node being drained"
    chunk_hash = chunk_id_for(data)
    client.post(
        "/files",
        json={
            "name": "f.bin",
            "size_bytes": len(data),
            "chunks": [
                {
                    "sequence_index": 0,
                    "hash": chunk_hash,
                    "size_bytes": len(data),
                    "node_ids": [node_a["id"], node_b["id"]],
                }
            ],
        },
    )

    with ExitStack() as stack:
        fake_a = spawn_fake_node(stack, tmp_path, "a:9000", monkeypatch)
        fake_b = spawn_fake_node(stack, tmp_path, "b:9000", monkeypatch)
        fake_spare1 = spawn_fake_node(stack, tmp_path, "spare1:9000", monkeypatch)
        fake_spare2 = spawn_fake_node(stack, tmp_path, "spare2:9000", monkeypatch)
        fake_a.put(f"/chunks/{chunk_hash}", content=data, headers=chunk_auth_headers())
        fake_b.put(f"/chunks/{chunk_hash}", content=data, headers=chunk_auth_headers())

        clients = {
            "a:9000": fake_a, "b:9000": fake_b, "spare1:9000": fake_spare1, "spare2:9000": fake_spare2,
        }
        import coordinator.nodes as coordinator_nodes_module
        import coordinator.replication as coordinator_replication_module

        monkeypatch.setattr(
            coordinator_replication_module, "_default_node_client", lambda address: clients[address]
        )
        monkeypatch.setattr(
            coordinator_nodes_module, "_default_node_client", lambda address: clients[address]
        )

        response = client.post("/nodes/drain", json={"address": "a:9000"})

        assert response.status_code == 200
        assert response.json() == {"remaining_chunks": 0}
        assert fake_a.get(f"/chunks/{chunk_hash}", headers=chunk_auth_headers()).status_code == 404  # released from a
        # Repair needs 2 more permanent replicas (RF 3, minus b) -- both spares get it.
        assert fake_spare1.get(f"/chunks/{chunk_hash}", headers=chunk_auth_headers()).status_code == 200
        assert fake_spare2.get(f"/chunks/{chunk_hash}", headers=chunk_auth_headers()).status_code == 200

    nodes = {n["address"]: n for n in client.get("/nodes").json()}
    assert nodes["a:9000"]["draining"] is True


def test_draining_node_reports_remaining_when_no_spare_exists(client, tmp_path, monkeypatch):
    register(client)
    login(client)
    node_a = register_node(client, address="a:9000").json()
    node_b = register_node(client, address="b:9000").json()

    data = b"chunk with nowhere else to go"
    chunk_hash = chunk_id_for(data)
    client.post(
        "/files",
        json={
            "name": "f.bin",
            "size_bytes": len(data),
            "chunks": [
                {
                    "sequence_index": 0,
                    "hash": chunk_hash,
                    "size_bytes": len(data),
                    "node_ids": [node_a["id"], node_b["id"]],
                }
            ],
        },
    )

    with ExitStack() as stack:
        fake_a = spawn_fake_node(stack, tmp_path, "a:9000", monkeypatch)
        fake_b = spawn_fake_node(stack, tmp_path, "b:9000", monkeypatch)
        fake_a.put(f"/chunks/{chunk_hash}", content=data, headers=chunk_auth_headers())
        fake_b.put(f"/chunks/{chunk_hash}", content=data, headers=chunk_auth_headers())

        clients = {"a:9000": fake_a, "b:9000": fake_b}
        import coordinator.nodes as coordinator_nodes_module
        import coordinator.replication as coordinator_replication_module

        monkeypatch.setattr(
            coordinator_replication_module, "_default_node_client", lambda address: clients[address]
        )
        monkeypatch.setattr(
            coordinator_nodes_module, "_default_node_client", lambda address: clients[address]
        )

        response = client.post("/nodes/drain", json={"address": "a:9000"})

        assert response.status_code == 200
        assert response.json() == {"remaining_chunks": 1}
        assert fake_a.get(f"/chunks/{chunk_hash}", headers=chunk_auth_headers()).status_code == 200  # not released -- still needed
