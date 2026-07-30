from __future__ import annotations

import os
import secrets

from fastapi import HTTPException
from sqlalchemy.orm import Session

from coordinator.db import SessionLocal
from coordinator.models import Settings
from coordinator.security import hash_namespace_passphrase

NAMESPACE_SALT_KEY = "namespace_salt"
NAMESPACE_VERIFIER_KEY = "namespace_verifier"
NAMESPACE_PASSPHRASE_HASH_KEY = "namespace_passphrase_hash"
JWT_SECRET_KEY_KEY = "jwt_secret_key"
REPLICATION_FACTOR_KEY = "replication_factor"
WRITE_QUORUM_KEY = "write_quorum"
MAX_FILE_SIZE_BYTES_KEY = "max_file_size_bytes"


def get_setting(db: Session, key: str) -> str | None:
    row = db.get(Settings, key)
    return row.value if row else None


def get_required_setting(db: Session, key: str) -> str:
    """Missing means the coordinator never finished startup seeding."""
    value = get_setting(db, key)
    if value is None:
        raise HTTPException(status_code=500, detail="Coordinator is not initialized")
    return value


def set_setting(db: Session, key: str, value: str) -> None:
    db.merge(Settings(key=key, value=value))


def seed_settings() -> None:
    db = SessionLocal()
    try:
        if get_setting(db, NAMESPACE_SALT_KEY) is None:
            import secrets

            set_setting(db, NAMESPACE_SALT_KEY, secrets.token_hex(16))

        if get_setting(db, NAMESPACE_PASSPHRASE_HASH_KEY) is None:
            passphrase = os.environ.get("NAMESPACE_PASSPHRASE")
            if not passphrase:
                raise RuntimeError(
                    "NAMESPACE_PASSPHRASE must be set in the environment on first run "
                    "(no namespace-passphrase hash exists yet)."
                )
            set_setting(
                db, NAMESPACE_PASSPHRASE_HASH_KEY, hash_namespace_passphrase(passphrase)
            )

        if get_setting(db, JWT_SECRET_KEY_KEY) is None:
            set_setting(db, JWT_SECRET_KEY_KEY, secrets.token_hex(32))

        db.commit()
    finally:
        db.close()


def _int_from_env(name: str, default: str) -> int:
    raw = os.environ.get(name, default)
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a whole number, got {raw!r}.") from None


def load_replication_config() -> None:
    """RF/W are ordinary config, re-read from the environment on every start
    (unlike the seeded secrets above), so a restart is how you change them."""
    replication_factor = _int_from_env("REPLICATION_FACTOR", "3")
    write_quorum = _int_from_env("WRITE_QUORUM", "2")

    if replication_factor < 2:
        raise RuntimeError("REPLICATION_FACTOR must be at least 2.")
    if write_quorum < 1:
        raise RuntimeError("WRITE_QUORUM must be at least 1, or nothing is ever durable.")
    if write_quorum > replication_factor:
        raise RuntimeError("WRITE_QUORUM cannot exceed REPLICATION_FACTOR.")

    db = SessionLocal()
    try:
        set_setting(db, REPLICATION_FACTOR_KEY, str(replication_factor))
        set_setting(db, WRITE_QUORUM_KEY, str(write_quorum))
        db.commit()
    finally:
        db.close()


def load_max_file_size_config() -> None:
    # 10 GiB default; no specific value is mandated, so this is configurable.
    max_file_size_bytes = _int_from_env("MAX_FILE_SIZE_BYTES", str(10 * 1024**3))
    if max_file_size_bytes <= 0:
        raise RuntimeError("MAX_FILE_SIZE_BYTES must be positive.")

    db = SessionLocal()
    try:
        set_setting(db, MAX_FILE_SIZE_BYTES_KEY, str(max_file_size_bytes))
        db.commit()
    finally:
        db.close()
