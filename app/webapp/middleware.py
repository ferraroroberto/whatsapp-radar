"""Auth middleware for the admin webapp.

The bearer-token middleware is the single auth choke point for the HTTP surface.
Loopback callers (the PC itself) bypass the token; non-loopback callers must
present it. The WebAuthn ceremony endpoints additionally require Tailscale —
they are refused outright over the public Cloudflare tunnel.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
from collections.abc import Callable, Mapping

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

# Loopback addresses bypass the bearer-token gate so local probes keep working
# without carrying the token. Tunnel traffic arrives with a non-loopback client
# IP and must present the token.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

AUTH_EXEMPT_PREFIXES = ("/static/", "/healthz")
AUTH_EXEMPT_EXACT = frozenset({"/", "/healthz", "/api/login"})

# Tailscale hands every node an address in the CGNAT range. The passkey
# ceremony endpoints are gated to this range (plus loopback and an optional
# user allowlist) and refused over the public tunnel.
_TAILNET_CGNAT = ipaddress.ip_network("100.64.0.0/10")
# Cloudflare's tunnel adds these headers — their presence means the request came
# in over the public edge, never acceptable for a passkey ceremony.
_CLOUDFLARE_HEADERS = ("cf-ray", "cf-connecting-ip")


def via_cloudflare(headers: Mapping[str, str]) -> bool:
    return any(h in headers for h in _CLOUDFLARE_HEADERS)


def client_in_tailnet(client_host: str, allowlist: list[str]) -> bool:
    """True when the client IP is loopback, in the tailnet, or allowlisted."""
    try:
        ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    if ip.is_loopback or ip in _TAILNET_CGNAT:
        return True
    for entry in allowlist or []:
        try:
            if ip in ipaddress.ip_network(str(entry), strict=False):
                return True
        except ValueError:
            if client_host == str(entry):
                return True
    return False


def passkey_http_gate(request: Request) -> JSONResponse | None:
    """Enforce Tailscale-only access on the passkey ceremony endpoints.

    Returns an error response to short-circuit with, or ``None`` to allow.
    Loopback callers are handled by the middleware before this runs.
    """
    if not request.url.path.startswith("/api/webauthn/"):
        return None
    if via_cloudflare(request.headers):
        return JSONResponse(
            status_code=403,
            content={"detail": "passkey endpoints are not reachable over the public tunnel"},
        )
    cfg = request.app.state.webapp_config
    client_host = request.client.host if request.client else ""
    if not client_in_tailnet(client_host, getattr(cfg, "tailnet_allowlist", [])):
        return JSONResponse(
            status_code=403,
            content={"detail": "passkey endpoints are Tailscale-only"},
        )
    return None


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Require Authorization: Bearer <token> on API endpoints (non-loopback only)."""

    def __init__(self, app: ASGIApp, get_token: Callable[[], str]) -> None:
        super().__init__(app)
        self._get_token = get_token

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        client_host = request.client.host if request.client else ""
        is_loopback = client_host in LOOPBACK_HOSTS
        path = request.url.path

        # Passkey ceremonies are Tailscale-only, enforced even when no bearer
        # token is configured. The PC itself (loopback) is trusted and skips it.
        if not is_loopback:
            gate_err = passkey_http_gate(request)
            if gate_err is not None:
                return gate_err

        token = (self._get_token() or "").strip()
        if not token or is_loopback:
            return await call_next(request)

        if path in AUTH_EXEMPT_EXACT or any(
            path.startswith(p) for p in AUTH_EXEMPT_PREFIXES
        ):
            return await call_next(request)

        presented = ""
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            presented = auth_header[7:].strip()
        if not presented:
            presented = request.query_params.get("token", "").strip()

        if presented and hmac.compare_digest(presented, token):
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"detail": "missing or invalid bearer token"},
            headers={"WWW-Authenticate": 'Bearer realm="whatsapp-radar"'},
        )
