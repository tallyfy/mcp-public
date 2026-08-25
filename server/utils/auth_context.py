"""
Authentication Context Utilities
Extract authenticated user credentials from MCP auth context
"""

import os
import time
import logging
import jwt
from typing import Optional, Tuple
# 🔴 READ THIS BEFORE "TIDYING" THE IMPORT BELOW.
#
# There are TWO functions called ``get_access_token`` and they answer different
# questions. Swapping one for the other is a one-line change with no visible
# symptom, and the wrong one silently forwards a DEAD credential.
#
#   mcp.server.auth.middleware.auth_context.get_access_token   <- STALE. Do not use.
#   fastmcp.server.dependencies.get_access_token               <- fresh. This one.
#
# The SDK accessor reads ``auth_context_var``, a ContextVar written in exactly one
# place: ``AuthContextMiddleware.__call__``, which runs in the ASGI REQUEST task.
# On the ``initialize`` POST, ``streamable_http_manager`` does
# ``await self._task_group.start(run_server)``; anyio copies the CALLER'S CONTEXT
# at that instant, so ``run_server`` inherits the token presented at the
# handshake. Every later message is dispatched by ``tg.start_soon(
# self._handle_message, ...)`` from inside ``run_server``, so each handler task
# copies that same handshake-time snapshot -- for the whole life of the session.
#
# ``_handle_request`` DOES rebuild ``request_ctx`` from the live Starlette request,
# so the REQUEST is fresh while the auth ContextVar is not. That asymmetry is what
# makes this invisible: everything else about the request is current.
#
# The session outlives the credential. ``_session_owners`` compares
# ``AuthorizationContext(client_id, issuer, subject)``, all of which survive a
# refresh, so a refreshed token keeps the same session, the same ``run_server``
# task and the same snapshot. Access tokens live 3600s (api-v2 ``config/mcp.php``),
# which is exactly the "works for a couple of hours then dies" a customer reported
# after a week of reconnecting by hand (#652).
#
# fastmcp already fixed this. Its accessor reads ``request.scope["user"]`` first,
# built by ``BearerAuthBackend.authenticate`` from THIS request's Authorization
# header, so it is fresh by construction, and falls back to the SDK ContextVar
# when there is no HTTP request (stdio, in-process clients, Docket workers). Its
# own docstring cites fastmcp issue #1863 and says the SDK var "may become stale
# after token refresh".
#
# The module-level NAME stays ``get_access_token`` deliberately: 26 existing tests
# patch ``utils.auth_context.get_access_token``, and rebinding the name costs zero
# test churn. That is also precisely why this comment is here -- those 26 tests
# pass just as happily against the reverted import, so they cannot protect this
# line. ``tests/regression/test_forwarded_token_source.py`` is what does.
from fastmcp.server.dependencies import get_http_headers, get_access_token
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


#: Closed label vocabulary for ``mcp_server_forwarded_token_freshness_total``.
#: Declared here rather than in metrics.py because this module is the only
#: emitter, and a vocabulary living apart from its single emitter is how the
#: two drift.
FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE_AVOIDED = "stale_avoided"
FRESHNESS_NO_REQUEST = "no_request"
_FRESHNESS_STATES = (FRESHNESS_FRESH, FRESHNESS_STALE_AVOIDED, FRESHNESS_NO_REQUEST)


def _record_freshness(forwarded_token: str) -> None:
    """Count whether the SDK ContextVar would have handed us a different token.

    This is the ONLY place in ``server/`` allowed to read the SDK accessor, and
    it reads it purely to compare. It never returns it and never forwards it.
    ``tests/regression/test_forwarded_token_source.py`` allowlists this one
    function by name; every other import of that symbol fails the build.

    Never raises. A metric must not be able to break an authenticated request.
    """
    try:
        from fastmcp.server.dependencies import get_http_request
        from mcp.server.auth.middleware.auth_context import (
            get_access_token as _sdk_get_access_token,
        )
        from metrics import record_forwarded_token_freshness

        try:
            get_http_request()
        except Exception:
            # stdio, an in-process FastMCP client, or a Docket worker. The SDK
            # ContextVar is the only source there and is not stale, because
            # there is no session outliving a request to make it so.
            record_forwarded_token_freshness(FRESHNESS_NO_REQUEST)
            return

        sdk_token = getattr(_sdk_get_access_token(), "token", None)
        if not sdk_token or sdk_token == forwarded_token:
            record_forwarded_token_freshness(FRESHNESS_FRESH)
        else:
            record_forwarded_token_freshness(FRESHNESS_STALE_AVOIDED)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not record forwarded-token freshness: %s", exc)


