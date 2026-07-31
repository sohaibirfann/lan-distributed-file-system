from tests.test_auth import register
from tests.test_files import make_file_body
from tests.test_nodes import login, register_node
from tests.test_replication_health import mark_down, mark_draining


def test_repair_plan_requires_session(client):
    assert client.get("/replication/repair-plan").status_code == 401


def test_fully_replicated_chunk_has_no_repair_plan(client):
    register(client)
    login(client)
    node_ids = [register_node(client, address=f"n{i}:9000").json()["id"] for i in range(3)]
    client.post("/files", json=make_file_body(chunk_sizes=(100,), node_ids=node_ids))

    assert client.get("/replication/repair-plan").json() == []


def test_under_replicated_chunk_gets_a_plan_to_a_spare_node(client):
    register(client)
    login(client)
    # 3 nodes hold the chunk at RF=3, plus a 4th spare node not holding it yet.
    node_ids = [register_node(client, address=f"n{i}:9000").json()["id"] for i in range(3)]
    spare_id = register_node(client, address="spare:9000").json()["id"]
    client.post("/files", json=make_file_body(chunk_sizes=(100,), node_ids=node_ids))

    mark_down("n0:9000")

    plans = client.get("/replication/repair-plan").json()

    assert len(plans) == 1
    plan = plans[0]
    assert plan["target_node_ids"] == [spare_id]
    assert plan["source_node_id"] in node_ids[1:]


def test_draining_node_still_gets_a_repair_plan(client):
    # Still up (can serve as source), but must not count toward RF.
    register(client)
    login(client)
    node_ids = [register_node(client, address=f"n{i}:9000").json()["id"] for i in range(3)]
    spare_id = register_node(client, address="spare:9000").json()["id"]
    client.post("/files", json=make_file_body(chunk_sizes=(100,), node_ids=node_ids))

    mark_draining("n0:9000")

    plans = client.get("/replication/repair-plan").json()

    assert len(plans) == 1
    plan = plans[0]
    assert plan["target_node_ids"] == [spare_id]
    assert plan["source_node_id"] in node_ids  # the draining node can still be the source


def test_unavailable_chunk_has_no_plan(client):
    # Nothing survives to copy from, so there's nothing to plan.
    register(client)
    login(client)
    node_ids = [register_node(client, address=f"n{i}:9000").json()["id"] for i in range(2)]
    client.post("/files", json=make_file_body(chunk_sizes=(100,), node_ids=node_ids))

    mark_down("n0:9000")
    mark_down("n1:9000")

    assert client.get("/replication/repair-plan").json() == []


def test_no_spare_node_available_yields_no_plan(client):
    # Under-replicated, but every other registered node already has a copy —
    # nowhere new to place it.
    register(client)
    login(client)
    node_ids = [register_node(client, address=f"n{i}:9000").json()["id"] for i in range(2)]
    client.post("/files", json=make_file_body(chunk_sizes=(100,), node_ids=node_ids))

    mark_down("n0:9000")

    assert client.get("/replication/repair-plan").json() == []


def test_plan_never_targets_a_node_that_already_holds_the_chunk(client):
    register(client)
    login(client)
    node_ids = [register_node(client, address=f"n{i}:9000").json()["id"] for i in range(3)]
    spare_ids = [register_node(client, address=f"spare{i}:9000").json()["id"] for i in range(2)]
    client.post("/files", json=make_file_body(chunk_sizes=(100,), node_ids=node_ids))

    mark_down("n0:9000")
    mark_down("n1:9000")

    plans = client.get("/replication/repair-plan").json()

    assert len(plans) == 1
    targets = plans[0]["target_node_ids"]
    assert len(targets) == 2  # needed = RF(3) - healthy(1)
    assert set(targets) == set(spare_ids)
    assert set(targets).isdisjoint(node_ids)


