from datetime import datetime, timedelta, timezone

NAMESPACE_PASSPHRASE = "correct horse battery staple"


def sign_session_token(payload):
    import jwt

    import coordinator.app as app_module
    from coordinator.db import SessionLocal
    from coordinator.security import JWT_ALGORITHM

    db = SessionLocal()
    try:
        secret = app_module.get_setting(db, app_module.JWT_SECRET_KEY_KEY)
    finally:
        db.close()
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def future_exp():
    return datetime.now(timezone.utc) + timedelta(days=1)


def register(client, username="alice", password="hunter22", passphrase=NAMESPACE_PASSPHRASE):
    return client.post(
        "/register",
        json={
            "username": username,
            "password": password,
            "namespace_passphrase": passphrase,
        },
    )


def test_register_success(client):
    response = register(client)

    assert response.status_code == 201
    body = response.json()
    assert body["username"] == "alice"
    assert "password" not in body
    assert "password_hash" not in body


def test_register_rejects_wrong_namespace_passphrase_before_creating_account(client):
    response = register(client, passphrase="wrong passphrase")
    assert response.status_code == 401

    login_response = client.post(
        "/login", json={"username": "alice", "password": "hunter22"}
    )
    assert login_response.status_code == 401


def test_register_rejects_duplicate_username(client):
    first = register(client)
    assert first.status_code == 201

    second = register(client)
    assert second.status_code == 409


def test_login_success_sets_session_cookie(client):
    register(client)

    response = client.post("/login", json={"username": "alice", "password": "hunter22"})

    assert response.status_code == 200
    assert "session" in response.cookies
    body = response.json()
    assert body["username"] == "alice"
    assert "password" not in body
    assert "password_hash" not in body


def test_login_rejects_wrong_password(client):
    register(client)

    response = client.post("/login", json={"username": "alice", "password": "wrong"})

    assert response.status_code == 401


def test_login_rejects_unknown_username(client):
    response = client.post("/login", json={"username": "nobody", "password": "hunter22"})

    assert response.status_code == 401


def test_protected_endpoint_requires_session(client):
    response = client.get("/me")
    assert response.status_code == 401


def test_protected_endpoint_works_with_valid_session(client):
    register(client)
    client.post("/login", json={"username": "alice", "password": "hunter22"})

    response = client.get("/me")

    assert response.status_code == 200
    assert response.json()["username"] == "alice"


def test_health_does_not_require_session(client):
    response = client.get("/health")
    assert response.status_code == 200


def test_no_route_is_reachable_without_a_session(client):
    """Walks the route table instead of checking endpoints one by one, so a
    future endpoint is covered automatically without touching this test."""
    from coordinator.app import PUBLIC_PATHS, app

    checked = 0
    for route in app.routes:
        path = getattr(route, "path", None)
        if path is None or path in PUBLIC_PATHS or "{" in path:
            continue
        for method in getattr(route, "methods", set()) & {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            response = client.request(method, path)
            assert response.status_code == 401, f"{method} {path} returned {response.status_code}"
            checked += 1

    assert checked, "route table walk matched nothing, so this test proved nothing"


def test_interactive_docs_are_not_exposed(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_token_missing_sub_claim_is_rejected(client):
    client.cookies.set("session", sign_session_token({"exp": future_exp()}))

    assert client.get("/me").status_code == 401


def test_token_with_non_numeric_sub_is_rejected(client):
    client.cookies.set("session", sign_session_token({"sub": "abc", "exp": future_exp()}))

    assert client.get("/me").status_code == 401


def test_token_without_expiry_is_rejected(client):
    register(client)  # so the rejection is about the missing exp, not an unknown account
    client.cookies.set("session", sign_session_token({"sub": "1"}))

    assert client.get("/me").status_code == 401


def test_expired_token_is_rejected(client):
    register(client)
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    client.cookies.set("session", sign_session_token({"sub": "1", "exp": expired}))

    assert client.get("/me").status_code == 401


def test_token_for_deleted_account_is_rejected(client):
    client.cookies.set("session", sign_session_token({"sub": "9999", "exp": future_exp()}))

    assert client.get("/me").status_code == 401


def test_namespace_salt_requires_session(client):
    response = client.get("/namespace/salt")
    assert response.status_code == 401


def test_namespace_salt_served_to_authenticated_session(client):
    register(client)
    client.post("/login", json={"username": "alice", "password": "hunter22"})

    response = client.get("/namespace/salt")

    assert response.status_code == 200
    salt = response.json()["salt"]
    assert isinstance(salt, str) and len(salt) == 32  # 16 bytes, hex-encoded
