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

# PEM-encoded RS256 public key used to verify Tallyfy-issued JWTs.
# Optional: when unset, the server verifies against the issuer's published JWKS
# instead (see utils/tallyfy_auth_provider.build_auth_provider). Setting it pins
# the key and removes the runtime network dependency, which is what production does.
TALLYFY_PUBLIC_KEY = os.getenv('TALLYFY_PUBLIC_KEY')

# Optional override for the JWKS document used when TALLYFY_PUBLIC_KEY is unset.
# Defaults to {TALLYFY_ISSUER}/.well-known/jwks.json.
TALLYFY_JWKS_URI = os.getenv('TALLYFY_JWKS_URI')

# Whether the `mcp_resource` comparison in tallyfy_auth_provider.py runs at all.
#
# ⚠️ TWO SIMILARLY-NAMED FLAGS EXIST AND THEIR DEFAULTS ARE OPPOSITE. Naming
# them together here because the alternative has been measured twice: a reader
# checks the wrong one and concludes enforcement is on when it is off, or off
# when it is on. Both errors were live in this repo's docs for over a year, in
# opposite directions.
#
#   ENFORCE_JWT_AUDIENCE      (THIS one, the SERVER)  default "false"
#   MCP_ENFORCE_JWT_AUDIENCE  (the HOST, host/core/jwt_audience.py)
#                                                     default "true"
#
# ⚠️ A THIRD flag now shares this vocabulary and is a DIFFERENT mechanism:
#
#   MCP_DOWNSTREAM_TOKEN_EXCHANGE  (utils/downstream_token.py)  default "off"
#                                  values: off | shadow | enforce
#
# The two above decide whether we CHECK the audience of an inbound token. That
# one decides whether we stop FORWARDING the inbound token upstream at all, by
# exchanging it for a separate short-lived credential. Neither implies the
# other, and neither is a step toward the other.
#
# It is deliberately read via os.getenv inside downstream_token._mode() rather
# than bound here. A module constant is captured at import, which would make it
# untestable without a reload and un-flippable at runtime. It is NOT declared
# here as a constant on purpose: an unread key in this file is worse than no
# key, because someone sets it and believes they changed something. That is
# exactly what config/mcp.php's rate_limits.dcr was.
#
# Measured 2026-08-09 from the running containers: this one is EXPLICITLY set to
# "false" in production and staging (rc=0, so configured off rather than merely
# defaulting), while the host's is unset and therefore enforcing. The whole
# audience block below is dead code in every environment we run.
#
# Do not flip this by hand. Turning it on is tallyfy/mcp#743's job, gated on the
# shadow census (mcp_server_jwt_audience_class_total, see
# utils/tallyfy_auth_provider.classify_audience).
#
# ⚠️ WHICH CLASSES GATE WHICH STEP. The cutover is THREE steps, and each one
# rejects a DIFFERENT set of classes. Reading "the census must read zero" without
# saying zero for WHAT is how the wrong step gets taken:
#
#   Step 1 - flip THIS flag to "true".
#            Rejects: `vault`, `none`, `unclassified`.
#            Still accepted: `resource_url`, `legacy_mcp_host`,
#            `first_party_client` (the last two are exactly the classes an
#            earlier version of this comment named as the gate, which was
#            backwards - they are what SURVIVES step 1).
#   Step 2 - delete the `elif str(aud) == "1"` arm in verify_token.
#            Additionally rejects: `first_party_client`, which is
#            chat.tallyfy.com's live authentication path today.
#   Step 3 - drop MCP_JWT_AUDIENCE from ACCEPTED_MCP_RESOURCES.
#            Additionally rejects: `legacy_mcp_host`.
#
# So step 1 waits on `vault` + `none` + `unclassified` reading zero for a full
# week, step 2 on `first_party_client`, step 3 on `legacy_mcp_host`. A class at
# zero for a week is the evidence; a class you did not name is an outage.
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
# OAuth discovery metadata per RFC 9728, and accepted in the `mcp_resource` JWT
# claim — see ACCEPTED_MCP_RESOURCES below).
MCP_RESOURCE_URL = os.getenv("MCP_RESOURCE_URL", "https://mcp.tallyfy.com")

