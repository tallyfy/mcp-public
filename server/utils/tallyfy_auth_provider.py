"""
Tallyfy Authentication Provider

Custom auth provider extending JWTVerifier with:
- RS256 signature verification using Tallyfy's public key (primary trust mechanism)
- Per-user org_id storage for session persistence
- MCP resource verification via custom `mcp_resource` JWT claim

Note: Tallyfy's authorization server does not include an 'iss' claim in JWT payloads.
Token authenticity is guaranteed by RS256 signature verification — only Tallyfy holds
the private key that corresponds to the configured public key.

IMPORTANT: The standard JWT `aud` claim is owned by Laravel Passport and must remain
the integer OAuth client ID. The MCP resource identifier is carried in the custom
`mcp_resource` claim instead. See tallyfy/api-v2#9089 for the full rationale.

Reference: RFC 8707 (Resource Indicators for OAuth 2.0)

Environment Support:
- TALLYFY_ENVIRONMENT=staging|production controls OAuth endpoint configuration
- Individual overrides available via TALLYFY_ISSUER environment variable (for OAuth metadata)
"""

import os
import logging
import threading
import time
from collections import OrderedDict
import jwt
from typing import Optional, Dict, List, Union
from fastmcp.server.auth.providers.jwt import JWTVerifier
from mcp.server.auth.provider import AccessToken
from constants import MCP_JWT_AUDIENCE, TALLYFY_ENVIRONMENT, TALLYFY_ISSUER, ENFORCE_AUDIENCE
from metrics import record_jwt_validation
logger = logging.getLogger(__name__)

# Bounded LRU + TTL cache of {user_id -> org_id} for session-resume convenience.
# Prevents the unbounded growth flagged in #235: each distinct authenticated user
# adds one entry, and absent eviction the dict grew monotonically. The pattern
# matches how `request_logging.py` already caps `_mcp_sessions` at 1000.
#
# Capacity defaults to 5000 entries (~ a few KB total) — overridable via
# MCP_USER_ORG_CACHE_SIZE. TTL of 24 h ensures stale rows are evicted even
# without write pressure (e.g., if a user disappears).
_USER_ORG_CACHE_MAX = int(os.getenv("MCP_USER_ORG_CACHE_SIZE", "5000"))
_USER_ORG_CACHE_TTL = float(os.getenv("MCP_USER_ORG_CACHE_TTL_SECONDS", str(24 * 3600)))

# OrderedDict gives us LRU semantics with move_to_end on access. Wrapped in
# a lock because the auth path can be entered concurrently across requests.
_user_org_ids: "OrderedDict[str, tuple]" = OrderedDict()  # user_id -> (org_id, set_at)
_user_org_lock = threading.Lock()


# Issuer URLs by environment
_ISSUER_BY_ENV = {
    "staging": "https://staging.account.tallyfy.com",
    "production": "https://account.tallyfy.com",
}


logger.info(
    f"Auth provider configuration: environment={TALLYFY_ENVIRONMENT}, "
    f"issuer={TALLYFY_ISSUER}, enforce_audience={ENFORCE_AUDIENCE}"
)


