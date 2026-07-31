import hashlib

import pytest

from tests.test_node_app import _client
from tests.test_nodes import chunk_auth_headers


def chunk_id_for(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_upload_then_download_round_trip(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        data = b"ciphertext bytes here"
        chunk_id = chunk_id_for(data)

        upload = c.put(f"/chunks/{chunk_id}", content=data, headers=chunk_auth_headers())
        assert upload.status_code == 200

        download = c.get(f"/chunks/{chunk_id}", headers=chunk_auth_headers())
        assert download.status_code == 200
        assert download.content == data


def test_download_missing_chunk_is_404(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        assert c.get(f"/chunks/{'a' * 64}", headers=chunk_auth_headers()).status_code == 404


def test_upload_hash_mismatch_is_400(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        response = c.put(f"/chunks/{'a' * 64}", content=b"wrong content", headers=chunk_auth_headers())
        assert response.status_code == 400


def test_upload_invalid_chunk_id_is_422(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        response = c.put("/chunks/not-a-valid-hash", content=b"data", headers=chunk_auth_headers())
        assert response.status_code == 422


def test_download_invalid_chunk_id_is_422(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        assert c.get("/chunks/not-a-valid-hash", headers=chunk_auth_headers()).status_code == 422


def test_upload_over_capacity_is_507(tmp_path, monkeypatch):
    # The capacity-limit logic itself (tiny byte-level budgets, no HTTP) is
    # already covered by test_node_chunks.py; this only checks that the
    # exception maps to the right status code, without a multi-GB payload.
    import node.app as app_module
    from node.chunks import InsufficientCapacity

    def fake_store(config, chunk_id, data):
        raise InsufficientCapacity("no room")

    monkeypatch.setattr(app_module, "store_chunk", fake_store)

    with _client(tmp_path, monkeypatch) as c:
        data = b"x" * 100
        response = c.put(f"/chunks/{chunk_id_for(data)}", content=data, headers=chunk_auth_headers())
        assert response.status_code == 507


def test_upload_over_max_size_is_413(tmp_path, monkeypatch):
    import node.app as app_module

    monkeypatch.setattr(app_module, "MAX_CHUNK_BYTES", 10)

    with _client(tmp_path, monkeypatch) as c:
        data = b"x" * 100
        response = c.put(f"/chunks/{chunk_id_for(data)}", content=data, headers=chunk_auth_headers())
        assert response.status_code == 413


def test_delete_then_download_is_404(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        data = b"ciphertext bytes here"
        chunk_id = chunk_id_for(data)
        c.put(f"/chunks/{chunk_id}", content=data, headers=chunk_auth_headers())

        delete = c.delete(f"/chunks/{chunk_id}", headers=chunk_auth_headers())

        assert delete.status_code == 204
        assert c.get(f"/chunks/{chunk_id}", headers=chunk_auth_headers()).status_code == 404


def test_delete_of_a_never_stored_chunk_is_still_204(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        assert c.delete(f"/chunks/{'a' * 64}", headers=chunk_auth_headers()).status_code == 204


def test_delete_invalid_chunk_id_is_422(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        assert c.delete("/chunks/not-a-valid-hash", headers=chunk_auth_headers()).status_code == 422


@pytest.mark.parametrize(
    "make_request",
    [
        lambda c, cid, headers: c.get("/chunks", headers=headers),
        lambda c, cid, headers: c.put(f"/chunks/{cid}", content=b"x" * 32, headers=headers),
        lambda c, cid, headers: c.get(f"/chunks/{cid}", headers=headers),
        lambda c, cid, headers: c.delete(f"/chunks/{cid}", headers=headers),
    ],
)
def test_chunk_endpoints_require_the_correct_bearer_token(tmp_path, monkeypatch, make_request):
    with _client(tmp_path, monkeypatch) as c:
        chunk_id = chunk_id_for(b"x" * 32)

        assert make_request(c, chunk_id, None).status_code == 401
        assert make_request(c, chunk_id, {"Authorization": "Bearer wrong-token"}).status_code == 401


def test_list_chunks_reflects_stored_and_deleted_chunks(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        data = b"ciphertext bytes here"
        chunk_id = chunk_id_for(data)

        assert c.get("/chunks", headers=chunk_auth_headers()).json() == []

        c.put(f"/chunks/{chunk_id}", content=data, headers=chunk_auth_headers())
        listed = c.get("/chunks", headers=chunk_auth_headers()).json()
        assert [entry["hash"] for entry in listed] == [chunk_id]
        assert isinstance(listed[0]["stored_at"], (int, float))

        c.delete(f"/chunks/{chunk_id}", headers=chunk_auth_headers())
        assert c.get("/chunks", headers=chunk_auth_headers()).json() == []


def test_upload_over_max_size_without_content_length_is_still_413(tmp_path, monkeypatch):
    # A streamed body has no Content-Length header, so this only passes if
    # the post-read size check (not just the pre-read one) actually works.
    import node.app as app_module

    monkeypatch.setattr(app_module, "MAX_CHUNK_BYTES", 10)

    with _client(tmp_path, monkeypatch) as c:
        data = b"x" * 100

        def stream():
            yield data

        response = c.put(f"/chunks/{chunk_id_for(data)}", content=stream(), headers=chunk_auth_headers())
        assert "content-length" not in {k.lower() for k in response.request.headers}
        assert response.status_code == 413
