import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from shared.placement import NodeState

# host:port, where host is a hostname or dotted IPv4. Deliberately strict: this
# value is handed to browsers as a chunk destination, so anything with a scheme,
# a path, or control characters has no business getting that far.
_ADDRESS = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*:(\d{1,5})")


def _validate_address(value: str) -> str:
    match = _ADDRESS.fullmatch(value)
    if match is None:
        raise ValueError("address must look like host:port")
    if not 1 <= int(match.group(1)) <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return value


_CHUNK_HASH = re.compile(r"[0-9a-f]{64}")


def _validate_chunk_hash(value: str) -> str:
    if not _CHUNK_HASH.fullmatch(value):
        raise ValueError("hash must be a 64-character lowercase sha256 hex digest")
    return value


def _validate_no_duplicate_node_ids(value: list[int]) -> list[int]:
    if len(set(value)) != len(value):
        raise ValueError("node_ids must not contain duplicates")
    return value


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    namespace_passphrase: str = Field(min_length=1, max_length=1024)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class AccountOut(BaseModel):
    id: int
    username: str
    created_at: datetime

    model_config = {"from_attributes": True}


class NamespaceVerifierRequest(BaseModel):
    verifier: str = Field(min_length=1, max_length=4096)


class NodeRegisterRequest(BaseModel):
    address: str = Field(min_length=1, max_length=256)
    capacity_budget_bytes: int = Field(gt=0)
    free_disk_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)

    _check_address = field_validator("address")(_validate_address)


class NodeHeartbeatRequest(BaseModel):
    address: str = Field(min_length=1, max_length=256)
    free_disk_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)

    _check_address = field_validator("address")(_validate_address)


class NodeDrainRequest(BaseModel):
    address: str = Field(min_length=1, max_length=256)

    _check_address = field_validator("address")(_validate_address)


class NodeDrainOut(BaseModel):
    remaining_chunks: int


class NodeOut(BaseModel):
    id: int
    address: str
    capacity_budget_bytes: int
    free_disk_bytes: int
    used_bytes: int
    effective_capacity_bytes: int
    state: NodeState
    draining: bool
    registered_at: datetime
    last_heartbeat_at: datetime

    model_config = {"from_attributes": True}


class ChunkIn(BaseModel):
    sequence_index: int = Field(ge=0)
    hash: str
    size_bytes: int = Field(gt=0)
    node_ids: list[int] = Field(min_length=1)

    _check_hash = field_validator("hash")(_validate_chunk_hash)
    _check_node_ids = field_validator("node_ids")(_validate_no_duplicate_node_ids)


class FileCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    chunks: list[ChunkIn] = Field(min_length=1)


class FileRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class FileOut(BaseModel):
    id: int
    name: str
    size_bytes: int
    uploader_account_id: int
    created_at: datetime
    updated_at: datetime
    chunk_count: int


class ChunkPlacementOut(BaseModel):
    node_id: int
    address: str


class ChunkDetailOut(BaseModel):
    sequence_index: int
    hash: str
    size_bytes: int
    nodes: list[ChunkPlacementOut]


class FileDetailOut(BaseModel):
    id: int
    name: str
    size_bytes: int
    uploader_account_id: int
    created_at: datetime
    updated_at: datetime
    chunks: list[ChunkDetailOut]


class ChunkUnavailableReport(BaseModel):
    node_id: int


class ChunkHealthOut(BaseModel):
    chunk_id: int
    file_id: int
    sequence_index: int
    healthy_node_ids: list[int]
    replication_factor: int
    status: Literal["under_replicated", "unavailable"]


class RepairPlanOut(BaseModel):
    chunk_id: int
    file_id: int
    sequence_index: int
    hash: str
    source_node_id: int
    target_node_ids: list[int]


class RepairResultOut(BaseModel):
    chunk_id: int
    repaired_node_ids: list[int]
    failed_node_ids: list[int]
    error: str | None


class EventOut(BaseModel):
    id: int
    created_at: datetime
    kind: str
    message: str

    model_config = {"from_attributes": True}
