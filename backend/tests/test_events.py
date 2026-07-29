from tests.test_auth import register
from tests.test_files import make_file_body
from tests.test_nodes import login, register_node
from tests.test_replication_health import mark_down
from tests.test_repair_execute import chunk_id_for


def test_list_events_requires_session(client):
    assert client.get("/events").status_code == 401


def test_list_events_empty_by_default(client):
    register(client)
    login(client)

    assert client.get("/events").json() == []


def test_node_going_down_is_recorded_as_an_event(client):
    register(client)
    login(client)
    register_node(client, address="a:9000")

    import coordinator.app as coordinator_app_module

    mark_down("a:9000")
    coordinator_app_module.run_one_repair_cycle()

    events = client.get("/events").json()
    assert any(
        e["kind"] == "node_state_transition" and "up -> down" in e["message"] for e in events
    )


def test_node_state_transition_is_not_re_recorded_once_settled(client):
    register(client)
    login(client)
    register_node(client, address="a:9000")

    import coordinator.app as coordinator_app_module

    mark_down("a:9000")
    coordinator_app_module.run_one_repair_cycle()
    coordinator_app_module.run_one_repair_cycle()  # still down, nothing changed

    transitions = [e for e in client.get("/events").json() if e["kind"] == "node_state_transition"]
    assert len(transitions) == 1


def test_repair_action_is_recorded_as_an_event(client, tmp_path, monkeypatch):
    from contextlib import ExitStack

    from tests.test_repair_execute import spawn_fake_node

    register(client)
    login(client)
    node_a = register_node(client, address="a:9000").json()
    node_b = register_node(client, address="b:9000").json()
    spare = register_node(client, address="spare:9000").json()

    data = b"chunk bytes for event logging"
    chunk_hash = chunk_id_for(data)
    client.post(
        "/files",
        json={
            "name": "f.bin",
            "size_bytes": len(data),
            "chunks": [
                {
                    "sequence_index": 0,
                    "hash": chunk_hash,
                    "size_bytes": len(data),
                    "node_ids": [node_a["id"], node_b["id"]],
                }
            ],
        },
    )

    with ExitStack() as stack:
        fake_a = spawn_fake_node(stack, tmp_path, "a:9000", monkeypatch)
        fake_b = spawn_fake_node(stack, tmp_path, "b:9000", monkeypatch)
        fake_spare = spawn_fake_node(stack, tmp_path, "spare:9000", monkeypatch)
        fake_a.put(f"/chunks/{chunk_hash}", content=data)
        fake_b.put(f"/chunks/{chunk_hash}", content=data)

        mark_down("a:9000")

        import coordinator.app as coordinator_app_module

        monkeypatch.setattr(
            coordinator_app_module,
            "_default_node_client",
            lambda address: {"a:9000": fake_a, "b:9000": fake_b, "spare:9000": fake_spare}[address],
        )

        coordinator_app_module.run_one_repair_cycle()

    events = client.get("/events").json()
    repair_events = [e for e in events if e["kind"] == "repair"]
    assert len(repair_events) == 1
    assert "repaired onto node" in repair_events[0]["message"]


def test_manual_repair_endpoint_also_records_an_event(client, tmp_path, monkeypatch):
    from contextlib import ExitStack

    from tests.test_repair_execute import spawn_fake_node

    register(client)
    login(client)
    node_a = register_node(client, address="a:9000").json()
    node_b = register_node(client, address="b:9000").json()
    register_node(client, address="spare:9000")

    data = b"chunk bytes for the manual endpoint"
    chunk_hash = chunk_id_for(data)
    client.post(
        "/files",
        json={
            "name": "f.bin",
            "size_bytes": len(data),
            "chunks": [
                {
                    "sequence_index": 0,
                    "hash": chunk_hash,
                    "size_bytes": len(data),
                    "node_ids": [node_a["id"], node_b["id"]],
                }
            ],
        },
    )

    with ExitStack() as stack:
        fake_a = spawn_fake_node(stack, tmp_path, "a:9000", monkeypatch)
        fake_b = spawn_fake_node(stack, tmp_path, "b:9000", monkeypatch)
        fake_spare = spawn_fake_node(stack, tmp_path, "spare:9000", monkeypatch)
        fake_a.put(f"/chunks/{chunk_hash}", content=data)
        fake_b.put(f"/chunks/{chunk_hash}", content=data)

        mark_down("a:9000")

        import coordinator.app as coordinator_app_module

        monkeypatch.setattr(
            coordinator_app_module,
            "_default_node_client",
            lambda address: {"a:9000": fake_a, "b:9000": fake_b, "spare:9000": fake_spare}[address],
        )

        # Unlike run_one_repair_cycle (the background loop), this hits the
        # HTTP endpoint directly -- it must record events too, not just the loop.
        client.post("/replication/repair")

    events = client.get("/events").json()
    assert any(e["kind"] == "repair" for e in events)
    assert any(e["kind"] == "node_state_transition" for e in events)


def test_report_chunk_unavailable_requires_session(client):
    response = client.post("/chunks/somehash/report-unavailable", json={"node_id": 1})
    assert response.status_code == 401


def test_report_chunk_unavailable_records_an_event(client):
    register(client)
    login(client)
    node = register_node(client, address="a:9000").json()

    response = client.post(
        f"/chunks/{'a' * 64}/report-unavailable", json={"node_id": node["id"]}
    )

    assert response.status_code == 204
    events = client.get("/events").json()
    assert any(
        e["kind"] == "chunk_unavailable"
        and "a" * 64 in e["message"]
        and str(node["id"]) in e["message"]
        for e in events
    )


def test_report_chunk_unavailable_rejects_malformed_chunk_id(client):
    register(client)
    login(client)
    node = register_node(client, address="a:9000").json()

    response = client.post(
        "/chunks/not-a-real-hash/report-unavailable", json={"node_id": node["id"]}
    )

    assert response.status_code == 422


def test_report_chunk_unavailable_rejects_unknown_node(client):
    register(client)
    login(client)

    response = client.post(
        f"/chunks/{'a' * 64}/report-unavailable", json={"node_id": 999999}
    )

    assert response.status_code == 404


def test_events_have_no_effect_when_nothing_changes(client):
    register(client)
    login(client)
    node_ids = [register_node(client, address=f"n{i}:9000").json()["id"] for i in range(3)]
    client.post("/files", json=make_file_body(chunk_sizes=(100,), node_ids=node_ids))

    import coordinator.app as coordinator_app_module

    coordinator_app_module.run_one_repair_cycle()

    assert client.get("/events").json() == []
