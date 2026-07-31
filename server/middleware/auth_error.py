"""
OAuth 2.1 Authentication Error Middleware

Intercepts 401/403 responses and adds RFC 6750 compliant WWW-Authenticate headers,
carrying the RFC 9728 section 5.1 resource_metadata pointer. This is required for
ChatGPT and other MCP clients to properly trigger OAuth flows.

References: RFC 6750 (Bearer Token Usage), RFC 9728 (Protected Resource Metadata)
"""

import os
import json
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
from utils.org_id_middleware import (
    build_www_authenticate_header,
    protected_resource_metadata_url,
)
logger = logging.getLogger(__name__)

# Wording for a 401 on a request that carried no bearer credentials at all.
# RFC 6750 section 3.1 separates "the client did not authenticate" from "the
# credentials were rejected", and the upstream FastMCP handler cannot tell them
# apart because it only sees that no authenticated user reached it. Telling a
# caller who sent nothing that their token expired sends them looking for a
# problem with a token they never had.
NO_CREDENTIALS_ERROR_DESCRIPTION = (
    "No bearer token was presented. This endpoint requires an OAuth 2.1 access token. "
    "Discover the authorization server at "
    f"{protected_resource_metadata_url()}, complete the OAuth flow, then retry the "
    "request with an Authorization: Bearer <token> header."
)

# Wording for a 401 on a request that DID carry a bearer token, which the token
# verifier then rejected. Usually replaced by the upstream body's own text.
REJECTED_CREDENTIALS_ERROR_DESCRIPTION = (
    "The access token is expired, revoked, or invalid"
)

# The MCP transport, served at '/' and aliased at '/mcp' + '/mcp/' (see server.py).
# This is the only auth-gated surface on the server, so it is the only place where
# "you did not send a token" is the true diagnosis for a 401. The OAuth proxy routes
# (/mcp/oauth/register, /mcp/oauth/token) are unauthenticated by design and forward
# the upstream authorization server's own errors, such as invalid_client, which must
# survive untouched even though they too carry no Authorization header.
MCP_TRANSPORT_PATHS = ("/", "/mcp", "/mcp/")


class AuthErrorMiddleware(BaseHTTPMiddleware):
    """
    Middleware that ensures all 401/403 responses include proper OAuth 2.1 headers.

    ChatGPT requires specific error response format to trigger authentication UI:
    - WWW-Authenticate header with Bearer scheme and error details
    - JSON body with _meta.mcp/www_authenticate for MCP protocol

    This middleware wraps responses from FastMCP's JWTVerifier and ensures
    they comply with RFC 6750 and MCP authentication requirements.
    """

    # Paths that should not have auth errors transformed
    SKIP_PATHS = {
        "/.well-known/oauth-protected-resource",
        "/.well-known/openid-configuration",
        "/health",
        "/ready",
    }

    async def dispatch(self, request: Request, call_next):
        # Skip well-known and health endpoints
        if request.url.path in self.SKIP_PATHS or request.url.path.startswith("/.well-known/"):
            return await call_next(request)

        response = await call_next(request)

        # Only process 401 and 403 responses
        if response.status_code not in (401, 403):
            return response

        # Always transform to ensure a proper JSON body is present.
        # FastMCP's JWTVerifier may return a 403 with a correct WWW-Authenticate
        # header but an empty body. Clients like Claude Code expect a JSON body
        # with an "error" field — passing through an empty body causes a JSON
        # parse error on the client side.
        return await self._transform_auth_error(response, request)

    @staticmethod
    def _bearer_credentials_presented(request: Request) -> bool:
        """Whether the caller actually supplied a bearer token to be checked.

        A missing Authorization header, a non-Bearer scheme (which the token
        verifier never inspects) and an empty bearer value all mean no
        credentials ever reached the verifier, so nothing was rejected.
        """
        header = request.headers.get("authorization", "")
        if not header:
            return False
        scheme, _, token = header.partition(" ")
        return scheme.lower() == "bearer" and bool(token.strip())

    async def _transform_auth_error(self, response: Response, request: Request) -> Response:
        """Transform auth error response to OAuth 2.1 compliant format."""

        # A 401 from the MCP transport on a request that presented no bearer
        # credential is the one response whose upstream wording is known to be
        # false, because FastMCP's auth middleware never sees the request headers
        # and so reports every 401 as a rejected token.
        is_no_credentials_challenge = (
            response.status_code == 401
            and request.url.path in MCP_TRANSPORT_PATHS
            and not self._bearer_credentials_presented(request)
        )

        # Determine error type based on status code
        if response.status_code == 401:
            error = "invalid_token"
            error_description = (
                NO_CREDENTIALS_ERROR_DESCRIPTION
                if is_no_credentials_challenge
                else REJECTED_CREDENTIALS_ERROR_DESCRIPTION
            )
        else:  # 403
            error = "insufficient_scope"
            error_description = "The access token does not have the required scope"

        # Try to get original error message from response body. The body is
        # always drained so the upstream response is not left half-read.
        original = None
        try:
            body = b""
            async for chunk in response.body_iterator:
                body += chunk

            if body:
                try:
                    parsed = json.loads(body.decode('utf-8'))
                    if isinstance(parsed, dict):
                        original = parsed
                except json.JSONDecodeError:
                    pass
        except Exception as e:
            logger.debug(f"Could not read response body: {e}")

        # Every response other than that one keeps the upstream message, so an
        # OAuth proxy route still surfaces the authorization server's own reason.
        if original is not None and not is_no_credentials_challenge:
            # Use original error description if available
            if "error_description" in original:
                error_description = original["error_description"]
            elif "detail" in original:
                error_description = original["detail"]
            elif "message" in original:
                error_description = original["message"]
            # Use original error code if valid OAuth error
            if original.get("error") in ("invalid_request", "invalid_token", "insufficient_scope"):
                error = original["error"]

        # Build WWW-Authenticate header
        resource_metadata = protected_resource_metadata_url()
        www_authenticate = build_www_authenticate_header(
            error, error_description, resource_metadata=resource_metadata
        )

        # Build response body with MCP metadata
        response_body = {
            "error": error,
            "error_description": error_description,
            "_meta": {
                "mcp/www_authenticate": {
                    "error": error,
                    "error_description": error_description,
                    "resource_metadata": resource_metadata,
                }
            }
        }

        # Demote OAuth discovery probes (POST/GET to a transport path without MCP body) to DEBUG.
        if request.method in ("POST", "GET") and request.url.path in MCP_TRANSPORT_PATHS:
            logger.debug(f"Auth error returned: {error} (status={response.status_code}) [oauth-discovery]")
        else:
            logger.info(f"Auth error returned: {error} (status={response.status_code})")

        return JSONResponse(
            content=response_body,
            status_code=response.status_code,
            headers={
                "WWW-Authenticate": www_authenticate,
            }
        )

