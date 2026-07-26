from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, Response

from node.chunks import (
    ChunkHashMismatch,
    InsufficientCapacity,
    InvalidChunkId,
    delete_chunk,
    list_chunk_ids,
    retrieve_chunk,
    store_chunk,
)
from node.config import load_config
from node.heartbeat import heartbeat_loop
from node.registration import register_with_coordinator

# A single request is fully buffered into memory before the hash/capacity
# checks run, so it needs its own cap independent of the coordinator's
# configured chunk size — this is a fixed safety ceiling, not that setting.
MAX_CHUNK_BYTES = 64 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    app.state.config = config

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


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/chunks")
def list_chunks(request: Request) -> list[str]:
    return list_chunk_ids(request.app.state.config)


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
