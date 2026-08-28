"""
Tallyfy MCP Server
Exposes Tallyfy SDK functions as MCP tools for use with LLM applications
"""

import os
import logging
import secrets
import asyncio
import concurrent.futures
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastmcp import FastMCP
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route
from sentry_config import init_sentry_server
from utils.tallyfy_auth_provider import build_auth_provider
from middleware import RequestLoggingMiddleware, AuthErrorMiddleware, RateLimitMiddleware, DownstreamAuthChallengeMiddleware, ToolScopeEnforcementMiddleware, RemovedToolHintsMiddleware
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
from tools.api_fallback import register_api_fallback_tool, report_fallback_flag_state
from tools.template_mapping_validation import register_template_mapping_validation_tools
from tools.org_context import register_org_context_tools
from utils.org_id_middleware import OrgIdMiddleware
from utils.tallyfy_spec_cache import SPEC_CACHE
from constants import FASTMCP_SETTINGS, SUPPRESSED_LOGGERS, DEFAULT_LOG_LEVEL, TALLYFY_ISSUER, INTERNAL_API_KEY, TALLYFY_PUBLIC_KEY, TALLYFY_JWKS_URI, MCP_RESOURCE_URL, MCP_JWT_AUDIENCE, ACCEPTED_MCP_RESOURCES, ENFORCE_AUDIENCE, SERVER_VERSION, INSTRUCTIONS_TEMPLATE

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

# Resolve the RS256 verification key and build the auth handler.
#
# Every request to an MCP tool is authenticated, in all configurations. What
# changes here is only WHERE the verification key comes from:
#   - TALLYFY_PUBLIC_KEY set   -> that pinned key (production; a malformed value raises)
#   - unset                    -> Tallyfy's published JWKS, fetched lazily over TLS
#                                 from the same TALLYFY_JWKS_BASE that
#                                 routes/oauth.py already proxies
#   - neither resolvable       -> a verifier that rejects every token
#
# Startup no longer depends on the key being present, which is what lets a fresh
# clone of the public mirror build, boot, and answer /health without credentials
# (#657). It does NOT let such a deployment reach Tallyfy data: an unverifiable
# token is rejected exactly as an invalid one is.
#
# The handler validates:
# - JWT signature (RS256 with Tallyfy's public key)
# - Token expiration
# - MCP resource claim - the token's `mcp_resource` must name this server, by
#   either value in ACCEPTED_MCP_RESOURCES. Enforced only if
#   ENFORCE_JWT_AUDIENCE=true, which as of 2026-08-09 is explicitly "false" in
#   both production and staging, so this check does not currently run anywhere.
#
# Passing the whole accept-set here rather than MCP_JWT_AUDIENCE alone is the
# point of tallyfy/mcp#812: naming one value at this call site would pin the
# accept-set back to that one value and silently undo the widening, since an
# explicit argument is respected verbatim.
auth_handler = build_auth_provider(
    public_key=TALLYFY_PUBLIC_KEY,
    expected_audience=ACCEPTED_MCP_RESOURCES,
    expected_issuer=TALLYFY_ISSUER,
    jwks_uri=TALLYFY_JWKS_URI,
)

# Create MCP server with explicit capability declaration and server metadata
# This ensures standards compliance with Anthropic Claude Connectors Directory
# and OpenAI ChatGPT App submission requirements
# The instructions template lives in constants.py (INSTRUCTIONS_TEMPLATE) so
# tests can import and pin it without importing this module, whose import
# has side effects (Sentry init, a thread pool). The .format(tool_count=...)
# call below is the only consumer; any literal brace in the template other
# than {tool_count} crashes startup, which test_server_instructions.py guards.

