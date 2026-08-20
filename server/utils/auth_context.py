"""
Authentication Context Utilities
Extract authenticated user credentials from MCP auth context
"""

import os
import logging
import jwt
from typing import Optional, Tuple
from fastmcp.server.dependencies import get_http_headers
from mcp.server.auth.middleware.auth_context import get_access_token
from tallyfy import TallyfyError
from utils.org_id_middleware import ORG_ID_HEADERS, get_org_id, get_jwt_claims
from constants import TALLYFY_API_BASE_URL
logger = logging.getLogger(__name__)

# The header spellings OrgIdMiddleware accepts, as lowercase str (it holds them
# as bytes). Imported rather than re-listed so the two cannot drift: a spelling
# added there is automatically covered by the mismatch check below.
_ORG_ID_HEADER_NAMES = frozenset(name.decode("ascii") for name in ORG_ID_HEADERS)


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


class OrgIdMismatchError(TallyfyError):
    """Raised when a request header names a different organization than the token.

    The organization is decided by the ``org_id`` claim on the JWT, which
    FastMCP's ``JWTVerifier`` has already validated. A header naming a DIFFERENT
    organization is a client trying to act outside the consent its token was
    issued for, so the request is refused outright rather than quietly resolved
    either way — header-switching breaks visibly here instead of silently
    reading or writing another tenant's data (#744).

    Why a named class rather than raising ``TallyfyError(msg, status_code=400)``
    directly: ``TallyfyError.__init__`` takes ``status_code`` as a defaulted
    keyword, so a bare raise depends on every future call site remembering to
    pass 400 — the exact regression ``MissingOrgIdError`` overrides ``__init__``
    to prevent. A distinct type also lets callers and tests tell "two different
    organizations were named" apart from "no organization could be resolved",
    which one shared type cannot.

    Carries MissingOrgIdError's 400 shape so ``handle_tallyfy_errors`` demotes
    it to WARNING: a contradictory header is malformed client input, not a
    server bug, and should not page.
    """

    def __init__(self, token_org_id: str, header_org_id: str):
        super().__init__(
            f"Organization mismatch: this request's organization header names "
            f"'{header_org_id}', but your access token was issued for "
            f"organization '{token_org_id}'. The organization in the verified "
            f"token always wins, so the request was refused rather than acted "
            f"on in '{header_org_id}'. To resolve, do one of: "
            f"(1) drop the organization header from this request, or "
            f"(2) authenticate with a token issued for '{header_org_id}'.",
            status_code=400,
        )
        self.token_org_id = token_org_id
        self.header_org_id = header_org_id


def _client_supplied_org_id() -> Optional[str]:
    """The organization id THIS request's headers carry, or None.

    Deliberately not ``get_org_id()``. That ContextVar is set by
    ``OrgIdMiddleware`` from EITHER an explicit header OR the persistent
    user->org cache populated by an earlier authenticated request, so it cannot
    answer "did the client send a header on this request". Treating a cached
    value as a header would refuse a legitimate request from a user who has
    since been issued a token for a different organization, and keep refusing it
    for the whole 24 h cache TTL.

    ``get_http_headers()`` never raises; it returns ``{}`` when no HTTP request
    is in context. That can only cost the loud error, never the security
    property — the verified claim wins whether or not a header is visible here.
    """
    org_id = None
    for name, value in get_http_headers().items():
        # Last match wins, mirroring OrgIdMiddleware._extract_from_headers.
        if name in _ORG_ID_HEADER_NAMES and value:
            org_id = value
    return org_id


