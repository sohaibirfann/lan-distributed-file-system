import hashlib
from contextlib import ExitStack

from tests.test_auth import register
from tests.test_nodes import login, register_node
from tests.test_repair_execute import spawn_fake_node


def chunk_id_for(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_deleting_a_file_reclaims_its_chunk_bytes_from_nodes(client, tmp_path, monkeypatch):
    register(client)
    login(client)
    node_a = register_node(client, address="a:9000").json()
    node_b = register_node(client, address="b:9000").json()

    data = b"bytes that should be reclaimed"
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
                    "node_ids": [node_a["id"], node_b["id"]],
                }
            ],
        },
    ).json()["id"]

    with ExitStack() as stack:
        fake_a = spawn_fake_node(stack, tmp_path, "a:9000", monkeypatch)
        fake_b = spawn_fake_node(stack, tmp_path, "b:9000", monkeypatch)
        fake_a.put(f"/chunks/{chunk_hash}", content=data)
        fake_b.put(f"/chunks/{chunk_hash}", content=data)

        import coordinator.files as coordinator_app_module

        monkeypatch.setattr(
            coordinator_app_module,
            "_default_node_client",
            lambda address: {"a:9000": fake_a, "b:9000": fake_b}[address],
        )

        response = client.delete(f"/files/{file_id}")

        assert response.status_code == 204
        assert fake_a.get(f"/chunks/{chunk_hash}").status_code == 404
        assert fake_b.get(f"/chunks/{chunk_hash}").status_code == 404


def test_deleting_a_file_does_not_fail_when_a_node_is_unreachable(client, tmp_path, monkeypatch):
    import httpx

    register(client)
    login(client)
    node_a = register_node(client, address="a:9000").json()
    node_b = register_node(client, address="b:9000").json()

    data = b"bytes on an unreachable node"
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
                    "node_ids": [node_a["id"], node_b["id"]],
                }
            ],
        },
    ).json()["id"]

    with ExitStack() as stack:
        fake_a = spawn_fake_node(stack, tmp_path, "a:9000", monkeypatch)
        fake_a.put(f"/chunks/{chunk_hash}", content=data)

        def get_node_client(address):
            if address == "b:9000":
                raise httpx.ConnectError("connection refused")
            return fake_a

        import coordinator.files as coordinator_app_module

        monkeypatch.setattr(coordinator_app_module, "_default_node_client", get_node_client)

        # The unreachable node must not stop the metadata delete from
        # succeeding, or block reclamation from the node that IS reachable.
        response = client.delete(f"/files/{file_id}")

        assert response.status_code == 204
        assert fake_a.get(f"/chunks/{chunk_hash}").status_code == 404

    assert client.get(f"/files/{file_id}").status_code == 404


def test_deleting_a_file_does_not_destroy_another_files_shared_chunk(client, tmp_path, monkeypatch):
    # Chunks aren't deduplicated across files, so two files can independently
    # place a chunk with the same hash on the same node. Deleting one must
    # not physically remove bytes the other still depends on.
    register(client)
    login(client)
    node = register_node(client, address="n:9000").json()

    data = b"shared chunk content"
    chunk_hash = chunk_id_for(data)

    def create_file(name):
        return client.post(
            "/files",
            json={
                "name": name,
                "size_bytes": len(data),
                "chunks": [
                    {
                        "sequence_index": 0,
                        "hash": chunk_hash,
                        "size_bytes": len(data),
                        "node_ids": [node["id"]],
                    }
                ],
            },
        ).json()["id"]

    file_a = create_file("a.txt")
    file_b = create_file("b.txt")

    with ExitStack() as stack:
        fake_n = spawn_fake_node(stack, tmp_path, "n:9000", monkeypatch)
        fake_n.put(f"/chunks/{chunk_hash}", content=data)

        import coordinator.files as coordinator_app_module

        monkeypatch.setattr(coordinator_app_module, "_default_node_client", lambda address: fake_n)

        response = client.delete(f"/files/{file_a}")

        assert response.status_code == 204
        # file_b's chunk must still be physically present on the node.
        assert fake_n.get(f"/chunks/{chunk_hash}").status_code == 200
        assert client.get(f"/files/{file_b}").status_code == 200

    # Deleting the last remaining reference must then actually reclaim it.
    with ExitStack() as stack:
        fake_n = spawn_fake_node(stack, tmp_path, "n:9000", monkeypatch)

        import coordinator.files as coordinator_app_module

        monkeypatch.setattr(coordinator_app_module, "_default_node_client", lambda address: fake_n)

        client.delete(f"/files/{file_b}")

        assert fake_n.get(f"/chunks/{chunk_hash}").status_code == 404
