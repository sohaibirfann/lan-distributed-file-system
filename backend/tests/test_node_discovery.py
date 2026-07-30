import socket

import pytest


class FakeInfo:
    def __init__(self, ip: str, port: int):
        self.addresses = [socket.inet_aton(ip)]
        self.port = port


class FakeZeroconf:
    def __init__(self, services: dict[str, tuple[str, int]]):
        self._services = services

    def get_service_info(self, type_, name):
        ip, port = self._services[name]
        return FakeInfo(ip, port)

    def close(self):
        pass


class FakeServiceBrowser:
    def __init__(self, zc, type_, listener):
        for name in zc._services:
            listener.add_service(zc, type_, name)

    def cancel(self):
        pass


def _patch_discovery(monkeypatch, services: dict[str, tuple[str, int]]):
    import node.discovery as discovery_module

    monkeypatch.setattr(discovery_module, "Zeroconf", lambda: FakeZeroconf(services))
    monkeypatch.setattr(discovery_module, "ServiceBrowser", FakeServiceBrowser)


def test_discover_returns_the_single_coordinator_found(monkeypatch):
    from node.discovery import discover_coordinator_address

    _patch_discovery(monkeypatch, {"coordinator-a": ("192.168.1.10", 8000)})

    address = discover_coordinator_address(timeout_seconds=0.2)

    assert address == "http://192.168.1.10:8000"


def test_discover_raises_when_nothing_is_found(monkeypatch):
    from node.discovery import discover_coordinator_address

    _patch_discovery(monkeypatch, {})

    with pytest.raises(RuntimeError, match="No coordinator found"):
        discover_coordinator_address(timeout_seconds=0.2)


def test_discover_raises_when_multiple_coordinators_are_found(monkeypatch):
    from node.discovery import discover_coordinator_address

    _patch_discovery(
        monkeypatch,
        {"coordinator-a": ("192.168.1.10", 8000), "coordinator-b": ("192.168.1.11", 8000)},
    )

    with pytest.raises(RuntimeError, match="Multiple coordinators found") as exc_info:
        discover_coordinator_address(timeout_seconds=0.2)

    assert "192.168.1.10:8000" in str(exc_info.value)
    assert "192.168.1.11:8000" in str(exc_info.value)
