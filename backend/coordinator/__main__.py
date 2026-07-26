from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "coordinator.app:app",
        host=os.environ.get("COORDINATOR_HOST", "0.0.0.0"),
        port=int(os.environ.get("COORDINATOR_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
