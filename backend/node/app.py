from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from node.config import load_config
from node.heartbeat import heartbeat_loop
from node.registration import register_with_coordinator


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