# MCP JWT audience — the value api-v2 emits in the `mcp_resource` JWT claim.
# TallyfyAuthProvider checks this when ENFORCE_JWT_AUDIENCE=true.
# Separate from MCP_RESOURCE_URL because Passport owns the `aud` claim (must be
# the integer OAuth client ID), so the MCP identifier lives in a custom claim.
# Default "mcp-host" matches api-v2's config('mcp.oauth.jwt_audience').
MCP_JWT_AUDIENCE = os.getenv("MCP_JWT_AUDIENCE", "mcp-host")

# The values a token's `mcp_resource` claim may carry and still be accepted as
# naming THIS server. Two entries, deliberately, and the pair is the whole point
# of issue #812:
#
#   MCP_RESOURCE_URL   the canonical RFC 8707 resource identifier. It is what
#                      /.well-known/oauth-protected-resource already publishes,
#                      what api-v2 will emit once tallyfy/api-v2#9802 lands, and
#                      what any RFC 8707 `resource` parameter would name. Before
#                      this constant existed we advertised this value and then
#                      compared against a different one, so a correctly-formed
#                      token would have been rejected.
#
#   MCP_JWT_AUDIENCE   the legacy literal ("mcp-host"). Every token in
#                      circulation today carries this, so it must stay accepted
#                      until they have all expired. api-v2's `mcp:mint-token`
#                      allows a 180-day lifetime (api-v2 config/mcp.php), so the
#                      floor for removal is 180 days after #9802 ships, not the
#                      day it ships.
#
# LEGACY-REMOVAL (added 2026-08-09): dropping MCP_JWT_AUDIENCE from this set is
# tallyfy/mcp#743's job, gated on the shadow census reading zero for the legacy
# class for a full week. Do not narrow it here.
ACCEPTED_MCP_RESOURCES: FrozenSet[str] = frozenset({
    MCP_RESOURCE_URL,
    MCP_JWT_AUDIENCE,
})

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

# Shared secret proving to api-v2 that an X-Tallyfy-Client-IP header really
# came from this proxy (issue #863). api-v2 honours the forwarded IP only when
# this matches AND the request arrives from the MCP egress CIDR -- two
# independent conditions, so a leak of this value alone does not let anyone
# choose a rate-limit bucket from outside our network.
#
# Empty by default and empty means OFF: we send no forwarding header at all,
# and api-v2 falls back to its socket-level view, which is exactly today's
# behaviour. That makes this safe to deploy on either side first.
MCP_PROXY_SHARED_SECRET = os.getenv("MCP_PROXY_SHARED_SECRET", "")

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


# ---------------------------------------------------------------------------
# `mcp_server_tool_errors_total{error_type}` -- the closed label vocabulary
# ---------------------------------------------------------------------------
#
# Lives here rather than beside the logic that produces it because TWO modules
# have to agree on it and neither may import the other: `utils/fastmcp_errors`
# stamps the class onto the ToolError it raises, and `metrics` reads it back
# one decorator further out. `constants` is the only module both already
# import, and it pulls in nothing but the standard library.
#
# WHY THE LABEL NEEDED FIXING AT ALL: on the live Prometheus it had exactly one
# value, `unknown`, across the whole retention window, because the two
# decorators are stacked `@track_tool_execution` OUTSIDE `@handle_tallyfy_errors`
# on 107 of 113 tools -- so the inner one had already turned every TallyfyError
# into a ToolError before the outer one tried to classify it by class name.
# Full reasoning, and why an unlisted status is still labelled honestly, is in
# the block comment in `utils/fastmcp_errors.py`.
#
# CARDINALITY IS THE POINT OF MAKING IT CLOSED. This multiplies against
# `tool_name`, which carries ~79 distinct values, and no member may ever be
# derived from message text or from anything a caller supplies -- otherwise a
# single request could mint unbounded Prometheus time series.
#
# `payload_rejected` AND `upstream_unavailable` BOTH ARRIVE WITH NO HTTP STATUS
# AND MEAN OPPOSITE THINGS, which is exactly why they are two members rather
# than one. `upstream_unavailable` is "Tallyfy could not be reached", the only
# signal an operator has that the API is down. `payload_rejected` is "the SDK
# refused the arguments before opening a socket", which says nothing at all
# about Tallyfy's health. Folding the second into the first would let a model
# mistyping a field name look, on the dashboard, exactly like an outage. They
# are told apart by EXCEPTION TYPE, never by the absent status. See #835 and
# `utils/fastmcp_errors.is_preflight_payload_error`.
TOOL_ERROR_CLASS_ATTR = "_tallyfy_error_class"

