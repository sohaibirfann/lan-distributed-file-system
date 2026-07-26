from __future__ import annotations

import asyncio
import logging
import shutil

import httpx

from node.config import NodeConfig
from node.registration import measure_used_bytes, raise_with_coordinator_detail

logger = logging.getLogger(__name__)


def send_heartbeat(config: NodeConfig, client: httpx.Client) -> httpx.Response:
    free_disk_bytes = shutil.disk_usage(config.storage_directory).free
    response = client.post(
        "/nodes/heartbeat",
        json={
            "address": config.node_address,
            "free_disk_bytes": free_disk_bytes,
            "used_bytes": measure_used_bytes(config.storage_directory),
        },
    )
    raise_with_coordinator_detail(response)
    return response


async def heartbeat_loop(config: NodeConfig, client: httpx.Client, stop: asyncio.Event) -> None:
    # Cancelling a task blocked in asyncio.to_thread stops awaiting it, not the
    # underlying OS thread — the caller's client could be closed while a beat
    # is still in flight. Waiting on `stop` cooperatively means the last
    # send_heartbeat call is always allowed to actually finish before this
    # returns, so the caller can safely close the client right after.
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.heartbeat_interval_seconds)
            return
        except TimeoutError:
            pass

        try:
            await asyncio.to_thread(send_heartbeat, config, client)
        except httpx.HTTPError as err:
            # A missed beat is exactly what the coordinator's own elapsed-time
            # detection is for — log it and try again next interval.
            logger.warning("heartbeat to coordinator failed: %s", err)
