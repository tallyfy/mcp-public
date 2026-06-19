"""
Tallyfy MCP Server
Exposes Tallyfy SDK functions as MCP tools for use with LLM applications
"""

import os
import logging
import secrets
from dotenv import load_dotenv
from fastmcp import FastMCP
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route
from sentry_config import init_sentry_server
from utils.tallyfy_auth_provider import TallyfyAuthProvider
from middleware import RequestLoggingMiddleware, AuthErrorMiddleware, RateLimitMiddleware
from routes import register_all_routes
from tools.user_management import register_user_management_tools
from tools.task_management import register_task_management_tools
from tools.process_management import register_process_management_tools
from tools.search import register_search_tools
from tools.template_management import register_template_management_tools
from tools.form_fields import register_form_fields_tools
from tools.automation import register_automation_tools
from tools.group_management import register_group_management_tools
from tools.comment_management import register_comment_management_tools
from tools.tag_management import register_tag_management_tools
from tools.folder_management import register_folder_management_tools
from tools.user_interaction import register_user_interaction_tools
from tools.api_fallback import register_api_fallback_tool
from tools.template_mapping_validation import register_template_mapping_validation_tools
from utils.org_id_middleware import OrgIdMiddleware
from utils.tallyfy_spec_cache import SPEC_CACHE
from constants import FASTMCP_SETTINGS, SUPPRESSED_LOGGERS, DEFAULT_LOG_LEVEL, TALLYFY_ISSUER, INTERNAL_API_KEY, TALLYFY_PUBLIC_KEY, MCP_RESOURCE_URL, MCP_JWT_AUDIENCE, ENFORCE_AUDIENCE

# Load environment variables from .env file
load_dotenv()

# Initialize Sentry for error tracking and performance monitoring
# CRITICAL: Must be initialized early, before any other code that might error
init_sentry_server()

# Initialize OpenTelemetry tracing if OTEL_EXPORTER_OTLP_ENDPOINT is set;
# silently no-ops otherwise (Phase 12.3 — closes #173).
from utils.otel_init import init_tracing as _init_otel  # noqa: E402
_init_otel(service_name="mcp-server")

# Instrument TallyfySDK._make_request so every Tallyfy API call is tracked
# by the tallyfy_api_* Prometheus metrics (Phase 5b — closes dead-metric gap).
from utils.sdk_metrics_patch import patch_tallyfy_sdk  # noqa: E402
patch_tallyfy_sdk()

# Configure FastMCP production settings via environment variables
# These settings enhance security and reliability in production
for key, default_value in FASTMCP_SETTINGS.items():
    os.environ.setdefault(key, os.getenv(key, default_value))

# Get log level from environment
log_level = os.getenv('FASTMCP_LOG_LEVEL', DEFAULT_LOG_LEVEL).upper()

# Configure logging with cleaner format
logging.basicConfig(
    level=log_level,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
    force=True  # Override existing handlers
)

# Suppress uvicorn access logs completely (we have custom request logging)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

# Keep uvicorn error logs but make them cleaner
uvicorn_error = logging.getLogger("uvicorn.error")
uvicorn_error.handlers.clear()
uvicorn_error.propagate = True

# Suppress noisy logs using centralized configuration
for logger_name, level_name in SUPPRESSED_LOGGERS.items():
    logging.getLogger(logger_name).setLevel(getattr(logging, level_name))




# MCP Server with JWT authentication
# Architecture: Simple JWT validation for all clients
#
# Authentication Flow:
# 1. Discovery: ChatGPT/Claude Desktop queries /.well-known/openid-configuration
# 2. OAuth Flow: Client connects directly to Tallyfy API (go.tallyfy.com) for OAuth 2.1
# 3. Token Usage: Client uses JWT token from Tallyfy API to access MCP tools
# 4. Validation: MCP Server validates JWT signature using Tallyfy's public key
#
# Supported Clients:
# - ChatGPT: Uses OAuth 2.1 flow with Tallyfy API directly
# - Claude Desktop: Uses OAuth 2.1 flow with Tallyfy API directly
# - WebSocket Host: Uses JWT tokens from Tallyfy API directly
#
# All clients:
# - JWT tokens validated via JWTVerifier (RS256 signature)
# - Org ID extracted via OrgIdMiddleware
# - Tools use get_authenticated_credentials() for credentials

