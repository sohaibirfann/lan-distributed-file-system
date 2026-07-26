import importlib

import pytest
from fastapi.testclient import TestClient

from tests.test_auth import NAMESPACE_PASSPHRASE, register
from tests.test_files import chunk_hash
from tests.test_nodes import login, register_node
from tests.test_replication_config import _fresh_client


def test_max_file_size_is_served_via_config(client):
    register(client)
    login(client)

    assert client.get("/config/replication").json()["max_file_size_bytes"] == 10 * 1024**3


def test_create_file_over_the_configured_limit_is_rejected(tmp_path, monkeypatch):
    app_module = _fresh_client(tmp_path, monkeypatch, MAX_FILE_SIZE_BYTES="100")

    with TestClient(app_module.app) as c:
        register(c)
        login(c)
        node_id = register_node(c).json()["id"]

        response = c.post(
            "/files",
            json={
                "name": "too-big.bin",
                "size_bytes": 101,
                "chunks": [
                    {
                        "sequence_index": 0,
                        "hash": chunk_hash("chunk-0"),
                        "size_bytes": 101,
                        "node_ids": [node_id],
                    }
                ],
            },
        )

        assert response.status_code == 413


def test_create_file_at_exactly_the_limit_is_accepted(tmp_path, monkeypatch):
    app_module = _fresh_client(tmp_path, monkeypatch, MAX_FILE_SIZE_BYTES="100")

    with TestClient(app_module.app) as c:
        register(c)
        login(c)
        node_id = register_node(c).json()["id"]

        response = c.post(
            "/files",
            json={
                "name": "exactly-at-limit.bin",
                "size_bytes": 100,
                "chunks": [
                    {
                        "sequence_index": 0,
                        "hash": chunk_hash("chunk-0"),
                        "size_bytes": 100,
                        "node_ids": [node_id],
                    }
                ],
            },
        )

        assert response.status_code == 201


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_invalid_max_file_size_fails_startup(tmp_path, monkeypatch, value):
    app_module = _fresh_client(tmp_path, monkeypatch, MAX_FILE_SIZE_BYTES=value)

    with pytest.raises(RuntimeError, match="MAX_FILE_SIZE_BYTES"):
        with TestClient(app_module.app):
            pass
