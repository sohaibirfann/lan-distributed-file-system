from __future__ import annotations

import socket
import threading
import time

from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

SERVICE_TYPE = "_dfs-coordinator._tcp.local."


class _CoordinatorListener(ServiceListener):
    def __init__(self) -> None:
        self.addresses: list[str] = []

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info is not None and info.addresses:
            self.addresses.append(f"http://{socket.inet_ntoa(info.addresses[0])}:{info.port}")

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass


def _browse(timeout_seconds: float) -> list[str]:
    zc = Zeroconf()
    listener = _CoordinatorListener()
    browser = ServiceBrowser(zc, SERVICE_TYPE, listener)
    try:
        # Waits out the full window even after an early hit, so a second coordinator
        # (multi-found is an error) still has a chance to answer.
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        browser.cancel()
        zc.close()
    return sorted(set(listener.addresses))


def discover_coordinator_address(timeout_seconds: float = 5.0) -> str:
    # zeroconf's sync API deadlocks when called from a thread with a running event
    # loop (node/app.py's lifespan does, via load_config()); a plain thread avoids that.
    result: list[str] = []
    error: BaseException | None = None

    def _run() -> None:
        nonlocal result, error
        try:
            result = _browse(timeout_seconds)
        except BaseException as exc:
            error = exc

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join()

    if error is not None:
        raise error

    if not result:
        raise RuntimeError(
            f"No coordinator found via mDNS discovery within {timeout_seconds}s; "
            "set COORDINATOR_ADDRESS explicitly."
        )
    if len(result) > 1:
        raise RuntimeError(
            f"Multiple coordinators found via mDNS discovery: {', '.join(result)}; "
            "set COORDINATOR_ADDRESS explicitly to choose one."
        )
    return result[0]