def get_authenticated_credentials() -> Tuple[str, str]:
    """
    Extract API key (JWT token) and org_id from the authenticated request context.

    This function runs AFTER JWTVerifier has validated the RS256 signature, so
    the token is trusted. The persistent user→org cache is written here (not in
    OrgIdMiddleware) to prevent pre-auth cache poisoning (P1-G).

    Organization resolution order (#744):

    1. The ``org_id`` claim on the VERIFIED token wins outright. It names the
       organization the OAuth consent was granted for, and it is the only
       statement of organization in the request that anything has checked.
    2. If the request ALSO carries an organization header naming a different
       organization, it is refused with ``OrgIdMismatchError`` rather than
       silently resolved either way.
    3. The request-scoped org id — an explicit header, or this user's
       organization persisted by an earlier authenticated request — is
       consulted only when the token carries no ``org_id`` claim at all. That
       is the header's legitimate use (a token valid for several organizations
       picking one) and it keeps working.
    4. ``TALLYFY_ORG_ID`` env var, last, unchanged.

    Before #744 this order was inverted: the client-supplied header was
    preferred over the verified claim, and whatever won was written into the
    persistent user→org cache, so a header could steer later requests that
    carried no header at all. ``org_id_middleware`` says in three separate
    docstrings that the header is observability-only and "MUST NEVER influence
    any authorization decision"; this function is what makes that true.

    Returns:
        Tuple of (api_key, org_id)

    Raises:
        OrgIdMismatchError: If a request header names a different organization
            than the verified token's ``org_id`` claim.
        MissingOrgIdError: If no org_id can be resolved from the JWT claim,
            header, persistent user→org cache, or TALLYFY_ORG_ID env var.
        Exception: If no access token is present (auth middleware misconfigured).
    """
    access_token = get_access_token()
    if not access_token:
        raise Exception("No authenticated user found. Please authenticate with a valid JWT token.")

    # Reuse claims already decoded by OrgIdMiddleware. Safe here because
    # get_access_token() confirms JWTVerifier validated the RS256 signature
    # before any tool handler runs — the claims are from the same token.
    token_claims = get_jwt_claims() or {}
    if not token_claims:
        try:
            token_claims = jwt.decode(access_token.token, options={"verify_signature": False})
        except jwt.DecodeError:
            token_claims = {}

    claim_org_id = token_claims.get('org_id')
    # Header OR this user's persisted organization — OrgIdMiddleware cannot
    # tell the two apart in this ContextVar, so neither can we.
    context_org_id = get_org_id()

    if claim_org_id:
        header_org_id = _client_supplied_org_id()
        if header_org_id and header_org_id != claim_org_id:
            raise OrgIdMismatchError(claim_org_id, header_org_id)
        if context_org_id and context_org_id != claim_org_id:
            # Not a client header — that case raised above. This is the user's
            # own organization persisted by an earlier request, now superseded
            # by a token issued for a different one. The claim wins; log it,
            # because this is the only place the divergence is visible.
            logger.warning(
                "Request-scoped org=%s superseded by verified token claim org=%s",
                context_org_id,
                claim_org_id,
            )
        org_id = claim_org_id
    else:
        # No org claim on the token, so the header (or this user's persisted
        # organization, or the env var) is the only thing that can resolve one.
        org_id = context_org_id or os.getenv("TALLYFY_ORG_ID")

    if not org_id:
        raise MissingOrgIdError(
            "Organization ID not found. To resolve, do one of: "
            "(1) include the X-Organization-ID header on this request, "
            "(2) ensure your OAuth access token contains an 'org_id' claim, "
            "or (3) set the TALLYFY_ORG_ID environment variable on the MCP client."
        )

    # Persist org_id for this user so subsequent requests (which may omit
    # the X-Organization-ID header) can look it up. This is always the org
    # actually returned above — the verified claim, or a header/env value
    # accepted only because the token named no organization at all. A header
    # that lost to a claim is never written here.
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


