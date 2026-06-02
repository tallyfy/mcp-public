"""
Organization ID Middleware
Extracts org_id from request headers and stores it per-user for session persistence.

Implements RFC 6750 Bearer Token error responses with WWW-Authenticate headers
for OAuth 2.1 compatibility with ChatGPT and other MCP clients.
"""

import logging
import jwt
from typing import Callable, MutableMapping, Any, Awaitable
from contextvars import ContextVar
from constants import MCP_RESOURCE_URL

logger = logging.getLogger(__name__)

# Context variable to store org_id per request
org_id_context: ContextVar[str | None] = ContextVar('org_id_context', default=None)

# Context variable to store decoded JWT claims per request (unverified decode
# for observability — user_id, org_id extraction). Populated once in
# OrgIdMiddleware, consumed by request_logging, metrics, and auth_context
# to avoid redundant jwt.decode calls (P2-I).
jwt_claims_context: ContextVar[dict | None] = ContextVar('jwt_claims_context', default=None)

# Endpoints that bypass org_id validation
HEALTH_ENDPOINTS = {"/health", "/ready", "/metrics"}

# Well-known endpoints that don't require auth
WELL_KNOWN_ENDPOINTS = {"/.well-known/oauth-protected-resource", "/.well-known/openid-configuration"}

# Supported org_id header names (lowercase bytes)
ORG_ID_HEADERS = {b'x-organization-id', b'x-org-id', b'organization-id', b'org-id', b'x-tallyfy-org-id'}


def build_www_authenticate_header(
    error: str = "invalid_token",
    error_description: str = None,
    scope: str = None
) -> str:
    """
    Build RFC 6750 compliant WWW-Authenticate header for Bearer token errors.

    Args:
        error: One of: invalid_request, invalid_token, insufficient_scope
        error_description: Human-readable error description
        scope: Required scope(s) if error is insufficient_scope

    Returns:
        WWW-Authenticate header value
    """
    parts = [f'Bearer realm="{MCP_RESOURCE_URL}"']

    if error:
        parts.append(f'error="{error}"')

    if error_description:
        # Escape quotes in description
        safe_desc = error_description.replace('"', '\\"')
        parts.append(f'error_description="{safe_desc}"')

    if scope:
        parts.append(f'scope="{scope}"')

    return ", ".join(parts)


class OrgIdMiddleware:
    """
    ASGI middleware that extracts org_id from headers and stores it per user.

    First request: Extract X-Organization-ID header → store for user_id
    Subsequent requests: Retrieve stored org_id by user_id
    """

    def __init__(self, app: Callable):
        self.app = app

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]]
    ) -> None:
        path = scope.get("path", "")

        # Skip middleware for non-HTTP, health endpoints, and well-known endpoints
        if scope["type"] != "http" or path in HEALTH_ENDPOINTS or path in WELL_KNOWN_ENDPOINTS:
            await self.app(scope, receive, send)
            return

        # Also skip for paths starting with /.well-known/ (catch-all for discovery)
        if path.startswith("/.well-known/"):
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", [])
        user_id, org_id = self._extract_from_headers(headers)

        # No user_id means no JWT - let JWTVerifier handle it
        if not user_id:
            await self.app(scope, receive, send)
            return

        from utils.tallyfy_auth_provider import get_org_id_for_user

        if org_id:
            # Header org_id: set request-scoped ContextVar only.
            # SECURITY (P1-G): never write to the persistent cache here —
            # the JWT is unverified at this point. Cache write happens in
            # get_authenticated_credentials() after JWTVerifier confirms
            # the signature.
            org_id_context.set(org_id)
        else:
            # Try to retrieve stored org_id from a previous authenticated request
            org_id = get_org_id_for_user(user_id)
            if org_id:
                org_id_context.set(org_id)
            # If no stored org_id, proceed without setting the context — the JWT may embed
            # an org_id that get_authenticated_credentials() will extract from the verified
            # token after JWTVerifier runs. An explicit error is raised there if still missing.

        await self.app(scope, receive, send)

    def _extract_from_headers(self, headers: list) -> tuple[str | None, str | None]:
        """Extract user_id from JWT (for session lookup) and org_id from explicit headers only.

        SECURITY — UNVERIFIED JWT DECODE (issue #215)
        ---------------------------------------------
        The JWT is decoded here with signature verification DISABLED. ``user_id``
        is used only to look up a previously stored org_id from ``_user_org_ids``
        (session persistence). ``org_id`` is taken exclusively from an explicit
        ``X-Organization-ID`` header — never from the JWT — to prevent a forged
        JWT from poisoning the session store for another user.

        Values produced here are **observability-only** (request logs, Prometheus
        labels). They MUST NEVER influence any authorization decision. For
        authorization, use ``utils.auth_context.get_authenticated_credentials()``
        which runs AFTER FastMCP's ``JWTVerifier`` has validated the RS256
        signature and expiry. JWT-embedded ``org_id`` is read from the verified
        token there, not here.

        Side effect: stores decoded claims in ``jwt_claims_context`` for
        downstream consumers (P2-I — single decode per request).
        """
        user_id = None
        org_id = None

        for name, value in headers:
            name_lower = name.lower()

            # Check for org_id header
            if name_lower in ORG_ID_HEADERS:
                org_id = value.decode('utf-8')

            # Check for Authorization header — extract user_id only, not org_id
            elif name_lower == b'authorization':
                auth_value = value.decode('utf-8')
                if auth_value.startswith('Bearer '):
                    try:
                        # SECURITY: signature intentionally NOT verified here.
                        # Used only to identify the user for session org_id lookup.
                        # org_id is NOT read from the JWT here (see method docstring).
                        claims = jwt.decode(auth_value[7:], options={"verify_signature": False})
                        user_id = claims.get('sub') or claims.get('user_id')
                        jwt_claims_context.set(claims)
                    except jwt.DecodeError:
                        pass

        return user_id, org_id


def get_jwt_claims() -> dict | None:
    """Get pre-decoded JWT claims from the current request context.

    Populated once per request in OrgIdMiddleware. Returns None if no JWT
    was present or decoding failed. Claims are from an UNVERIFIED decode —
    use for observability only (user_id for logs/metrics), never for
    authorization decisions.
    """
    return jwt_claims_context.get()


def get_org_id() -> str | None:
    """Get org_id from current request context.

    SECURITY (issue #215): the value returned here originates from an explicit
    ``X-Organization-ID`` header or from the ``_user_org_ids`` session store
    populated by a prior authenticated request. It is **observability-only** —
    used for request logs and Prometheus labels. Never use it as input to any
    authorization decision.

    For authorization, use ``utils.auth_context.get_authenticated_credentials()``
    which reads org_id from the verified JWT after FastMCP's ``JWTVerifier``
    has validated the RS256 signature and expiry.
    """
    return org_id_context.get()


def set_org_id(org_id: str) -> None:
    """Manually set org_id in current request context.

    SECURITY (issue #215): see ``get_org_id`` docstring. The value stored
    here is observability-only and must never be used for authorization.
    """
    org_id_context.set(org_id)
