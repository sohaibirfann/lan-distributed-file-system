from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

GB = 1024**3


@dataclass(frozen=True)
class NodeConfig:
    storage_directory: Path
    capacity_budget_bytes: int
    coordinator_address: str
    node_address: str
    owner_username: str
    owner_password: str = field(repr=False)


def _int_from_env(name: str) -> int:
    raw = os.environ.get(name)
    if not raw:
        raise RuntimeError(f"{name} must be set.")
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a whole number, got {raw!r}.") from None


def _str_from_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be set.")
    return value


def load_config() -> NodeConfig:
    storage_directory = _str_from_env("STORAGE_DIRECTORY")

    capacity_budget_gb = _int_from_env("CAPACITY_BUDGET_GB")
    if capacity_budget_gb <= 0:
        raise RuntimeError("CAPACITY_BUDGET_GB must be positive.")

    path = Path(storage_directory)
    path.mkdir(parents=True, exist_ok=True)

    coordinator_address = _str_from_env("COORDINATOR_ADDRESS")
    if not coordinator_address.startswith(("http://", "https://")):
        raise RuntimeError("COORDINATOR_ADDRESS must start with http:// or https://.")

    return NodeConfig(
        storage_directory=path,
        capacity_budget_bytes=capacity_budget_gb * GB,
        coordinator_address=coordinator_address,
        node_address=_str_from_env("NODE_ADDRESS"),
        owner_username=_str_from_env("OWNER_USERNAME"),
        owner_password=_str_from_env("OWNER_PASSWORD"),
    )
