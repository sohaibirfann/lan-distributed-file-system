from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path as FilePath

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from coordinator import auth, files, nodes, replication
from coordinator.auth import enforce_session
from coordinator.db import init_db
from coordinator.discovery import start_mdns_advertisement
from coordinator.replication import gc_sweep_loop, repair_loop
from coordinator.settings import (
    _int_from_env,
    load_max_file_size_config,
    load_replication_config,
    seed_settings,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_settings()
    load_replication_config()
    load_max_file_size_config()

    repair_interval_seconds = _int_from_env("REPAIR_INTERVAL_SECONDS", "60")
    if repair_interval_seconds <= 0:
        raise RuntimeError("REPAIR_INTERVAL_SECONDS must be positive.")

    gc_sweep_interval_seconds = _int_from_env("GC_SWEEP_INTERVAL_SECONDS", "300")
    if gc_sweep_interval_seconds <= 0:
        raise RuntimeError("GC_SWEEP_INTERVAL_SECONDS must be positive.")

    # On by default for zero-config discovery; tests disable it (slow/flaky in bulk).
    mdns = None
    if os.environ.get("MDNS_ADVERTISE", "true").lower() != "false":
        mdns = await start_mdns_advertisement(_int_from_env("COORDINATOR_PORT", "8000"))

    stop = asyncio.Event()
    task = asyncio.create_task(repair_loop(stop, repair_interval_seconds))
    gc_stop = asyncio.Event()
    gc_task = asyncio.create_task(gc_sweep_loop(gc_stop, gc_sweep_interval_seconds))
    try:
        yield
    finally:
        stop.set()
        gc_stop.set()
        await task  # waits for any in-flight repair cycle to actually finish
        await gc_task  # waits for any in-flight sweep to actually finish
        if mdns is not None:
            mdns_zc, mdns_info = mdns
            await mdns_zc.async_unregister_service(mdns_info)
            await mdns_zc.async_close()


app = FastAPI(
    lifespan=lifespan,
    dependencies=[Depends(enforce_session)],
    # These bypass the dependency above, so disable them rather than leave them open.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.include_router(auth.router)
app.include_router(nodes.router)
app.include_router(files.router)
app.include_router(replication.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# Mounted last so it never shadows an API route above; skipped if unbuilt. Everything
# under this directory is unauthenticated -- nothing sensitive belongs in a build output.
_default_dashboard_dist = FilePath(__file__).resolve().parent.parent.parent / "frontend" / "dist"
_dashboard_dist = FilePath(os.environ.get("DASHBOARD_DIST_DIR", str(_default_dashboard_dist)))
if _dashboard_dist.is_dir():
    app.mount("/", StaticFiles(directory=_dashboard_dist, html=True), name="dashboard")
