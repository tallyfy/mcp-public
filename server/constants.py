"""
Shared Constants for MCP Server

This module centralizes all configuration constants, defaults, and settings
used across the MCP server, including authentication, pagination, metrics,
logging, and date parsing.
"""

import os
import sys
import importlib.metadata
from typing import Dict, Set, FrozenSet

# ============================================================================
# Environment & Authentication
# ============================================================================

# Tallyfy environment (auto-configure auth server based on this)
TALLYFY_ENVIRONMENT = os.getenv("TALLYFY_ENVIRONMENT", "production").lower()

# Get Tallyfy issuer from environment (auto-configured based on TALLYFY_ENVIRONMENT)
TALLYFY_ISSUER = os.getenv('TALLYFY_ISSUER', 'https://account.tallyfy.com')

TALLYFY_API_BASE_URL = os.getenv("TALLYFY_API_BASE_URL", "https://api.tallyfy.com")

INTERNAL_API_KEY = os.getenv('INTERNAL_API_KEY')

TALLYFY_PUBLIC_KEY = os.getenv('TALLYFY_PUBLIC_KEY')

ENFORCE_AUDIENCE = os.getenv("ENFORCE_JWT_AUDIENCE", "false").lower()


# Auth server URLs by environment
AUTH_SERVER_BY_ENV: Dict[str, str] = {
    "staging": "https://staging.account.tallyfy.com",
    "production": "https://account.tallyfy.com",
}

# Current Tallyfy auth server (respects explicit override via TALLYFY_AUTH_SERVER)
TALLYFY_AUTH_SERVER = os.getenv(
    "TALLYFY_AUTH_SERVER",
    AUTH_SERVER_BY_ENV.get(TALLYFY_ENVIRONMENT, AUTH_SERVER_BY_ENV["production"])
)

# MCP Resource URL (canonical identifier for this protected resource — used in
# OAuth discovery metadata per RFC 9728). NOT used for JWT audience verification.
MCP_RESOURCE_URL = os.getenv("MCP_RESOURCE_URL", "https://mcp.tallyfy.com")

# MCP JWT audience — the value api-v2 emits in the `mcp_resource` JWT claim.
# TallyfyAuthProvider checks this when ENFORCE_JWT_AUDIENCE=true.
# Separate from MCP_RESOURCE_URL because Passport owns the `aud` claim (must be
# the integer OAuth client ID), so the MCP identifier lives in a custom claim.
# Default "mcp-host" matches api-v2's config('mcp.oauth.jwt_audience').
MCP_JWT_AUDIENCE = os.getenv("MCP_JWT_AUDIENCE", "mcp-host")

# Allowlist of hostnames that are acceptable to reflect in OAuth discovery URLs.
# Any X-Forwarded-Host / Host header not in this set is ignored and
# MCP_RESOURCE_URL is used instead (see issue #217).
# Comma-separated env. Defaults cover current Tallyfy deployments.
MCP_ALLOWED_HOSTS: FrozenSet[str] = frozenset(
    h.strip() for h in os.getenv(
        "MCP_ALLOWED_HOSTS",
        "mcp.tallyfy.com,staging.mcp.tallyfy.com,chat.tallyfy.com,staging.chat.tallyfy.com,dev.mcp.tallyfy.com",
    ).split(",") if h.strip()
)

MCP_SESSION_TIMEOUT = float(os.getenv('MCP_SESSION_TIMEOUT', '300'))

# JWKS base URL (where public keys are hosted for JWT validation)
_ENV_JWKS_CONFIG: Dict[str, str] = {
    "staging": "https://staging.account.tallyfy.com",
    "production": "https://account.tallyfy.com",
}
TALLYFY_JWKS_BASE = os.getenv(
    "TALLYFY_JWKS_BASE",
    _ENV_JWKS_CONFIG.get(TALLYFY_ENVIRONMENT, _ENV_JWKS_CONFIG["production"])
)

# OAuth documentation URL — public product page for the MCP server (closes #420).
# Previous default `https://tallyfy.com/docs/mcp` returned 404; the live product
# documentation surface is `https://tallyfy.com/products/pro/integrations/mcp-server/`.
MCP_DOCS_URL = os.getenv(
    "MCP_DOCS_URL",
    "https://tallyfy.com/products/pro/integrations/mcp-server/",
)

