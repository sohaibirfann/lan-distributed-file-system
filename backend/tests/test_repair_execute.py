import hashlib
import importlib
from contextlib import ExitStack

from fastapi.testclient import TestClient

from tests.test_auth import register
from tests.test_files import make_file_body
from tests.test_nodes import login, register_node
from tests.test_replication_health import mark_down


def spawn_fake_node(stack: ExitStack, tmp_path, address: str, monkeypatch):
    """A real node.app instance, isolated from any other fake node in the same
    test by reloading the module so each gets its own fresh app.state — two
    TestClients sharing one FastAPI object would clobber each other's config.
    """
    monkeypatch.setenv("STORAGE_DIRECTORY", str(tmp_path / address.replace(":", "_")))
    monkeypatch.setenv("CAPACITY_BUDGET_GB", "10")
    monkeypatch.setenv("COORDINATOR_ADDRESS", "http://coordinator.invalid")
    monkeypatch.setenv("NODE_ADDRESS", address)
    monkeypatch.setenv("OWNER_USERNAME", "alice")
    monkeypatch.setenv("OWNER_PASSWORD", "hunter22")

    import node.app as node_app_module

    importlib.reload(node_app_module)
    monkeypatch.setattr(node_app_module, "register_with_coordinator", lambda config, client: None)

    return stack.enter_context(TestClient(node_app_module.app))


