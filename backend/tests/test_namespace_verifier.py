from tests.test_auth import register


def login(client, username="alice", password="hunter22"):
    return client.post("/login", json={"username": username, "password": password})


def test_get_verifier_requires_session(client):
    assert client.get("/namespace/verifier").status_code == 401


def test_put_verifier_requires_session(client):
    response = client.put("/namespace/verifier", json={"verifier": "some-ciphertext"})
    assert response.status_code == 401


def test_verifier_is_null_before_first_set(client):
    register(client)
    login(client)

    response = client.get("/namespace/verifier")

    assert response.status_code == 200
    assert response.json() == {"verifier": None}


def test_set_then_get_verifier_round_trips(client):
    register(client)
    login(client)

    put_response = client.put("/namespace/verifier", json={"verifier": "some-ciphertext"})
    assert put_response.status_code == 200
    assert put_response.json() == {"verifier": "some-ciphertext"}

    get_response = client.get("/namespace/verifier")
    assert get_response.json() == {"verifier": "some-ciphertext"}


def test_verifier_cannot_be_overwritten_once_set(client):
    register(client)
    login(client)
    client.put("/namespace/verifier", json={"verifier": "first-ciphertext"})

    response = client.put("/namespace/verifier", json={"verifier": "second-ciphertext"})

    assert response.status_code == 409
    assert client.get("/namespace/verifier").json() == {"verifier": "first-ciphertext"}


def test_put_verifier_rejects_empty_string(client):
    register(client)
    login(client)

    response = client.put("/namespace/verifier", json={"verifier": ""})

    assert response.status_code == 422