# OAuth proxy request timeout (seconds)
OAUTH_PROXY_TIMEOUT = float(os.getenv("OAUTH_PROXY_TIMEOUT", "30"))

# ============================================================================
# Rate Limiting
# ============================================================================

# Max requests per IP during rate limit window
RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "100"))

# Rate limit window duration (seconds)
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))

# ============================================================================
# Pagination & Result Sizing
# ============================================================================

# Default items per page (20 keeps pages comfortably under 25KB cap)
DEFAULT_PAGE_SIZE = 20

# Maximum serialized JSON size for tool results (bytes)
MAX_RESULT_SIZE_CHARS = 25_000

# ============================================================================
# Date Parsing
# ============================================================================

# Maximum parsing attempts before giving up on date extraction
DATE_PARSING_MAX_ATTEMPTS = int(os.getenv("DATE_PARSING_MAX_ATTEMPTS", "3"))

# How many years in the future is considered valid (e.g., don't accept year 2199)
DATE_PARSING_FUTURE_YEAR_LIMIT = int(os.getenv("DATE_PARSING_FUTURE_YEAR_LIMIT", "2"))

# Time expressions mapping (verbal time -> 12-hour format)
TIME_MAPPINGS = {
    "midday": "12:00 PM",
    "noon": "12:00 PM",
    "midnight": "12:00 AM",
    "morning": "9:00 AM",
    "afternoon": "2:00 PM",
    "evening": "6:00 PM",
    "night": "8:00 PM",
}

# ============================================================================
# Metrics & Observability
# ============================================================================

METRICS_ALLOWED_IPS = os.getenv('METRICS_ALLOWED_IPS')

METRICS_USERNAME = os.getenv('METRICS_USERNAME', 'prometheus')
METRICS_PASSWORD = os.getenv('METRICS_PASSWORD')

# Prometheus histogram buckets for request duration (seconds)
# Buckets cover the full range:
#   - low-end (5ms - 100ms) for fast read-only tools (get_me, get_template)
#   - mid (250ms - 2s) for typical Tallyfy API round-trips
#   - high (5s - 60s) for tail latency / hung-call detection
# Issue #174/#276: tighter low-end resolution for accurate p50/p95/p99.
REQUEST_DURATION_BUCKETS = [
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0
]

# Prometheus histogram buckets for API call duration (seconds)
API_DURATION_BUCKETS = [0.1, 0.5, 1.0, 2.0, 5.0, 10.0]

# Sensitive parameter keys (redacted from logs)
SENSITIVE_KEYS: FrozenSet[str] = frozenset({
    "api_key",
    "token",
    "password",
    "secret",
    "auth",
    "credential",
    "private",
})

# ============================================================================
# Logging Configuration
# ============================================================================

# Default log level if not specified
DEFAULT_LOG_LEVEL = "INFO"

LOG_VERBOSITY = int(os.getenv('LOG_VERBOSITY', '1'))

# ANSI color codes for formatted log output
class LogColors:
    """ANSI color codes for terminal output."""
    RESET = "\033[0m"
    GRAY = "\033[90m"
    GREEN = "\033[92m"
    RED = "\033[91m"
    WHITE = "\033[97m"
    CYAN = "\033[96m"


# Loggers to suppress (name -> level)
# These loggers are noisy and we handle their concerns via custom middleware
SUPPRESSED_LOGGERS: Dict[str, str] = {
    "mcp.server.lowlevel.server": "WARNING",
    "FastMCP.fastmcp.tools.tool_manager": "FATAL",
    "mcp.server.streamable_http_manager": "WARNING",
    "mcp.server.streamable_http": "WARNING",
    "docket.worker": "WARNING",
    "uvicorn.access": "WARNING",
}

# FastMCP production settings (set as environment defaults if not already set)
FASTMCP_SETTINGS = {
    "FASTMCP_MASK_ERROR_DETAILS": "true",
    "FASTMCP_STRICT_INPUT_VALIDATION": "true",
    "FASTMCP_INCLUDE_FASTMCP_META": "false"
}

# ============================================================================
# Server Metadata
# ============================================================================

