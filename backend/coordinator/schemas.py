from datetime import datetime

from pydantic import BaseModel, Field

from shared.placement import NodeState


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
    used_bytes: int = Field(ge=0, default=0)


class NodeOut(BaseModel):
    id: int
    address: str
    capacity_budget_bytes: int
    free_disk_bytes: int
    used_bytes: int
    state: NodeState
    registered_at: datetime
    last_heartbeat_at: datetime

    model_config = {"from_attributes": True}
