from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "node.app:app",
        host=os.environ.get("NODE_HOST", "0.0.0.0"),
        port=int(os.environ.get("NODE_PORT", "9000")),
    )


if __name__ == "__main__":
    main()
