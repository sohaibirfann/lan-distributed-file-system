import hashlib

from tests.test_auth import register
from tests.test_nodes import login, register_node


def chunk_hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def make_file_body(name="photo.jpg", chunk_sizes=(100, 200), node_ids=(1,)):
    chunks = [
        {
            "sequence_index": i,
            "hash": chunk_hash(f"chunk-{i}"),
            "size_bytes": size,
            "node_ids": list(node_ids),
        }
        for i, size in enumerate(chunk_sizes)
    ]
    return {"name": name, "size_bytes": sum(chunk_sizes), "chunks": chunks}


def setup_account_and_node(client):
    register(client)
    login(client)
    node = register_node(client).json()
    return node["id"]


def test_create_file_requires_session(client):
    body = make_file_body()
    assert client.post("/files", json=body).status_code == 401


def test_create_file_succeeds(client):
    node_id = setup_account_and_node(client)

    response = client.post("/files", json=make_file_body(node_ids=(node_id,)))

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "photo.jpg"
    assert body["size_bytes"] == 300
    assert body["chunk_count"] == 2
    assert body["uploader_account_id"] == 1


def test_list_files_requires_session(client):
    assert client.get("/files").status_code == 401


def test_list_files_shows_created_files(client):
    node_id = setup_account_and_node(client)
    client.post("/files", json=make_file_body(name="a.txt", node_ids=(node_id,)))
    client.post("/files", json=make_file_body(name="b.txt", node_ids=(node_id,)))

    names = {f["name"] for f in client.get("/files").json()}

    assert names == {"a.txt", "b.txt"}


def test_create_file_rejects_size_mismatch(client):
    node_id = setup_account_and_node(client)
    body = make_file_body(node_ids=(node_id,))
    body["size_bytes"] = 999  # doesn't match the sum of chunk sizes

    response = client.post("/files", json=body)

    assert response.status_code == 422


def test_create_file_rejects_non_contiguous_sequence_indices(client):
    node_id = setup_account_and_node(client)
    body = make_file_body(node_ids=(node_id,))
    body["chunks"][1]["sequence_index"] = 5  # skips 1

    response = client.post("/files", json=body)

    assert response.status_code == 422


def test_create_file_rejects_duplicate_sequence_indices(client):
    node_id = setup_account_and_node(client)
    body = make_file_body(node_ids=(node_id,))
    body["chunks"][1]["sequence_index"] = 0  # duplicate of chunk 0

    response = client.post("/files", json=body)

    assert response.status_code == 422


def test_create_file_rejects_unknown_node_id(client):
    register(client)
    login(client)

    response = client.post("/files", json=make_file_body(node_ids=(9999,)))

    assert response.status_code == 422


def test_create_file_rejects_malformed_chunk_hash(client):
    node_id = setup_account_and_node(client)
    body = make_file_body(node_ids=(node_id,))
    body["chunks"][0]["hash"] = "not-a-real-hash"

    response = client.post("/files", json=body)

    assert response.status_code == 422


def test_create_file_rejects_empty_chunk_list(client):
    node_id = setup_account_and_node(client)
    body = make_file_body(node_ids=(node_id,))
    body["chunks"] = []

    response = client.post("/files", json=body)

    assert response.status_code == 422


def test_create_file_rejects_duplicate_node_ids_in_one_chunk(client):
    node_id = setup_account_and_node(client)
    body = make_file_body(node_ids=(node_id, node_id))

    response = client.post("/files", json=body)

    assert response.status_code == 422


def test_create_file_records_one_placement_row_per_chunk_per_node(client):
    from coordinator.db import SessionLocal
    from coordinator.models import ChunkPlacement

    register(client)
    login(client)
    node_ids = [register_node(client, address=f"n{i}:9000").json()["id"] for i in range(3)]

    client.post("/files", json=make_file_body(chunk_sizes=(100, 200), node_ids=node_ids))

    db = SessionLocal()
    try:
        placements = db.query(ChunkPlacement).all()
        # 2 chunks x 3 nodes each = 6 rows, all distinct (chunk_id, node_id) pairs
        assert len(placements) == 6
        assert len({(p.chunk_id, p.node_id) for p in placements}) == 6
    finally:
        db.close()
