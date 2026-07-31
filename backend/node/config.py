from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from node.discovery import discover_coordinator_address
from shared.placement import SUSPECT_THRESHOLD

GB = 1024**3


@dataclass(frozen=True)
class NodeConfig:
    storage_directory: Path
    capacity_budget_bytes: int
    coordinator_address: str
    node_address: str
    owner_username: str
    owner_password: str = field(repr=False)
    chunk_token: str = field(repr=False, default="unconfigured")
    heartbeat_interval_seconds: int = 10


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


def _int_from_env_with_default(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a whole number, got {raw!r}.") from None


def load_config() -> NodeConfig:
    storage_directory = _str_from_env("STORAGE_DIRECTORY")

    capacity_budget_gb = _int_from_env("CAPACITY_BUDGET_GB")
    if capacity_budget_gb <= 0:
        raise RuntimeError("CAPACITY_BUDGET_GB must be positive.")

    path = Path(storage_directory)
    path.mkdir(parents=True, exist_ok=True)

    coordinator_address = os.environ.get("COORDINATOR_ADDRESS")
    if coordinator_address:
        coordinator_address = coordinator_address.rstrip("/")
        if not coordinator_address.startswith(("http://", "https://")):
            raise RuntimeError("COORDINATOR_ADDRESS must start with http:// or https://.")
    else:
        coordinator_address = discover_coordinator_address()

    heartbeat_interval_seconds = _int_from_env_with_default("HEARTBEAT_INTERVAL_SECONDS", 10)
    if heartbeat_interval_seconds <= 0:
        raise RuntimeError("HEARTBEAT_INTERVAL_SECONDS must be positive.")
    if heartbeat_interval_seconds >= SUSPECT_THRESHOLD.total_seconds():
        raise RuntimeError(
            f"HEARTBEAT_INTERVAL_SECONDS ({heartbeat_interval_seconds}) must be less than "
            f"the coordinator's suspect threshold ({SUSPECT_THRESHOLD.total_seconds():.0f}s), "
            "or a healthy node would be marked suspect between beats."
        )

    return NodeConfig(
        storage_directory=path,
        capacity_budget_bytes=capacity_budget_gb * GB,
        coordinator_address=coordinator_address,
        node_address=_str_from_env("NODE_ADDRESS"),
        owner_username=_str_from_env("OWNER_USERNAME"),
        owner_password=_str_from_env("OWNER_PASSWORD"),
        chunk_token=os.environ.get("CHUNK_TOKEN") or secrets.token_hex(32),
        heartbeat_interval_seconds=heartbeat_interval_seconds,
    )
