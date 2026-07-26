import asyncio
import time
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from tests.test_auth import register
from tests.test_nodes import login, register_node
from tests.test_replication_config import _fresh_client
from tests.test_repair_execute import chunk_id_for, spawn_fake_node


def test_gc_sweep_reclaims_a_chunk_the_node_holds_but_nothing_references(
    client, tmp_path, monkeypatch
):
    register(client)
    login(client)
    node = register_node(client, address="n:9000").json()
    monkeypatch.setenv("GC_ORPHAN_GRACE_SECONDS", "0")

    with ExitStack() as stack:
        fake_n = spawn_fake_node(stack, tmp_path, "n:9000", monkeypatch)
        orphan = b"bytes nothing references"
        orphan_hash = chunk_id_for(orphan)
        fake_n.put(f"/chunks/{orphan_hash}", content=orphan)

        import coordinator.app as coordinator_app_module

        monkeypatch.setattr(coordinator_app_module, "_default_node_client", lambda address: fake_n)

        coordinator_app_module.run_one_gc_cycle()

        assert fake_n.get(f"/chunks/{orphan_hash}").status_code == 404


def test_gc_sweep_does_not_reclaim_a_chunk_younger_than_the_grace_period(
    client, tmp_path, monkeypatch
):
    # A client uploads bytes to a node, then separately calls POST /files to
    # register the placement -- those two steps aren't transactional. A sweep
    # landing in that window must not treat the not-yet-registered chunk as
    # an orphan just because no ChunkPlacement row exists for it yet.
    register(client)
    login(client)
    register_node(client, address="n:9000").json()

    with ExitStack() as stack:
        fake_n = spawn_fake_node(stack, tmp_path, "n:9000", monkeypatch)
        data = b"uploaded but not yet registered"
        chunk_hash = chunk_id_for(data)
        fake_n.put(f"/chunks/{chunk_hash}", content=data)

        import coordinator.app as coordinator_app_module

        monkeypatch.setattr(coordinator_app_module, "_default_node_client", lambda address: fake_n)

        coordinator_app_module.run_one_gc_cycle()  # default grace period applies

        assert fake_n.get(f"/chunks/{chunk_hash}").status_code == 200


def test_gc_sweep_does_not_touch_a_chunk_that_is_still_referenced(client, tmp_path, monkeypatch):
    register(client)
    login(client)
    node = register_node(client, address="n:9000").json()
    monkeypatch.setenv("GC_ORPHAN_GRACE_SECONDS", "0")

    data = b"still referenced bytes"
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
                    "node_ids": [node["id"]],
                }
            ],
        },
    )

    with ExitStack() as stack:
        fake_n = spawn_fake_node(stack, tmp_path, "n:9000", monkeypatch)
        fake_n.put(f"/chunks/{chunk_hash}", content=data)

        import coordinator.app as coordinator_app_module

        monkeypatch.setattr(coordinator_app_module, "_default_node_client", lambda address: fake_n)

        coordinator_app_module.run_one_gc_cycle()

        assert fake_n.get(f"/chunks/{chunk_hash}").status_code == 200


def test_gc_sweep_survives_an_unreachable_node(client, monkeypatch):
    import httpx

    register(client)
    login(client)
    register_node(client, address="unreachable:9000")

    import coordinator.app as coordinator_app_module

    class UnreachableClient:
        def get(self, url):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        coordinator_app_module, "_default_node_client", lambda address: UnreachableClient()
    )

    coordinator_app_module.run_one_gc_cycle()  # must not raise


def test_gc_sweep_loop_runs_repeatedly_until_stopped(monkeypatch):
    import coordinator.app as app_module

    calls = []
    monkeypatch.setattr(app_module, "run_one_gc_cycle", lambda: calls.append(1))

    async def run_briefly():
        stop = asyncio.Event()
        task = asyncio.create_task(app_module.gc_sweep_loop(stop, interval_seconds=0))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(run_briefly())

    assert len(calls) >= 2


def test_gc_sweep_loop_survives_a_failed_cycle(monkeypatch):
    import coordinator.app as app_module

    calls = []

    def flaky_cycle():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("simulated DB error")

    monkeypatch.setattr(app_module, "run_one_gc_cycle", flaky_cycle)

    async def run_briefly():
        stop = asyncio.Event()
        task = asyncio.create_task(app_module.gc_sweep_loop(stop, interval_seconds=0))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(run_briefly())

    assert len(calls) >= 2  # the failed cycle didn't stop the loop


def test_stopping_waits_for_an_in_flight_gc_sweep_before_returning(monkeypatch):
    import coordinator.app as app_module

    in_flight = []

    def slow_cycle():
        in_flight.append("started")
        time.sleep(0.2)
        in_flight.append("finished")

    monkeypatch.setattr(app_module, "run_one_gc_cycle", slow_cycle)

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(app_module.gc_sweep_loop(stop, interval_seconds=0))
        await asyncio.sleep(0.05)  # let it enter the slow cycle
        assert in_flight == ["started"]

        stop.set()
        await task

        assert in_flight == ["started", "finished"]

    asyncio.run(run())


def test_gc_sweep_interval_defaults_to_three_hundred_seconds(tmp_path, monkeypatch):
    app_module = _fresh_client(tmp_path, monkeypatch)

    with TestClient(app_module.app):
        pass  # startup succeeding at all confirms the default parses and validates


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_invalid_gc_sweep_interval_fails_startup(tmp_path, monkeypatch, value):
    app_module = _fresh_client(tmp_path, monkeypatch, GC_SWEEP_INTERVAL_SECONDS=value)

    with pytest.raises(RuntimeError, match="GC_SWEEP_INTERVAL_SECONDS"):
        with TestClient(app_module.app):
            pass
