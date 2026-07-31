from __future__ import annotations

import asyncio
import logging
import time

import httpx

import coordinator.replication as replication
from coordinator.db import SessionLocal
from coordinator.models import Chunk, ChunkPlacement, Node
from coordinator.settings import _int_from_env

logger = logging.getLogger(__name__)


def run_one_gc_cycle() -> None:
    # A very recent chunk may just be awaiting POST /files, not truly orphaned.
    grace_period_seconds = _int_from_env("GC_ORPHAN_GRACE_SECONDS", "600")

    db = SessionLocal()
    try:
        now = time.time()
        for node in db.query(Node).all():
            try:
                response = replication._default_node_client(node.address).get(
                    "/chunks", headers=replication._bearer(node.chunk_token)
                )
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
                    replication._default_node_client(node.address).delete(
                        f"/chunks/{orphan_hash}", headers=replication._bearer(node.chunk_token)
                    )
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
