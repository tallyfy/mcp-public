"""
Middleware modules for MCP Server

Available middleware:
- RequestLoggingMiddleware: Logs request/response details with session tracking
- AuthErrorMiddleware: Transforms auth errors to OAuth 2.1 compliant format
- RateLimitMiddleware: Per-IP rate limiting for unauthenticated requests
- ToolScopeEnforcementMiddleware: Enforces the token's mcp_scopes per tool.
  A FastMCP tool middleware, NOT an ASGI one -- it is added with
  mcp.add_middleware(), not app.add_middleware(), because it needs the tool
  name that only exists inside the JSON-RPC body.
"""

from middleware.request_logging import RequestLoggingMiddleware
from middleware.auth_error import AuthErrorMiddleware
from middleware.rate_limit import RateLimitMiddleware
from middleware.tool_scope_enforcement import ToolScopeEnforcementMiddleware

__all__ = [
    "RequestLoggingMiddleware",
    "AuthErrorMiddleware",
    "RateLimitMiddleware",
    "ToolScopeEnforcementMiddleware",
]
