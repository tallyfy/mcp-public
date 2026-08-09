"""
Tallyfy Authentication Provider

Custom auth provider extending JWTVerifier with:
- RS256 signature verification using Tallyfy's public key (primary trust mechanism)
- Per-user org_id storage for session persistence
- MCP resource verification via custom `mcp_resource` JWT claim

Note: Tallyfy's authorization server does not include an 'iss' claim in JWT payloads.
Token authenticity is guaranteed by RS256 signature verification — only Tallyfy holds
the private key that corresponds to the configured public key.

Key resolution (see `build_auth_provider` below): the RS256 verification key comes
from `TALLYFY_PUBLIC_KEY` when it is set, and otherwise from Tallyfy's published
JWKS document at `{TALLYFY_JWKS_BASE}/.well-known/jwks.json`. Either way a token
must carry a valid RS256 signature; there is no mode in which an unsigned or
unverifiable token is accepted.

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
from urllib.parse import urlparse
import jwt
from typing import FrozenSet, Iterable, Optional, Dict, List, Union
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.auth import TokenVerifier
from mcp.server.auth.provider import AccessToken
from constants import (
    ACCEPTED_MCP_RESOURCES,
    MCP_JWT_AUDIENCE,
    MCP_RESOURCE_URL,
    TALLYFY_ENVIRONMENT,
    TALLYFY_ISSUER,
    TALLYFY_JWKS_BASE,
    ENFORCE_AUDIENCE,
)
from metrics import record_jwt_validation, record_jwt_audience_class
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


def normalise_accepted_resources(
    expected_audience: Optional[Union[str, Iterable[str]]],
) -> FrozenSet[str]:
    """Turn a caller's ``expected_audience`` into the set the check compares against.

    ``None`` means "no opinion", so the server accepts its own canonical resource
    identifier alongside the legacy literal (``ACCEPTED_MCP_RESOURCES``). A caller
    that names values explicitly gets exactly those and nothing added, so an
    operator can still pin the accept-set to one value.

    A string is one value, not an iterable of characters. That distinction is the
    whole reason this is a function: ``frozenset("mcp-host")`` is a set of eight
    letters, which would accept the token whose resource claim is the single
    character ``"m"`` and reject the real one.
    """
    if expected_audience is None:
        return ACCEPTED_MCP_RESOURCES
    if isinstance(expected_audience, str):
        return frozenset({expected_audience})
    return frozenset(expected_audience)


# ---------------------------------------------------------------------------
# Shadow audience census (tallyfy/mcp#743 AC1)
# ---------------------------------------------------------------------------
#
# AC1 says an inventory of who relies on the `aud == "1"` arm must come from
# production, not from reasoning, and that nothing else on #743 should ship
# until it exists. This is that inventory. It changes no behaviour: every
# caller counts and then returns exactly what it would have returned.
#
# The vocabulary is CLOSED and is asserted as a complete set in
# tests/unit/server/test_audience_census.py. A value leaving this tuple is the
# direction neither a green run nor a diff review surfaces, so it is pinned by
# name rather than sampled.
AUDIENCE_CLASSES = (
    "resource_url",         # mcp_resource == MCP_RESOURCE_URL (canonical RFC 8707 id)
    "legacy_mcp_host",      # mcp_resource == MCP_JWT_AUDIENCE (the "mcp-host" literal)
    "first_party_client",   # aud == "1", the Passport client id: chat.tallyfy.com
    "vault",                # aud == "tallyfy-vault", api-v2's VaultSessionService
    "none",                 # carries neither claim at all
    "unclassified",         # a shape nobody anticipated - see below
)

# api-v2's VaultSessionService signs this with the SAME key and the SAME kid as
# every other Tallyfy token, so a Vault session token verifies cryptographically
# here and reaches the accept block. It is a real population, not a curiosity,
# and folding it silently into `unclassified` would hide it inside the one
# bucket that is supposed to mean "we do not know what this is".
VAULT_AUDIENCE = "tallyfy-vault"

# The Laravel Passport OAuth CLIENT id. Not a resource identifier - it names the
# caller, not this server - which is exactly why #743 wants it gone.
PASSPORT_CLIENT_AUD = "1"


# Substrings that make a claim NAME look like it is trying to be an audience or
# resource identifier. Deliberately narrow: these three catch a rename
# (`mcp_resource_v2`, `resource`, `mcp_audience`, `aud_v2`) without matching the
# claims Tallyfy tokens genuinely carry today - sub, exp, iat, nbf, jti, scopes,
# org_id, mcp_scopes, client_id, azp - none of which contains any of them.
_AUDIENCE_SHAPED_KEY_MARKERS = ("resource", "audience", "aud")


def _has_audience_shaped_key(claims: dict) -> bool:
    """True if some claim NAME looks audience-shaped but is not one we read.

    Kept separate from classify_audience so the rename tripwire can be asserted
    on directly, and so it is obvious that only keys are inspected.
    """
    for key in claims:
        if not isinstance(key, str):
            continue
        if key in ("aud", "mcp_resource"):
            continue
        lowered = key.lower()
        if any(marker in lowered for marker in _AUDIENCE_SHAPED_KEY_MARKERS):
            return True
    return False


def classify_audience(claims, accepted_resources: FrozenSet[str]) -> str:
    """Name the audience class of one token's claims. Pure; never raises.

    **Precedence mirrors the accept block in `verify_token`, and that is the
    whole value of this function.** There, `mcp_resource` is checked first and
    `aud == "1"` is only a fallback, so a token carrying BOTH is admitted by the
    resource arm and would survive the removal of the legacy one. Classifying it
    as `first_party_client` would overstate the population that breaks when
    tallyfy/mcp#743 deletes that arm, and the census exists precisely to size
    that population. Read top to bottom: the first arm that matches wins, in the
    same order the real check tries them.

    **Mirroring means mirroring the FALL-THROUGH too, which is the half that is
    easy to get wrong.** The real check is `if <resource arm> ... elif str(aud)
    == "1"`, so a token whose `mcp_resource` is present but NOT accepted - a
    foreign URL, an empty string, a list - does not stop at the resource arm. It
    falls through and is admitted by the legacy arm. An earlier cut of this
    function returned `unclassified` for exactly those shapes, which under-counted
    `first_party_client` by every token that names something we do not accept
    while carrying `aud: "1"` - and each of those 401s the day the arm is
    deleted, which is the one number this census exists to produce. So the
    resource arm here returns only on a MATCH, never on a miss.

    `unclassified` is load-bearing rather than a dumping ground. A token that
    carries an audience-shaped claim we do not recognise - a renamed claim, a
    new issuer, a list where a string belongs - must be visible as its own
    number, because the failure this whole census guards against is a claim
    rename downstream silently zeroing every bucket. A census reading zero
    everywhere is indistinguishable from a healthy one unless something says
    "these tokens arrived and I could not name them".

    Args:
        claims: Decoded JWT claims, or None if the token could not be decoded.
            Values are attacker-supplied, so every branch is type-guarded and
            no claim value ever becomes a metric label.
        accepted_resources: The provider's own accept-set, passed in rather than
            imported so the census cannot drift from the check it describes.

    Returns:
        One of AUDIENCE_CLASSES. Always a str, so the caller can label safely.
    """
    if not isinstance(claims, dict):
        return "unclassified"

    mcp_resource = claims.get("mcp_resource")
    aud = claims.get("aud")

    # `isinstance` before the set test for the same reason the accept block does
    # it: `mcp_resource` can arrive as a list or a dict, and `unhashable in
    # frozenset` raises TypeError. A census that can 500 the auth path is worse
    # than no census.
    # ---- Arm 1: the resource arm, tried first exactly as verify_token does.
    # Returns ONLY on a match. Every miss falls through to arm 2 below, because
    # the real check is an `elif`, not a second `if`.
    if isinstance(mcp_resource, str):
        # MCP_RESOURCE_URL first. If an operator has misconfigured the two env
        # vars to the same string, the token is reported under the canonical
        # name, which is the truthful answer to "was this an RFC 8707 token".
        if mcp_resource == MCP_RESOURCE_URL:
            return "resource_url"
        if mcp_resource == MCP_JWT_AUDIENCE:
            return "legacy_mcp_host"
        # In the accept-set but matching neither constant: an operator has
        # widened it by hand. Accepted by the real check, so count it as a
        # resource-named token rather than pretending we do not understand it.
        if mcp_resource in accepted_resources:
            return "resource_url"

    # ---- Arm 2: the legacy `aud == "1"` arm.
    # `str(aud)` matches the live arm character for character, so the count of
    # `first_party_client` is exactly the population that 401s the day the arm
    # is deleted - INCLUDING tokens that also carry an unaccepted `mcp_resource`,
    # which reach here by the fall-through described in the docstring. `str(None)`
    # is `"None"` and matches neither literal, so an absent claim needs no guard.
    aud_str = str(aud)
    if aud_str == PASSPORT_CLIENT_AUD:
        return "first_party_client"

    # ---- Neither arm accepts. Everything below is a class that a flip of
    # ENFORCE_JWT_AUDIENCE to "true" REJECTS today, which is the opposite half
    # of the gate and the half that is easy to leave undocumented.
    if aud_str == VAULT_AUDIENCE:
        return "vault"

    if mcp_resource is not None or aud is not None:
        # It named SOMETHING - a resource we do not accept, a non-string shape
        # nobody anticipated, or an `aud` matching neither literal (an RFC 7519
        # array audience stringifies to exactly this). Not "none", and not a
        # class we can act on: `unclassified` is the honest answer.
        return "unclassified"

    # Neither claim we read is present. Before calling that "none", check
    # whether the token carries an audience-shaped claim under a name we do
    # not read - which is the single failure this whole census exists to
    # survive. If api-v2 renames `mcp_resource`, every MCP token starts
    # arriving with neither claim, and a classifier that answers "none"
    # reports a clean, confident, entirely wrong zero for the population
    # #743 is about to 401. Answering "unclassified" instead makes the
    # rename visible as a number on the day it happens.
    #
    # KEY NAMES ONLY, never values: a label is derived from the fixed
    # vocabulary regardless, and reading a value here would be the first
    # step toward the cardinality bug the counter's declaration warns about.
    if _has_audience_shaped_key(claims):
        return "unclassified"
    return "none"


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
        public_key: Optional[str] = None,
        expected_audience: Optional[Union[str, Iterable[str]]] = None,
        expected_issuer: Optional[str] = None,
        jwks_uri: Optional[str] = None,
        **kwargs
    ):
        """
        Initialize the auth provider.

        Exactly one key source must be given, and the parent JWTVerifier enforces
        that: it raises if both `public_key` and `jwks_uri` are missing, and also
        if both are supplied. There is no keyless construction.

        Args:
            public_key: PEM-encoded RSA public key for JWT signature verification
            expected_audience: The value, or values, a token's `mcp_resource`
                claim may carry and still name this server. A bare string means
                exactly that one value. If None, the accept-set is
                ACCEPTED_MCP_RESOURCES - the canonical resource URL plus the
                legacy literal.
            expected_issuer: Recorded, never enforced. See the note in the body:
                Tallyfy's MCP tokens carry no `iss` claim, so enforcing one would
                reject every token. Used only to derive a JWKS URL.
            jwks_uri: URL of Tallyfy's JWKS document, used instead of `public_key`.
                Keys are fetched lazily on the first token verification (never at
                construction), so a server configured this way still starts offline
                and simply rejects every token until the JWKS can be read.
        """
        super().__init__(public_key=public_key, jwks_uri=jwks_uri, **kwargs)
        self.expected_audience = expected_audience or MCP_JWT_AUDIENCE

        # The set the `mcp_resource` check actually compares against. Kept
        # separate from `expected_audience` above, which callers and tests read
        # back verbatim and which must therefore stay whatever was passed in.
        #
        # Before this existed the check was `mcp_resource == self.expected_audience`,
        # a scalar comparison, which had two consequences worth naming because
        # neither surfaced as an error:
        #   - the canonical resource URL this server publishes in
        #     /.well-known/oauth-protected-resource was NOT accepted, so a
        #     correctly-formed RFC 8707 token would have been rejected; and
        #   - the `List[str]` this signature has always advertised could never
        #     match, since a list is never equal to a string.
        self.accepted_resources: FrozenSet[str] = normalise_accepted_resources(
            expected_audience
        )

        # NOT ENFORCED ANYWHERE, and that is deliberate rather than an oversight.
        # Tallyfy's authorization server mints MCP tokens with no `iss` claim at
        # all - see api-v2 McpAccessTokenService, whose payload is exactly
        # aud/jti/iat/nbf/exp/sub/scopes/org_id/mcp_scopes/mcp_resource. So
        # forwarding `issuer=` to the parent JWTVerifier would reject 100% of
        # real tokens, not tighten anything. This attribute exists because
        # callers pass it and build_auth_provider() uses the PARAMETER to derive
        # a JWKS URL; it is not an input to any check.
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

        # The parent handles SIGNATURE VERIFICATION and EXPIRY. It does not check
        # issuer, audience or scopes here, because this class constructs it
        # without `issuer=` or `audience=` - see __init__ for why forwarding
        # either would reject every real token. The resource check below is the
        # only audience-shaped check that runs, and scopes are unenforced
        # pending tallyfy/mcp#559.
        access_token = await super().verify_token(token)

        if access_token is None:
            logger.debug("JWT signature/expiration verification failed")
            record_jwt_validation('failed')
            return None

        if not claims:
            logger.warning("Failed to decode JWT claims")
            record_jwt_validation('failed')
            return None

        # SHADOW CENSUS (tallyfy/mcp#743 AC1). Counts, changes nothing.
        #
        # ⚠️ This sits OUTSIDE the `if ENFORCE_AUDIENCE == "true":` block below,
        # deliberately and load-bearingly. That flag is explicitly "false" in
        # production AND staging (measured 2026-08-09 from the running
        # containers), so anything inside that block executes nowhere we run and
        # a census placed there would report a confident zero forever. Moving
        # these two lines inside it silently destroys the measurement while
        # leaving the counter present in /metrics, which is worse than deleting
        # it. `test_census_runs_when_enforcement_is_off` goes red if it moves.
        #
        # The population counted is every token that has passed RS256 signature
        # verification and yielded decodable claims: the tokens whose acceptance
        # #743 proposes to narrow. Tokens rejected before this point were never
        # candidates for that narrowing.
        record_jwt_audience_class(
            classify_audience(claims, self.accepted_resources)
        )

        # Accept two token types:
        # 1. MCP-issued tokens, whose `mcp_resource` claim names this server -
        #    either by its canonical resource URL or by the legacy literal. See
        #    ACCEPTED_MCP_RESOURCES in constants.py for why both are live.
        # 2. Passport tokens: aud == "1" (Laravel Passport OAuth client ID).
        #
        # LEGACY-BYPASS (dated 2026-08-09): arm 2 is NOT dead code. It is the
        # live authentication path for chat.tallyfy.com, Tallyfy's own AI
        # sidebar, which presents a first-party client-UI JWT carrying no
        # `mcp_resource` at all. Deleting it 401s that product for every user.
        # It may be removed only by tallyfy/mcp#743, after the host is migrated
        # onto a real MCP token and after the audience census reads zero for
        # this population for a full week. Do not narrow it here.
        if ENFORCE_AUDIENCE == "true":
            mcp_resource = claims.get("mcp_resource")
            aud = claims.get("aud")
            # `isinstance` first, and not for tidiness: `claims` is decoded from
            # an attacker-supplied JWT, so `mcp_resource` can be a list or a
            # dict. `unhashable in frozenset` raises TypeError, which would turn
            # a token that should 401 into a 500. Non-strings fall through to
            # the reject branch.
            if isinstance(mcp_resource, str) and mcp_resource in self.accepted_resources:
                pass
            elif str(aud) == "1":
                pass
            else:
                logger.warning(
                    "JWT rejected: mcp_resource=%r aud=%r (expected mcp_resource in %r or aud='1')",
                    mcp_resource,
                    aud,
                    sorted(self.accepted_resources),
                )
                record_jwt_validation('invalid_token')
                return None

        record_jwt_validation('success')
        return access_token


class NoVerificationKeyVerifier(TokenVerifier):
    """Fail-closed verifier used when no RS256 verification key can be resolved.

    This exists so the process can START without a key, not so it can WORK
    without one. It rejects every bearer token unconditionally, which is
    strictly more restrictive than a configured verifier: there is no code
    path through this class that can return an AccessToken. A deployment that
    lands here can serve `/health` and the unauthenticated OAuth discovery
    routes, and nothing else - every MCP request is answered with the usual
    401 challenge, so no Tallyfy data is reachable.

    Reached only when `TALLYFY_PUBLIC_KEY` is unset AND no usable https JWKS
    URL can be derived from the configured base. See `build_auth_provider`.
    """

    def __init__(self, reason: str, **kwargs):
        super().__init__(**kwargs)
        self.reason = reason
        self._warned = False

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        """Reject every token. Never returns an AccessToken."""
        if not self._warned:
            # Log the cause once per process; the per-request path stays quiet
            # so a scanner cannot flood the logs.
            logger.error(
                "Rejecting all bearer tokens: no JWT verification key is configured (%s). "
                "Set TALLYFY_PUBLIC_KEY, or point TALLYFY_JWKS_BASE (or TALLYFY_JWKS_URI) "
                "at an https:// location that publishes /.well-known/jwks.json.",
                self.reason,
            )
            self._warned = True
        record_jwt_validation('failed')
        return None


def https_url_or_none(url: Optional[str]) -> Optional[str]:
    """Return the stripped URL when it is a usable https URL, else None.

    Every URL we would fetch signing keys from goes through here, whether it was
    derived from a base or handed to us whole. Fetching a key over plaintext http
    would let a network attacker substitute their own key and mint tokens we would
    then accept, so a non-https URL is refused and the caller falls back to
    rejecting every token.
    """
    if not url:
        return None
    candidate = url.strip()
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return candidate


def derive_jwks_uri(base: Optional[str]) -> Optional[str]:
    """Derive a JWKS URL from a base origin, or None when it cannot be trusted.

    Returns ``{base}/.well-known/jwks.json`` - the location Tallyfy's own
    OAuth authorization-server metadata advertises, the one api-v2's
    ``MCP\\OAuthController::jwks`` serves, and the same URL this server's own
    ``routes/oauth.py`` already proxies.

    Only ``https`` bases are accepted, per :func:`https_url_or_none`.
    """
    checked = https_url_or_none(base)
    if not checked:
        return None
    return f"{checked.rstrip('/')}/.well-known/jwks.json"


def build_auth_provider(
    public_key: Optional[str] = None,
    expected_audience: Optional[Union[str, Iterable[str]]] = None,
    expected_issuer: Optional[str] = None,
    jwks_uri: Optional[str] = None,
    jwks_base: Optional[str] = None,
) -> TokenVerifier:
    """Build the server's token verifier from whatever key material is available.

    Resolution order, most explicit first:

    1. ``TALLYFY_PUBLIC_KEY`` - the production configuration. A key that is set
       but malformed raises, because a typo in a configured key is an operator
       error that should be loud rather than silently downgraded.
    2. An explicit ``TALLYFY_JWKS_URI``, else ``{jwks_base}/.well-known/jwks.json``.
       BOTH must be https: an override that skipped that check would reopen the
       plaintext-MITM hole through a different env var, so a non-https override
       drops straight to branch 3 rather than being ignored or substituted.
       ``jwks_base`` defaults to ``TALLYFY_JWKS_BASE``, which is already
       environment-aware and is already what ``routes/oauth.py`` proxies for the
       same document, so the verifier and the proxy cannot disagree about where
       Tallyfy's keys live. Signature verification is unchanged; the key is
       simply fetched over TLS instead of being pasted into the environment.
       Nothing is fetched at construction, so this cannot block or fail startup.
    3. :class:`NoVerificationKeyVerifier` - rejects every token.

    Every branch verifies an RS256 signature or refuses the token. None of them
    disables authentication, and none of them accepts an unsigned token.
    """
    if public_key:
        _assert_pem_public_key(public_key)
        return TallyfyAuthProvider(
            public_key=public_key,
            expected_audience=expected_audience,
            expected_issuer=expected_issuer,
        )

    # An explicit override must clear the SAME https bar as a derived URL.
    # Skipping it here would reopen the plaintext-MITM hole the derivation
    # closes, just through a different env var.
    #
    # Bugbot's autofix for this finding zeroed the override and fell through to
    # the derived base. That is equally safe against the MITM, but it silently
    # swaps in a DIFFERENT key document than the operator named, and this file
    # already sets the opposite precedent one branch up: a TALLYFY_PUBLIC_KEY
    # that is set but malformed raises rather than being quietly downgraded. So
    # a set-but-unusable override refuses here too. The server still boots; it
    # just authenticates nobody until the config is fixed.
    if jwks_uri and not https_url_or_none(jwks_uri):
        return NoVerificationKeyVerifier(
            reason=f"TALLYFY_JWKS_URI {jwks_uri!r} is not an https URL"
        )

    resolved_jwks_uri = https_url_or_none(jwks_uri) or derive_jwks_uri(
        jwks_base or TALLYFY_JWKS_BASE or expected_issuer or TALLYFY_ISSUER
    )
    if resolved_jwks_uri:
        logger.warning(
            "TALLYFY_PUBLIC_KEY is not set. Falling back to Tallyfy's published "
            "JWKS at %s for RS256 verification. Tokens are still signature-verified; "
            "if that document cannot be fetched, every token is rejected. Set "
            "TALLYFY_PUBLIC_KEY to pin the key and remove the network dependency.",
            resolved_jwks_uri,
        )
        return TallyfyAuthProvider(
            jwks_uri=resolved_jwks_uri,
            expected_audience=expected_audience,
            expected_issuer=expected_issuer,
        )

    return NoVerificationKeyVerifier(
        reason=f"TALLYFY_PUBLIC_KEY unset and no https JWKS URL derivable from "
               f"base {jwks_base or TALLYFY_JWKS_BASE or expected_issuer or TALLYFY_ISSUER!r}"
    )


def _assert_pem_public_key(public_key: str) -> None:
    """Raise ValueError unless `public_key` parses as a PEM public key.

    Catches a truncated or mangled key at startup rather than on the first
    request. Silently skipped when `cryptography` is unavailable, matching the
    behaviour this check has had since it was introduced.
    """
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend
    except ImportError:
        logger.warning(
            "cryptography library not available; skipping public key format validation"
        )
        return

    try:
        serialization.load_pem_public_key(
            public_key.encode('utf-8'),
            backend=default_backend()
        )
    except (ValueError, TypeError) as e:
        raise ValueError(
            f"TALLYFY_PUBLIC_KEY is not a valid PEM-encoded RSA public key: {e}"
        ) from e


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
