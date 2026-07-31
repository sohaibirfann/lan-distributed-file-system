from contextlib import ExitStack

from tests.test_auth import register
from tests.test_nodes import chunk_auth_headers, login, register_node
from tests.test_replication_health import mark_down
from tests.test_repair_execute import chunk_id_for, spawn_fake_node


def test_rejoining_node_releases_a_placement_repair_already_replaced(client, tmp_path, monkeypatch):
    register(client)
    login(client)
    node_a = register_node(client, address="a:9000").json()
    node_b = register_node(client, address="b:9000").json()
    node_c = register_node(client, address="c:9000").json()
    spare = register_node(client, address="spare:9000").json()

    data = b"chunk bytes for rejoin reconciliation"
    chunk_hash = chunk_id_for(data)
    file_id = client.post(
        "/files",
        json={
            "name": "f.bin",
            "size_bytes": len(data),
            "chunks": [
                {
                    "sequence_index": 0,
                    "hash": chunk_hash,
                    "size_bytes": len(data),
                    "node_ids": [node_a["id"], node_b["id"], node_c["id"]],
                }
            ],
        },
    ).json()["id"]

    with ExitStack() as stack:
        fake_a = spawn_fake_node(stack, tmp_path, "a:9000", monkeypatch)
        fake_b = spawn_fake_node(stack, tmp_path, "b:9000", monkeypatch)
        fake_c = spawn_fake_node(stack, tmp_path, "c:9000", monkeypatch)
        fake_spare = spawn_fake_node(stack, tmp_path, "spare:9000", monkeypatch)
        fake_a.put(f"/chunks/{chunk_hash}", content=data, headers=chunk_auth_headers())
        fake_b.put(f"/chunks/{chunk_hash}", content=data, headers=chunk_auth_headers())
        fake_c.put(f"/chunks/{chunk_hash}", content=data, headers=chunk_auth_headers())

        mark_down("a:9000")

        clients = {
            "a:9000": fake_a, "b:9000": fake_b, "c:9000": fake_c, "spare:9000": fake_spare,
        }

        import coordinator.nodes as coordinator_nodes_module
        import coordinator.replication as coordinator_app_module

        monkeypatch.setattr(
            coordinator_app_module, "_default_node_client", lambda address: clients[address]
        )
        monkeypatch.setattr(
            coordinator_nodes_module, "_default_node_client", lambda address: clients[address]
        )

        # Repair replaces a's replica with the spare while a is down.
        response = client.post("/replication/repair")
        assert response.json()[0]["repaired_node_ids"] == [spare["id"]]

        register_node(client, address="a:9000")  # a rejoins, b/c/spare already satisfy RF

        assert fake_a.get(f"/chunks/{chunk_hash}", headers=chunk_auth_headers()).status_code == 404

    detail = client.get(f"/files/{file_id}").json()
    node_ids_holding_chunk = {n["node_id"] for n in detail["chunks"][0]["nodes"]}
    assert node_ids_holding_chunk == {node_b["id"], node_c["id"], spare["id"]}


