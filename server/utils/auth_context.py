"""
Authentication Context Utilities
Extract authenticated user credentials from MCP auth context
"""

import os
import logging
import jwt
from typing import Optional, Tuple
from mcp.server.auth.middleware.auth_context import get_access_token
from tallyfy import TallyfyError
from utils.org_id_middleware import get_org_id, get_jwt_claims
from constants import TALLYFY_API_BASE_URL
logger = logging.getLogger(__name__)


class MissingOrgIdError(TallyfyError):
    """Raised when an authenticated request has no resolvable organization context.

    Subclasses TallyfyError and forces ``status_code = 400`` in ``__init__`` so the
    ``handle_tallyfy_errors`` wrapper demotes it to WARNING (input is missing —
    not a server bug — so it should not page). Carries the same shape as a real
    Tallyfy API 400 response.

    Note: ``TallyfyError.__init__`` sets ``self.status_code = status_code`` (the
    kwarg, defaulting to None), so a class attribute alone gets shadowed. The
    override below ensures every instance carries 400 without callers needing
    to remember the keyword.

    See Sentry MCP-4T (issue 7453280232) for the original noise pattern.
    """

    def __init__(self, message: str):
        super().__init__(message, status_code=400)


def get_authenticated_credentials() -> Tuple[str, str]:
    """
    Extract API key (JWT token) and org_id from the authenticated request context.

    This function runs AFTER JWTVerifier has validated the RS256 signature, so
    the token is trusted. The persistent user→org cache is written here (not in
    OrgIdMiddleware) to prevent pre-auth cache poisoning (P1-G).

    Returns:
        Tuple of (api_key, org_id)

    Raises:
        MissingOrgIdError: If no org_id can be resolved from header, JWT claim,
            persistent user→org cache, or TALLYFY_ORG_ID env var.
        Exception: If no access token is present (auth middleware misconfigured).
    """
    access_token = get_access_token()
    if not access_token:
        raise Exception("No authenticated user found. Please authenticate with a valid JWT token.")

    # Prefer org_id from explicit header (set by OrgIdMiddleware in the request-scoped ContextVar)
    org_id = get_org_id()

    # Reuse claims already decoded by OrgIdMiddleware. Safe here because
    # get_access_token() confirms JWTVerifier validated the RS256 signature
    # before any tool handler runs — the claims are from the same token.
    token_claims = get_jwt_claims() or {}
    if not token_claims:
        try:
            token_claims = jwt.decode(access_token.token, options={"verify_signature": False})
        except jwt.DecodeError:
            token_claims = {}

    if not org_id:
        org_id = token_claims.get('org_id')

    if not org_id:
        org_id = os.getenv("TALLYFY_ORG_ID")

    if not org_id:
        raise MissingOrgIdError(
            "Organization ID not found. To resolve, do one of: "
            "(1) include the X-Organization-ID header on this request, "
            "(2) ensure your OAuth access token contains an 'org_id' claim, "
            "or (3) set the TALLYFY_ORG_ID environment variable on the MCP client."
        )

    # Persist org_id for this user so subsequent requests (which may omit
    # the X-Organization-ID header) can look it up.
    user_id = token_claims.get('sub') or token_claims.get('user_id')
    if user_id and org_id:
        from utils.tallyfy_auth_provider import store_org_id_for_user
        store_org_id_for_user(user_id, org_id)

    # Log token fingerprint for request tracing (never log full token)
    token_hint = access_token.token[-8:] if len(access_token.token) > 8 else "***"
    logger.info(f"Auth OK │ org={org_id} │ token=...{token_hint}")

    return access_token.token, org_id


def get_user_id_from_token() -> Optional[str]:
    """Extract user ID from the authenticated JWT token."""
    claims = get_jwt_claims()
    if claims:
        return claims.get('sub') or claims.get('user_id')

    access_token = get_access_token()
    if not access_token:
        return None

    try:
        claims = jwt.decode(access_token.token, options={"verify_signature": False})
        return claims.get('sub') or claims.get('user_id')
    except jwt.DecodeError:
        return None