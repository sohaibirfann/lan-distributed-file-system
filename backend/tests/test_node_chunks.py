import hashlib

import pytest

from node.chunks import (
    ChunkHashMismatch,
    InsufficientCapacity,
    InvalidChunkId,
    delete_chunk,
    list_chunk_inventory,
    retrieve_chunk,
    store_chunk,
)
from node.config import NodeConfig


def make_config(storage_dir, capacity_gb=10):
    return NodeConfig(
        storage_directory=storage_dir,
        capacity_budget_bytes=capacity_gb * 1024**3,
        coordinator_address="http://coordinator.invalid",
        node_address="192.168.1.5:9000",
        owner_username="alice",
        owner_password="hunter22",
    )


def chunk_id_for(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_store_and_retrieve_round_trip(tmp_path):
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = make_config(storage_dir)
    data = b"ciphertext bytes here"

    store_chunk(config, chunk_id_for(data), data)

    assert retrieve_chunk(config, chunk_id_for(data)) == data


def test_retrieve_missing_chunk_returns_none(tmp_path):
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = make_config(storage_dir)

    assert retrieve_chunk(config, "a" * 64) is None


def test_store_rejects_hash_mismatch(tmp_path):
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = make_config(storage_dir)

    with pytest.raises(ChunkHashMismatch):
        store_chunk(config, "a" * 64, b"this does not hash to a repeated 'a'")


@pytest.mark.parametrize(
    "bad_id",
    [
        "too-short",
        "g" * 64,  # not hex
        "A" * 64,  # uppercase
        "../../etc/passwd",
        "",
        "a" * 63,
        "a" * 65,
        "a" * 64 + "\n",  # `$` (not full-string anchoring) would accept this
    ],
)
def test_invalid_chunk_id_rejected_on_store_and_retrieve(tmp_path, bad_id):
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = make_config(storage_dir)

    with pytest.raises(InvalidChunkId):
        store_chunk(config, bad_id, b"data")
    with pytest.raises(InvalidChunkId):
        retrieve_chunk(config, bad_id)


def test_store_rejects_chunk_exceeding_capacity(tmp_path):
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = NodeConfig(
        storage_directory=storage_dir,
        capacity_budget_bytes=10,  # 10-byte budget
        coordinator_address="http://coordinator.invalid",
        node_address="192.168.1.5:9000",
        owner_username="alice",
        owner_password="hunter22",
    )
    data = b"x" * 100  # bigger than the budget

    with pytest.raises(InsufficientCapacity):
        store_chunk(config, chunk_id_for(data), data)


def test_capacity_check_accounts_for_already_stored_chunks(tmp_path):
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = NodeConfig(
        storage_directory=storage_dir,
        capacity_budget_bytes=150,
        coordinator_address="http://coordinator.invalid",
        node_address="192.168.1.5:9000",
        owner_username="alice",
        owner_password="hunter22",
    )

    first = b"x" * 100
    store_chunk(config, chunk_id_for(first), first)  # 100 of 150 bytes used

    second = b"y" * 100  # only 50 bytes remain
    with pytest.raises(InsufficientCapacity):
        store_chunk(config, chunk_id_for(second), second)


def test_re_storing_the_same_chunk_does_not_double_count_capacity(tmp_path):
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = NodeConfig(
        storage_directory=storage_dir,
        capacity_budget_bytes=100,
        coordinator_address="http://coordinator.invalid",
        node_address="192.168.1.5:9000",
        owner_username="alice",
        owner_password="hunter22",
    )
    data = b"x" * 100  # exactly the whole budget

    store_chunk(config, chunk_id_for(data), data)
    store_chunk(config, chunk_id_for(data), data)  # re-uploading the same chunk must still succeed

    assert retrieve_chunk(config, chunk_id_for(data)) == data


def test_a_corrupted_existing_file_is_repaired_not_trusted(tmp_path):
    # Simulates a crash mid-write leaving a truncated file at the chunk's
    # path. A later correct upload of the same chunk_id must not silently
    # trust that stale, wrong-hash file forever.
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = make_config(storage_dir)
    data = b"X" * 1000
    chunk_id = chunk_id_for(data)
    (storage_dir / chunk_id).write_bytes(data[:500])

    store_chunk(config, chunk_id, data)

    assert retrieve_chunk(config, chunk_id) == data


def test_delete_removes_a_stored_chunk(tmp_path):
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = make_config(storage_dir)
    data = b"chunk to be deleted"
    chunk_id = chunk_id_for(data)
    store_chunk(config, chunk_id, data)

    delete_chunk(config, chunk_id)

    assert retrieve_chunk(config, chunk_id) is None


def test_delete_is_idempotent_for_a_chunk_that_was_never_stored(tmp_path):
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = make_config(storage_dir)

    delete_chunk(config, "a" * 64)  # must not raise
    delete_chunk(config, "a" * 64)  # deleting twice must not raise either


def test_delete_rejects_invalid_chunk_id(tmp_path):
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = make_config(storage_dir)

    with pytest.raises(InvalidChunkId):
        delete_chunk(config, "../../etc/passwd")


def test_list_chunk_inventory_returns_stored_chunks(tmp_path):
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = make_config(storage_dir)
    a = b"first chunk"
    b = b"second chunk"
    store_chunk(config, chunk_id_for(a), a)
    store_chunk(config, chunk_id_for(b), b)

    ids = {chunk_id for chunk_id, _ in list_chunk_inventory(config)}
    assert ids == {chunk_id_for(a), chunk_id_for(b)}


def test_list_chunk_inventory_reports_a_recent_stored_at(tmp_path):
    import time

    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = make_config(storage_dir)
    data = b"a chunk"
    store_chunk(config, chunk_id_for(data), data)

    [(_, stored_at)] = list_chunk_inventory(config)
    assert abs(time.time() - stored_at) < 5


def test_list_chunk_inventory_ignores_non_chunk_files(tmp_path):
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = make_config(storage_dir)
    (storage_dir / "not-a-chunk.tmp").write_bytes(b"stray file")

    assert list_chunk_inventory(config) == []


def test_list_chunk_inventory_empty_when_nothing_stored(tmp_path):
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = make_config(storage_dir)

    assert list_chunk_inventory(config) == []


def test_delete_frees_up_capacity_for_reuse(tmp_path):
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = NodeConfig(
        storage_directory=storage_dir,
        capacity_budget_bytes=100,
        coordinator_address="http://coordinator.invalid",
        node_address="192.168.1.5:9000",
        owner_username="alice",
        owner_password="hunter22",
    )
    first = b"x" * 100  # fills the entire budget
    store_chunk(config, chunk_id_for(first), first)
    delete_chunk(config, chunk_id_for(first))

    second = b"y" * 100  # would have been rejected if the first chunk's
    store_chunk(config, chunk_id_for(second), second)  # space wasn't reclaimed

    assert retrieve_chunk(config, chunk_id_for(second)) == second
