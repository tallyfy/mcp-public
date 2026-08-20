"""
OAuth 2.1 Proxy and Discovery Endpoints for MCP Server

Provides OAuth 2.1 compatibility for external clients (ChatGPT, Claude Desktop).
Implements RFC 9728 (Protected Resource Metadata) and OpenID Connect Discovery.

Key Endpoints:
- Discovery:
  - /.well-known/oauth-protected-resource: Resource metadata (RFC 9728)
  - /.well-known/openid-configuration: Authorization server metadata
  - /.well-known/oauth-authorization-server: OAuth 2.0 AS metadata (RFC 8414)

- OAuth Proxy (forwards to Tallyfy Authorization Server):
  - POST /mcp/oauth/register: Dynamic Client Registration (RFC 7591)
  - GET  /mcp/oauth/authorize: Authorization endpoint
  - POST /mcp/oauth/token: Token endpoint

Environment Support:
- TALLYFY_ENVIRONMENT=staging|production controls which Tallyfy endpoints are used
- Individual endpoint overrides available via environment variables
"""

import json
import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import parse_qs

import httpx

from starlette.responses import JSONResponse, RedirectResponse, Response
from constants import (
    MCP_RESOURCE_URL,
    MCP_ALLOWED_HOSTS,
    TALLYFY_ENVIRONMENT,
    TALLYFY_AUTH_SERVER,
    TALLYFY_ISSUER,
    TALLYFY_JWKS_BASE,
    MCP_DOCS_URL,
    OAUTH_PROXY_TIMEOUT,
)

logger = logging.getLogger(__name__)

# Log the OAuth configuration on module load
logger.info(
    f"OAuth configuration: environment={TALLYFY_ENVIRONMENT}, "
    f"auth_server={TALLYFY_AUTH_SERVER}, issuer={TALLYFY_ISSUER}"
)

# Scopes supported by this MCP server (provided by Tallyfy Authorization Server)
SUPPORTED_SCOPES = [
    "mcp.users.read",
    "mcp.users.write",
    "mcp.tasks.read",
    "mcp.tasks.write",
    "mcp.processes.read",
    "mcp.processes.write",
    "mcp.templates.read",
    "mcp.templates.write",
    "mcp.forms.read",
    "mcp.forms.write",
    "mcp.automation.read",
    "mcp.automation.write",
]

# Valid host[:port] pattern. Only alphanumeric chars, dots, hyphens, and an
# optional numeric port are allowed. This rejects URL-special characters
# (@, /, ?, #, \) that can cause RFC 3986 authority-confusion attacks such as
# "mcp.tallyfy.com:@evil.com" — which passes a naive split(":")[0] hostname
# check but is parsed by RFC 3986 clients as host=evil.com (see issue #217).
_SAFE_HOST_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9.\-]*(:\d{1,5})?$')


