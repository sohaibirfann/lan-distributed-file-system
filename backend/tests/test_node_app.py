import pytest
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("STORAGE_DIRECTORY", str(tmp_path / "storage"))
    monkeypatch.setenv("CAPACITY_BUDGET_GB", "10")
    monkeypatch.setenv("COORDINATOR_ADDRESS", "http://coordinator.invalid")
    monkeypatch.setenv("NODE_ADDRESS", "node.invalid:9000")
    monkeypatch.setenv("OWNER_USERNAME", "alice")
    monkeypatch.setenv("OWNER_PASSWORD", "hunter22")
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import node.app as app_module

    # These tests are about the app/config, not the coordinator round trip —
    # that gets its own test against a real coordinator instance.
    monkeypatch.setattr(app_module, "register_with_coordinator", lambda config, client: None)

    return TestClient(app_module.app)


def test_health(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        assert c.get("/health").json() == {"status": "ok"}


def test_creates_storage_directory_if_missing(tmp_path, monkeypatch):
    storage_dir = tmp_path / "does-not-exist-yet"
    with _client(tmp_path, monkeypatch, STORAGE_DIRECTORY=str(storage_dir)):
        assert storage_dir.is_dir()


def test_missing_storage_directory_fails_startup(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_DIRECTORY", raising=False)
    monkeypatch.setenv("CAPACITY_BUDGET_GB", "10")

    from node.app import app

    with pytest.raises(RuntimeError, match="STORAGE_DIRECTORY"):
        with TestClient(app):
            pass


@pytest.mark.parametrize("capacity", ["0", "-5", "abc", ""])
def test_invalid_capacity_budget_fails_startup(tmp_path, monkeypatch, capacity):
    monkeypatch.setenv("STORAGE_DIRECTORY", str(tmp_path / "storage"))
    monkeypatch.setenv("CAPACITY_BUDGET_GB", capacity)

    from node.app import app

    with pytest.raises(RuntimeError, match="CAPACITY_BUDGET_GB"):
        with TestClient(app):
            pass


def test_heartbeat_interval_defaults_to_ten_seconds(tmp_path, monkeypatch):
    from node.config import load_config

    monkeypatch.setenv("STORAGE_DIRECTORY", str(tmp_path / "storage"))
    monkeypatch.setenv("CAPACITY_BUDGET_GB", "10")
    monkeypatch.setenv("COORDINATOR_ADDRESS", "http://coordinator.invalid")
    monkeypatch.setenv("NODE_ADDRESS", "node.invalid:9000")
    monkeypatch.setenv("OWNER_USERNAME", "alice")
    monkeypatch.setenv("OWNER_PASSWORD", "hunter22")
    monkeypatch.delenv("HEARTBEAT_INTERVAL_SECONDS", raising=False)

    assert load_config().heartbeat_interval_seconds == 10


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_invalid_heartbeat_interval_fails_startup(tmp_path, monkeypatch, value):
    monkeypatch.setenv("STORAGE_DIRECTORY", str(tmp_path / "storage"))
    monkeypatch.setenv("CAPACITY_BUDGET_GB", "10")
    monkeypatch.setenv("COORDINATOR_ADDRESS", "http://coordinator.invalid")
    monkeypatch.setenv("NODE_ADDRESS", "node.invalid:9000")
    monkeypatch.setenv("OWNER_USERNAME", "alice")
    monkeypatch.setenv("OWNER_PASSWORD", "hunter22")
    monkeypatch.setenv("HEARTBEAT_INTERVAL_SECONDS", value)

    from node.app import app

    with pytest.raises(RuntimeError, match="HEARTBEAT_INTERVAL_SECONDS"):
        with TestClient(app):
            pass


def test_heartbeat_interval_at_or_above_suspect_threshold_fails_startup(tmp_path, monkeypatch):
    from shared.placement import SUSPECT_THRESHOLD

    monkeypatch.setenv("STORAGE_DIRECTORY", str(tmp_path / "storage"))
    monkeypatch.setenv("CAPACITY_BUDGET_GB", "10")
    monkeypatch.setenv("COORDINATOR_ADDRESS", "http://coordinator.invalid")
    monkeypatch.setenv("NODE_ADDRESS", "node.invalid:9000")
    monkeypatch.setenv("OWNER_USERNAME", "alice")
    monkeypatch.setenv("OWNER_PASSWORD", "hunter22")
    monkeypatch.setenv("HEARTBEAT_INTERVAL_SECONDS", str(int(SUSPECT_THRESHOLD.total_seconds())))

    from node.app import app

    with pytest.raises(RuntimeError, match="suspect threshold"):
        with TestClient(app):
            pass


@pytest.mark.parametrize(
    "var", ["COORDINATOR_ADDRESS", "NODE_ADDRESS", "OWNER_USERNAME", "OWNER_PASSWORD"]
)
def test_missing_required_var_fails_startup(tmp_path, monkeypatch, var):
    monkeypatch.setenv("STORAGE_DIRECTORY", str(tmp_path / "storage"))
    monkeypatch.setenv("CAPACITY_BUDGET_GB", "10")
    monkeypatch.setenv("COORDINATOR_ADDRESS", "http://coordinator.invalid")
    monkeypatch.setenv("NODE_ADDRESS", "node.invalid:9000")
    monkeypatch.setenv("OWNER_USERNAME", "alice")
    monkeypatch.setenv("OWNER_PASSWORD", "hunter22")
    monkeypatch.delenv(var, raising=False)

    from node.app import app

    with pytest.raises(RuntimeError, match=var):
        with TestClient(app):
            pass


def test_coordinator_address_without_scheme_fails_startup(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="COORDINATOR_ADDRESS"):
        with _client(tmp_path, monkeypatch, COORDINATOR_ADDRESS="coordinator.invalid:8000"):
            pass


def test_register_with_coordinator_raises_when_coordinator_is_unreachable(tmp_path):
    import httpx

    from node.config import NodeConfig
    from node.registration import register_with_coordinator

    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = NodeConfig(
        storage_directory=storage_dir,
        capacity_budget_bytes=10 * 1024**3,
        coordinator_address="http://127.0.0.1:1",  # nothing listens on port 1
        node_address="192.168.1.5:9000",
        owner_username="alice",
        owner_password="hunter22",
    )

    with httpx.Client(base_url=config.coordinator_address, timeout=2.0) as client:
        # A closed port is refused instantly on some networks, timed out on
        # others (observed on Windows) — either way it must fail cleanly.
        with pytest.raises(httpx.TransportError):
            register_with_coordinator(config, client)


def test_register_with_coordinator_creates_a_node_row(client, tmp_path):
    # `client` is the coordinator's own TestClient, which is an httpx.Client —
    # register_with_coordinator only needs something that can .post(), so the
    # real coordinator app runs in-process with no network and no mocking.
    from tests.test_auth import register as register_account

    from node.config import NodeConfig
    from node.registration import register_with_coordinator

    register_account(client)
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = NodeConfig(
        storage_directory=storage_dir,
        capacity_budget_bytes=10 * 1024**3,
        coordinator_address="unused-client-is-already-bound",
        node_address="192.168.1.5:9000",
        owner_username="alice",
        owner_password="hunter22",
    )

    register_with_coordinator(config, client)

    addresses = {n["address"] for n in client.get("/nodes").json()}
    assert "192.168.1.5:9000" in addresses


def test_register_with_coordinator_reports_real_disk_usage(client, tmp_path):
    from tests.test_auth import register as register_account

    from node.config import NodeConfig
    from node.registration import register_with_coordinator

    register_account(client)
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    (storage_dir / "chunk-1").write_bytes(b"x" * 1000)
    (storage_dir / "subdir").mkdir()
    (storage_dir / "subdir" / "chunk-2").write_bytes(b"y" * 500)
    config = NodeConfig(
        storage_directory=storage_dir,
        capacity_budget_bytes=10 * 1024**3,
        coordinator_address="unused-client-is-already-bound",
        node_address="192.168.1.5:9000",
        owner_username="alice",
        owner_password="hunter22",
    )

    register_with_coordinator(config, client)

    node = next(n for n in client.get("/nodes").json() if n["address"] == "192.168.1.5:9000")
    assert node["used_bytes"] == 1500


def test_register_with_coordinator_raises_on_bad_credentials(client, tmp_path):
    from node.config import NodeConfig
    from node.registration import register_with_coordinator
    import httpx

    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    config = NodeConfig(
        storage_directory=storage_dir,
        capacity_budget_bytes=10 * 1024**3,
        coordinator_address="unused-client-is-already-bound",
        node_address="192.168.1.5:9000",
        owner_username="nobody",
        owner_password="wrong",
    )

    with pytest.raises(httpx.HTTPStatusError):
        register_with_coordinator(config, client)
