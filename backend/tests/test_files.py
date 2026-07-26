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


def test_get_file_requires_session(client):
    assert client.get("/files/1").status_code == 401


def test_get_file_404_for_unknown_id(client):
    register(client)
    login(client)

    assert client.get("/files/999").status_code == 404


def test_get_file_returns_chunks_in_sequence_order_regardless_of_submission_order(client):
    node_id = setup_account_and_node(client)
    body = make_file_body(chunk_sizes=(100, 200, 300), node_ids=(node_id,))
    body["chunks"].reverse()  # submitted as [2, 1, 0], must still come back [0, 1, 2]
    file_id = client.post("/files", json=body).json()["id"]

    detail = client.get(f"/files/{file_id}").json()

    assert [c["sequence_index"] for c in detail["chunks"]] == [0, 1, 2]
    assert [c["size_bytes"] for c in detail["chunks"]] == [100, 200, 300]


def test_get_file_returns_replica_addresses_per_chunk(client):
    register(client)
    login(client)
    node_a = register_node(client, address="a:9000").json()
    node_b = register_node(client, address="b:9000").json()
    body = make_file_body(chunk_sizes=(100,), node_ids=(node_a["id"], node_b["id"]))
    file_id = client.post("/files", json=body).json()["id"]

    detail = client.get(f"/files/{file_id}").json()

    addresses = {n["address"] for n in detail["chunks"][0]["nodes"]}
    assert addresses == {"a:9000", "b:9000"}


def test_get_file_includes_chunk_hash(client):
    node_id = setup_account_and_node(client)
    body = make_file_body(chunk_sizes=(100,), node_ids=(node_id,))
    expected_hash = body["chunks"][0]["hash"]
    file_id = client.post("/files", json=body).json()["id"]

    detail = client.get(f"/files/{file_id}").json()

    assert detail["chunks"][0]["hash"] == expected_hash


def test_delete_file_requires_session(client):
    assert client.delete("/files/1").status_code == 401


def test_delete_file_404_for_unknown_id(client):
    register(client)
    login(client)

    assert client.delete("/files/999").status_code == 404


def test_delete_file_removes_it_from_listing_and_detail(client):
    node_id = setup_account_and_node(client)
    file_id = client.post("/files", json=make_file_body(node_ids=(node_id,))).json()["id"]

    response = client.delete(f"/files/{file_id}")

    assert response.status_code == 204
    assert client.get(f"/files/{file_id}").status_code == 404
    assert file_id not in {f["id"] for f in client.get("/files").json()}


def test_rename_file_requires_session(client):
    assert client.patch("/files/1", json={"name": "new.txt"}).status_code == 401


def test_rename_file_404_for_unknown_id(client):
    register(client)
    login(client)

    assert client.patch("/files/999", json={"name": "new.txt"}).status_code == 404


def test_rename_file_updates_name_and_is_reflected_in_listing_and_detail(client):
    node_id = setup_account_and_node(client)
    file_id = client.post(
        "/files", json=make_file_body(name="old.txt", node_ids=(node_id,))
    ).json()["id"]

    response = client.patch(f"/files/{file_id}", json={"name": "new.txt"})

    assert response.status_code == 200
    assert response.json()["name"] == "new.txt"
    assert client.get(f"/files/{file_id}").json()["name"] == "new.txt"
    assert {f["name"] for f in client.get("/files").json()} == {"new.txt"}


def test_rename_file_does_not_touch_chunks(client):
    node_id = setup_account_and_node(client)
    file_id = client.post(
        "/files", json=make_file_body(chunk_sizes=(100, 200), node_ids=(node_id,))
    ).json()["id"]
    original_chunks = client.get(f"/files/{file_id}").json()["chunks"]

    client.patch(f"/files/{file_id}", json={"name": "renamed.txt"})

    assert client.get(f"/files/{file_id}").json()["chunks"] == original_chunks


def test_rename_file_rejects_empty_name(client):
    node_id = setup_account_and_node(client)
    file_id = client.post("/files", json=make_file_body(node_ids=(node_id,))).json()["id"]

    response = client.patch(f"/files/{file_id}", json={"name": ""})

    assert response.status_code == 422


def test_delete_file_removes_its_chunks_and_placements(client):
    from coordinator.db import SessionLocal
    from coordinator.models import Chunk, ChunkPlacement

    node_id = setup_account_and_node(client)
    file_id = client.post(
        "/files", json=make_file_body(chunk_sizes=(100, 200), node_ids=(node_id,))
    ).json()["id"]

    # Capture the chunk ids *before* deleting — a join through Chunk would
    # find nothing after the delete regardless of whether ChunkPlacement was
    # actually cleaned up, since the join itself has no rows left to match.
    db = SessionLocal()
    try:
        chunk_ids = [row.id for row in db.query(Chunk.id).filter(Chunk.file_id == file_id).all()]
        assert chunk_ids  # sanity: the file really has chunks before we delete it
    finally:
        db.close()

    client.delete(f"/files/{file_id}")

    db = SessionLocal()
    try:
        assert db.query(Chunk).filter(Chunk.file_id == file_id).count() == 0
        assert db.query(ChunkPlacement).filter(ChunkPlacement.chunk_id.in_(chunk_ids)).count() == 0
    finally:
        db.close()


def test_deleting_one_file_does_not_affect_another(client):
    node_id = setup_account_and_node(client)
    keep_id = client.post("/files", json=make_file_body(name="keep.txt", node_ids=(node_id,))).json()["id"]
    delete_id = client.post(
        "/files", json=make_file_body(name="delete.txt", node_ids=(node_id,))
    ).json()["id"]

    client.delete(f"/files/{delete_id}")

    assert client.get(f"/files/{keep_id}").status_code == 200


def test_deleting_the_same_file_twice_is_clean(client):
    node_id = setup_account_and_node(client)
    file_id = client.post("/files", json=make_file_body(node_ids=(node_id,))).json()["id"]

    first = client.delete(f"/files/{file_id}")
    second = client.delete(f"/files/{file_id}")

    assert first.status_code == 204
    assert second.status_code == 404
