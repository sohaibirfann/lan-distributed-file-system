import asyncio

import uvicorn

from node.__main__ import _DrainingServer


def test_shutdown_drains_before_closing_the_listening_socket(monkeypatch):
    order = []

    monkeypatch.setattr(
        "node.__main__.drain_on_shutdown", lambda config, client: order.append("drain")
    )

    async def fake_super_shutdown(self, sockets=None):
        order.append("super")

    monkeypatch.setattr(uvicorn.Server, "shutdown", fake_super_shutdown)

    import node.__main__ as main_module

    main_module.app.state.config = object()
    main_module.app.state.coordinator_client = object()

    server = _DrainingServer(uvicorn.Config(main_module.app))
    asyncio.run(server.shutdown())

    assert order == ["drain", "super"]
