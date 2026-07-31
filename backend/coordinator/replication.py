from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from coordinator.auth import require_session
from coordinator.db import get_db, SessionLocal
from coordinator.events import record_event
from coordinator.models import Account, Chunk, ChunkPlacement, Node
from coordinator.schemas import ChunkHealthOut, RepairPlanOut, RepairResultOut
from coordinator.settings import (
    MAX_FILE_SIZE_BYTES_KEY,
    REPLICATION_FACTOR_KEY,
    WRITE_QUORUM_KEY,
    _int_from_env,
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

    # A transport failure here fails only this chunk, not the whole batch.
    try:
        response = get_node_client(source_node.address).get(f"/chunks/{plan.hash}")
    except httpx.HTTPError as err:
        return failed_result(f"could not reach source node: {err}")

    if response.status_code != 200:
        return failed_result(f"could not fetch chunk from source node: HTTP {response.status_code}")

    data = response.content
    if hashlib.sha256(data).hexdigest() != plan.hash:
        # Catches bit-rot since the source's own PUT already verified it once.
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
            record_event(db, "node_state_transition", message)
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
        record_event(db, "repair", message)
    return results


@router.post("/replication/repair", response_model=list[RepairResultOut])
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
    # Cancelling a task in asyncio.to_thread stops awaiting it, not the OS thread,
    # so waiting on `stop` is what lets an in-flight cycle actually finish first.
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            return
        except TimeoutError:
            pass

        try:
            await asyncio.to_thread(run_one_repair_cycle)
        except Exception as err:
            # One bad cycle logs and waits for the next interval, not ends the loop.
            logger.warning("repair cycle failed: %s", err)


def run_one_gc_cycle() -> None:
    # A very recent chunk may just be awaiting POST /files, not truly orphaned.
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