# Server version (used in Prometheus metrics, the server card, and server.json).
# Keep this in step with server.json — the MCP registry reads that file, and the
# three copies had already drifted (server.json 1.0.1 vs 1.0.0 here and in the
# server card) before they were reconciled. server_card.py now imports this
# rather than repeating the literal.
# 1.1.0: launch_process prerun/roles now take an ID-keyed object, a breaking
# change to the advertised tool schema.
SERVER_VERSION = "1.1.0"

# FastMCP framework version — read at runtime so it stays accurate after upgrades
FASTMCP_VERSION = importlib.metadata.version("fastmcp")

# Python version — read at runtime so it reflects the actual interpreter
PYTHON_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


# ============================================================================
# Sentry Config
# ============================================================================

SENTRY_ENABLED = os.getenv("SENTRY_ENABLED", "true").lower()
SENTRY_DSN = os.getenv("SENTRY_DSN")
SENTRY_ENVIRONMENT = os.getenv("SENTRY_ENVIRONMENT", 'production')
SENTRY_RELEASE = os.getenv("SENTRY_RELEASE", "mcp-server-unknown")
# Errors-only Sentry mode: both rates default to 0.0 so no transactions, no
# spans, no profile_duration units, no profiles reach Sentry. Only error
# events (via LoggingIntegration / FastApiIntegration) flow. Env vars still
# win if set, so the rates can be re-enabled per-environment without code.
SENTRY_TRACES_SAMPLE_RATE = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0"))
SENTRY_PROFILES_SAMPLE_RATE = float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", "0.0"))
# ============================================================================
# OAuth Scopes (Security)
# ============================================================================

class MCPScopes:
    """OAuth 2.1 scopes for Tallyfy MCP tools."""

    USERS_READ = "mcp.users.read"
    USERS_WRITE = "mcp.users.write"
    TASKS_READ = "mcp.tasks.read"
    TASKS_WRITE = "mcp.tasks.write"
    PROCESSES_READ = "mcp.processes.read"
    PROCESSES_WRITE = "mcp.processes.write"
    TEMPLATES_READ = "mcp.templates.read"
    TEMPLATES_WRITE = "mcp.templates.write"
    FORMS_READ = "mcp.forms.read"
    FORMS_WRITE = "mcp.forms.write"
    AUTOMATION_READ = "mcp.automation.read"
    AUTOMATION_WRITE = "mcp.automation.write"


# OAuth security metadata for client discovery
TOOL_SECURITY_METADATA = {
    "securitySchemes": {
        "oauth2": {
            "type": "oauth2",
            "description": "OAuth 2.1 authentication via Tallyfy Authorization Server",
            "flows": {
                "authorizationCode": {
                    "authorizationUrl": f"{TALLYFY_AUTH_SERVER}/mcp/oauth/authorize",
                    "tokenUrl": f"{TALLYFY_AUTH_SERVER}/mcp/oauth/token",
                    "scopes": {
                        MCPScopes.USERS_READ: "Read access to organization users and guests",
                        MCPScopes.USERS_WRITE: "Invite and manage organization users",
                        MCPScopes.TASKS_READ: "Read access to tasks",
                        MCPScopes.TASKS_WRITE: "Create and modify tasks",
                        MCPScopes.PROCESSES_READ: "Read access to processes (runs)",
                        MCPScopes.PROCESSES_WRITE: "Create and modify processes",
                        MCPScopes.TEMPLATES_READ: "Read access to templates",
                        MCPScopes.TEMPLATES_WRITE: "Create and modify templates",
                        MCPScopes.FORMS_READ: "Read access to form fields",
                        MCPScopes.FORMS_WRITE: "Create and modify form fields",
                        MCPScopes.AUTOMATION_READ: "Read access to automation rules",
                        MCPScopes.AUTOMATION_WRITE: "Create and modify automation rules",
                    },
                }
            },
        }
    },
    "security": [
        {
            "oauth2": [
                MCPScopes.USERS_READ,
                MCPScopes.TASKS_READ,
                MCPScopes.PROCESSES_READ,
                MCPScopes.TEMPLATES_READ,
                MCPScopes.FORMS_READ,
                MCPScopes.AUTOMATION_READ,
            ]
        }
    ],
}