def test_plan_requests_only_as_many_targets_as_needed(client):
    register(client)
    login(client)
    node_ids = [register_node(client, address=f"n{i}:9000").json()["id"] for i in range(3)]
    # 3 spares available, but only 1 replica short.
    for i in range(3):
        register_node(client, address=f"spare{i}:9000")
    client.post("/files", json=make_file_body(chunk_sizes=(100,), node_ids=node_ids))

    mark_down("n0:9000")

    plans = client.get("/replication/repair-plan").json()

    assert len(plans[0]["target_node_ids"]) == 1


def test_plan_includes_chunk_hash_and_file_reference(client):
    register(client)
    login(client)
    node_ids = [register_node(client, address=f"n{i}:9000").json()["id"] for i in range(3)]
    register_node(client, address="spare:9000")
    body = make_file_body(chunk_sizes=(100,), node_ids=node_ids)
    file_id = client.post("/files", json=body).json()["id"]

    mark_down("n0:9000")

    plan = client.get("/replication/repair-plan").json()[0]
    assert plan["file_id"] == file_id
    assert plan["sequence_index"] == 0
    assert plan["hash"] == body["chunks"][0]["hash"]


def test_repair_plan_evaluates_the_same_instant_for_every_node(client, monkeypatch):
    # Node states must come from one shared "now", not a fresh timestamp per
    # node — otherwise a node crossing a threshold mid-request could get
    # different answers in the healthy-check vs. the ring's eligibility check.
    import coordinator.replication as app_module

    register(client)
    login(client)
    node_ids = [register_node(client, address=f"n{i}:9000").json()["id"] for i in range(3)]
    register_node(client, address="spare:9000")
    client.post("/files", json=make_file_body(chunk_sizes=(100, 200), node_ids=node_ids))
    mark_down("n0:9000")

    real_datetime = app_module.datetime
    calls = []

    class SpyDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            calls.append(1)
            return real_datetime.now(tz)

    monkeypatch.setattr(app_module, "datetime", SpyDatetime)

    response = client.get("/replication/repair-plan")

    assert response.status_code == 200
    assert len(calls) == 1


def test_placement_referencing_a_nonexistent_node_does_not_count_as_healthy(client):
    # There's no node-deregistration path today, so this simulates a stale
    # placement row the same way the DB would end up with one if there were.
    from sqlalchemy import text

    from coordinator.db import SessionLocal
    from coordinator.models import Chunk, ChunkPlacement

    register(client)
    login(client)
    node_ids = [register_node(client, address=f"n{i}:9000").json()["id"] for i in range(2)]
    register_node(client, address="spare:9000")
    client.post("/files", json=make_file_body(chunk_sizes=(100,), node_ids=node_ids))

    db = SessionLocal()
    try:
        chunk = db.query(Chunk).first()
        # FK enforcement would otherwise reject this -- there's no real
        # deregistration path, so this is the only way to simulate it.
        db.execute(text("PRAGMA foreign_keys=OFF"))
        db.add(ChunkPlacement(chunk_id=chunk.id, node_id=99999))  # no such node
        db.commit()
        db.execute(text("PRAGMA foreign_keys=ON"))
    finally:
        db.close()

    plans = client.get("/replication/repair-plan").json()

    # 3 placements total, but only 2 point at real nodes — still under RF=3
    # and the phantom one must not be treated as a healthy third replica.
    assert len(plans) == 1
    assert 99999 not in plans[0]["target_node_ids"]


def test_two_under_replicated_chunks_can_target_the_same_lone_spare(client):
    # Documents a known, accepted limitation: plans are computed per chunk,
    # independently. Whatever executes these plans must handle two plans
    # racing for the same node — this endpoint doesn't reserve capacity.
    register(client)
    login(client)
    node_ids = [register_node(client, address=f"n{i}:9000").json()["id"] for i in range(3)]
    spare_id = register_node(client, address="spare:9000").json()["id"]
    client.post("/files", json=make_file_body(chunk_sizes=(100, 200), node_ids=node_ids))

    mark_down("n0:9000")

    plans = client.get("/replication/repair-plan").json()

    assert len(plans) == 2
    assert all(plan["target_node_ids"] == [spare_id] for plan in plans)
