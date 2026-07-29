from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Path, Query, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from coordinator.db import get_db, init_db, SessionLocal
from coordinator.models import Account, Chunk, ChunkPlacement, Event, File, Node, Settings
from coordinator.schemas import (
    AccountOut,
    ChunkDetailOut,
    ChunkHealthOut,
    ChunkPlacementOut,
    ChunkUnavailableReport,
    EventOut,
    FileCreateRequest,
    FileDetailOut,
    FileOut,
    FileRenameRequest,
    LoginRequest,
    NamespaceVerifierRequest,
    NodeHeartbeatRequest,
    NodeOut,
    NodeRegisterRequest,
    RegisterRequest,
    RepairPlanOut,
    RepairResultOut,
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

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "session"
SESSION_LIFETIME = timedelta(days=int(os.environ.get("SESSION_LIFETIME_DAYS", "7")))

PUBLIC_PATHS = frozenset({"/health", "/register", "/login"})

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_settings()
    load_replication_config()
    load_max_file_size_config()

    repair_interval_seconds = _int_from_env("REPAIR_INTERVAL_SECONDS", "60")
    if repair_interval_seconds <= 0:
        raise RuntimeError("REPAIR_INTERVAL_SECONDS must be positive.")

    gc_sweep_interval_seconds = _int_from_env("GC_SWEEP_INTERVAL_SECONDS", "300")
    if gc_sweep_interval_seconds <= 0:
        raise RuntimeError("GC_SWEEP_INTERVAL_SECONDS must be positive.")

    stop = asyncio.Event()
    task = asyncio.create_task(repair_loop(stop, repair_interval_seconds))
    gc_stop = asyncio.Event()
    gc_task = asyncio.create_task(gc_sweep_loop(gc_stop, gc_sweep_interval_seconds))
    try:
        yield
    finally:
        stop.set()
        gc_stop.set()
        await task  # waits for any in-flight repair cycle to actually finish
        await gc_task  # waits for any in-flight sweep to actually finish


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


def issue_session_cookie(response: Response, request: Request, account: Account, db: Session) -> None:
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


@app.post("/register", response_model=AccountOut, status_code=201)
def register(
    body: RegisterRequest, request: Request, response: Response, db: Session = Depends(get_db)
) -> Account:
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
    issue_session_cookie(response, request, account, db)
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

    issue_session_cookie(response, request, account, db)
    return account


@app.get("/me", response_model=AccountOut)
def me(account: Account = Depends(require_session)) -> Account:
    return account


@app.get("/namespace/salt")
def namespace_salt(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> dict[str, str]:
    return {"salt": get_required_setting(db, NAMESPACE_SALT_KEY)}


@app.get("/namespace/verifier")
def get_namespace_verifier(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> dict[str, str | None]:
    return {"verifier": get_setting(db, NAMESPACE_VERIFIER_KEY)}


@app.put("/namespace/verifier")
def put_namespace_verifier(
    body: NamespaceVerifierRequest,
    account: Account = Depends(require_session),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    # First-write-wins: the coordinator can't tell a "better" verifier apart.
    if get_setting(db, NAMESPACE_VERIFIER_KEY) is not None:
        raise HTTPException(status_code=409, detail="Namespace verifier is already set")
    set_setting(db, NAMESPACE_VERIFIER_KEY, body.verifier)
    db.commit()
    return {"verifier": body.verifier}


@app.get("/config/replication")
def replication_config(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> dict[str, int | bool]:
    replication_factor = int(get_required_setting(db, REPLICATION_FACTOR_KEY))
    write_quorum = int(get_required_setting(db, WRITE_QUORUM_KEY))
    max_file_size_bytes = int(get_required_setting(db, MAX_FILE_SIZE_BYTES_KEY))
    nodes = db.query(Node).all()
    eligible_node_count = sum(1 for node in nodes if node.to_placement_node().is_eligible)

    return {
        "replication_factor": replication_factor,
        "write_quorum": write_quorum,
        "max_file_size_bytes": max_file_size_bytes,
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


def _release_surplus_placements(db: Session, node: Node) -> None:
    # Releases replicas repair already replaced with a spare while this node was down.
    replication_factor = int(get_required_setting(db, REPLICATION_FACTOR_KEY))
    now = datetime.now(timezone.utc)
    node_states = {
        n.id: state_from_heartbeat(n.last_heartbeat_at, now, n.draining)
        for n in db.query(Node).all()
    }

    placements = (
        db.query(ChunkPlacement, Chunk.hash)
        .join(Chunk, ChunkPlacement.chunk_id == Chunk.id)
        .filter(ChunkPlacement.node_id == node.id)
        .all()
    )

    released_hashes = []
    for placement, chunk_hash in placements:
        other_placements = (
            db.query(ChunkPlacement)
            .filter(
                ChunkPlacement.chunk_id == placement.chunk_id,
                ChunkPlacement.node_id != node.id,
            )
            .all()
        )
        other_healthy_count = sum(
            1
            for p in other_placements
            if p.node_id in node_states and node_states[p.node_id] is not NodeState.DOWN
        )
        if other_healthy_count < replication_factor:
            continue

        db.delete(placement)
        released_hashes.append(chunk_hash)

    db.commit()  # metadata is the reliable guarantee; physical deletes below are best-effort

    for chunk_hash in released_hashes:
        # Another Chunk row can share this hash and still be placed on this node.
        still_needed = (
            db.query(ChunkPlacement)
            .join(Chunk, ChunkPlacement.chunk_id == Chunk.id)
            .filter(Chunk.hash == chunk_hash, ChunkPlacement.node_id == node.id)
            .first()
            is not None
        )
        if still_needed:
            continue

        try:
            _default_node_client(node.address).delete(f"/chunks/{chunk_hash}")
        except httpx.HTTPError as err:
            logger.warning(
                "could not release surplus chunk %s from %s: %s", chunk_hash, node.address, err
            )


def _count_chunks_with_different_placement(
    db: Session,
    replication_factor: int,
    before_nodes: list[PlacementNode],
    after_nodes: list[PlacementNode],
) -> int:
    before_ring = build_ring(before_nodes)
    after_ring = build_ring(after_nodes)
    before_by_id = {n.node_id: n for n in before_nodes}
    after_by_id = {n.node_id: n for n in after_nodes}

    moved = 0
    for (chunk_hash,) in db.query(Chunk.hash).distinct().all():
        before_set = placement_candidates(before_ring, before_by_id, chunk_hash, replication_factor)
        after_set = placement_candidates(after_ring, after_by_id, chunk_hash, replication_factor)
        if set(before_set) != set(after_set):
            moved += 1
    return moved


def _log_placement_shift_for_new_node(
    node_id: int, replication_factor: int, before_nodes: list[PlacementNode]
) -> None:
    # Own session -- runs as a background task, after node registration has already responded.
    db = SessionLocal()
    try:
        node = db.get(Node, node_id)
        if node is None:
            return
        after_nodes = before_nodes + [node.to_placement_node()]
        moved = _count_chunks_with_different_placement(db, replication_factor, before_nodes, after_nodes)
        if moved:
            _record_event(
                db,
                "placement_shift",
                f"node {node.id} ({node.address}) joined; {moved} chunk(s) would now map to different placement",
            )
            db.commit()
    finally:
        db.close()


@app.post("/nodes/register", response_model=NodeOut, status_code=201)
def register_node(
    body: NodeRegisterRequest,
    background_tasks: BackgroundTasks,
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
        _release_surplus_placements(db, node)
        return node

    replication_factor = int(get_required_setting(db, REPLICATION_FACTOR_KEY))
    before_nodes = [n.to_placement_node() for n in db.query(Node).all()]

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
    background_tasks.add_task(
        _log_placement_shift_for_new_node, node.id, replication_factor, before_nodes
    )
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


@app.get("/events", response_model=list[EventOut])
def list_events(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> list[Event]:
    return db.query(Event).order_by(Event.id.desc()).limit(200).all()


@app.get("/placement/{chunk_id}", response_model=list[NodeOut])
def placement_for_chunk(
    chunk_id: str,
    exclude: list[str] = Query([]),
    account: Account = Depends(require_session),
    db: Session = Depends(get_db),
) -> list[Node]:
    nodes_by_id = {str(node.id): node for node in db.query(Node).all()}
    placement_nodes = {node_id: node.to_placement_node() for node_id, node in nodes_by_id.items()}
    ring = build_ring(list(placement_nodes.values()))

    replication_factor = int(get_required_setting(db, REPLICATION_FACTOR_KEY))
    candidate_ids = placement_candidates(
        ring, placement_nodes, chunk_id, replication_factor, exclude=frozenset(exclude)
    )

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

    max_file_size_bytes = int(get_required_setting(db, MAX_FILE_SIZE_BYTES_KEY))
    if body.size_bytes > max_file_size_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds the maximum allowed size ({max_file_size_bytes} bytes)",
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


@app.patch("/files/{file_id}", response_model=FileOut)
def rename_file(
    file_id: int,
    body: FileRenameRequest,
    account: Account = Depends(require_session),
    db: Session = Depends(get_db),
) -> FileOut:
    file = db.get(File, file_id)
    if file is None:
        raise HTTPException(status_code=404, detail="file not found")

    file.name = body.name
    file.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(file)
    return _file_out(db, file)


@app.delete("/files/{file_id}", status_code=204)
def delete_file(
    file_id: int, account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> None:
    file = db.get(File, file_id)
    if file is None:
        raise HTTPException(status_code=404, detail="file not found")

    # Gathered before the rows are gone, so bytes can still be reclaimed from
    # nodes after the metadata delete below has already committed. Chunks
    # aren't deduplicated across files, so two files can independently claim
    # the same hash on the same node — node_id is kept alongside address so
    # the post-commit check below can tell whether another file still needs
    # this exact physical blob before it's actually deleted.
    to_reclaim = (
        db.query(Chunk.hash, Node.id, Node.address)
        .join(ChunkPlacement, ChunkPlacement.chunk_id == Chunk.id)
        .join(Node, ChunkPlacement.node_id == Node.id)
        .filter(Chunk.file_id == file_id)
        .all()
    )

    chunk_ids = [row.id for row in db.query(Chunk.id).filter(Chunk.file_id == file_id).all()]
    if chunk_ids:
        db.query(ChunkPlacement).filter(ChunkPlacement.chunk_id.in_(chunk_ids)).delete(
            synchronize_session=False
        )
        db.query(Chunk).filter(Chunk.file_id == file_id).delete(synchronize_session=False)

    db.delete(file)
    db.commit()

    # Best-effort: metadata deletion is the reliable, immediate guarantee.
    # A node that's unreachable right now just keeps the bytes until the
    # periodic gc sweep catches up.
    for chunk_hash, node_id, address in to_reclaim:
        # This file's own rows are already gone, so any remaining match here
        # belongs to a different file that still needs this exact blob.
        still_needed = (
            db.query(ChunkPlacement)
            .join(Chunk, ChunkPlacement.chunk_id == Chunk.id)
            .filter(Chunk.hash == chunk_hash, ChunkPlacement.node_id == node_id)
            .first()
            is not None
        )
        if still_needed:
            continue

        try:
            _default_node_client(address).delete(f"/chunks/{chunk_hash}")
        except httpx.HTTPError as err:
            logger.warning("could not reclaim chunk %s from %s: %s", chunk_hash, address, err)


@app.post("/chunks/{chunk_id}/report-unavailable", status_code=204)
def report_chunk_unavailable(
    body: ChunkUnavailableReport,
    chunk_id: str = Path(pattern=r"^[0-9a-f]{64}$"),
    account: Account = Depends(require_session),
    db: Session = Depends(get_db),
) -> None:
    if db.get(Node, body.node_id) is None:
        raise HTTPException(status_code=404, detail="node not found")
    _record_event(
        db, "chunk_unavailable", f"chunk {chunk_id} unreachable on node {body.node_id}"
    )
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


def _compute_repair_plans(db: Session, replication_factor: int) -> list[RepairPlanOut]:
    # Each chunk's plan is computed independently, so two chunks short a
    # replica can both target the same spare node in one response. Fine for
    # a planning-only computation; whatever executes these plans needs to
    # notice a node filling up partway through and re-check before writing.
    #
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


@app.get("/replication/repair-plan", response_model=list[RepairPlanOut])
def repair_plan(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> list[RepairPlanOut]:
    replication_factor = int(get_required_setting(db, REPLICATION_FACTOR_KEY))
    return _compute_repair_plans(db, replication_factor)


def _default_node_client(address: str) -> httpx.Client:
    return httpx.Client(base_url=f"http://{address}")


def _execute_repair(
    db: Session, plan: RepairPlanOut, get_node_client: Callable[[str], httpx.Client]
) -> RepairResultOut:
    def failed_result(error: str) -> RepairResultOut:
        return RepairResultOut(
            chunk_id=plan.chunk_id,
            repaired_node_ids=[],
            failed_node_ids=list(plan.target_node_ids),
            error=error,
        )

    source_node = db.get(Node, plan.source_node_id)
    if source_node is None:
        return failed_result(f"source node {plan.source_node_id} no longer exists")

    # A down/unreachable node is the whole reason repair exists, so a
    # transport failure here must fail this one chunk, not the whole batch.
    try:
        response = get_node_client(source_node.address).get(f"/chunks/{plan.hash}")
    except httpx.HTTPError as err:
        return failed_result(f"could not reach source node: {err}")

    if response.status_code != 200:
        return failed_result(f"could not fetch chunk from source node: HTTP {response.status_code}")

    data = response.content
    if hashlib.sha256(data).hexdigest() != plan.hash:
        # The source node's own PUT already verifies this on the way in, but a
        # bit-flip on disk or in transit since then is exactly what repair
        # exists to catch — never propagate a copy that fails its own hash.
        return failed_result("source data failed hash verification")

    repaired_node_ids: list[int] = []
    failed_node_ids: list[int] = []
    for target_node_id in plan.target_node_ids:
        target_node = db.get(Node, target_node_id)
        if target_node is None:
            failed_node_ids.append(target_node_id)
            continue

        try:
            put_response = get_node_client(target_node.address).put(
                f"/chunks/{plan.hash}", content=data
            )
            ok = put_response.status_code == 200
        except httpx.HTTPError:
            ok = False

        if ok:
            db.add(ChunkPlacement(chunk_id=plan.chunk_id, node_id=target_node_id))
            repaired_node_ids.append(target_node_id)
        else:
            failed_node_ids.append(target_node_id)

    db.commit()
    return RepairResultOut(
        chunk_id=plan.chunk_id,
        repaired_node_ids=repaired_node_ids,
        failed_node_ids=failed_node_ids,
        error=None,
    )


def _record_event(db: Session, kind: str, message: str) -> None:
    db.add(Event(kind=kind, message=message))


def _record_node_state_transitions(db: Session) -> None:
    now = datetime.now(timezone.utc)
    for node in db.query(Node).all():
        current_state = state_from_heartbeat(node.last_heartbeat_at, now, node.draining).value
        if current_state != node.last_known_state:
            message = (
                f"node {node.id} ({node.address}) transitioned "
                f"{node.last_known_state} -> {current_state}"
            )
            if current_state == NodeState.DOWN.value:
                placement_count = (
                    db.query(ChunkPlacement).filter(ChunkPlacement.node_id == node.id).count()
                )
                if placement_count:
                    message += f"; {placement_count} chunk(s) were placed on it"
            _record_event(db, "node_state_transition", message)
            node.last_known_state = current_state
    db.commit()


def _execute_repair_plans(db: Session, plans: list[RepairPlanOut]) -> list[RepairResultOut]:
    if not plans:
        return []

    def run_one(plan: RepairPlanOut) -> RepairResultOut:
        # Own session per task -- SQLAlchemy sessions aren't thread-safe.
        plan_db = SessionLocal()
        try:
            return _execute_repair(plan_db, plan, _default_node_client)
        except Exception as err:
            # Fail this one chunk, not the whole batch's event log along with it.
            return RepairResultOut(
                chunk_id=plan.chunk_id,
                repaired_node_ids=[],
                failed_node_ids=list(plan.target_node_ids),
                error=str(err),
            )
        finally:
            plan_db.close()

    concurrency = _int_from_env("REPAIR_CONCURRENCY", "3")
    if concurrency <= 0:
        raise RuntimeError("REPAIR_CONCURRENCY must be positive.")

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        results = list(executor.map(run_one, plans))
    for result in results:
        if result.error is not None or result.failed_node_ids:
            message = (
                f"chunk {result.chunk_id} repair had failures: "
                f"repaired={result.repaired_node_ids} failed={result.failed_node_ids} "
                f"error={result.error}"
            )
            logger.warning(message)
        else:
            message = f"chunk {result.chunk_id} repaired onto node(s) {result.repaired_node_ids}"
            logger.info(message)
        _record_event(db, "repair", message)
    return results


@app.post("/replication/repair", response_model=list[RepairResultOut])
def execute_repairs(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> list[RepairResultOut]:
    _record_node_state_transitions(db)
    replication_factor = int(get_required_setting(db, REPLICATION_FACTOR_KEY))
    plans = _compute_repair_plans(db, replication_factor)
    results = _execute_repair_plans(db, plans)
    db.commit()
    return results


def run_one_repair_cycle() -> list[RepairResultOut]:
    """Same work as POST /replication/repair, but with its own session — for
    the background loop, which has no request-scoped one to reuse."""
    db = SessionLocal()
    try:
        _record_node_state_transitions(db)
        replication_factor = int(get_required_setting(db, REPLICATION_FACTOR_KEY))
        plans = _compute_repair_plans(db, replication_factor)
        results = _execute_repair_plans(db, plans)
        db.commit()
    finally:
        db.close()

    return results


async def repair_loop(stop: asyncio.Event, interval_seconds: float) -> None:
    # Same cooperative-stop pattern as the node's heartbeat_loop: cancelling a
    # task blocked in asyncio.to_thread stops awaiting it, not the underlying
    # OS thread, so waiting on `stop` is what lets an in-flight cycle actually
    # finish before shutdown proceeds.
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            return
        except TimeoutError:
            pass

        try:
            await asyncio.to_thread(run_one_repair_cycle)
        except Exception as err:
            # A cycle can fail in more ways than a single node heartbeat can
            # (DB errors, multiple network calls) — one bad cycle logs and
            # waits for the next interval rather than ending the loop.
            logger.warning("repair cycle failed: %s", err)


def run_one_gc_cycle() -> None:
    # Chunks younger than this are skipped: upload-to-a-node and POST /files
    # registration aren't transactional, so a very recent chunk might just be
    # awaiting registration rather than truly orphaned.
    grace_period_seconds = _int_from_env("GC_ORPHAN_GRACE_SECONDS", "600")

    db = SessionLocal()
    try:
        now = time.time()
        for node in db.query(Node).all():
            try:
                response = _default_node_client(node.address).get("/chunks")
                response.raise_for_status()
            except httpx.HTTPError as err:
                logger.warning("could not list chunks on node %s: %s", node.address, err)
                continue

            referenced_hashes = {
                row.hash
                for row in db.query(Chunk.hash)
                .join(ChunkPlacement, ChunkPlacement.chunk_id == Chunk.id)
                .filter(ChunkPlacement.node_id == node.id)
                .all()
            }

            for entry in response.json():
                orphan_hash = entry["hash"]
                if orphan_hash in referenced_hashes:
                    continue
                if now - entry["stored_at"] < grace_period_seconds:
                    continue

                try:
                    _default_node_client(node.address).delete(f"/chunks/{orphan_hash}")
                    logger.info("reclaimed orphaned chunk %s from %s", orphan_hash, node.address)
                except httpx.HTTPError as err:
                    logger.warning(
                        "could not reclaim orphaned chunk %s from %s: %s",
                        orphan_hash, node.address, err,
                    )
    finally:
        db.close()


async def gc_sweep_loop(stop: asyncio.Event, interval_seconds: float) -> None:
    # Same cooperative-stop pattern as repair_loop/heartbeat_loop.
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            return
        except TimeoutError:
            pass

        try:
            await asyncio.to_thread(run_one_gc_cycle)
        except Exception as err:
            logger.warning("gc sweep cycle failed: %s", err)