def _get_base_url(request) -> str:
    """
    Get the base URL for this MCP server from the request.

    SECURITY (issue #217): An attacker can supply an ``X-Forwarded-Host`` or
    ``Host`` header pointing at a hostile server. The OAuth discovery document
    is served unauthenticated and downstream OAuth clients may treat the
    reflected ``issuer`` / ``authorization_endpoint`` as authoritative. To
    block that, we only honor header-derived hosts that:

      1. Match ``_SAFE_HOST_RE`` — rejects any host containing ``@``, ``/``,
         ``?``, ``#``, ``\\`` etc. that could cause RFC 3986 authority confusion
         (e.g. ``mcp.tallyfy.com:@evil.com`` passes a naive hostname split but
         encodes ``evil.com`` as the actual host).
      2. Appear in the ``MCP_ALLOWED_HOSTS`` allowlist (see ``constants.py``).

    Anything else falls back to ``MCP_RESOURCE_URL``.

    Only the host portion is validated — scheme is locked to https in
    production configurations because the MCP_RESOURCE_URL default is https.
    """
    try:
        raw_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        # Take only the first value if a list came through (some proxies append).
        first_host = raw_host.split(",")[0].strip()
        # Reject hosts containing URL-special characters before any further
        # processing. This prevents authority-confusion via e.g.
        # "mcp.tallyfy.com:@evil.com" which passes split(":")[0] but encodes
        # evil.com as the RFC 3986 host.
        if not _SAFE_HOST_RE.match(first_host):
            if first_host:
                logger.warning(
                    "Host header with invalid characters rejected | header=%r",
                    first_host,
                )
            return MCP_RESOURCE_URL
        # Strip port suffix for allowlist comparison (allowlist carries hostnames only).
        hostname = first_host.split(":")[0]
        if hostname and hostname in MCP_ALLOWED_HOSTS:
            scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
            # Defensive scheme allowlist — never reflect non-http(s) schemes.
            if scheme not in ("http", "https"):
                scheme = "https"
            return f"{scheme}://{first_host}"
        if hostname:
            logger.warning(
                "Unrecognized host header ignored | header=%r | allowlist=%s",
                first_host,
                sorted(MCP_ALLOWED_HOSTS),
            )
    except Exception as e:
        logger.debug("Host resolution exception (falling back to MCP_RESOURCE_URL): %s", e)
    return MCP_RESOURCE_URL


# ---------------------------------------------------------------------------
# RFC 6749 section 5.2 error normalisation (issue #849)
# ---------------------------------------------------------------------------
#
# The token endpoint is a proxy in front of Tallyfy's authorization server, and
# that server answers a failed grant with a Laravel-shaped body:
#
#     {"error": true, "message": "...", "code": "...", "errors": {...}}
#
# ``error`` is a BOOLEAN there. RFC 6749 section 5.2 requires it to be one of a
# fixed set of error CODE strings, so an OAuth client reading that body finds no
# error code at all. Worse, Laravel renders HTML and 302s back to the login page
# whenever the request does not explicitly ask for JSON, and this proxy used to
# forward the caller's ``Accept`` header verbatim. Measured 2026-08-15 against
# production, same request body, only the Accept header varied:
#
#     Accept: */*               -> 500 text/html      (HTML error page)
#     Accept: */*               -> 302 text/html      (redirect to the login page)
#     Accept: application/json  -> 500 application/json  {"error": true, ...}
#     Accept: application/json  -> 422 application/json  {"error": true, ...}
#
# So the two fixes are: always ask upstream for JSON, and rewrite anything that
# is not already an RFC 6749 error object.

# RFC 6749 section 5.1 requires these on every token endpoint response, success
# or failure, so no client or intermediary caches a credential.
_NO_STORE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}

# Sent upstream instead of the caller's Accept header. The token and
# registration endpoints are JSON-only by specification, so there is no content
# negotiation to honour, and honouring it is what produced the HTML pages above.
_JSON_ONLY_ACCEPT = "application/json"

# Cap on how much upstream prose is echoed back in error_description.
_MAX_DESCRIPTION_CHARS = 500


def _oauth_error_response(error: str, description: str, status_code: int) -> JSONResponse:
    """Build an RFC 6749 section 5.2 error response.

    Args:
        error: An OAuth error CODE (a string such as ``invalid_grant``).
        description: Human-readable detail. Empty string omits the key.
        status_code: HTTP status to answer with.

    Returns:
        A JSONResponse carrying ``application/json`` and the no-store headers.
    """
    body: Dict[str, Any] = {"error": error}
    if description:
        body["error_description"] = description[:_MAX_DESCRIPTION_CHARS]
    return JSONResponse(body, status_code=status_code, headers=dict(_NO_STORE_HEADERS))


