import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from shared.placement import NodeState

# host:port, where host is a hostname or dotted IPv4. Deliberately strict: this
# value is handed to browsers as a chunk destination, so anything with a scheme,
# a path, or control characters has no business getting that far.
_ADDRESS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]*:(\d{1,5})$")


def _validate_address(value: str) -> str:
    match = _ADDRESS.match(value)
    if match is None:
        raise ValueError("address must look like host:port")
    if not 1 <= int(match.group(1)) <= 65535:
        raise ValueError("port must be between 1 and 65535")
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