def _warn_if_already_expired(token_claims: dict, token_hint: str) -> None:
    """Log at ERROR if the credential about to be forwarded is past its own exp.

    After the fix this is impossible: ``TallyfyAuthProvider.load_access_token``
    pre-checks ``exp``, and fastmcp's ``BearerAuthBackend.authenticate``
    re-checks ``expires_at`` before it builds the ``AuthenticatedUser`` this
    request reads. So a line here means the chain broke again, which is exactly
    the state that produced #652 and produced no log line at all at the time.

    Only the fingerprint is logged, never the token.
    """
    try:
        exp = token_claims.get("exp")
        if exp is None:
            return
        if float(exp) < time.time():
            logger.error(
                "Forwarding a token that is already past its own exp "
                "(token=...%s, exp=%s). The freshness chain is broken; see "
                "utils/auth_context.py and issue #652.",
                token_hint,
                exp,
            )
    except (TypeError, ValueError):  # pragma: no cover - a non-numeric exp
        return


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

    # Decode the claims from THIS token, and fall back to OrgIdMiddleware's
    # pre-decoded claims only if that fails.
    #
    # The order used to be the other way round, under a comment asserting "the
    # claims are from the same token". That is exactly what is false across a
    # token refresh: get_jwt_claims() is another ContextVar written in the ASGI
    # request task, so it carries the SAME handshake-time snapshot the SDK
    # accessor does (see the import block at the top of this file). Reading it
    # first would re-introduce the staleness one line below the fix for it.
    #
    # Cost is one unverified decode per call, about 30 microseconds. The
    # signature was already checked upstream by fastmcp's JWTVerifier on this
    # exact string, which is why verify_signature=False is correct here and not
    # a shortcut. get_mcp_scopes() below already gives this identical reasoning
    # for deliberately not reading get_jwt_claims(); this now matches it.
    token_claims = {}
    try:
        token_claims = jwt.decode(access_token.token, options={"verify_signature": False})
    except jwt.DecodeError:
        token_claims = get_jwt_claims() or {}

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

    _record_freshness(access_token.token)
    _warn_if_already_expired(token_claims, token_hint)

    # Mark that this request actually resolved a credential to present upstream.
    # DownstreamAuthChallengeMiddleware's circuit breaker resets a session's
    # challenge count only on a success that got this far, because `initialize`
    # and `tools/list` never touch api-v2 and so succeed happily on a dead
    # credential. Resetting on those would defeat the breaker entirely while
    # every test still passed. This return is the single chokepoint -- 132 call
    # sites reach it, including tools/api_fallback.py, which builds its own
    # Authorization header from the value.
    from middleware.downstream_auth_challenge import note_credential_presented
    note_credential_presented()

    # The MCP specification forbids presenting the caller's own token to the
    # upstream API (2026-07-28, "Access Token Privilege Restriction", and again
    # in "Token Handling"). Swap it for a short-lived downstream token that
    # carries the same mcp_scopes and drops the mcp_resource claim, so api-v2
    # can refuse the caller's token everywhere else without refusing ours.
    #
    # This is the ONE place it can be done. Every credential a tool presents
    # comes through this return, so the swap reaches all of them with no
    # call-site edits. Doing it anywhere else means doing it 123 times and
    # missing one.
    #
    # Inert unless MCP_DOWNSTREAM_TOKEN_EXCHANGE is set; the default is "off".
    from utils.downstream_token import get_downstream_token
    api_key = get_downstream_token(access_token.token, org_id, token_claims)

    return api_key, org_id


def get_user_id_from_token() -> Optional[str]:
    """Extract user ID from the authenticated JWT token.

    Reads the token FIRST and ``get_jwt_claims()`` only as a fallback, for the
    same reason ``get_authenticated_credentials`` does: those claims come from a
    ContextVar written in the ASGI request task, so on a long-lived MCP session
    they are the handshake-time snapshot rather than this request's token. See
    the import block at the top of this file.
    """
    access_token = get_access_token()
    if access_token:
        try:
            claims = jwt.decode(access_token.token, options={"verify_signature": False})
            return claims.get('sub') or claims.get('user_id')
        except jwt.DecodeError:
            pass

    claims = get_jwt_claims()
    if claims:
        return claims.get('sub') or claims.get('user_id')

    return None


def get_jwt_scopes() -> Tuple[str, ...]:
    """OAuth ``scope`` / ``scp`` scopes on the VERIFIED access token.

    🔴 **UNUSABLE FOR AUTHORIZATION. It returns ``()`` for every real Tallyfy
    token, whatever the user actually approved on the consent screen.** Use
    ``get_mcp_scopes()`` below instead, and ``utils.tool_scopes.decide`` for
    how to decide over the result.

    Reads ``AccessToken.scopes``, which fastmcp's ``JWTVerifier`` populates from
    the token's ``scope`` / ``scp`` claim AFTER the RS256 signature has been
    checked. Deliberately NOT taken from ``get_jwt_claims()``: those come from
    an UNVERIFIED decode and are documented there as observability-only, so
    they must never drive an authorization decision.

    **A Tallyfy MCP token carries neither claim.**
    ``McpAccessTokenService::issue`` mints ``scopes: []`` on purpose (that field
    is a Passport ORGANIZATION allowlist, not a permission set) and puts the MCP
    permissions in a separate ``mcp_scopes`` claim beside it. So an empty tuple
    from here does not distinguish "granted nothing" from "granted everything,
    through a claim this function cannot see".

    This had exactly one caller, ``tools/api_fallback.py``, feeding
    ``utils.tallyfy_endpoint_allowlist.check``. Because that gate fails closed
    on an empty set, the pair DENIED every write to a rule-covered path for
    every real token, including one holding the required scope. Measured and
    fixed in #856; that caller now reads ``get_mcp_scopes()``.

    It is kept rather than deleted because it correctly answers a different
    question -- "what standard OAuth scopes did this token declare" -- and
    nothing in this repo asks that today.
    ``tests/unit/server/utils/test_auth_context_scope_sources.py`` fails if a
    module under ``server/`` starts calling it again, so a future caller is a
    deliberate act rather than a repeat of #856.

    Returns:
        The ``scope`` / ``scp`` scopes as a tuple. ``()`` covers three
        different states -- no access token, no such claim, an empty claim --
        and cannot tell them apart, which is the second reason it is unfit for
        an authorization decision.

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