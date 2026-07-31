from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from coordinator.auth import require_session
from coordinator.db import get_db, SessionLocal
from coordinator.events import record_event
from coordinator.models import Account, Chunk, ChunkPlacement, Event, Node
from coordinator.replication import (
    _compute_repair_plans,
    _default_node_client,
    _execute_repair_plans,
    _record_node_state_transitions,
)
from coordinator.schemas import (
    EventOut,
    NodeDrainOut,
    NodeDrainRequest,
    NodeHeartbeatRequest,
    NodeOut,
    NodeRegisterRequest,
)
from coordinator.settings import REPLICATION_FACTOR_KEY, WRITE_QUORUM_KEY, get_required_setting
from shared.placement import Node as PlacementNode
from shared.placement import NodeState, build_ring, placement_candidates, state_from_heartbeat

logger = logging.getLogger(__name__)

router = APIRouter()


def _find_node(db: Session, address: str) -> Node | None:
    return db.query(Node).filter(Node.address == address).first()


def _apply_report(node: Node, body: NodeRegisterRequest) -> None:
    node.capacity_budget_bytes = body.capacity_budget_bytes
    node.free_disk_bytes = body.free_disk_bytes
    node.used_bytes = body.used_bytes
    node.last_heartbeat_at = datetime.now(timezone.utc)
    node.draining = False  # a node that re-registers has come back, not left


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
        # DRAINING doesn't count either, or two nodes draining at once could both release.
        other_permanent_count = sum(
            1
            for p in other_placements
            if p.node_id in node_states and node_states[p.node_id] not in (NodeState.DOWN, NodeState.DRAINING)
        )
        if other_permanent_count < replication_factor:
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

    # Two Chunk rows can share a hash with independent placements, so count rows,
    # not distinct hashes -- memoized since the shift check is the same either way.
    shifted_by_hash: dict[str, bool] = {}
    moved = 0
    for (chunk_hash,) in db.query(Chunk.hash).all():
        if chunk_hash not in shifted_by_hash:
            before_set = placement_candidates(before_ring, before_by_id, chunk_hash, replication_factor)
            after_set = placement_candidates(after_ring, after_by_id, chunk_hash, replication_factor)
            shifted_by_hash[chunk_hash] = set(before_set) != set(after_set)
        if shifted_by_hash[chunk_hash]:
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
            record_event(
                db,
                "placement_shift",
                f"node {node.id} ({node.address}) joined; {moved} chunk(s) would now map to different placement",
            )
            db.commit()
    finally:
        db.close()


@router.post("/nodes/register", response_model=NodeOut, status_code=201)
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
        if node.last_known_state != NodeState.UP.value:
            record_event(
                db,
                "node_state_transition",
                f"node {node.id} ({node.address}) transitioned {node.last_known_state} -> up",
            )
            node.last_known_state = NodeState.UP.value
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


@router.post("/nodes/drain", response_model=NodeDrainOut)
def drain_node(
    body: NodeDrainRequest,
    account: Account = Depends(require_session),
    db: Session = Depends(get_db),
) -> NodeDrainOut:
    node = _find_node(db, body.address)
    if node is None or node.owner_account_id != account.id:
        raise HTTPException(status_code=404, detail="Node is not registered")

    node.draining = True
    db.commit()

    _record_node_state_transitions(db)
    replication_factor = int(get_required_setting(db, REPLICATION_FACTOR_KEY))
    plans = _compute_repair_plans(db, replication_factor)
    _execute_repair_plans(db, plans)
    db.commit()

    _release_surplus_placements(db, node)

    remaining = db.query(ChunkPlacement).filter(ChunkPlacement.node_id == node.id).count()
    return NodeDrainOut(remaining_chunks=remaining)


@router.post("/nodes/heartbeat", response_model=NodeOut)
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


@router.get("/nodes", response_model=list[NodeOut])
def list_nodes(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> list[Node]:
    return db.query(Node).order_by(Node.id).all()


@router.get("/events", response_model=list[EventOut])
def list_events(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> list[Event]:
    return db.query(Event).order_by(Event.id.desc()).limit(200).all()


@router.get("/placement/{chunk_id}", response_model=list[NodeOut])
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
