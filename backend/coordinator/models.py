from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from coordinator.db import Base
from shared.placement import NodeState


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Node(Base):
    __tablename__ = "nodes"
    __table_args__ = (UniqueConstraint("owner_account_id", "address"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    address: Mapped[str] = mapped_column(String)
    capacity_budget_bytes: Mapped[int]
    free_disk_bytes: Mapped[int]
    used_bytes: Mapped[int]
    state: Mapped[NodeState] = mapped_column(SAEnum(NodeState), default=NodeState.UP)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