# `version=` is NOT optional in practice. FastMCP's constructor does
#     version=_coerce_version(version) or fastmcp.__version__
# (fastmcp/server/server.py), and that value becomes `serverInfo.version` in
# the MCP `initialize` response. Omitting it therefore published the FRAMEWORK
# version as ours: a live initialize against production returned
# `serverInfo.version: "3.4.2"` while server.json declared 1.1.2. Every MCP
# client, every directory reviewer, and our own tool manifest saw the wrong
# number. SERVER_VERSION (server/constants.py) is the single source of truth;
# server.json and the server card are pinned to it by
# tests/unit/server/test_server_version.py. See #654.
mcp = FastMCP(
    "Tallyfy MCP Server",
    version=SERVER_VERSION,
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
register_org_context_tools(mcp)

# Enforce the token's mcp_scopes per tool (#559). This is a FastMCP TOOL
# middleware, so it is added here with mcp.add_middleware() rather than to the
# ASGI app below -- it authorises a tool NAME, which exists only inside the
# JSON-RPC body. Registered after the tools so the map it checks against and the
# tools it guards are established together; ordering relative to the tools does
# not affect dispatch, but a reader should see them adjacent.
#
# A token carrying no mcp_scopes claim passes through untouched, in every mode.
# That is not leniency -- chat.tallyfy.com and the desktop AI shell forward the
# user's raw Tallyfy session token, which has no such claim, so failing closed on
# its absence would 403 Tallyfy's own products. See utils/tool_scopes.py.
#
# Mode: MCP_TOOL_SCOPE_ENFORCEMENT = enforce (default) | log | off.
# Answer the 7 tool names removed in #492 with the current path (#1030).
# Registered BEFORE ToolScopeEnforcementMiddleware because first-added is
# outermost: a removed name must get its hint even when the caller's scopes
# would also have denied it -- a scope denial for a tool that no longer
# exists sends the caller off to re-authorize for nothing.
mcp.add_middleware(RemovedToolHintsMiddleware())
mcp.add_middleware(ToolScopeEnforcementMiddleware())

# Register all routes and resources
register_all_routes(mcp)

# Set instructions with actual tool count now that all tools are registered
# list_tools() is async in fastmcp 3.x; run it in a worker thread so the count
# works at import time even when the importer (uvicorn) has a running event loop.
with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _ex:
    tool_count = _ex.submit(lambda: len(asyncio.run(mcp.list_tools()))).result()
mcp.instructions = INSTRUCTIONS_TEMPLATE.format(tool_count=tool_count)
logging.info(f"   Registered tools: {tool_count}")


app = mcp.http_app(path='/', json_response=True)

# ALSO serve the MCP protocol at /mcp (both slash forms), keeping / for back-compat.
# FastMCP's ecosystem default path is /mcp, so external clients try it first; the
# catch-all Route('/{path:path}') registered below turns that into a 404 today.
# Reuse the transport route's endpoint object already registered at '/', so /mcp
# shares the same session manager, JWT enforcement, request logging, and lifespan
# as '/'. Exact Routes (NOT a Mount): Route('/mcp') matches only the literal path,
# so it can never shadow the /mcp/oauth/* routes, and a Mount('/mcp', ...) would
# not match bare /mcp at all. Do NOT call mcp.http_app(path='/mcp') a second time
# (each call builds its own session manager + lifespan; only the first runs).
_mcp_transport_route = next(
    r for r in app.routes if isinstance(r, Route) and r.path == '/'
)
for _mcp_alias in ('/mcp', '/mcp/'):
    app.routes.append(Route(_mcp_alias, endpoint=_mcp_transport_route.endpoint,
                            methods=_mcp_transport_route.methods))


# Kick off the Tallyfy OpenAPI spec refresh at ASGI startup.
# Background task will refresh hourly; the API fallback tools depend on this.
async def _start_spec_cache():
    try:
        await SPEC_CACHE.start_refresh_task()
    except Exception as e:
        logging.warning("tallyfy spec cache failed to start: %s", e)


# starlette 1.x (pulled by fastmcp 3.x) removed add_event_handler("startup", ...).
# Compose the spec-cache startup into FastMCP's existing lifespan, which runs the
# MCP streamable-http session manager.
_mcp_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _combined_lifespan(scope_app):
    # Announce the API-fallback flag as this process actually sees it (#1009).
    # It runs HERE, on the deployed server, because that is the only place the
    # question can be answered: the deploy deliberately excludes environment
    # files, so no repo-side check can ever see the real value.
    report_fallback_flag_state()
    await _start_spec_cache()
    async with _mcp_lifespan(scope_app):
        yield


app.router.lifespan_context = _combined_lifespan

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

    tools = await mcp.list_tools()
    names = {}
    for tool in tools:
        name = tool.name
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
from routes.landing import _LANDING_HTML, _render_landing_for_host  # noqa: E402 - deliberately late: the comment above explains that this middleware is installed after the app and its routes exist

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

# Answer a tool call whose Tallyfy API request came back 401 with a 401 of our
# own, so the client re-runs its OAuth flow (#652). Added FIRST, which makes it
# the INNERMOST of the app.add_middleware() layers, because Starlette inserts
# each new middleware at the front of the list and builds the stack in reverse.
# Being inside AuthErrorMiddleware is the point: this emits a plain OAuth error
# body and AuthErrorMiddleware attaches the WWW-Authenticate header carrying the
# RFC 9728 resource_metadata pointer, so that middleware stays the single place
# in this server that builds a challenge header.
app.add_middleware(DownstreamAuthChallengeMiddleware)

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
