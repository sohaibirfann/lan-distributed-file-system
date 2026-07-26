from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

from node.config import NodeConfig
from node.registration import measure_used_bytes
from shared.placement import Node as PlacementNode
from shared.placement import NodeState

CHUNK_ID_PATTERN = re.compile(r"[0-9a-f]{64}")


class InvalidChunkId(ValueError):
    pass


class ChunkHashMismatch(ValueError):
    pass


class InsufficientCapacity(ValueError):
    pass


def _chunk_path(config: NodeConfig, chunk_id: str) -> Path:
    # chunk_id becomes a filename, so this also blocks path traversal —
    # anything other than exactly a sha256 hex digest is rejected outright.
    if not CHUNK_ID_PATTERN.fullmatch(chunk_id):
        raise InvalidChunkId(
            f"chunk_id must be a 64-character lowercase sha256 hex digest, got {chunk_id!r}"
        )
    return config.storage_directory / chunk_id


def _current_capacity(config: NodeConfig) -> PlacementNode:
    return PlacementNode(
        node_id="self",
        capacity_budget_bytes=config.capacity_budget_bytes,
        free_disk_bytes=shutil.disk_usage(config.storage_directory).free,
        used_bytes=measure_used_bytes(config.storage_directory),
        state=NodeState.UP,
    )


def store_chunk(config: NodeConfig, chunk_id: str, data: bytes) -> None:
    path = _chunk_path(config, chunk_id)

    actual_hash = hashlib.sha256(data).hexdigest()
    if actual_hash != chunk_id:
        raise ChunkHashMismatch(f"expected hash {chunk_id}, got {actual_hash}")

    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == chunk_id:
        # Already correctly stored — skip the capacity check so re-uploading
        # doesn't count the chunk's own existing bytes against itself. A file
        # at this path that DOESN'T verify (e.g. a truncated write from a
        # prior crash) falls through and gets overwritten from the verified
        # incoming data instead of being trusted forever.
        return

    remaining = _current_capacity(config).remaining_bytes
    if len(data) > remaining:
        raise InsufficientCapacity(f"chunk is {len(data)} bytes, only {remaining} remaining")

    path.write_bytes(data)


def retrieve_chunk(config: NodeConfig, chunk_id: str) -> bytes | None:
    path = _chunk_path(config, chunk_id)
    if not path.is_file():
        return None
    return path.read_bytes()


def delete_chunk(config: NodeConfig, chunk_id: str) -> None:
    # Idempotent: deleting an already-gone (or never-stored) chunk is success,
    # not an error — matches the immutable, content-addressed model where
    # "delete" just means "stop being responsible for this blob."
    _chunk_path(config, chunk_id).unlink(missing_ok=True)
