from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from coordinator.auth import require_session
from coordinator.db import get_db
from coordinator.models import Account, Chunk, ChunkPlacement, Node
from coordinator.schemas import ChunkHealthOut, RepairPlanOut, RepairResultOut
from coordinator.settings import (
    MAX_FILE_SIZE_BYTES_KEY,
    REPLICATION_FACTOR_KEY,
    WRITE_QUORUM_KEY,
    get_required_setting,
)
from shared.placement import Node as PlacementNode
from shared.placement import NodeState, build_ring, placement_candidates, state_from_heartbeat

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/config/replication")
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
        # A down node is still "registered", so this alone can hide an active outage.
        "under_replicated": len(nodes) < replication_factor,
        "eligible_node_count": eligible_node_count,
        "write_available": eligible_node_count >= write_quorum,
    }


@router.get("/replication/health", response_model=list[ChunkHealthOut])
def replication_health(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> list[ChunkHealthOut]:
    replication_factor = int(get_required_setting(db, REPLICATION_FACTOR_KEY))

    # Snapshotted once so every chunk here is judged against the same instant.
    now = datetime.now(timezone.utc)
    node_states = {
        node.id: state_from_heartbeat(node.last_heartbeat_at, now, node.draining)
        for node in db.query(Node).all()
    }

    at_risk = []
    for chunk in db.query(Chunk).all():
        placements = db.query(ChunkPlacement).filter(ChunkPlacement.chunk_id == chunk.id).all()
        # DOWN is presumed lost; DRAINING won't stay a replica either, so neither counts.
        healthy_node_ids = [
            placement.node_id
            for placement in placements
            if placement.node_id in node_states
            and node_states[placement.node_id] not in (NodeState.DOWN, NodeState.DRAINING)
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
    # Plans are independent, so two chunks can target the same spare node; the
    # executor must re-check capacity rather than trust this planning snapshot.
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
        servable_node_ids = [
            node_id
            for node_id in placed_node_ids
            if node_id in node_states and node_states[node_id] is not NodeState.DOWN
        ]
        permanent_node_ids = [
            node_id for node_id in servable_node_ids if node_states[node_id] is not NodeState.DRAINING
        ]

        needed = replication_factor - len(permanent_node_ids)
        if needed <= 0 or not servable_node_ids:
            continue  # fully healthy, or nothing left to copy from

        # Drop nodes that already hold this chunk, then take only what's needed.
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
                source_node_id=min(servable_node_ids),  # arbitrary tiebreaker, not a latency pick
                target_node_ids=target_node_ids,
            )
        )

    return plans


@router.get("/replication/repair-plan", response_model=list[RepairPlanOut])
def repair_plan(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> list[RepairPlanOut]:
    replication_factor = int(get_required_setting(db, REPLICATION_FACTOR_KEY))
    return _compute_repair_plans(db, replication_factor)


_node_clients: dict[str, httpx.Client] = {}
_node_clients_lock = threading.Lock()


def _default_node_client(address: str) -> httpx.Client:
    # One persistent client per node address, reused for the life of the
    # process -- a fresh client per call was never closed, leaking sockets
    # under sustained load.
    with _node_clients_lock:
        client = _node_clients.get(address)
        if client is None:
            client = httpx.Client(base_url=f"http://{address}")
            _node_clients[address] = client
        return client


def _bearer(chunk_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {chunk_token}"}


@router.post("/replication/repair", response_model=list[RepairResultOut])
def execute_repairs(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> list[RepairResultOut]:
    from coordinator.repair import _execute_repair_plans, _record_node_state_transitions

    _record_node_state_transitions(db)
    replication_factor = int(get_required_setting(db, REPLICATION_FACTOR_KEY))
    plans = _compute_repair_plans(db, replication_factor)
    results = _execute_repair_plans(db, plans)
    db.commit()
    return results
