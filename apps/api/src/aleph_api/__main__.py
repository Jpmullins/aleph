"""`python -m aleph_api` entry point. Used by uv run / scripts."""

from __future__ import annotations

import uvicorn

from aleph_api.settings import get_settings


def main() -> None:
    s = get_settings()
    uvicorn.run(
        "aleph_api.main:create_app",
        host=s.api_host,
        port=s.api_port,
        factory=True,
        log_config=None,
    )


if __name__ == "__main__":
    main()
