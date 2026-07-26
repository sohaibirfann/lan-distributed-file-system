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
from coordinator.models import Account, Chunk, ChunkPlacement, File, Node, Settings
from coordinator.schemas import (
    AccountOut,
    ChunkDetailOut,
    ChunkHealthOut,
    ChunkPlacementOut,
    FileCreateRequest,
    FileDetailOut,
    FileOut,
    LoginRequest,
    NodeHeartbeatRequest,
    NodeOut,
    NodeRegisterRequest,
    RegisterRequest,
    RepairPlanOut,
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
from shared.placement import Node as PlacementNode
from shared.placement import NodeState, build_ring, placement_candidates, state_from_heartbeat

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


def _file_out(db: Session, file: File) -> FileOut:
    chunk_count = db.query(Chunk).filter(Chunk.file_id == file.id).count()
    return FileOut(
        id=file.id,
        name=file.name,
        size_bytes=file.size_bytes,
        uploader_account_id=file.uploader_account_id,
        created_at=file.created_at,
        updated_at=file.updated_at,
        chunk_count=chunk_count,
    )


@app.post("/files", response_model=FileOut, status_code=201)
def create_file(
    body: FileCreateRequest,
    account: Account = Depends(require_session),
    db: Session = Depends(get_db),
) -> FileOut:
    sequence_indices = sorted(chunk.sequence_index for chunk in body.chunks)
    if sequence_indices != list(range(len(body.chunks))):
        raise HTTPException(
            status_code=422, detail="chunk sequence_index values must be exactly 0..N-1"
        )

    declared_total = sum(chunk.size_bytes for chunk in body.chunks)
    if declared_total != body.size_bytes:
        raise HTTPException(
            status_code=422,
            detail=f"size_bytes ({body.size_bytes}) does not match the sum of chunk sizes ({declared_total})",
        )

    node_ids = {node_id for chunk in body.chunks for node_id in chunk.node_ids}
    existing_node_ids = {
        row.id for row in db.query(Node.id).filter(Node.id.in_(node_ids)).all()
    }
    missing = node_ids - existing_node_ids
    if missing:
        raise HTTPException(status_code=422, detail=f"unknown node_ids: {sorted(missing)}")

    file = File(name=body.name, size_bytes=body.size_bytes, uploader_account_id=account.id)
    db.add(file)
    db.flush()  # assigns file.id for the chunks below, without committing yet

    for chunk_in in body.chunks:
        chunk = Chunk(
            file_id=file.id,
            sequence_index=chunk_in.sequence_index,
            hash=chunk_in.hash,
            size_bytes=chunk_in.size_bytes,
        )
        db.add(chunk)
        db.flush()  # assigns chunk.id for the placements below
        for node_id in chunk_in.node_ids:
            db.add(ChunkPlacement(chunk_id=chunk.id, node_id=node_id))

    db.commit()
    db.refresh(file)
    return _file_out(db, file)


@app.get("/files", response_model=list[FileOut])
def list_files(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> list[FileOut]:
    files = db.query(File).order_by(File.id).all()
    return [_file_out(db, file) for file in files]


@app.get("/files/{file_id}", response_model=FileDetailOut)
def get_file(
    file_id: int, account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> FileDetailOut:
    file = db.get(File, file_id)
    if file is None:
        raise HTTPException(status_code=404, detail="file not found")

    chunks = (
        db.query(Chunk).filter(Chunk.file_id == file_id).order_by(Chunk.sequence_index).all()
    )
    chunk_details = []
    for chunk in chunks:
        placements = (
            db.query(ChunkPlacement, Node)
            .join(Node, ChunkPlacement.node_id == Node.id)
            .filter(ChunkPlacement.chunk_id == chunk.id)
            .all()
        )
        chunk_details.append(
            ChunkDetailOut(
                sequence_index=chunk.sequence_index,
                hash=chunk.hash,
                size_bytes=chunk.size_bytes,
                nodes=[
                    ChunkPlacementOut(node_id=node.id, address=node.address)
                    for _, node in placements
                ],
            )
        )

    return FileDetailOut(
        id=file.id,
        name=file.name,
        size_bytes=file.size_bytes,
        uploader_account_id=file.uploader_account_id,
        created_at=file.created_at,
        updated_at=file.updated_at,
        chunks=chunk_details,
    )


@app.delete("/files/{file_id}", status_code=204)
def delete_file(
    file_id: int, account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> None:
    file = db.get(File, file_id)
    if file is None:
        raise HTTPException(status_code=404, detail="file not found")

    chunk_ids = [row.id for row in db.query(Chunk.id).filter(Chunk.file_id == file_id).all()]
    if chunk_ids:
        db.query(ChunkPlacement).filter(ChunkPlacement.chunk_id.in_(chunk_ids)).delete(
            synchronize_session=False
        )
        db.query(Chunk).filter(Chunk.file_id == file_id).delete(synchronize_session=False)

    db.delete(file)
    db.commit()


@app.get("/replication/health", response_model=list[ChunkHealthOut])
def replication_health(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> list[ChunkHealthOut]:
    replication_factor = int(get_required_setting(db, REPLICATION_FACTOR_KEY))

    # Snapshot "now" once so every chunk in this response is judged against
    # the same instant — evaluating Node.state fresh per access could let a
    # node crossing the DOWN boundary mid-request answer differently for
    # different chunks it's a replica of.
    now = datetime.now(timezone.utc)
    node_states = {
        node.id: state_from_heartbeat(node.last_heartbeat_at, now, node.draining)
        for node in db.query(Node).all()
    }

    at_risk = []
    for chunk in db.query(Chunk).all():
        placements = db.query(ChunkPlacement).filter(ChunkPlacement.chunk_id == chunk.id).all()
        # A DOWN replica's bytes are presumed lost; anything else (UP, SUSPECT,
        # DRAINING) still counts as a copy that hasn't actually failed yet —
        # repair only kicks in once a node is confirmed DOWN, not merely flaky.
        healthy_node_ids = [
            placement.node_id
            for placement in placements
            if placement.node_id in node_states and node_states[placement.node_id] is not NodeState.DOWN
        ]

        if len(healthy_node_ids) >= replication_factor:
            continue

        at_risk.append(
            ChunkHealthOut(
                chunk_id=chunk.id,
                file_id=chunk.file_id,
                sequence_index=chunk.sequence_index,
                healthy_node_ids=healthy_node_ids,
                replication_factor=replication_factor,
                status="unavailable" if not healthy_node_ids else "under_replicated",
            )
        )

    return at_risk


@app.get("/replication/repair-plan", response_model=list[RepairPlanOut])
def repair_plan(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> list[RepairPlanOut]:
    # Each chunk's plan is computed independently, so two chunks short a
    # replica can both target the same spare node in one response. Fine for
    # a planning-only endpoint; whatever executes these plans needs to notice
    # a node filling up partway through and re-check before writing.
    replication_factor = int(get_required_setting(db, REPLICATION_FACTOR_KEY))

    # Built directly from one shared `now` rather than via Node.to_placement_node()
    # (which stamps its own fresh timestamp) — otherwise the healthy/down check
    # below and the ring's eligibility check could disagree on a node that
    # crosses a threshold mid-request.
    now = datetime.now(timezone.utc)
    all_nodes = db.query(Node).all()
    placement_nodes = {
        str(node.id): PlacementNode(
            node_id=str(node.id),
            capacity_budget_bytes=node.capacity_budget_bytes,
            free_disk_bytes=node.free_disk_bytes,
            used_bytes=node.used_bytes,
            state=state_from_heartbeat(node.last_heartbeat_at, now, node.draining),
        )
        for node in all_nodes
    }
    node_states = {int(node_id): pn.state for node_id, pn in placement_nodes.items()}
    ring = build_ring(list(placement_nodes.values()))

    plans = []
    for chunk in db.query(Chunk).all():
        placements = db.query(ChunkPlacement).filter(ChunkPlacement.chunk_id == chunk.id).all()
        placed_node_ids = {placement.node_id for placement in placements}
        healthy_node_ids = [
            node_id
            for node_id in placed_node_ids
            if node_id in node_states and node_states[node_id] is not NodeState.DOWN
        ]

        needed = replication_factor - len(healthy_node_ids)
        if needed <= 0 or not healthy_node_ids:
            # Fully healthy needs no repair; zero healthy replicas means
            # there's nothing left to copy from, so there's no plan to make.
            continue

        # Ask for every node the ring could offer, then drop ones that
        # already hold this chunk (healthy or not — no point doubling up on
        # a node that still has a copy) and take only as many as needed.
        candidate_ids = placement_candidates(ring, placement_nodes, chunk.hash, len(all_nodes))
        target_node_ids = [
            int(node_id) for node_id in candidate_ids if int(node_id) not in placed_node_ids
        ][:needed]
        if not target_node_ids:
            continue  # no eligible node currently available to repair onto

        plans.append(
            RepairPlanOut(
                chunk_id=chunk.id,
                file_id=chunk.file_id,
                sequence_index=chunk.sequence_index,
                hash=chunk.hash,
                # Simplest defensible tiebreaker among surviving replicas —
                # not a real latency measurement.
                source_node_id=min(healthy_node_ids),
                target_node_ids=target_node_ids,
            )
        )

    return plans
