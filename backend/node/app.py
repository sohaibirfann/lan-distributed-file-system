from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from node.config import load_config
from node.registration import register_with_coordinator


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    app.state.config = config

    with httpx.Client(base_url=config.coordinator_address) as client:
        register_with_coordinator(config, client)
        app.state.coordinator_client = client
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
