# src/middleware.py
# Shared middleware, decorators, and request helpers

import os
import secrets

from fastapi import HTTPException, Request


# Per-process token that lets the in-app tool layer hit admin-gated
# routes via HTTP loopback (the agent's tool calls don't carry the
# admin user's session cookie). Set once at import; tools read the
# same value from this module. Never persisted or exposed externally.
INTERNAL_TOOL_TOKEN = os.environ.get("ODYSSEUS_INTERNAL_TOKEN") or secrets.token_hex(32)
INTERNAL_TOOL_HEADER = "X-Odysseus-Internal-Token"


def require_admin(request: Request):
    """Raise 403 if the current user isn't an admin.
    Allows access when auth is explicitly disabled, or when the request carries
    the in-process internal-tool token used by loopback agent tools.
    """
    # In-process bypass for tool-layer loopback calls. Two paths:
    # (a) header-direct (caller set X-Odysseus-Internal-Token), or
    # (b) the auth middleware already validated the token and stamped
    #     request.state.current_user = "internal-tool".
    try:
        hdr = request.headers.get(INTERNAL_TOOL_HEADER)
        if hdr and secrets.compare_digest(hdr, INTERNAL_TOOL_TOKEN):
            return
        if getattr(request.state, "current_user", None) == "internal-tool":
            return
    except Exception:
        pass

    auth_mgr = getattr(request.app.state, "auth_manager", None)
    if os.getenv("AUTH_ENABLED", "true").lower() == "false":
        return
    if not auth_mgr or not auth_mgr.is_configured:
        raise HTTPException(403, "Admin only")
    user = getattr(request.state, "current_user", None)
    if not user or not auth_mgr.is_admin(user):
        raise HTTPException(403, "Admin only")


class SecurityHeadersMiddleware:
    """Add standard security headers to all responses.
    Pure ASGI middleware — does NOT extend BaseHTTPMiddleware, avoiding the
    Starlette 1.4.x streaming bug where a content-length mismatch during
    exception handling causes uvicorn to raise:
        RuntimeError: Response content shorter than Content-Length
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Generate a per-request nonce for inline scripts
        nonce = secrets.token_hex(16)

        path = scope.get("path", "")
        is_tool_render = path.startswith("/api/tools/") and path.endswith("/render")
        is_report = path.startswith("/api/research/report/")

        # Determine CSP / framing headers upfront (path is known before response)
        if is_report:
            extra_headers = [
                (b"X-Content-Type-Options", b"nosniff"),
                (b"Referrer-Policy", b"no-referrer"),
                (b"Content-Security-Policy", (
                    b"default-src 'self'; "
                    b"script-src 'self' 'unsafe-inline'; "
                    b"style-src 'self' 'unsafe-inline'; "
                    b"font-src 'self'; "
                    b"img-src 'self' data: blob: https:; "
                    b"connect-src 'self'; "
                    b"frame-ancestors 'none'"
                )),
            ]
        elif is_tool_render:
            extra_headers = [
                (b"X-Content-Type-Options", b"nosniff"),
                (b"Referrer-Policy", b"no-referrer"),
            ]
        else:
            extra_headers = [
                (b"X-Content-Type-Options", b"nosniff"),
                (b"Referrer-Policy", b"no-referrer"),
                (b"X-Frame-Options", b"DENY"),
                (b"Content-Security-Policy", (
                    b"default-src 'self'; "
                    b"script-src 'self' 'nonce-" + nonce.encode() + b"' https://cdn.jsdelivr.net; "
                    b"style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                    b"font-src 'self' https://cdn.jsdelivr.net; "
                    b"img-src 'self' data: blob:; "
                    b"media-src 'self' blob:; "
                    b"connect-src 'self'; "
                    b"frame-src 'self'; "
                    b"frame-ancestors 'none'"
                )),
            ]

        # Stash nonce for downstream access via request.state.csp_nonce
        # (Starlette's Request.state reads from scope["state"] dict)
        scope.setdefault("state", {})["csp_nonce"] = nonce

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                original_headers = message.get("headers", [])
                merged = list(original_headers)
                seen_keys = {k.lower() for k, _ in original_headers}
                for k, v in extra_headers:
                    if k.lower() not in seen_keys:
                        merged.append((k, v))
                message["headers"] = merged
            await send(message)

        await self.app(scope, receive, send_with_headers)
