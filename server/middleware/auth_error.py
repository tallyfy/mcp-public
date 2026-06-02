"""
OAuth 2.1 Authentication Error Middleware

Intercepts 401/403 responses and adds RFC 6750 compliant WWW-Authenticate headers.
This is required for ChatGPT and other MCP clients to properly trigger OAuth flows.

Reference: RFC 6750 (Bearer Token Usage)
"""

import os
import json
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
from utils.org_id_middleware import build_www_authenticate_header
logger = logging.getLogger(__name__)


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

    async def _transform_auth_error(self, response: Response, request: Request) -> Response:
        """Transform auth error response to OAuth 2.1 compliant format."""

        # Determine error type based on status code
        if response.status_code == 401:
            error = "invalid_token"
            error_description = "The access token is missing, expired, or invalid"
        else:  # 403
            error = "insufficient_scope"
            error_description = "The access token does not have the required scope"

        # Try to get original error message from response body
        try:
            body = b""
            async for chunk in response.body_iterator:
                body += chunk

            if body:
                try:
                    original = json.loads(body.decode('utf-8'))
                    if isinstance(original, dict):
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
                except json.JSONDecodeError:
                    pass
        except Exception as e:
            logger.debug(f"Could not read response body: {e}")

        # Build WWW-Authenticate header
        www_authenticate = build_www_authenticate_header(error, error_description)

        # Build response body with MCP metadata
        response_body = {
            "error": error,
            "error_description": error_description,
            "_meta": {
                "mcp/www_authenticate": {
                    "error": error,
                    "error_description": error_description,
                }
            }
        }

        # Demote OAuth discovery probes (POST/GET to root without MCP body) to DEBUG
        if request.method in ("POST", "GET") and request.url.path == "/":
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