TOOL_ERROR_CLASSES: FrozenSet[str] = frozenset({
    "validation",            # upstream 422, or a pydantic ValidationError
    "not_found",             # upstream 404
    "auth",                  # upstream 401 or 403
    "bad_request",           # upstream 400
    "conflict",              # upstream 409
    "rate_limited",          # upstream 429
    "client_error",          # any other upstream 4xx
    "upstream_error",        # upstream 5xx
    "upstream_unavailable",  # TallyfyError with no int status: retries exhausted
    "payload_rejected",      # the SDK refused the payload BEFORE any request
    "internal_error",        # an unexpected exception inside the tool body
    "tool_rejected",         # the tool's own code raised ToolError
    "unknown",               # nothing above applied
})

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

# THE single source of truth for the server's own version.
#
# Everything the server reports reads THIS constant: the MCP `initialize`
# handshake (server.py passes it as FastMCP's `version=`), the static server
# card (routes/server_card.py), and the Prometheus build-info label
# (metrics.py). The repo-root server.json, the record the MCP Registry
# publishes, is pinned to it by tests/unit/server/test_server_version.py, so
# the two cannot drift into a merge.
#
# server.json is deliberately NOT the runtime source: it lives at the repo
# root, while the server image is built with `server/` as its Docker context
# (`build: .` + `COPY . .`), so the file is not present in the running
# container. A runtime read would fall back exactly where accuracy matters
# most. Bump this constant and server.json in the same commit.
#
# Do not drop `version=` from the FastMCP(...) call. FastMCP substitutes its
# OWN package version when the argument is omitted, which is how production
# came to advertise `serverInfo.version: "3.4.2"` (the framework) instead of
# 1.1.2 (us). See #654.
#
# History: server.json 1.0.1 vs 1.0.0 here and in the server card had already
# drifted once before, which is why server_card.py imports this rather than
# repeating the literal.
#
# Changelog:
# 1.1.0: launch_process prerun/roles now take an ID-keyed object, a breaking
# change to the advertised tool schema.
# 1.1.1: contract-correctness bug fixes (no tool-count change) - stop
# update_process/update_task/complete_task/kickoff writes from silently
# detaching assignees or corrupting state; form-field option/type/required
# contract corrections; automation alias + orphan-detector fixes.
# 1.1.2: per-type form-field VALUE coercion (no tool-count change). 1.1.0 fixed
# the prerun CONTAINER shape; the values inside it were still passed through
# untouched, so dropdown/radio/multiselect kept 422-ing. launch_process and both
# task update paths now resolve an option id or its text against the field's own
# options and emit the shape FormValuesValidator requires, including the
# selected:true that multiselect needs to render at all. get_dropdown_options
# returns {id,text} pairs so a writable value can actually be read.
SERVER_VERSION = "1.2.0"

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
def _resolve_sentry_release() -> str:
    """Build the Sentry release identifier, preferring the deployed commit.

    ``MCP_GIT_SHA`` wins because it is the only input here that changes per
    deploy. ``SENTRY_RELEASE`` lives in the droplet's env file, which nobody
    edits: both MCP Sentry projects carried ``*@1.2.0`` from 2026-04-29 through
    2026-08-05 across dozens of deploys, so no event could be attributed to the
    deploy that caused it. Checking whether a fix had shipped meant SSHing to
    the droplet and grepping the running container.

    Order: MCP_GIT_SHA, then SENTRY_RELEASE verbatim, then the legacy default.
    """
    git_sha = (os.getenv("MCP_GIT_SHA") or "").strip()
    if git_sha:
        return f"mcp-server@{git_sha}"

    env_release = (os.getenv("SENTRY_RELEASE") or "").strip()
    if env_release:
        return env_release

    return "mcp-server-unknown"