def chunk_id_for(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_repair_requires_session(client):
    assert client.post("/replication/repair").status_code == 401


def test_repair_copies_chunk_to_target_and_records_placement(client, tmp_path, monkeypatch):
    register(client)
    login(client)
    node_a = register_node(client, address="a:9000").json()
    node_b = register_node(client, address="b:9000").json()
    spare = register_node(client, address="spare:9000").json()

    data = b"the actual chunk bytes"
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
        fake_spare = spawn_fake_node(stack, tmp_path, "spare:9000", monkeypatch)
        fake_a.put(f"/chunks/{chunk_hash}", content=data)
        fake_b.put(f"/chunks/{chunk_hash}", content=data)

        mark_down("a:9000")

        def get_node_client(address):
            return {"a:9000": fake_a, "b:9000": fake_b, "spare:9000": fake_spare}[address]

        import coordinator.app as coordinator_app_module

        monkeypatch.setattr(coordinator_app_module, "_default_node_client", get_node_client)

        response = client.post("/replication/repair")

        assert response.status_code == 200
        results = response.json()
        assert len(results) == 1
        assert results[0]["repaired_node_ids"] == [spare["id"]]
        assert results[0]["failed_node_ids"] == []
        assert results[0]["error"] is None

        # The spare node actually received the bytes, not just a DB row.
        assert fake_spare.get(f"/chunks/{chunk_hash}").content == data

    detail = client.get(f"/files/{file_id}").json()
    node_ids_holding_chunk = {n["node_id"] for n in detail["chunks"][0]["nodes"]}
    assert spare["id"] in node_ids_holding_chunk


def test_repair_reports_failure_when_source_node_is_unreachable(client, tmp_path, monkeypatch):
    register(client)
    login(client)
    node_a = register_node(client, address="a:9000").json()
    node_b = register_node(client, address="b:9000").json()
    register_node(client, address="spare:9000")

    data = b"chunk bytes"
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
        # b is the sole healthy replica but never actually receives the bytes
        # in this test, so fetching from it will 404.
        fake_b = spawn_fake_node(stack, tmp_path, "b:9000", monkeypatch)

        mark_down("a:9000")

        import coordinator.app as coordinator_app_module

        monkeypatch.setattr(coordinator_app_module, "_default_node_client", lambda address: fake_b)

        response = client.post("/replication/repair")

        results = response.json()
        assert len(results) == 1
        assert results[0]["repaired_node_ids"] == []
        assert results[0]["error"] is not None


def test_repair_returns_empty_list_when_nothing_needs_repair(client):
    register(client)
    login(client)
    node_ids = [register_node(client, address=f"n{i}:9000").json()["id"] for i in range(3)]
    client.post("/files", json=make_file_body(chunk_sizes=(100,), node_ids=node_ids))

    assert client.post("/replication/repair").json() == []


def test_repair_handles_a_genuinely_unreachable_source_without_crashing_the_batch(
    client, monkeypatch
):
    # A bad HTTP status and a real transport failure (connection refused,
    # timeout) are different code paths — this is specifically the latter.
    import httpx

    register(client)
    login(client)
    node_a = register_node(client, address="a:9000").json()
    node_b = register_node(client, address="b:9000").json()
    register_node(client, address="spare:9000")

    data = b"chunk bytes"
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

    mark_down("a:9000")

    class UnreachableClient:
        def get(self, url):
            raise httpx.ConnectError("connection refused")

    import coordinator.app as coordinator_app_module

    monkeypatch.setattr(
        coordinator_app_module, "_default_node_client", lambda address: UnreachableClient()
    )

    response = client.post("/replication/repair")

    assert response.status_code == 200  # the batch itself must not 500
    results = response.json()
    assert len(results) == 1
    assert results[0]["repaired_node_ids"] == []
    assert "connection refused" in results[0]["error"]


def test_repair_continues_to_other_targets_when_one_target_is_unreachable(
    client, tmp_path, monkeypatch
):
    import httpx

    register(client)
    login(client)
    node_a = register_node(client, address="a:9000").json()
    node_b = register_node(client, address="b:9000").json()
    spare_ok = register_node(client, address="spare-ok:9000").json()
    register_node(client, address="spare-down:9000")

    data = b"chunk bytes"
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
        fake_b = spawn_fake_node(stack, tmp_path, "b:9000", monkeypatch)
        fake_spare_ok = spawn_fake_node(stack, tmp_path, "spare-ok:9000", monkeypatch)
        fake_b.put(f"/chunks/{chunk_hash}", content=data)

        mark_down("a:9000")

        def get_node_client(address):
            if address == "spare-down:9000":
                raise httpx.ConnectError("connection refused")
            return {"b:9000": fake_b, "spare-ok:9000": fake_spare_ok}[address]

        import coordinator.app as coordinator_app_module

        monkeypatch.setattr(coordinator_app_module, "_default_node_client", get_node_client)

        response = client.post("/replication/repair")

        results = response.json()
        assert len(results) == 1
        assert set(results[0]["repaired_node_ids"]) == {spare_ok["id"]}
        assert results[0]["error"] is None  # overall repair succeeded even though one target failed


def test_execute_repair_handles_a_source_node_that_no_longer_exists(client):
    # No node-deregistration endpoint exists yet, so this can't happen via
    # HTTP today — exercised directly at the function level as a defensive
    # guard against a future feature reintroducing the "one bad chunk
    # crashes the whole batch" bug via AttributeError instead of httpx.HTTPError.
    from coordinator.app import _default_node_client, _execute_repair
    from coordinator.db import SessionLocal
    from coordinator.schemas import RepairPlanOut

    db = SessionLocal()
    try:
        plan = RepairPlanOut(
            chunk_id=1, file_id=1, sequence_index=0, hash="a" * 64,
            source_node_id=99999, target_node_ids=[1],
        )
        result = _execute_repair(db, plan, _default_node_client)
    finally:
        db.close()

    assert result.repaired_node_ids == []
    assert result.error is not None


def test_execute_repair_handles_a_target_node_that_no_longer_exists(client):
    from coordinator.app import _execute_repair
    from coordinator.db import SessionLocal
    from coordinator.schemas import RepairPlanOut

    register(client)
    login(client)
    register_node(client, address="source:9000")

    data = b"real chunk bytes"
    chunk_hash = chunk_id_for(data)

    class FakeResponse:
        status_code = 200
        content = data

    class FakeClient:
        def get(self, url):
            return FakeResponse()

    db = SessionLocal()
    try:
        from coordinator.models import Node

        source_id = db.query(Node).filter_by(address="source:9000").first().id
        plan = RepairPlanOut(
            chunk_id=1, file_id=1, sequence_index=0, hash=chunk_hash,
            source_node_id=source_id, target_node_ids=[99999],
        )
        result = _execute_repair(db, plan, lambda address: FakeClient())
    finally:
        db.close()

    # The hash matches, so this reaches the target loop — 99999 doesn't
    # exist, and the point is that it fails cleanly rather than crashing.
    assert result.repaired_node_ids == []
    assert result.failed_node_ids == [99999]
