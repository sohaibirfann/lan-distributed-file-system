from __future__ import annotations

import socket

from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

SERVICE_TYPE = "_dfs-coordinator._tcp.local."


def _local_ip() -> str:
    # No traffic actually sent; just asks the OS which interface reaches the internet.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


async def start_mdns_advertisement(port: int) -> tuple[AsyncZeroconf, ServiceInfo]:
    # The sync Zeroconf API deadlocks when called from a thread with a running event
    # loop (app.py's lifespan) -- AsyncZeroconf is zeroconf's own way around that.
    ip = _local_ip()
    info = ServiceInfo(
        SERVICE_TYPE,
        f"coordinator-{ip}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(ip)],
        port=port,
    )
    azc = AsyncZeroconf()
    await azc.async_register_service(info)
    return azc, info
