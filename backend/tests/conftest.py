import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "coordinator.db"
    monkeypatch.setenv("COORDINATOR_DB_PATH", str(db_path))
    monkeypatch.setenv("NAMESPACE_PASSPHRASE", "correct horse battery staple")

    import coordinator.app as app_module
    import coordinator.db as db_module
    import coordinator.models as models_module

    importlib.reload(db_module)
    importlib.reload(models_module)
    importlib.reload(app_module)

    with TestClient(app_module.app) as test_client:
        yield test_client