class TallyfyAuthProvider(JWTVerifier):
    """
    Auth provider that extends JWTVerifier with:
    - RS256 signature verification (token authenticity via Tallyfy's public key)
    - MCP resource claim verification (custom `mcp_resource` JWT claim)
    - Org ID session storage

    The `mcp_resource` claim identifies this MCP server as the intended
    resource. It is emitted by api-v2's OAuthController and checked here
    when ENFORCE_JWT_AUDIENCE=true. The standard `aud` claim is reserved
    for Passport's internal client ID and is NOT used for MCP verification.
    """

    def __init__(
        self,
        public_key: str,
        expected_audience: Optional[Union[str, List[str]]] = None,
        expected_issuer: Optional[str] = None,
        **kwargs
    ):
        """
        Initialize the auth provider.

        Args:
            public_key: RSA public key for JWT signature verification
            expected_audience: Expected MCP resource value. If None, uses MCP_JWT_AUDIENCE
            expected_issuer: Expected issuer for the JWT. If None, uses TALLYFY_ISSUER
        """
        super().__init__(public_key=public_key, **kwargs)
        self.expected_audience = expected_audience or MCP_JWT_AUDIENCE
        self.expected_issuer = expected_issuer or TALLYFY_ISSUER

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        """
        Verify JWT with extended validation including MCP resource claim.

        This ensures:
        1. Valid RS256 signature using Tallyfy's public key (via parent JWTVerifier)
        2. Token not expired (via parent JWTVerifier)
        3. Correct MCP resource — token was issued for this server (if ENFORCE_AUDIENCE=true)

        Note: Tallyfy's authorization server does not include an 'iss' claim in JWT
        payloads. Trust is established via RS256 signature verification against
        Tallyfy's public key — only Tallyfy holds the corresponding private key.
        """
        # Reuse pre-decoded claims from OrgIdMiddleware if available (P2-I),
        # otherwise decode once here for both expiry check and post-verify use.
        from utils.org_id_middleware import get_jwt_claims
        claims = get_jwt_claims()
        if not claims:
            try:
                claims = jwt.decode(token, options={"verify_signature": False})
            except jwt.DecodeError:
                claims = None

        # Pre-check expiry so operators see a specific reason in INFO logs rather
        # than the parent's generic "Bearer token rejected" message.
        if claims:
            exp = claims.get("exp")
            client_id = (
                claims.get("client_id")
                or claims.get("azp")
                or claims.get("sub")
                or "unknown"
            )
            if exp and exp < time.time():
                logger.info(
                    "Bearer token rejected: expired | client=%s | expired_at=%s",
                    client_id,
                    exp,
                )
                record_jwt_validation('expired')
                return None

        # Let parent handle signature verification, issuer, audience, scopes
        access_token = await super().verify_token(token)

        if access_token is None:
            logger.debug("JWT signature/expiration verification failed")
            record_jwt_validation('failed')
            return None

        if not claims:
            logger.warning("Failed to decode JWT claims")
            record_jwt_validation('failed')
            return None

        # Accept two token types:
        # 1. MCP-issued tokens: mcp_resource == "mcp-host"
        # 2. Passport tokens: aud == "1" (Laravel Passport OAuth client ID)
        if ENFORCE_AUDIENCE == "true":
            mcp_resource = claims.get("mcp_resource")
            aud = claims.get("aud")
            if mcp_resource == self.expected_audience:
                pass
            elif str(aud) == "1":
                pass
            else:
                logger.warning(
                    "JWT rejected: mcp_resource='%s' aud='%s' (expected mcp_resource='%s' or aud='1')",
                    mcp_resource,
                    aud,
                    self.expected_audience,
                )
                record_jwt_validation('invalid_token')
                return None

        record_jwt_validation('success')
        return access_token



def store_org_id_for_user(user_id: str, org_id: str) -> None:
    """Store org_id for a user session, evicting LRU entries past the cap.

    See module-level constants for the cap and TTL. Entry timestamp is
    refreshed on every store, which doubles as access-time tracking for
    the TTL check in :func:`get_org_id_for_user`.
    """
    if not user_id:
        return
    now = time.time()
    with _user_org_lock:
        if user_id in _user_org_ids:
            # Touch the LRU position so frequently-stored users are kept warm.
            _user_org_ids.move_to_end(user_id)
        _user_org_ids[user_id] = (org_id, now)
        while len(_user_org_ids) > _USER_ORG_CACHE_MAX:
            evicted_user, _ = _user_org_ids.popitem(last=False)
            logger.debug(
                "user_org_id cache: evicting LRU user=%s (cap=%d)",
                evicted_user,
                _USER_ORG_CACHE_MAX,
            )


def get_org_id_for_user(user_id: str) -> Optional[str]:
    """Get stored org_id for a user, honouring LRU + TTL eviction."""
    if not user_id:
        return None
    now = time.time()
    with _user_org_lock:
        entry = _user_org_ids.get(user_id)
        if entry is None:
            return None
        org_id, set_at = entry
        if (now - set_at) > _USER_ORG_CACHE_TTL:
            _user_org_ids.pop(user_id, None)
            return None
        # Move to end so freshly-read users stay in the cache.
        _user_org_ids.move_to_end(user_id)
        return org_id


def clear_org_id_for_user(user_id: str) -> None:
    """Clear stored org_id for a user."""
    if not user_id:
        return
    with _user_org_lock:
        _user_org_ids.pop(user_id, None)


def _user_org_cache_size() -> int:
    """Test/diagnostic helper — current entry count."""
    with _user_org_lock:
        return len(_user_org_ids)


def _user_org_cache_clear() -> None:
    """Test helper — clear the entire cache."""
    with _user_org_lock:
        _user_org_ids.clear()
