from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi import WebSocket


def _authority(value: str) -> tuple[str | None, int | None]:
    try:
        parsed = urlsplit(f"//{value}")
        return parsed.hostname.lower() if parsed.hostname else None, parsed.port
    except ValueError:
        return None, None


async def authorize(websocket: WebSocket) -> bool:
    container = websocket.app.state.container
    settings = container.settings
    host_value = websocket.headers.get("host", "")
    is_test = settings.allow_test_host and host_value.lower() == "testserver"
    host, port = _authority(host_value)
    if not is_test and (host not in {"127.0.0.1", "localhost", "::1"} or port != settings.port):
        await websocket.close(code=4403, reason="loopback host and selected port required")
        return False
    origin = websocket.headers.get("origin")
    if origin:
        try:
            parsed = urlsplit(origin)
            allowed = parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port == settings.port
        except ValueError:
            allowed = False
        if not allowed:
            await websocket.close(code=4403, reason="origin not allowed")
            return False
    is_loopback = host in {"127.0.0.1", "localhost", "::1"} or is_test
    if not is_loopback and settings.bootstrap_token and websocket.cookies.get("spa_session") != container.session_secret:
        await websocket.close(code=4401, reason="bootstrap session required")
        return False
    await websocket.accept()
    return True


def install(module: Any) -> None:
    # Route functions resolve their module-global authorizer at connection time.
    module._authorize = authorize