# Get Tallyfy public key for JWT validation
public_key = TALLYFY_PUBLIC_KEY
if not public_key:
    raise ValueError("TALLYFY_PUBLIC_KEY environment variable is required")

# Validate public key format at startup to catch configuration errors early
try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend

    serialization.load_pem_public_key(
        public_key.encode('utf-8'),
        backend=default_backend()
    )
except ImportError:
    logging.warning("cryptography library not available; skipping public key format validation")
except (ValueError, TypeError) as e:
    raise ValueError(
        f"TALLYFY_PUBLIC_KEY is not a valid PEM-encoded RSA public key: {e}"
    ) from e

# Create custom auth handler with audience verification
# This validates:
# - JWT signature (RS256 with Tallyfy public key)
# - Token expiration
# - Issuer
# - MCP resource claim (MCP_JWT_AUDIENCE) - enforced if ENFORCE_JWT_AUDIENCE=true
auth_handler = TallyfyAuthProvider(
    public_key=public_key,
    expected_audience=MCP_JWT_AUDIENCE,
    expected_issuer=TALLYFY_ISSUER,
)

# Create MCP server with explicit capability declaration and server metadata
# This ensures standards compliance with Anthropic Claude Connectors Directory
# and OpenAI ChatGPT App submission requirements
_INSTRUCTIONS_TEMPLATE = """# Tallyfy Workflow Automation

Connect Tallyfy and run your operations right from chat: launch and track
workflows, complete and assign tasks, manage approvals, search across your
organization, and build or edit templates, all in your own authenticated
Tallyfy account.

## Try these to start

- "Launch the Employee Onboarding workflow for Jane Doe"
- "Show me my open tasks" (or "what is Sarah working on?")
- "What templates do we have for customer onboarding?"
- "Create an approval workflow for vendor onboarding"
- "Complete the 'Send welcome email' task"

## Template Creation Best Practices

When building a template from a user's description, document, or image:

1. **create_template** — create the shell (title, type, summary).
2. **add_step_to_template** — add each step in order. Choose the right step_type:
   - `approval` for any approve/reject or review step (enables approved/rejected automation conditions)
   - `email` for notification-only steps
   - `expiring` for steps that auto-complete after a deadline
   - `task` for standard work items (default)
3. **add_form_field_to_step** — add data-capture fields to steps that collect information (costs, dates, file uploads, selections). Most steps that "clarify", "submit", or "request" something need form fields.
4. **add_kickoff_field** — add pre-launch fields for data collected BEFORE the workflow starts (e.g. requester name, department, priority, description of the request).
5. **create_automation_rule** — add if-then rules for conditional branching:
   - Steps that should only appear after an approval → hide by default, show when the approval step is approved
   - Cancellation/rejection paths → show cancel steps when an approval is rejected
   - Every conditional branch in a flowchart needs both a "show" rule for the happy path AND a "hide" rule (or hide-by-default + show) for the alternative path
6. **launch_process** — optionally offer to launch a test run.

Tallyfy steps are sequential. To model parallel paths from a flowchart, use visibility automations to show/hide steps based on conditions rather than expecting simultaneous execution.

## Technical details

- **Tools**: {tool_count} tools (static list). Resources: supported. Prompts, logging, completions, and tasks are not supported.
- **Authentication**: OAuth 2.1 with a Tallyfy-issued JWT (RS256) on every request, so the assistant only ever sees what the signed-in user can see.
- **Responses**: capped at 25KB (auto-compacted); HTTP streaming supported; graceful error messages.
- **Rate limiting**: unauthenticated requests are IP-limited; authenticated requests have higher limits.

## Support

For docs or help: https://tallyfy.com/products/pro/integrations/mcp-server/
"""

mcp = FastMCP(
    "Tallyfy MCP Server",
    auth=auth_handler,
    instructions="",
    website_url="https://tallyfy.com/products/pro/integrations/mcp-server/"
)

# Log initialization status
logging.info("✅ MCP Server initialized with JWT authentication")
logging.info(f"   Resource URL: {MCP_RESOURCE_URL}")
logging.info(f"   JWT Audience: {MCP_JWT_AUDIENCE}")
logging.info(f"   OAuth Issuer: {TALLYFY_ISSUER}")
logging.info(f"   Audience verification: {'ENFORCED' if ENFORCE_AUDIENCE == 'true' else 'logging only'}")


