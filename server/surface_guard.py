"""Minimal public surface (MEM0_PUBLIC_SURFACE=minimal).

Outermost ASGI layer: only the platform-compat routes with a valid
``Authorization: Token <MEM0_PLATFORM_TOKEN>`` and ``/mcp`` with a valid
``Authorization: Bearer <MEM0_MCP_TOKEN>`` reach the app. Everything else --
unknown paths, native routes, docs, wrong method or missing/bad credentials --
gets the same response as a nonexistent path, so the service can't be mapped
without a token.
"""

import os
import re
import secrets

NOT_FOUND_BODY = b'{"detail":"Not Found"}'

_RULES = [
    (re.compile(r"^/v3/memories/(add|search)/$"), "token", "MEM0_PLATFORM_TOKEN"),
    (re.compile(r"^/v1/event/[^/]+/$"), "token", "MEM0_PLATFORM_TOKEN"),
    (re.compile(r"^/v2/memories/$"), "token", "MEM0_PLATFORM_TOKEN"),
    (re.compile(r"^/v1/memories/[^/]+/$"), "token", "MEM0_PLATFORM_TOKEN"),
    (re.compile(r"^/v1/ping/$"), "token", "MEM0_PLATFORM_TOKEN"),
    (re.compile(r"^/mcp$"), "bearer", "MEM0_MCP_TOKEN"),
]


def enabled() -> bool:
    return os.environ.get("MEM0_PUBLIC_SURFACE", "").strip().lower() == "minimal"


def _authorized(path: str, authorization: str) -> bool:
    scheme, _, value = authorization.partition(" ")
    for pattern, expected_scheme, token_env in _RULES:
        if pattern.match(path):
            token = os.environ.get(token_env, "")
            return bool(token) and scheme.lower() == expected_scheme and secrets.compare_digest(value.strip(), token)
    return False


class SurfaceGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})

        headers = dict(scope.get("headers") or [])
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        if _authorized(scope.get("path", ""), authorization):
            return await self.app(scope, receive, send)

        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(NOT_FOUND_BODY)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": NOT_FOUND_BODY})
