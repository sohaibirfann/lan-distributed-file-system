import os
import socket
import subprocess
import sys
import threading
import time

import httpx
import pytest

BACKEND_ROOT = "."


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn(module: str, env: dict) -> tuple[subprocess.Popen, list[str]]:
    process = subprocess.Popen(
        [sys.executable, "-m", module],
        cwd=BACKEND_ROOT,
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # Left unread, a startup traceback fills the OS pipe buffer and the child
    # blocks forever on the write — draining it is also what makes a failure
    # here diagnosable instead of a bare timeout.
    output: list[str] = []
    threading.Thread(target=lambda: output.extend(iter(process.stdout.readline, "")), daemon=True).start()
    return process, output


def _wait_for_health(
    base_url: str, process: subprocess.Popen, output: list[str], timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"process exited early with code {process.returncode}:\n{''.join(output)}"
            )
        try:
            if httpx.get(f"{base_url}/health", timeout=0.5).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise TimeoutError(f"{base_url}/health never came up within {timeout}s:\n{''.join(output)}")


def _terminate(process: subprocess.Popen) -> None:
    # On Windows this is TerminateProcess (an abrupt kill, not a graceful
    # signal) — this suite only checks that the entry point starts and serves
    # traffic; graceful shutdown itself is covered in test_node_heartbeat.py.
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


@pytest.fixture
def coordinator_process(tmp_path):
    port = _free_port()
    env = {
        "COORDINATOR_DB_PATH": str(tmp_path / "coordinator.db"),
        "NAMESPACE_PASSPHRASE": "correct horse battery staple",
        "COORDINATOR_HOST": "127.0.0.1",
        "COORDINATOR_PORT": str(port),
        "MDNS_ADVERTISE": "false",
    }
    process, output = _spawn("coordinator", env)
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_health(base_url, process, output)
        yield base_url
    finally:
        _terminate(process)


def test_coordinator_entrypoint_serves_health(coordinator_process):
    assert httpx.get(f"{coordinator_process}/health").json() == {"status": "ok"}


def test_node_entrypoint_registers_with_a_running_coordinator(coordinator_process, tmp_path):
    coord = httpx.Client(base_url=coordinator_process)
    coord.post(
        "/register",
        json={
            "username": "alice",
            "password": "hunter22",
            "namespace_passphrase": "correct horse battery staple",
        },
    )

    node_port = _free_port()
    env = {
        "STORAGE_DIRECTORY": str(tmp_path / "storage"),
        "CAPACITY_BUDGET_GB": "10",
        "COORDINATOR_ADDRESS": coordinator_process,
        "NODE_ADDRESS": "127.0.0.1:9999",
        "OWNER_USERNAME": "alice",
        "OWNER_PASSWORD": "hunter22",
        "NODE_HOST": "127.0.0.1",
        "NODE_PORT": str(node_port),
    }
    process, output = _spawn("node", env)
    node_url = f"http://127.0.0.1:{node_port}"
    try:
        _wait_for_health(node_url, process, output)

        coord.post("/login", json={"username": "alice", "password": "hunter22"})
        addresses = {n["address"] for n in coord.get("/nodes").json()}
        assert "127.0.0.1:9999" in addresses
    finally:
        _terminate(process)
