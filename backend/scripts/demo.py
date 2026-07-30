"""Starts a coordinator and three storage nodes for a local demo."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DEMO_DATA = BACKEND_ROOT / "demo-data"

NAMESPACE_PASSPHRASE = "demo passphrase"
USERNAME = "demo"
PASSWORD = "demo1234"

COORDINATOR_PORT = 8000
NODE_PORTS = [9001, 9002, 9003]


def _stream_output(process: subprocess.Popen, prefix: str, output: list[str]) -> None:
    for line in iter(process.stdout.readline, ""):
        output.append(line)
        print(f"[{prefix}] {line}", end="", flush=True)


def _spawn(module: str, env: dict, prefix: str) -> tuple[subprocess.Popen, list[str]]:
    process = subprocess.Popen(
        [sys.executable, "-m", module],
        cwd=BACKEND_ROOT,
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output: list[str] = []
    threading.Thread(target=_stream_output, args=(process, prefix, output), daemon=True).start()
    return process, output


def _wait_for_health(
    url: str, process: subprocess.Popen, output: list[str], timeout: float = 15.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"{url} process exited early with code {process.returncode}:\n{''.join(output)}"
            )
        try:
            if httpx.get(f"{url}/health", timeout=1.0).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    raise TimeoutError(f"{url}/health never came up within {timeout}s:\n{''.join(output)}")


def main() -> None:
    if DEMO_DATA.exists():
        try:
            shutil.rmtree(DEMO_DATA)
        except OSError as err:
            raise RuntimeError(
                f"could not clear {DEMO_DATA} -- stop any leftover demo process first: {err}"
            ) from None
    DEMO_DATA.mkdir(parents=True)

    coordinator_url = f"http://127.0.0.1:{COORDINATOR_PORT}"
    coordinator, coordinator_output = _spawn(
        "coordinator",
        {
            "NAMESPACE_PASSPHRASE": NAMESPACE_PASSPHRASE,
            "COORDINATOR_DB_PATH": str(DEMO_DATA / "coordinator.db"),
            "COORDINATOR_HOST": "127.0.0.1",
            "COORDINATOR_PORT": str(COORDINATOR_PORT),
        },
        "coordinator",
    )
    processes = [coordinator]
    try:
        _wait_for_health(coordinator_url, coordinator, coordinator_output)

        httpx.post(
            f"{coordinator_url}/register",
            json={
                "username": USERNAME,
                "password": PASSWORD,
                "namespace_passphrase": NAMESPACE_PASSPHRASE,
            },
        ).raise_for_status()

        for i, port in enumerate(NODE_PORTS, start=1):
            node_url = f"http://127.0.0.1:{port}"
            node, node_output = _spawn(
                "node",
                {
                    "STORAGE_DIRECTORY": str(DEMO_DATA / f"node-{i}"),
                    "CAPACITY_BUDGET_GB": "5",
                    "COORDINATOR_ADDRESS": coordinator_url,
                    "NODE_ADDRESS": f"127.0.0.1:{port}",
                    "OWNER_USERNAME": USERNAME,
                    "OWNER_PASSWORD": PASSWORD,
                    "NODE_HOST": "127.0.0.1",
                    "NODE_PORT": str(port),
                },
                f"node-{i}",
            )
            processes.append(node)
            _wait_for_health(node_url, node, node_output)

        print(flush=True)
        print(f"Dashboard: {coordinator_url}", flush=True)
        print(f"Login: {USERNAME} / {PASSWORD}", flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for process in reversed(processes):  # nodes before the coordinator they depend on
            process.terminate()
        for process in reversed(processes):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    main()
