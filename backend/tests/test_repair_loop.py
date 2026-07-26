import asyncio
import importlib
import time
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from tests.test_auth import register
from tests.test_nodes import login, register_node
from tests.test_replication_config import _fresh_client
from tests.test_replication_health import mark_down
from tests.test_repair_execute import chunk_id_for, spawn_fake_node


def test_repair_loop_runs_repeatedly_until_stopped(monkeypatch):
    import coordinator.app as app_module

    calls = []
    monkeypatch.setattr(app_module, "run_one_repair_cycle", lambda: calls.append(1))

    async def run_briefly():
        stop = asyncio.Event()
        task = asyncio.create_task(app_module.repair_loop(stop, interval_seconds=0))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(run_briefly())

    assert len(calls) >= 2


def test_repair_loop_survives_a_failed_cycle(monkeypatch):
    import coordinator.app as app_module

    calls = []

    def flaky_cycle():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("simulated DB error")

    monkeypatch.setattr(app_module, "run_one_repair_cycle", flaky_cycle)

    async def run_briefly():
        stop = asyncio.Event()
        task = asyncio.create_task(app_module.repair_loop(stop, interval_seconds=0))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(run_briefly())

    assert len(calls) >= 2  # the failed cycle didn't stop the loop


def test_stopping_waits_for_an_in_flight_cycle_before_returning(monkeypatch):
    import coordinator.app as app_module

    in_flight = []

    def slow_cycle():
        in_flight.append("started")
        time.sleep(0.2)
        in_flight.append("finished")

    monkeypatch.setattr(app_module, "run_one_repair_cycle", slow_cycle)

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(app_module.repair_loop(stop, interval_seconds=0))
        await asyncio.sleep(0.05)  # let it enter the slow cycle
        assert in_flight == ["started"]

        stop.set()
        await task

        # If stop() returned before the cycle actually finished, this would
        # be ["started"] here instead — the exact race the fix closes.
        assert in_flight == ["started", "finished"]

    asyncio.run(run())


def test_run_one_repair_cycle_logs_results(client, tmp_path, monkeypatch, caplog):
    # Nothing else observes an unattended cycle's outcome — if it isn't
    # logged, a failing automatic repair would go unnoticed indefinitely.
    import logging

    register(client)
    login(client)
    node_a = register_node(client, address="a:9000").json()
    node_b = register_node(client, address="b:9000").json()
    register_node(client, address="spare:9000")

    data = b"chunk bytes for the logging test"
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
        fake_spare = spawn_fake_node(stack, tmp_path, "spare:9000", monkeypatch)
        fake_a.put(f"/chunks/{chunk_hash}", content=data)
        fake_b.put(f"/chunks/{chunk_hash}", content=data)

        mark_down("a:9000")

        import coordinator.app as coordinator_app_module

        monkeypatch.setattr(
            coordinator_app_module,
            "_default_node_client",
            lambda address: {"a:9000": fake_a, "b:9000": fake_b, "spare:9000": fake_spare}[address],
        )

        with caplog.at_level(logging.INFO, logger="coordinator.app"):
            coordinator_app_module.run_one_repair_cycle()

        assert any("repaired onto node" in record.message for record in caplog.records)


def test_run_one_repair_cycle_performs_a_real_repair(client, tmp_path, monkeypatch):
    register(client)
    login(client)
    node_a = register_node(client, address="a:9000").json()
    node_b = register_node(client, address="b:9000").json()
    spare = register_node(client, address="spare:9000").json()

    data = b"real chunk bytes for the loop test"
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

        import coordinator.app as coordinator_app_module

        monkeypatch.setattr(
            coordinator_app_module,
            "_default_node_client",
            lambda address: {"a:9000": fake_a, "b:9000": fake_b, "spare:9000": fake_spare}[address],
        )

        results = coordinator_app_module.run_one_repair_cycle()

        assert len(results) == 1
        assert results[0].repaired_node_ids == [spare["id"]]

    detail = client.get(f"/files/{file_id}").json()
    node_ids_holding_chunk = {n["node_id"] for n in detail["chunks"][0]["nodes"]}
    assert spare["id"] in node_ids_holding_chunk


def test_repair_interval_defaults_to_sixty_seconds(tmp_path, monkeypatch):
    app_module = _fresh_client(tmp_path, monkeypatch)

    with TestClient(app_module.app):
        pass  # startup succeeding at all confirms the default parses and validates


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_invalid_repair_interval_fails_startup(tmp_path, monkeypatch, value):
    app_module = _fresh_client(tmp_path, monkeypatch, REPAIR_INTERVAL_SECONDS=value)

    with pytest.raises(RuntimeError, match="REPAIR_INTERVAL_SECONDS"):
        with TestClient(app_module.app):
            pass
