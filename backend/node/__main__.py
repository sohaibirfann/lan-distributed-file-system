from __future__ import annotations

import os
import socket

import uvicorn

from node.app import app, drain_on_shutdown


class _DrainingServer(uvicorn.Server):
    """Sends the shutdown drain request before uvicorn closes its listening
    socket, so the coordinator's repair can still fetch chunks from this node."""

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        config = getattr(app.state, "config", None)
        client = getattr(app.state, "coordinator_client", None)
        if config is not None and client is not None:
            drain_on_shutdown(config, client)
        await super().shutdown(sockets)


def main() -> None:
    config = uvicorn.Config(
        app,
        host=os.environ.get("NODE_HOST", "0.0.0.0"),
        port=int(os.environ.get("NODE_PORT", "9000")),
    )
    _DrainingServer(config).run()


if __name__ == "__main__":
    main()