SENTRY_RELEASE = _resolve_sentry_release()
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


# The product-expert instructions served to every external MCP client
# (ChatGPT, Claude Desktop, Cursor) via the `initialize` response. This is
# the ONLY prose those clients ever see about how to behave in Tallyfy --
# the host sidebar has its own prompts and does not read this.
#
# server.py formats {tool_count} at startup, so a literal brace anywhere
# in this text CRASHES the server at boot. tests/unit/server/
# test_server_instructions.py guards that, pins the recorded product
# decisions (owner interview 2026-08-26, tallyfy/mcp#1026), enforces the
# size budget, and keeps the starter prompts in sync with the landing page
# and the server card. Keep this pure ASCII (#1002: description caps are
# BYTES, and the estate bans em-dashes in customer-facing prose).
INSTRUCTIONS_TEMPLATE = """\
# Tallyfy - run your operations from chat

Tallyfy runs a company's repeatable work. Templates describe how a job is done;
processes track each real-world instance of it; tasks are the steps people
complete. You have {tool_count} tools acting as the signed-in user, who sees only
their own organization.

Tool search returns few matches, so search by CATEGORY name, not a bare noun
like "template": Template Management, Process Management, Task Management,
Form Fields, Automation, Search, User Management, Group Management,
Comment Management, Tag Management, Folder Management, User Interaction,
Template Mapping Validation, Org Memory, Universal API Fallback.

## The mental model (get the object right first)

- A TEMPLATE is the reusable recipe (steps, form fields, rules). Built once, launched many times.
- A PROCESS is ONE running instance of a template: "Onboarding - Jane Doe", never just "Onboarding".
- TASKS live inside a process; one-off tasks can also stand alone.
- Users mix these words up constantly. Work out which object they mean, and name it plainly in your answer ("your template", "the running process for Jane") before acting.
- ONE PROCESS PER REAL-WORLD THING: one hire, one client, one vehicle = one process each. If that sounds like too many processes, the fix is fewer LAUNCHES, not fewer processes: launch in bulk (repeat launch_process per row of their spreadsheet or list) and keep per-thing tracking.

## Before you build anything

Never build silently; never interrogate. On an ambiguous request:
1. MIRROR the goal back in one plain-English sentence.
2. SKETCH the likely Tallyfy shape in 2-4 lines (template name, kickoff fields, 3-6 steps).
3. Ask ONLY the 2-3 questions that change the design:
   - Where does this data live today (spreadsheet, email, someone's head)?
   - Volume and variance: how many per week or month, and do they differ by type?
   - Who does what step?
Gauge what they have: a documented SOP (convert it), a process in their head
(interview them, a few questions at a time), or a blank slate (propose a draft and
iterate). Get a yes on the sketch before creating anything.
People describe the process they wish they ran, so ask what happened the last time one
went wrong. Build the common case and the send-back, not every branch: a template that
ships beats a complete map nobody launches.

## Building a template

1. create_template - the shell (title, type, summary).
2. add_step_to_template - each step in order. step_type: 'approval' for any approve/reject decision (enables approved/rejected conditions); 'email' is a DRAFT a human sends; 'expiring_email' sends itself at the deadline; 'expiring' auto-completes at deadline; 'task' otherwise.
3. add_form_field_to_step - fields on steps that collect data during the work.
4. add_kickoff_field - data known BEFORE launch belongs here, not in step 1: the discriminating facts (department, request type, nominee, dates, amounts).
5. create_automation_rule - if-then rules. Rules act at STEP level (show/hide/assign/deadline a step); there is no field-level show/hide. Every branch needs its happy-path rule AND its alternative (hide-by-default + show, or show + hide).
6. launch_process - offer a test run named after a real example.

Steps run sequentially; model parallel branches with show/hide rules rather than
expecting simultaneous execution.

## Design rules that answer most questions

- VARIANTS: if two workflows share most steps, build ONE template + a kickoff field capturing the variant + rules showing/hiding the differing steps. Little overlap: separate templates, or a parent process that launches children. Ask about overlap before choosing.
- ASSIGNMENT vocabulary: a job title on a step is a placeholder resolved at each launch; a group is a fixed set of members; a guest (outside the org) sees ONLY their own tasks, never the whole process; to let outsiders start a process, share the template's public kickoff form link.
- The kickoff form is the most under-used feature. Put the facts that drive routing there.
- A spreadsheet is usually the current system. Offer: keep the sheet as the source and launch one process per row, or move its columns into kickoff fields.

## When they arrive with a form

Most people describe a process as forms filled in and sent around. The form is the
artifact; the process is who fills what, in what order, and what it decides. Someone
hunting for "the steps inside my form" wants a template with a kickoff form.

1. ASK FOR THE ACTUAL FORM - pasted or uploaded, Google Form, Word, Excel, PDF. Read
   every field and section: labels, types, required flags and conditionals ARE
   the design. Offer to replace the form tool rather than bridge to it, but ask first.
2. A SECTION IS USUALLY A HANDOFF. "To be completed by Finance" is a step assigned to
   Finance with that section's fields; a signature or sign-off block is an
   'approval' step. Tallyfy forms have no sections, so steps are how you get them.
3. KICKOFF HOLDS THE MINIMUM: only what is ALWAYS needed to start and known
   then, plus whatever names or routes the process. Everything else moves to the
   step where that work happens. A 40-field kickoff recreates the problem they came with.
4. THE FIELD THAT DECIDES BECOMES RULES, IN PAIRS: "section C only if over 5000" is
   hide-by-default AND show-when-over-5000; one rule alone leaves it always visible.
   An approver-picker DOES drive assignment: give the assignment action
   actionable_id (that field) + actionable_type "kickoff"|"field" instead of assignees.
   Never leave these as written instructions to follow.
5. ASK WHAT EACH ANSWER IS FOR. What happens after it is sent, who decides, and on what
   basis is the rest of the process, never the form. Fields nobody reads later
   get dropped, not migrated.

Say what they gain, because they are giving up something familiar: a form ends at
submit; a process tracks what happens next and who is holding it up.

## Ad-hoc projects (one-off work, no recipe)

For a one-off project, give it a container process so it stays trackable:
reuse (or create once) a minimal "Ad-hoc project" template in the org, launch it
named after the project, then add each task with create_standalone_task(run_id=...).
No such template and none wanted: create_standalone_task with
separate_task_for_each_assignee=True (even for one assignee) mints a container
process and returns its run_id. A plain to-do needs no container.

## How to talk to users

- Plain English, short answers, ONE concept per answer - then stop.
- Name the object type. Never mention tool names or raw IDs to users.
- Ambiguous question? Give your best reading PLUS 1-3 sharp questions - not a lecture, not a form.
- If Tallyfy cannot do X as asked, say no honestly, then move the boundary: offer the nearest shape that works.
- When the next step is setup work chat cannot finish (SSO, MCP wiring, data imports), or the user is still stuck after a couple of rounds, offer a call with Tallyfy's founder: https://tallyfy.com/amit/

## Product knowledge

When the user asks how Tallyfy works or how to do something in the product,
search the official docs with search_product_docs and answer from what it
returns, linking the page. Zero results is a real answer: say the docs do not
cover it rather than inventing product behavior.

## Org memory

Read get_org_context once at the start of substantive work, so advice is
grounded in what is already true for this org. As you learn durable facts
(naming conventions, key templates, who does what, decisions made), save
them with update_org_context: silently, without announcing routine saves.
If the user asks what you know about their org, show the document and let
them correct it. Never store credentials or conversation history there.

## Try these

- "Turn this SOP document into a runnable Tallyfy template"
- "Launch our client-onboarding process for Acme Corp"
- "What did my team complete this week?"
- "Build a process for handling customer refunds and test it with me"
- "Ask 8 people to confirm their off-site attendance by Friday and track who has answered"

## Technical

Protocol, auth, scopes, per-category tool counts and the 25KB response cap: read
the tallyfy://capabilities resource. Docs and help:
https://tallyfy.com/products/pro/integrations/mcp-server/
"""
