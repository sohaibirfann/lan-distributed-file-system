from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.middleware.cors import CORSMiddleware

from node.chunks import (
    ChunkHashMismatch,
    InsufficientCapacity,
    InvalidChunkId,
    delete_chunk,
    list_chunk_inventory,
    retrieve_chunk,
    store_chunk,
)
from node.config import load_config
from node.heartbeat import heartbeat_loop
from node.registration import measure_used_bytes, register_with_coordinator

# A single request is fully buffered into memory before the hash/capacity
# checks run, so it needs its own cap independent of the coordinator's
# configured chunk size — this is a fixed safety ceiling, not that setting.
MAX_CHUNK_BYTES = 64 * 1024 * 1024

logger = logging.getLogger(__name__)


# Mutated in place from lifespan startup -- CORSMiddleware keeps this by reference.
cors_allowed_origins: list[str] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    app.state.config = config
    cors_allowed_origins[:] = [config.coordinator_address]

    with httpx.Client(base_url=config.coordinator_address) as client:
        register_with_coordinator(config, client)
        app.state.coordinator_client = client

        stop = asyncio.Event()
        task = asyncio.create_task(heartbeat_loop(config, client, stop))
        try:
            yield
        finally:
            stop.set()
            await task  # waits for any in-flight beat to actually finish
            try:
                resp = client.post("/nodes/drain", json={"address": config.node_address})
                resp.raise_for_status()
                remaining = resp.json()["remaining_chunks"]
                if remaining:
                    logger.warning("drain finished with %d chunk(s) still needing a home", remaining)
            except httpx.HTTPError as err:
                logger.warning("drain request failed: %s", err)


app = FastAPI(lifespan=lifespan)
# Chunk bytes travel browser -> node directly (cross-origin); everything else is same-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allowed_origins,
    allow_methods=["GET", "PUT"],  # deletes are coordinator-orchestrated, never browser -> node
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


security = HTTPBasic()


def require_owner_auth(
    request: Request, credentials: HTTPBasicCredentials = Depends(security)
) -> None:
    config = request.app.state.config
    valid = secrets.compare_digest(
        credentials.username.encode(), config.owner_username.encode()
    ) and secrets.compare_digest(credentials.password.encode(), config.owner_password.encode())
    if not valid:
        raise HTTPException(
            status_code=401, detail="Invalid credentials", headers={"WWW-Authenticate": "Basic"}
        )


@app.get("/stats")
def stats(request: Request, _: None = Depends(require_owner_auth)) -> dict[str, int]:
    config = request.app.state.config
    return {
        "chunk_count": len(list_chunk_inventory(config)),
        "used_bytes": measure_used_bytes(config.storage_directory),
        "capacity_budget_bytes": config.capacity_budget_bytes,
    }


@app.get("/chunks")
def list_chunks(request: Request) -> list[dict[str, str | float]]:
    return [
        {"hash": chunk_id, "stored_at": stored_at}
        for chunk_id, stored_at in list_chunk_inventory(request.app.state.config)
    ]


@app.put("/chunks/{chunk_id}")
async def upload_chunk(chunk_id: str, request: Request) -> dict[str, str]:
    content_length = request.headers.get("content-length")
    if content_length is not None and int(content_length) > MAX_CHUNK_BYTES:
        raise HTTPException(status_code=413, detail="chunk exceeds the maximum accepted size")

    data = await request.body()
    if len(data) > MAX_CHUNK_BYTES:
        raise HTTPException(status_code=413, detail="chunk exceeds the maximum accepted size")

    try:
        # store_chunk does blocking disk I/O; running it inline here would
        # stall the event loop — and with it, the heartbeat loop — for the
        # duration of every upload.
        await asyncio.to_thread(store_chunk, request.app.state.config, chunk_id, data)
    except InvalidChunkId as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    except ChunkHashMismatch as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    except InsufficientCapacity as err:
        raise HTTPException(status_code=507, detail=str(err)) from err

    return {"status": "stored"}


@app.get("/chunks/{chunk_id}")
def download_chunk(chunk_id: str, request: Request) -> Response:
    try:
        data = retrieve_chunk(request.app.state.config, chunk_id)
    except InvalidChunkId as err:
        raise HTTPException(status_code=422, detail=str(err)) from err

    if data is None:
        raise HTTPException(status_code=404, detail="chunk not found")

    return Response(content=data, media_type="application/octet-stream")


@app.delete("/chunks/{chunk_id}", status_code=204)
async def delete_chunk_route(chunk_id: str, request: Request) -> None:
    try:
        await asyncio.to_thread(delete_chunk, request.app.state.config, chunk_id)
    except InvalidChunkId as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
