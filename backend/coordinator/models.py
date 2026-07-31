from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from coordinator.db import Base, UTCDateTime
from shared.placement import Node as PlacementNode
from shared.placement import NodeState, state_from_heartbeat


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Settings(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    address: Mapped[str] = mapped_column(String, unique=True)
    capacity_budget_bytes: Mapped[int]
    free_disk_bytes: Mapped[int]
    used_bytes: Mapped[int]
    chunk_token: Mapped[str] = mapped_column(String)
    draining: Mapped[bool] = mapped_column(default=False)
    registered_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    last_heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    # Persisted so transitions can be detected -- `state` itself is never stored.
    last_known_state: Mapped[str] = mapped_column(String, default=NodeState.UP.value)

    def to_placement_node(self) -> PlacementNode:
        """The ring's view of this row. Capacity and eligibility rules live in
        the placement module so there is only ever one copy of them."""
        return PlacementNode(
            node_id=str(self.id),
            capacity_budget_bytes=self.capacity_budget_bytes,
            free_disk_bytes=self.free_disk_bytes,
            used_bytes=self.used_bytes,
            state=state_from_heartbeat(self.last_heartbeat_at, _utcnow(), self.draining),
        )

    @property
    def state(self) -> NodeState:
        return self.to_placement_node().state

    @property
    def effective_capacity_bytes(self) -> int:
        return self.to_placement_node().effective_capacity_bytes


class File(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int]
    uploader_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id"))
    sequence_index: Mapped[int]
    hash: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int]


class ChunkPlacement(Base):
    __tablename__ = "chunk_placements"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chunk_id: Mapped[int] = mapped_column(ForeignKey("chunks.id"))
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id"))
    written_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    kind: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(String)
