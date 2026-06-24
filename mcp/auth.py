"""
Bearer-token authentication for the switchyard MCP server.

Exposes BearerAuthMiddleware that checks Authorization: Bearer <token> on all
/mcp/* paths.  Token is read from SWITCHYARD_MCP_TOKEN at request time.

Auth failures are tracked per source IP; repeated failures from the same IP
trigger a warning.  No active IP blocking — Tailscale ACLs are the first line
of defence.
"""

import hmac
import logging
import os

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

log = logging.getLogger(__name__)

# source-IP → consecutive failure count; grows without bound but negligible
# in homelab use (small Tailscale peer set, never GC-ed intentionally).
_auth_failures: dict[str, int] = {}
_WARN_THRESHOLD = 5


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, protected_prefix: str = "/mcp") -> None:
        super().__init__(app)
        self._prefix = protected_prefix

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith(self._prefix):
            return await call_next(request)

        token = os.environ.get("SWITCHYARD_MCP_TOKEN", "")
        auth_header = request.headers.get("Authorization", "")

        if auth_header.startswith("Bearer ") and token:
            provided = auth_header[len("Bearer "):]
            if hmac.compare_digest(provided.encode(), token.encode()):
                client_ip = _client_ip(request)
                _auth_failures.pop(client_ip, None)
                return await call_next(request)

        client_ip = _client_ip(request)
        count = _auth_failures.get(client_ip, 0) + 1
        _auth_failures[client_ip] = count
        if count >= _WARN_THRESHOLD:
            log.warning(
                "Auth failures from %s: %d consecutive failures", client_ip, count
            )
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