def _decode_json_object(content: bytes) -> Optional[Dict[str, Any]]:
    """Parse an upstream body as a JSON object, or return None.

    Returns None for an HTML page, an empty body, malformed JSON, or a JSON
    value that is not an object.
    """
    try:
        parsed = json.loads(content)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_oauth_error_object(payload: Optional[Dict[str, Any]]) -> bool:
    """True when the upstream body is ALREADY an RFC 6749 error object.

    The discriminator is that ``error`` holds a non-empty STRING. Tallyfy puts a
    boolean there, so this is exactly what separates a compliant body from a
    Laravel one, and it must not be relaxed to a truthiness check.
    """
    if not payload:
        return False
    error = payload.get("error")
    return isinstance(error, str) and error != ""


def _upstream_description(payload: Optional[Dict[str, Any]], fallback: str) -> str:
    """Pull the most useful human-readable line out of an upstream error body."""
    if payload:
        for key in ("error_description", "message", "code"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return fallback


def _names_field(payload: Optional[Dict[str, Any]], field: str) -> bool:
    """True when a Laravel validation body blames ``field``."""
    if not payload:
        return False
    errors = payload.get("errors")
    return isinstance(errors, dict) and field in errors


def _requested_grant_type(body: bytes, content_type: str) -> str:
    """Best-effort read of the caller's ``grant_type``. Never raises.

    Used to tell "no grant type was supplied" (RFC 6749 ``invalid_request``)
    from "the supplied grant type was rejected" (``unsupported_grant_type``).
    Deciding from the REQUEST avoids parsing the upstream's English validation
    prose, which would break the moment Laravel rewords a message.
    """
    try:
        if "json" in (content_type or "").lower():
            payload = _decode_json_object(body)
            value = payload.get("grant_type") if payload else None
            return value if isinstance(value, str) else ""
        parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        values = parsed.get("grant_type") or []
        return values[0] if values else ""
    except Exception:  # pragma: no cover - defensive, must never break the proxy
        return ""


def _normalize_token_error(status_code: int, content: bytes, grant_type: str) -> JSONResponse:
    """Map an upstream token-endpoint failure onto RFC 6749 section 5.2.

    Mapping, derived from what production actually answers rather than guessed:

    ======  ==================================================  ===============
    status  upstream shape                                      answer
    ======  ==================================================  ===============
    3xx     HTML redirect to the login page                     502 server_error
    401/403 anything not already OAuth-shaped                   401 invalid_client
    422     Laravel validation, ``errors.grant_type`` present   400 unsupported_grant_type
    422     any other Laravel validation failure                400 invalid_request
    4xx     anything else                                       same invalid_request
    5xx     ``{"error": true, "message": "Internal error..."}`` same server_error
    ======  ==================================================  ===============

    A body that is already an RFC 6749 error object is passed through unchanged
    apart from the headers, because rewriting a compliant upstream code would
    lose information the authorization server took care to send.
    """
    payload = _decode_json_object(content)

    if _is_oauth_error_object(payload):
        # 3xx and 422 are still not legal token-endpoint statuses even when the
        # body is well formed, so normalise those; keep everything else.
        out_status = status_code
        if status_code < 400:
            out_status = 502
        elif status_code == 422:
            out_status = 400
        return JSONResponse(payload, status_code=out_status, headers=dict(_NO_STORE_HEADERS))

    if status_code < 400:
        return _oauth_error_response(
            "server_error",
            "The authorization server answered the token request with an HTTP "
            f"{status_code} redirect, which is not a valid token endpoint response.",
            502,
        )

    if status_code in (401, 403):
        # 401 is what RFC 6749 section 5.2 requires when the client authenticated
        # through the Authorization header, and our discovery document advertises
        # client_secret_basic, so this must stay a 401 rather than becoming a 400.
        #
        # NOTE, so nobody reads more into this than is true: middleware/auth_error.py
        # intercepts EVERY 401 and 403 leaving this app, and /mcp/oauth/token is
        # deliberately not in its SKIP_PATHS (see server/CLAUDE.md, "The 401
        # challenge"). It preserves this error_description but only preserves an
        # ``error`` code drawn from its own allowlist, which does not include
        # ``invalid_client``, so the code a caller finally sees on this path is
        # ``invalid_token`` plus a correct WWW-Authenticate header. That is a
        # deliberate design in that middleware, not a defect here, and no
        # acceptance criterion on issue #849 depends on this branch. Left correct
        # at this layer so the right thing happens if that allowlist ever widens.
        return _oauth_error_response(
            "invalid_client",
            _upstream_description(payload, "Client authentication failed."),
            401,
        )

    if status_code == 422:
        if grant_type and _names_field(payload, "grant_type"):
            return _oauth_error_response(
                "unsupported_grant_type",
                _upstream_description(payload, "The requested grant type is not supported."),
                400,
            )
        return _oauth_error_response(
            "invalid_request",
            _upstream_description(payload, "The token request failed validation."),
            400,
        )

    if status_code >= 500:
        return _oauth_error_response(
            "server_error",
            _upstream_description(
                payload, "The authorization server failed to process the token request."
            ),
            status_code,
        )

    return _oauth_error_response(
        "invalid_request",
        _upstream_description(payload, "The token request was rejected."),
        status_code,
    )


def _normalize_registration_error(status_code: int, content: bytes) -> JSONResponse:
    """Same treatment for Dynamic Client Registration (RFC 7591 section 3.2.2).

    The registration endpoint already rewrote Laravel-shaped JSON, but an HTML
    body fell through to a raw passthrough carrying ``Content-Type: text/html``,
    which an OAuth client can no more read here than on the token endpoint.
    """
    payload = _decode_json_object(content)

    if _is_oauth_error_object(payload):
        return JSONResponse(payload, status_code=status_code, headers=dict(_NO_STORE_HEADERS))

    description = _upstream_description(payload, "Client registration failed.")
    # 4xx keeps the pre-existing ``invalid_request``. A 5xx is not a client
    # metadata problem, and calling it one sends the caller off fixing a
    # correct registration request.
    error = "server_error" if status_code >= 500 else "invalid_request"
    logger.warning("DCR upstream returned non-OAuth error body; normalized: %s", description)
    return _oauth_error_response(error, description, status_code)


def register_oauth_routes(mcp):
    """Register OAuth 2.1 discovery endpoints with the MCP server."""

    @mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
    async def oauth_protected_resource(request):
        """
        Protected Resource Metadata (RFC 9728).

        This is the FIRST endpoint ChatGPT queries to discover:
        1. The resource identifier (this MCP server's canonical URL)
        2. Which authorization server(s) can issue tokens for this resource
        3. What scopes this resource supports

        ChatGPT uses this to:
        - Identify the resource for the 'resource' parameter in OAuth requests
        - Discover the authorization server to authenticate with
        - Understand what permissions are available

        Reference: https://datatracker.ietf.org/doc/html/rfc9728
        """
        # Get base URL - MCP server acts as both resource and auth server (proxy)
        base_url = _get_base_url(request)

        return JSONResponse({
            # Canonical identifier for this protected resource (REQUIRED)
            "resource": base_url,

            # Authorization servers - point to MCP server which proxies to Tallyfy (REQUIRED)
            "authorization_servers": [base_url],

            # Scopes this resource understands (RECOMMENDED)
            "scopes_supported": SUPPORTED_SCOPES,

            # Human-readable documentation
            "resource_documentation": MCP_DOCS_URL,

            # Bearer token is the only supported method
            "bearer_methods_supported": ["header"],

            # Resource signing algorithms (for token binding)
            "resource_signing_alg_values_supported": ["RS256"],
        })

    @mcp.custom_route("/.well-known/openid-configuration", methods=["GET"])
    async def openid_configuration(request):
        """
        OpenID Connect Discovery metadata.

        This endpoint advertises the MCP server's OAuth proxy endpoints.
        All OAuth traffic is proxied through this server to Tallyfy's
        Authorization Server.

        Architecture:
        - MCP Server: Proxies OAuth requests to Tallyfy
        - Tallyfy API: Handles actual OAuth 2.1 logic
        - ChatGPT/Claude Desktop: Connects to MCP Server for all OAuth
        """
        # Get base URL from request or use configured MCP_RESOURCE_URL
        base_url = _get_base_url(request)

        return JSONResponse({
            # Issuer identifier (REQUIRED) - use MCP server as issuer for proxy
            "issuer": base_url,

            # OAuth 2.1 endpoints - point to MCP server proxy (REQUIRED)
            "authorization_endpoint": f"{base_url}/mcp/oauth/authorize",
            "token_endpoint": f"{base_url}/mcp/oauth/token",

            # Dynamic Client Registration (REQUIRED for ChatGPT)
            "registration_endpoint": f"{base_url}/mcp/oauth/register",

            # JWKS for token validation - proxy through MCP server
            "jwks_uri": f"{base_url}/.well-known/jwks.json",

            # Supported OAuth flows
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],

            # Client authentication methods supported by the upstream Tallyfy
            # authorization server (see api-v2 OAuthController::SUPPORTED_AUTH_METHODS).
            # "none" = public PKCE clients (Claude Code, ChatGPT, Cursor, Gemini CLI).
            # "client_secret_post" / "client_secret_basic" = confidential clients
            # (Gemini Enterprise Custom MCP Server data store requires one of these).
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],

            # PKCE support (REQUIRED - ChatGPT enforces S256)
            "code_challenge_methods_supported": ["S256"],

            # Scopes this authorization server supports
            "scopes_supported": SUPPORTED_SCOPES,

            # Documentation
            "service_documentation": MCP_DOCS_URL,
        })

    @mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
    async def oauth_authorization_server(request):
        """
        OAuth 2.0 Authorization Server Metadata (RFC 8414).

        This endpoint provides OAuth 2.0 authorization server metadata.
        Points to MCP server's proxy endpoints.

        Reference: https://datatracker.ietf.org/doc/html/rfc8414
        """
        base_url = _get_base_url(request)

        return JSONResponse({
            # Issuer identifier (REQUIRED)
            "issuer": base_url,

            # OAuth 2.1 endpoints - point to MCP server proxy (REQUIRED)
            "authorization_endpoint": f"{base_url}/mcp/oauth/authorize",
            "token_endpoint": f"{base_url}/mcp/oauth/token",

            # Dynamic Client Registration (RFC 7591)
            "registration_endpoint": f"{base_url}/mcp/oauth/register",

            # JWKS for token validation - proxy through MCP server
            "jwks_uri": f"{base_url}/.well-known/jwks.json",

            # Supported OAuth flows
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],

            # Client authentication methods supported by the upstream Tallyfy
            # authorization server (see api-v2 OAuthController::SUPPORTED_AUTH_METHODS).
            # "none" = public PKCE clients (Claude Code, ChatGPT, Cursor, Gemini CLI).
            # "client_secret_post" / "client_secret_basic" = confidential clients
            # (Gemini Enterprise Custom MCP Server data store requires one of these).
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],

            # PKCE support (REQUIRED - RFC 7636)
            "code_challenge_methods_supported": ["S256"],

            # Scopes this authorization server supports
            "scopes_supported": SUPPORTED_SCOPES,

            # Documentation
            "service_documentation": MCP_DOCS_URL,
        })

    @mcp.custom_route("/token/.well-known/openid-configuration", methods=["GET"])
    async def openid_configuration_token_path(request):
        """
        OpenID Connect Discovery at non-standard path.

        Some OAuth clients construct the discovery URL by appending
        /.well-known/openid-configuration to the token endpoint URL.
        This redirects to the correct location.
        """
        logger.info("Client requested OIDC discovery at non-standard path /token/.well-known/openid-configuration - redirecting to standard path")
        return RedirectResponse(url="/.well-known/openid-configuration", status_code=307)

    # =========================================================================
    # OAuth 2.1 Proxy Endpoints
    # These endpoints proxy OAuth requests to Tallyfy's Authorization Server
    # =========================================================================

    @mcp.custom_route("/.well-known/jwks.json", methods=["GET"])
    async def jwks_proxy(request):
        """
        Proxy JWKS endpoint to Tallyfy Authorization Server.
        """
        upstream_url = f"{TALLYFY_JWKS_BASE}/.well-known/jwks.json"
        logger.info(f"Proxying JWKS request to {upstream_url}")

        try:
            async with httpx.AsyncClient(timeout=OAUTH_PROXY_TIMEOUT) as client:
                response = await client.get(upstream_url)

            return Response(
                content=response.content,
                status_code=response.status_code,
                headers={
                    "Content-Type": response.headers.get("Content-Type", "application/json"),
                    "Cache-Control": response.headers.get("Cache-Control", "public, max-age=3600"),
                },
            )
        except httpx.RequestError as e:
            logger.error(f"JWKS proxy error: {e}")
            return JSONResponse(
                {"error": "server_error", "error_description": "Failed to fetch JWKS"},
                status_code=502,
            )

    @mcp.custom_route("/mcp/oauth/register", methods=["POST"])
    async def oauth_register_proxy(request):
        """
        Proxy Dynamic Client Registration (RFC 7591) to Tallyfy.

        This endpoint receives DCR requests from OAuth clients and forwards
        them to Tallyfy's Authorization Server.
        """
        upstream_url = f"{TALLYFY_AUTH_SERVER}/mcp/oauth/register"
        logger.info(f"Proxying DCR request to {upstream_url}")

        try:
            # Read request body
            body = await request.body()

            # Forward headers (filter sensitive ones). Accept is deliberately
            # NOT forwarded, for the same reason as the token endpoint below:
            # registration is JSON-only per RFC 7591, and a caller sending "*/*"
            # got an HTML error page back.
            headers = {
                "Content-Type": request.headers.get("Content-Type", "application/json"),
                "Accept": _JSON_ONLY_ACCEPT,
            }

            async with httpx.AsyncClient(timeout=OAUTH_PROXY_TIMEOUT) as client:
                response = await client.post(
                    upstream_url,
                    content=body,
                    headers=headers,
                    follow_redirects=False,
                )

            logger.info(f"DCR response status: {response.status_code}")
            # On error responses, normalize to RFC 7591 / OAuth 2.1 error format.
            # Tallyfy may return non-standard bodies like {"error": true, "message": "..."}
            # but OAuth clients (e.g. Claude Code) expect {"error": "<string>"}.
            # An HTML body used to fall through to the raw passthrough below,
            # which is the same defect as issue #849 on a sibling endpoint.
            if response.status_code >= 400:
                return _normalize_registration_error(response.status_code, response.content)

            return Response(
                content=response.content,
                status_code=response.status_code,
                headers={
                    "Content-Type": response.headers.get("Content-Type", "application/json"),
                },
            )
        except httpx.RequestError as e:
            logger.error(f"DCR proxy error: {e}")
            return _oauth_error_response(
                "server_error", "Failed to reach the authorization server.", 502
            )

    # Known/trusted redirect_uri domains for OAuth clients.
    # NOTE: This set is for *logging-only* warnings — it does NOT block
    # registration (DCR is proxied to TALLYFY_AUTH_SERVER which validates
    # upstream). Add domains here to silence unknown-client warnings when
    # known third-party MCP clients (Claude, ChatGPT, etc.) register.
    # Refs Anthropic Connectors Directory submission (#419, #120).
    _KNOWN_OAUTH_DOMAINS = {
        "tallyfy.com",
        "chatgpt.com",
        "chat.openai.com",
        "claude.ai",
        "claude.com",  # Anthropic — used by https://claude.com/api/mcp/auth_callback
        "anthropic.com",
        "localhost",
        "127.0.0.1",
    }

    @mcp.custom_route("/mcp/oauth/authorize", methods=["GET"])
    async def oauth_authorize_proxy(request):
        """
        Proxy Authorization endpoint to Tallyfy.

        This redirects the user to Tallyfy's authorization page with all
        query parameters preserved.
        """
        # Build upstream URL with query parameters
        query_string = str(request.url.query)
        upstream_url = f"{TALLYFY_AUTH_SERVER}/mcp/oauth/authorize"
        if query_string:
            upstream_url = f"{upstream_url}?{query_string}"

        # Log unknown OAuth client redirect_uri domains at WARNING level
        redirect_uri = request.query_params.get("redirect_uri", "")
        client_id = request.query_params.get("client_id", "")
        if redirect_uri:
            try:
                from urllib.parse import urlparse
                parsed = urlparse(redirect_uri)
                domain = parsed.hostname or ""
                # Check if domain matches any known domain (including subdomains)
                is_known = any(
                    domain == known or domain.endswith(f".{known}")
                    for known in _KNOWN_OAUTH_DOMAINS
                )
                if not is_known:
                    logger.warning(
                        f"Unknown OAuth client | redirect_uri={redirect_uri} | "
                        f"client_id={client_id} | domain={domain}"
                    )
            except Exception:
                pass

        logger.info(f"Redirecting authorization request to {upstream_url}")

        # Redirect to Tallyfy's authorization endpoint
        return RedirectResponse(url=upstream_url, status_code=302)

    @mcp.custom_route("/mcp/oauth/token", methods=["POST"])
    async def oauth_token_proxy(request):
        """
        Proxy Token endpoint to Tallyfy.

        This endpoint handles token exchange requests (authorization code,
        refresh token) and forwards them to Tallyfy's Authorization Server.

        Every failure answer is rewritten into an RFC 6749 section 5.2 error
        object (issue #849). Nothing this endpoint returns is HTML, and nothing
        it returns is a 3xx, because an OAuth client can parse neither.
        """
        upstream_url = f"{TALLYFY_AUTH_SERVER}/mcp/oauth/token"
        logger.info(f"Proxying token request to {upstream_url}")

        try:
            # Read request body
            body = await request.body()
            request_content_type = request.headers.get(
                "Content-Type", "application/x-www-form-urlencoded"
            )

            # Forward headers. Accept is deliberately NOT forwarded: the token
            # endpoint is JSON-only per RFC 6749, and forwarding a caller's
            # "*/*" made the upstream render an HTML error page and 302 to the
            # login screen instead of answering with a body anyone can parse.
            headers = {
                "Content-Type": request_content_type,
                "Accept": _JSON_ONLY_ACCEPT,
            }

            # Add authorization header if present
            if "Authorization" in request.headers:
                headers["Authorization"] = request.headers["Authorization"]

            async with httpx.AsyncClient(timeout=OAUTH_PROXY_TIMEOUT) as client:
                response = await client.post(
                    upstream_url,
                    content=body,
                    headers=headers,
                    follow_redirects=False,
                )

            logger.info(f"Token response status: {response.status_code}")

            if response.status_code >= 300:
                grant_type = _requested_grant_type(body, request_content_type)
                logger.warning(
                    "Token request failed upstream | status=%s | grant_type=%r",
                    response.status_code,
                    grant_type,
                )
                return _normalize_token_error(
                    response.status_code, response.content, grant_type
                )

            # Success path is unchanged apart from the no-store headers RFC 6749
            # section 5.1 requires on a response that carries a credential.
            success_headers = {
                "Content-Type": response.headers.get("Content-Type", "application/json"),
            }
            success_headers.update(_NO_STORE_HEADERS)
            return Response(
                content=response.content,
                status_code=response.status_code,
                headers=success_headers,
            )
        except httpx.RequestError as e:
            logger.error(f"Token proxy error: {e}")
            return _oauth_error_response(
                "server_error", "Failed to reach the authorization server.", 502
            )
