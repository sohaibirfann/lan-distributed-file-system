import importlib

from fastapi.testclient import TestClient


def _reload_with_env(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("COORDINATOR_DB_PATH", str(tmp_path / "coordinator.db"))
    monkeypatch.setenv("NAMESPACE_PASSPHRASE", "correct horse battery staple")
    monkeypatch.setenv("MDNS_ADVERTISE", "false")
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import coordinator.app as app_module
    import coordinator.auth as auth_module
    import coordinator.db as db_module
    import coordinator.events as events_module
    import coordinator.files as files_module
    import coordinator.models as models_module
    import coordinator.nodes as nodes_module
    import coordinator.replication as replication_module
    import coordinator.settings as settings_module

    importlib.reload(db_module)
    importlib.reload(models_module)
    importlib.reload(settings_module)
    importlib.reload(events_module)
    importlib.reload(auth_module)
    importlib.reload(replication_module)
    importlib.reload(nodes_module)
    importlib.reload(files_module)
    importlib.reload(app_module)
    return app_module


def test_dashboard_is_served_without_a_session_when_the_dist_directory_exists(tmp_path, monkeypatch):
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html>the dashboard</html>")

    app_module = _reload_with_env(monkeypatch, tmp_path, DASHBOARD_DIST_DIR=str(dist_dir))

    with TestClient(app_module.app) as client:
        response = client.get("/")

        assert response.status_code == 200
        assert "the dashboard" in response.text


def test_api_routes_still_win_over_the_static_mount(tmp_path, monkeypatch):
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html>the dashboard</html>")
    # A decoy that would shadow the real endpoint if route order were ever wrong.
    (dist_dir / "health").write_text("not the real health endpoint")

    app_module = _reload_with_env(monkeypatch, tmp_path, DASHBOARD_DIST_DIR=str(dist_dir))

    with TestClient(app_module.app) as client:
        response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_missing_dist_directory_is_skipped_without_crashing_the_app(tmp_path, monkeypatch):
    app_module = _reload_with_env(
        monkeypatch, tmp_path, DASHBOARD_DIST_DIR=str(tmp_path / "does-not-exist")
    )

    with TestClient(app_module.app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 404
