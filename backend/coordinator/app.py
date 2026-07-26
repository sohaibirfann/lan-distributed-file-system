from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from coordinator.db import get_db, init_db, SessionLocal
from coordinator.models import Account, Node, Settings
from coordinator.schemas import (
    AccountOut,
    LoginRequest,
    NodeHeartbeatRequest,
    NodeOut,
    NodeRegisterRequest,
    RegisterRequest,
)
from coordinator.security import (
    DUMMY_PASSWORD_HASH,
    create_session_token,
    decode_session_token,
    hash_account_password,
    hash_namespace_passphrase,
    verify_account_password,
    verify_namespace_passphrase,
)
from shared.placement import build_ring, placement_candidates

SESSION_COOKIE_NAME = "session"
SESSION_LIFETIME = timedelta(days=int(os.environ.get("SESSION_LIFETIME_DAYS", "7")))

PUBLIC_PATHS = frozenset({"/health", "/register", "/login"})

NAMESPACE_SALT_KEY = "namespace_salt"
NAMESPACE_PASSPHRASE_HASH_KEY = "namespace_passphrase_hash"
JWT_SECRET_KEY_KEY = "jwt_secret_key"
REPLICATION_FACTOR_KEY = "replication_factor"
WRITE_QUORUM_KEY = "write_quorum"


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_settings()
    load_replication_config()
    yield


def require_session(request: Request, db: Session = Depends(get_db)) -> Account:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    jwt_secret_key = get_required_setting(db, JWT_SECRET_KEY_KEY)
    try:
        account_id = decode_session_token(token, jwt_secret_key)
    except jwt.PyJWTError as err:
        raise HTTPException(status_code=401, detail="Not authenticated") from err

    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return account


def enforce_session(request: Request, db: Session = Depends(get_db)) -> None:
    if request.url.path in PUBLIC_PATHS:
        return
    require_session(request, db)


app = FastAPI(
    lifespan=lifespan,
    dependencies=[Depends(enforce_session)],
    # These bypass the dependency above, so disable them rather than leave them open.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/register", response_model=AccountOut, status_code=201)
def register(body: RegisterRequest, db: Session = Depends(get_db)) -> Account:
    namespace_passphrase_hash = get_required_setting(db, NAMESPACE_PASSPHRASE_HASH_KEY)
    if not verify_namespace_passphrase(body.namespace_passphrase, namespace_passphrase_hash):
        raise HTTPException(status_code=401, detail="Invalid namespace passphrase")

    existing = db.query(Account).filter(Account.username == body.username).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Username already taken")

    account = Account(
        username=body.username,
        password_hash=hash_account_password(body.password),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@app.post("/login", response_model=AccountOut)
def login(body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> Account:
    account = db.query(Account).filter(Account.username == body.username).first()
    invalid_credentials = HTTPException(status_code=401, detail="Invalid username or password")

    # Hash unconditionally so "no such user" and "wrong password" take the same time.
    password_hash = account.password_hash if account is not None else DUMMY_PASSWORD_HASH
    password_valid = verify_account_password(body.password, password_hash)
    if account is None or not password_valid:
        raise invalid_credentials

    jwt_secret_key = get_required_setting(db, JWT_SECRET_KEY_KEY)
    token = create_session_token(account.id, jwt_secret_key, SESSION_LIFETIME)

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=int(SESSION_LIFETIME.total_seconds()),
    )
    return account


@app.get("/me", response_model=AccountOut)
def me(account: Account = Depends(require_session)) -> Account:
    return account


@app.get("/namespace/salt")
def namespace_salt(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> dict[str, str]:
    return {"salt": get_required_setting(db, NAMESPACE_SALT_KEY)}


@app.get("/config/replication")
def replication_config(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> dict[str, int | bool]:
    replication_factor = int(get_required_setting(db, REPLICATION_FACTOR_KEY))
    write_quorum = int(get_required_setting(db, WRITE_QUORUM_KEY))
    nodes = db.query(Node).all()
    eligible_node_count = sum(1 for node in nodes if node.to_placement_node().is_eligible)

    return {
        "replication_factor": replication_factor,
        "write_quorum": write_quorum,
        "registered_node_count": len(nodes),
        # registered_node_count can undercount a live outage (a down node is
        # still "registered"), so this is a separate, honest write-health signal.
        "under_replicated": len(nodes) < replication_factor,
        "eligible_node_count": eligible_node_count,
        "write_available": eligible_node_count >= write_quorum,
    }


def _find_node(db: Session, address: str) -> Node | None:
    return db.query(Node).filter(Node.address == address).first()


def _apply_report(node: Node, body: NodeRegisterRequest) -> None:
    node.capacity_budget_bytes = body.capacity_budget_bytes
    node.free_disk_bytes = body.free_disk_bytes
    node.used_bytes = body.used_bytes
    node.last_heartbeat_at = datetime.now(timezone.utc)


@app.post("/nodes/register", response_model=NodeOut, status_code=201)
def register_node(
    body: NodeRegisterRequest,
    account: Account = Depends(require_session),
    db: Session = Depends(get_db),
) -> Node:
    node = _find_node(db, body.address)
    if node is not None:
        if node.owner_account_id != account.id:
            raise HTTPException(status_code=409, detail="Address is already registered")
        _apply_report(node, body)
        db.commit()
        db.refresh(node)
        return node

    node = Node(owner_account_id=account.id, address=body.address)
    _apply_report(node, body)
    db.add(node)
    try:
        db.commit()
    except IntegrityError:
        # Someone else claimed this address between the lookup and the insert.
        db.rollback()
        raise HTTPException(status_code=409, detail="Address is already registered")

    db.refresh(node)
    return node


@app.post("/nodes/heartbeat", response_model=NodeOut)
def heartbeat(
    body: NodeHeartbeatRequest,
    account: Account = Depends(require_session),
    db: Session = Depends(get_db),
) -> Node:
    node = _find_node(db, body.address)
    if node is None or node.owner_account_id != account.id:
        raise HTTPException(status_code=404, detail="Node is not registered")

    node.free_disk_bytes = body.free_disk_bytes
    node.used_bytes = body.used_bytes
    node.last_heartbeat_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(node)
    return node


@app.get("/nodes", response_model=list[NodeOut])
def list_nodes(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> list[Node]:
    return db.query(Node).order_by(Node.id).all()


@app.get("/placement/{chunk_id}", response_model=list[NodeOut])
def placement_for_chunk(
    chunk_id: str, account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> list[Node]:
    nodes_by_id = {str(node.id): node for node in db.query(Node).all()}
    placement_nodes = {node_id: node.to_placement_node() for node_id, node in nodes_by_id.items()}
    ring = build_ring(list(placement_nodes.values()))

    replication_factor = int(get_required_setting(db, REPLICATION_FACTOR_KEY))
    candidate_ids = placement_candidates(ring, placement_nodes, chunk_id, replication_factor)

    write_quorum = int(get_required_setting(db, WRITE_QUORUM_KEY))
    if len(candidate_ids) < write_quorum:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Only {len(candidate_ids)} node(s) available, "
                f"need at least {write_quorum} to accept a write."
            ),
        )

    return [nodes_by_id[node_id] for node_id in candidate_ids]