def get_jwt_scopes() -> Tuple[str, ...]:
    """OAuth scopes carried by the VERIFIED access token for this request.

    Sibling of ``get_user_id_from_token``: a small read of the already-verified
    request context, so a caller that needs to make an authorization decision
    does not re-implement the auth plumbing at its own call site.

    Reads ``AccessToken.scopes``, which fastmcp's ``JWTVerifier`` populates from
    the token's ``scope`` / ``scp`` claim AFTER the RS256 signature has been
    checked. Deliberately NOT taken from ``get_jwt_claims()``: those come from
    an UNVERIFIED decode and are documented there as observability-only, so
    they must never drive an authorization decision.

    Returns:
        The scopes as a tuple. An empty tuple means this caller PROVED no
        scope — either no access token is present or the token carries none.
        Callers must treat that as "no authority" and fail CLOSED; see
        ``utils.tallyfy_endpoint_allowlist.check`` and #746.

    Note:
        ``AccessToken.scopes`` is a required ``list[str]`` on the shipped model,
        so the defensive read below is for a duck-typed or subclassed provider,
        not for a shape the current SDK can produce.
    """
    access_token = get_access_token()
    if access_token is None:
        return ()

    scopes = getattr(access_token, "scopes", None)
    if not scopes:
        return ()

    return tuple(str(scope) for scope in scopes)


def get_mcp_scopes() -> Optional[Tuple[str, ...]]:
    """The ``mcp_scopes`` claim on the VERIFIED access token, or ``None``.

    This is the claim that names what the user approved on the MCP consent
    screen. It is NOT the same thing as ``get_jwt_scopes()``: fastmcp's
    ``JWTVerifier._extract_scopes`` reads the ``scope`` / ``scp`` claims, and a
    Tallyfy MCP token carries neither. Its Passport ``scopes`` claim is an
    organization allowlist that ``McpAccessTokenService::issue`` deliberately
    mints as ``[]``, with the MCP permissions in ``mcp_scopes`` beside it. So
    ``AccessToken.scopes`` cannot answer this question and never could.

    The claim is read by decoding ``access_token.token`` -- the exact string
    fastmcp's ``JWTVerifier`` already validated the RS256 signature of before
    any tool handler or tool middleware ran. Signature verification is disabled
    on this decode because it has already happened, upstream, on this same
    string; this is the identical pattern ``get_authenticated_credentials``
    uses. It deliberately does NOT read ``org_id_middleware.get_jwt_claims()``,
    whose claims come from an unverified pre-auth decode and are documented
    there as observability-only.

    Returns:
        A tuple of scope strings when the token carries an ``mcp_scopes`` claim
        that is a JSON array, or ``None`` otherwise.

        ``None`` and ``()`` mean different things and callers must not conflate
        them. ``None`` means the token is not MCP-issued and must NOT be scope
        gated -- chat.tallyfy.com and the desktop AI shell forward the user's
        raw Tallyfy session token, which carries no such claim, so gating on
        its absence would 403 Tallyfy's own products. ``()`` means an MCP token
        that was granted nothing, which IS gated.

        A present-but-not-a-list claim returns ``None`` and logs a warning. That
        mirrors api-v2's ``mcpScopesFromRequest``, which returns null unless
        ``is_array($scopes)``; diverging would mean one token that api-v2
        accepts and this server refuses. Nothing mints such a token today --
        ``normaliseScopes`` refuses to -- so the warning exists to make it
        visible rather than to handle an expected case.
    """
    access_token = get_access_token()
    if access_token is None:
        return None

    try:
        claims = jwt.decode(access_token.token, options={"verify_signature": False})
    except jwt.PyJWTError:
        # Unreachable for a token that passed RS256 verification. Fail the same
        # way an absent claim does rather than inventing an empty grant set,
        # which would deny every gated tool on a token we simply could not read.
        logger.warning("Could not decode a verified access token to read mcp_scopes")
        return None

    raw = claims.get("mcp_scopes")
    if raw is None:
        return None
    if not isinstance(raw, list):
        logger.warning(
            "Ignoring non-list mcp_scopes claim of type %s; treating this token "
            "as not MCP-issued, as api-v2's own middleware does",
            type(raw).__name__,
        )
        return None

    return tuple(str(scope) for scope in raw)