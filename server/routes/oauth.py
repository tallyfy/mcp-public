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

import logging
import re
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

            # Forward headers (filter sensitive ones)
            headers = {
                "Content-Type": request.headers.get("Content-Type", "application/json"),
                "Accept": request.headers.get("Accept", "application/json"),
            }

            async with httpx.AsyncClient(timeout=OAUTH_PROXY_TIMEOUT) as client:
                response = await client.post(
                    upstream_url,
                    content=body,
                    headers=headers,
                )

            logger.info(f"DCR response status: {response.status_code}")
            # On error responses, normalize to RFC 7591 / OAuth 2.1 error format.
            # Tallyfy may return non-standard bodies like {"error": true, "message": "..."}
            # but OAuth clients (e.g. Claude Code) expect {"error": "<string>"}.

            if response.status_code >= 400:
                try:

                    import json as _json
                    error_body = _json.loads(response.content)
                    if not isinstance(error_body.get("error"), str):
                        description = (
                            error_body.get("message")
                            or error_body.get("code")
                            or "Client registration failed"
                        )
                        error_body = {
                             "error": "invalid_request",
                           "error_description": description,
                        }
                        logger.warning(f"DCR upstream returned non-OAuth error body; normalized: {description}")
                        return JSONResponse(content=error_body, status_code=response.status_code)
                except Exception:
                    pass
            return Response(
                content=response.content,
                status_code=response.status_code,
                headers={
                    "Content-Type": response.headers.get("Content-Type", "application/json"),
                },
            )
        except httpx.RequestError as e:
            logger.error(f"DCR proxy error: {e}")
            return JSONResponse(
                {"error": "server_error", "error_description": "Failed to register client"},
                status_code=502,
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
        """
        upstream_url = f"{TALLYFY_AUTH_SERVER}/mcp/oauth/token"
        logger.info(f"Proxying token request to {upstream_url}")

        try:
            # Read request body
            body = await request.body()

            # Forward headers
            headers = {
                "Content-Type": request.headers.get("Content-Type", "application/x-www-form-urlencoded"),
                "Accept": request.headers.get("Accept", "application/json"),
            }

            # Add authorization header if present
            if "Authorization" in request.headers:
                headers["Authorization"] = request.headers["Authorization"]

            async with httpx.AsyncClient(timeout=OAUTH_PROXY_TIMEOUT) as client:
                response = await client.post(
                    upstream_url,
                    content=body,
                    headers=headers,
                )

            logger.info(f"Token response status: {response.status_code}")

            return Response(
                content=response.content,
                status_code=response.status_code,
                headers={
                    "Content-Type": response.headers.get("Content-Type", "application/json"),
                },
            )
        except httpx.RequestError as e:
            logger.error(f"Token proxy error: {e}")
            return JSONResponse(
                {"error": "server_error", "error_description": "Failed to exchange token"},
                status_code=502,
            )
