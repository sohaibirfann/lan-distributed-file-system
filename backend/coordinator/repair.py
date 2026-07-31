from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import coordinator.replication as replication
from coordinator.db import SessionLocal
from coordinator.events import record_event
from coordinator.models import ChunkPlacement, Node
from coordinator.replication import _compute_repair_plans
from coordinator.schemas import RepairPlanOut, RepairResultOut
from coordinator.settings import REPLICATION_FACTOR_KEY, _int_from_env, get_required_setting
from shared.placement import NodeState, state_from_heartbeat

logger = logging.getLogger(__name__)


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
        response = get_node_client(source_node.address).get(
            f"/chunks/{plan.hash}", headers=replication._bearer(source_node.chunk_token)
        )
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
                f"/chunks/{plan.hash}", content=data, headers=replication._bearer(target_node.chunk_token)
            )
            ok = put_response.status_code == 200
        except httpx.HTTPError:
            ok = False

        if ok:
            try:
                with db.begin_nested():
                    db.add(ChunkPlacement(chunk_id=plan.chunk_id, node_id=target_node_id))
            except IntegrityError:
                pass  # a concurrent repair cycle already recorded this same placement
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
            return _execute_repair(plan_db, plan, replication._default_node_client)
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
