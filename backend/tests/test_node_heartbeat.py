import asyncio
import time

import httpx
import pytest

from node.config import NodeConfig
from node.heartbeat import heartbeat_loop, send_heartbeat
from node.registration import register_with_coordinator
from tests.test_auth import register as register_account


def make_config(storage_dir, **overrides):
    defaults = dict(
        storage_directory=storage_dir,
        capacity_budget_bytes=10 * 1024**3,
        coordinator_address="unused-client-is-already-bound",
        node_address="192.168.1.5:9000",
        owner_username="alice",
        owner_password="hunter22",
        heartbeat_interval_seconds=10,
    )
    defaults.update(overrides)
    return NodeConfig(**defaults)


def fake_status_error() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://coordinator.invalid/nodes/heartbeat")
    response = httpx.Response(503, request=request)
    return httpx.HTTPStatusError("simulated", request=request, response=response)


def test_send_heartbeat_updates_node_row(client, tmp_path):
    register_account(client)
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = make_config(storage_dir)
    register_with_coordinator(config, client)

    (storage_dir / "chunk-1").write_bytes(b"x" * 2000)
    send_heartbeat(config, client)

    node = next(n for n in client.get("/nodes").json() if n["address"] == "192.168.1.5:9000")
    assert node["used_bytes"] == 2000


def test_send_heartbeat_before_registration_fails(client, tmp_path):
    register_account(client)
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = make_config(storage_dir)

    with pytest.raises(httpx.HTTPStatusError):
        send_heartbeat(config, client)


def test_heartbeat_loop_sends_repeatedly_until_stopped(client, tmp_path, monkeypatch):
    register_account(client)
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = make_config(storage_dir, heartbeat_interval_seconds=0)  # fast, this is a test
    register_with_coordinator(config, client)

    calls = []
    real_send = send_heartbeat
    monkeypatch.setattr(
        "node.heartbeat.send_heartbeat",
        lambda cfg, c: (calls.append(1), real_send(cfg, c))[1],
    )

    async def run_briefly():
        stop = asyncio.Event()
        task = asyncio.create_task(heartbeat_loop(config, client, stop))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(run_briefly())

    assert len(calls) >= 2


def test_heartbeat_loop_survives_a_failed_beat(client, tmp_path, monkeypatch):
    # A coordinator hiccup on one beat must not kill the loop or the process —
    # that's the whole point of timestamp-based failure detection.
    register_account(client)
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = make_config(storage_dir, heartbeat_interval_seconds=0)
    register_with_coordinator(config, client)

    calls = []
    real_send = send_heartbeat

    def flaky_send(cfg, c):
        calls.append(1)
        if len(calls) == 1:
            raise fake_status_error()
        return real_send(cfg, c)

    monkeypatch.setattr("node.heartbeat.send_heartbeat", flaky_send)

    async def run_briefly():
        stop = asyncio.Event()
        task = asyncio.create_task(heartbeat_loop(config, client, stop))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(run_briefly())

    assert len(calls) >= 2  # the failed beat didn't stop the loop


def test_stopping_waits_for_an_in_flight_beat_before_returning(tmp_path, monkeypatch):
    # Regression test: stopping the loop must not return while a beat is still
    # running, or a caller that closes the client right after would race it.
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = make_config(storage_dir, heartbeat_interval_seconds=0)

    in_flight = []

    def slow_send(cfg, c):
        in_flight.append("started")
        time.sleep(0.2)
        in_flight.append("finished")

    monkeypatch.setattr("node.heartbeat.send_heartbeat", slow_send)

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(heartbeat_loop(config, None, stop))
        await asyncio.sleep(0.05)  # let it enter the slow send
        assert in_flight == ["started"]

        stop.set()
        await task

        # If stop() returned before the beat actually finished, this would be
        # ["started"] here instead — the exact race the fix closes.
        assert in_flight == ["started", "finished"]

    asyncio.run(run())