def test_rejoining_node_keeps_a_placement_still_needed_to_meet_replication_factor(
    client, tmp_path, monkeypatch
):
    register(client)
    login(client)
    node_a = register_node(client, address="a:9000").json()
    node_b = register_node(client, address="b:9000").json()
    node_c = register_node(client, address="c:9000").json()

    data = b"chunk bytes still needed"
    chunk_hash = chunk_id_for(data)
    file_id = client.post(
        "/files",
        json={
            "name": "f.bin",
            "size_bytes": len(data),
            "chunks": [
                {
                    "sequence_index": 0,
                    "hash": chunk_hash,
                    "size_bytes": len(data),
                    "node_ids": [node_a["id"], node_b["id"], node_c["id"]],
                }
            ],
        },
    ).json()["id"]

    with ExitStack() as stack:
        fake_a = spawn_fake_node(stack, tmp_path, "a:9000", monkeypatch)
        fake_b = spawn_fake_node(stack, tmp_path, "b:9000", monkeypatch)
        fake_a.put(f"/chunks/{chunk_hash}", content=data, headers=chunk_auth_headers())
        fake_b.put(f"/chunks/{chunk_hash}", content=data, headers=chunk_auth_headers())

        mark_down("a:9000")

        clients = {"a:9000": fake_a, "b:9000": fake_b}
        import coordinator.nodes as coordinator_nodes_module
        import coordinator.replication as coordinator_app_module

        monkeypatch.setattr(
            coordinator_app_module, "_default_node_client", lambda address: clients[address]
        )
        monkeypatch.setattr(
            coordinator_nodes_module, "_default_node_client", lambda address: clients[address]
        )

        client.post("/replication/repair")  # no spare exists, so RF=3 can't be restored

        register_node(client, address="a:9000")  # a rejoins, only b is healthy elsewhere

        assert fake_a.get(f"/chunks/{chunk_hash}", headers=chunk_auth_headers()).status_code == 200

    detail = client.get(f"/files/{file_id}").json()
    node_ids_holding_chunk = {n["node_id"] for n in detail["chunks"][0]["nodes"]}
    assert node_a["id"] in node_ids_holding_chunk


def test_releasing_a_surplus_placement_does_not_destroy_a_different_files_shared_chunk(
    client, tmp_path, monkeypatch
):
    register(client)
    login(client)
    node_a = register_node(client, address="a:9000").json()
    node_b = register_node(client, address="b:9000").json()
    node_c = register_node(client, address="c:9000").json()
    spare = register_node(client, address="spare:9000").json()

    data = b"shared content across two different files"
    chunk_hash = chunk_id_for(data)

    # file_A: replicated on a, b, c -- a's placement becomes surplus once repair backfills a spare.
    client.post(
        "/files",
        json={
            "name": "a.bin",
            "size_bytes": len(data),
            "chunks": [
                {
                    "sequence_index": 0,
                    "hash": chunk_hash,
                    "size_bytes": len(data),
                    "node_ids": [node_a["id"], node_b["id"], node_c["id"]],
                }
            ],
        },
    )
    # file_B: a different file sharing the same hash, placed ONLY on a -- must survive.
    client.post(
        "/files",
        json={
            "name": "b.bin",
            "size_bytes": len(data),
            "chunks": [
                {
                    "sequence_index": 0,
                    "hash": chunk_hash,
                    "size_bytes": len(data),
                    "node_ids": [node_a["id"]],
                }
            ],
        },
    )

    with ExitStack() as stack:
        fake_a = spawn_fake_node(stack, tmp_path, "a:9000", monkeypatch)
        fake_b = spawn_fake_node(stack, tmp_path, "b:9000", monkeypatch)
        fake_c = spawn_fake_node(stack, tmp_path, "c:9000", monkeypatch)
        fake_spare = spawn_fake_node(stack, tmp_path, "spare:9000", monkeypatch)
        fake_a.put(f"/chunks/{chunk_hash}", content=data, headers=chunk_auth_headers())
        fake_b.put(f"/chunks/{chunk_hash}", content=data, headers=chunk_auth_headers())
        fake_c.put(f"/chunks/{chunk_hash}", content=data, headers=chunk_auth_headers())

        mark_down("a:9000")

        clients = {
            "a:9000": fake_a, "b:9000": fake_b, "c:9000": fake_c, "spare:9000": fake_spare,
        }
        import coordinator.nodes as coordinator_nodes_module
        import coordinator.replication as coordinator_app_module

        monkeypatch.setattr(
            coordinator_app_module, "_default_node_client", lambda address: clients[address]
        )
        monkeypatch.setattr(
            coordinator_nodes_module, "_default_node_client", lambda address: clients[address]
        )

        client.post("/replication/repair")  # backfills file_A's chunk onto spare
        register_node(client, address="a:9000")  # a rejoins

        assert fake_a.get(f"/chunks/{chunk_hash}", headers=chunk_auth_headers()).status_code == 200  # file_B's copy survives


def test_first_time_registration_does_not_crash_reconciliation(client):
    register(client)
    login(client)

    response = register_node(client, address="brand-new:9000")

    assert response.status_code == 201