# Register all tool categories
register_user_management_tools(mcp)
register_task_management_tools(mcp)
register_process_management_tools(mcp)
register_search_tools(mcp)
register_template_management_tools(mcp)
register_form_fields_tools(mcp)
register_automation_tools(mcp)
register_group_management_tools(mcp)
register_comment_management_tools(mcp)
register_tag_management_tools(mcp)
register_folder_management_tools(mcp)
register_user_interaction_tools(mcp)
register_api_fallback_tool(mcp)
register_template_mapping_validation_tools(mcp)

# Register all routes and resources
register_all_routes(mcp)

# Set instructions with actual tool count now that all tools are registered
tool_count = len(mcp._tool_manager._tools)
mcp.instructions = _INSTRUCTIONS_TEMPLATE.format(tool_count=tool_count)
logging.info(f"   Registered tools: {tool_count}")


app = mcp.http_app(path='/', json_response=True)


# Kick off the Tallyfy OpenAPI spec refresh at ASGI startup.
# Background task will refresh hourly; ``tallyfy_api_call`` depends on this.
async def _start_spec_cache():
    try:
        await SPEC_CACHE.start_refresh_task()
    except Exception as e:
        logging.warning("tallyfy spec cache failed to start: %s", e)


app.add_event_handler("startup", _start_spec_cache)

# Tool display names endpoint — returns {tool_name: title} for the UI.
# Internal only: requires X-Internal-Key header matching INTERNAL_API_KEY env var.
_internal_api_key = INTERNAL_API_KEY


async def tool_display_names(request):
    """Return tool name -> display title mapping from ToolAnnotations."""
    if not _internal_api_key:
        return JSONResponse({"error": "endpoint not configured"}, status_code=503)
    provided = request.headers.get('X-Internal-Key') or ""
    if not secrets.compare_digest(provided, _internal_api_key):
        return JSONResponse({"error": "unauthorized"}, status_code=403)

    tools = await mcp.get_tools()
    names = {}
    for name, tool in tools.items():
        title = None
        if hasattr(tool, 'annotations') and tool.annotations:
            title = tool.annotations.title
        names[name] = title or name
    return JSONResponse(names)


app.routes.insert(0, Route("/api/tool-names", tool_display_names, methods=["GET"]))

# Root landing page middleware — pure ASGI (no BaseHTTPMiddleware, which breaks
# streaming). Intercepts GET/HEAD on "/" for plain browser/monitor requests and
# returns the landing HTML. MCP client SSE polls (Mcp-Session-Id or
# Accept: text/event-stream) pass through untouched to the MCP transport.
from routes.landing import _LANDING_HTML, _render_landing_for_host

class RootLandingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/" and scope["method"] in ("GET", "HEAD"):
            headers = dict(scope.get("headers", []))
            has_session = b"mcp-session-id" in headers
            accepts_sse = b"text/event-stream" in headers.get(b"accept", b"")
            if not has_session and not accepts_sse:
                host = headers.get(b"host", b"").decode("ascii", errors="ignore")
                response = HTMLResponse(_render_landing_for_host(host))
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


# Serve robots.txt to discourage crawlers
async def robots_txt(request):
    """Serve robots.txt that disallows all crawling."""
    return PlainTextResponse("User-agent: *\nDisallow: /\n", media_type="text/plain")

app.routes.append(Route("/robots.txt", robots_txt, methods=["GET", "HEAD"]))

# Return 404 for undefined endpoints (instead of redirecting, which leaks the target URL)
async def catch_all_not_found(request):
    """Return 404 for undefined endpoints."""
    return JSONResponse({"error": "not_found"}, status_code=404)

app.routes.append(Route("/{path:path}", catch_all_not_found, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]))

# Add auth error middleware (transforms 401/403 to OAuth 2.1 compliant format)
# Must be added before request logging to ensure errors are logged correctly
app.add_middleware(AuthErrorMiddleware)

# Add request logging middleware
app.add_middleware(RequestLoggingMiddleware)

# Add rate limiting for unauthenticated requests
app.add_middleware(RateLimitMiddleware)

# Root landing page (outermost — runs first, before rate limiting)
app.add_middleware(RootLandingMiddleware)

# Add OrgId middleware to extract and store org_id from requests
app = OrgIdMiddleware(app)
