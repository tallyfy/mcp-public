"""
Middleware modules for MCP Server

Available middleware:
- RequestLoggingMiddleware: Logs request/response details with session tracking
- AuthErrorMiddleware: Transforms auth errors to OAuth 2.1 compliant format
- RateLimitMiddleware: Per-IP rate limiting for unauthenticated requests
- DownstreamAuthChallengeMiddleware: Rewrites a 2xx MCP response into a 401
  challenge when a tool's Tallyfy API call came back 401, so the client
  re-runs its OAuth flow instead of being told the call succeeded (#652).
- ToolScopeEnforcementMiddleware: Enforces the token's mcp_scopes per tool.
  A FastMCP tool middleware, NOT an ASGI one -- it is added with
  mcp.add_middleware(), not app.add_middleware(), because it needs the tool
  name that only exists inside the JSON-RPC body.
- RemovedToolHintsMiddleware: Answers the 7 tool names removed in #492 with
  a ToolError naming the current path, instead of a bare unknown-tool error.
  Also a FastMCP tool middleware, registered above scope enforcement.
"""

from middleware.request_logging import RequestLoggingMiddleware
from middleware.auth_error import AuthErrorMiddleware
from middleware.rate_limit import RateLimitMiddleware
from middleware.downstream_auth_challenge import DownstreamAuthChallengeMiddleware
from middleware.tool_scope_enforcement import ToolScopeEnforcementMiddleware
from middleware.removed_tool_hints import RemovedToolHintsMiddleware

__all__ = [
    "RequestLoggingMiddleware",
    "AuthErrorMiddleware",
    "RateLimitMiddleware",
    "DownstreamAuthChallengeMiddleware",
    "ToolScopeEnforcementMiddleware",
    "RemovedToolHintsMiddleware",
]
