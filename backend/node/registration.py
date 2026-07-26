from __future__ import annotations

import shutil
from pathlib import Path

import httpx

from node.config import NodeConfig


def _used_bytes(storage_directory: Path) -> int:
    total = 0
    for f in storage_directory.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except FileNotFoundError:
            continue  # deleted between the listing and the stat
    return total


def _raise_with_detail(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as err:
        raise httpx.HTTPStatusError(
            f"{err}. Response body: {response.text}", request=err.request, response=err.response
        ) from err


def register_with_coordinator(config: NodeConfig, client: httpx.Client) -> None:
    login = client.post(
        "/login", json={"username": config.owner_username, "password": config.owner_password}
    )
    _raise_with_detail(login)

    free_disk_bytes = shutil.disk_usage(config.storage_directory).free
    register = client.post(
        "/nodes/register",
        json={
            "address": config.node_address,
            "capacity_budget_bytes": config.capacity_budget_bytes,
            "free_disk_bytes": free_disk_bytes,
            "used_bytes": _used_bytes(config.storage_directory),
        },
    )
    _raise_with_detail(register)
