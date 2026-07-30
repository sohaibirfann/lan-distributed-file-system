from __future__ import annotations

from sqlalchemy.orm import Session

from coordinator.models import Event


def record_event(db: Session, kind: str, message: str) -> None:
    db.add(Event(kind=kind, message=message))
