"""
Prometheus metrics for MCP Server

This module defines and manages Prometheus metrics for the MCP server,
including request counters, latency histograms, and error tracking.
"""

from prometheus_client import Counter, Histogram, Gauge, Info
import time
import inspect
from functools import wraps
from typing import Callable, Any
import logging
from constants import (
    SENSITIVE_KEYS,
    LogColors,
    REQUEST_DURATION_BUCKETS,
    API_DURATION_BUCKETS,
    SERVER_VERSION,
    FASTMCP_VERSION,
    PYTHON_VERSION,
)

logger = logging.getLogger(__name__)

# ============================================================================
# MCP Server Metrics
# ============================================================================

# Request metrics
mcp_requests_total = Counter(
    'mcp_server_requests_total',
    'Total number of MCP server requests',
    ['tool_name', 'status']
)

mcp_request_duration_seconds = Histogram(
    'mcp_server_request_duration_seconds',
    'MCP server request duration in seconds',
    ['tool_name'],
    buckets=REQUEST_DURATION_BUCKETS
)

# Tool execution errors
mcp_tool_errors_total = Counter(
    'mcp_server_tool_errors_total',
    'Total number of tool execution errors',
    ['tool_name', 'error_type']
)

# Active connections
mcp_active_connections = Gauge(
    'mcp_server_active_connections',
    'Number of active MCP server connections'
)

# Durable-record shipping outcome (#890).
#
# status="fail" means a failure happened and NOTHING recorded it anywhere that
# survives a deploy, so this counter going non-zero is itself an incident. Each
# fail is also logged at WARNING, which reaches Sentry via LoggingIntegration -
# the counter is the trend, the log line is the page. Deliberately not modelled
# on log_bridge_events_sent_total, whose counter has no alert behind it and which
# is how tallyfy/mcp#697 dropped every batched flush unnoticed for three months.
# status="dropped" means the in-memory queue overflowed, i.e. ingest has been
# unreachable long enough that events are now being discarded.
event_log_total = Counter(
    'mcp_server_event_log_total',
    'MCP server durable failure-record shipping outcomes',
    ['status']
)

# Tallyfy API metrics
tallyfy_api_calls_total = Counter(
    'tallyfy_api_calls_total',
    'Total number of Tallyfy API calls',
    ['operation', 'status']
)

tallyfy_api_duration_seconds = Histogram(
    'tallyfy_api_duration_seconds',
    'Tallyfy API call duration in seconds',
    ['operation'],
    buckets=API_DURATION_BUCKETS
)

# System info
mcp_server_info = Info(
    'mcp_server',
    'MCP server information'
)

# Initialize server info
mcp_server_info.info({
    'version': SERVER_VERSION,
    'fastmcp_version': FASTMCP_VERSION,
    'python_version': PYTHON_VERSION
})

# ============================================================================
# Authentication Metrics (JWT validation)
# ============================================================================

jwt_validation_total = Counter(
    'mcp_server_jwt_validation_total',
    'Total number of JWT validation attempts',
    ['status']  # status: success, failed, invalid_token
)

# Shadow census of WHO is presenting tokens here (tallyfy/mcp#743 AC1).
#
# Counted on every signature-verified token REGARDLESS of ENFORCE_JWT_AUDIENCE,
# which is the entire point: that flag is "false" in production and staging
# (measured 2026-08-09), so the accept/reject block it guards never runs and
# `jwt_validation_total` therefore reports nothing but `status="success"`. A
# quiet metric is equally consistent with a healthy population and with a
# control that is switched off, so this counter answers the question the other
# one cannot.
#
# The label vocabulary is CLOSED - the six values in AUDIENCE_CLASSES and
# nothing else. `claims` is decoded from an attacker-supplied JWT, so a label
# derived from a claim VALUE would let any caller mint unbounded time series
# and exhaust Prometheus memory. classify_audience() maps to a fixed set
# instead, capping cardinality at 6 per environment.
jwt_audience_class_total = Counter(
    'mcp_server_jwt_audience_class_total',
    'Verified JWTs by audience class, counted regardless of ENFORCE_JWT_AUDIENCE',
    ['audience_class']  # see utils.tallyfy_auth_provider.AUDIENCE_CLASSES
)

# Downstream token exchange census (#894), the sibling of the counter above and
# built the same way for the same reason.
#
# utils/downstream_token.py has a `shadow` mode whose ENTIRE JOB is to produce a
# census you watch before deciding on `enforce`. Until this counter existed there
# was nothing to watch: the module emitted two log lines and registered no metric,
# so the census lived only in container stdout and every deploy discarded it. A
# gate whose evidence cannot be read is a gate that cannot fail.
#
# COUNTED IN EVERY MODE, `off` INCLUDED, and that is the point rather than an
# oversight. `off` short-circuits before any exchange, so counting it is the only
# thing that distinguishes "shadow is running and nothing failed" from "nobody
# called the server this week". It makes the series self-denominating: summed
# across outcomes it IS the authenticated-credential-resolution count for the
# window, so the usage denominator the audience census needs a second query for
# is already here.
#
# THE LABEL VOCABULARY IS CLOSED, both labels, and neither is derived from
# anything a caller controls.
#   mode    - exactly the three in downstream_token._VALID_MODES. _mode()
#             normalises an unrecognised env value to "off" BEFORE it reaches
#             this label, so a typo in the deploy config cannot mint a series.
#   outcome - the seven in downstream_token._OUTCOMES.
# Cap is 3 x 7 = 21 series per environment.
#
# ⚠️ NOTHING HERE MAY CARRY A TOKEN, A SUBJECT, AN ORG, A `jti` OR AN api-v2
# ERROR BODY. The cache key is (sub, org_id, jti) and every one of those is
# per-user unbounded; a label built from any of them is both a Prometheus memory
# exhaustion and a credential leak into a scrape endpoint. The failure REASON is
# recorded, the failure MESSAGE is not.
#
# ⚠️ A closed vocabulary is worth nothing if the code only ever emits one member.
# That is live in this repo right now: mcp_server_tool_errors_total{error_type}
# has exactly one value ever, the literal "unknown", against 79 distinct
# tool_name values (#603). Guarded here by
# tests/unit/server/utils/test_downstream_token.py, which walks the module AST
# and fails if any `raise DownstreamTokenError` omits an explicit reason, so a
# raise site added tomorrow cannot silently default to "unknown".
downstream_token_exchange_total = Counter(
    'mcp_server_downstream_token_exchange_total',
    'Downstream token exchange outcomes by mode, counted in every mode including off',
    ['mode', 'outcome']  # see utils.downstream_token._VALID_MODES and _OUTCOMES
)

# ============================================================================
# Helpers
# ============================================================================

def _format_params(kwargs: dict) -> str:
    """
    Format tool kwargs for logging.
    - Drops keys containing sensitive words (api_key, token, etc.)
    - Truncates strings longer than 80 chars
    - Shows length hint for lists/dicts that would produce >120 chars
    """
    if not kwargs:
        return ""
    parts = []
    for k, v in kwargs.items():
        if any(s in k.lower() for s in SENSITIVE_KEYS):
            continue
        if v is None:
            continue
        if isinstance(v, str):
            display = repr(v[:80] + '…') if len(v) > 80 else repr(v)
        elif isinstance(v, list):
            raw = repr(v)
            display = f"[…{len(v)} items]" if len(raw) > 120 else raw
        elif isinstance(v, dict):
            raw = repr(v)
            display = f"{{…{len(v)} keys}}" if len(raw) > 120 else raw
        else:
            display = repr(v)
        parts.append(f"{k}={display}")
    return ', '.join(parts)


# ============================================================================
# Metric Decorators
# ============================================================================

def track_tool_execution(tool_name: str):
    """
    Decorator to track tool execution metrics.

    This decorator measures:
    - Request count by status (success, validation_error, tallyfy_error, error)
    - Request duration
    - Error counts by type

    Args:
        tool_name: Name of the tool being tracked

    Returns:
        Decorated function with metrics tracking

    Example:
        @mcp.tool()
        @track_tool_execution("create_task")
        def create_task(api_key: str, org_id: str, title: str):
            # tool implementation
            pass
    """
    def decorator(func: Callable) -> Callable:
        # Capture param names once at decoration time
        try:
            _param_names = list(inspect.signature(func).parameters.keys())
        except (ValueError, TypeError):
            _param_names = []

        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            # Merge positional args (by position) with keyword args for logging
            all_params = dict(zip(_param_names, args))
            all_params.update(kwargs)

            # Get org_id from context
            from utils.org_id_middleware import get_org_id
            org_id = get_org_id() or 'unknown'

            # Read pre-decoded JWT claims from OrgIdMiddleware (P2-I — single decode per request)
            api_key = ''
            user_id = 'unknown'
            try:
                from utils.org_id_middleware import get_jwt_claims
                claims = get_jwt_claims()
                if claims:
                    user_id = claims.get('sub') or claims.get('user_id') or claims.get('uid', 'unknown')
                from mcp.server.auth.middleware.auth_context import get_access_token
                access_token = get_access_token()
                if access_token:
                    api_key = access_token.token
            except Exception:
                pass

            start_time = time.time()
            status = 'success'
            error_type = None

            # Log tool execution start with all non-sensitive params
            logger.info(f"{LogColors.GRAY}│ {LogColors.WHITE}┌─TOOL START {LogColors.GRAY}│{LogColors.WHITE} {tool_name}{LogColors.RESET}")
            params_str = _format_params(all_params)
            if params_str:
                logger.info(f"{LogColors.GRAY}│          └─ {LogColors.CYAN}{params_str}{LogColors.RESET}")

            try:
                # Open an OTel span around the tool body when tracing is on
                # (no-op when OTEL_EXPORTER_OTLP_ENDPOINT is unset).
                from utils.otel_init import trace_span
                with trace_span(
                    f"mcp.tool.{tool_name}",
                    tool_name=tool_name,
                    user_id=user_id,
                    org_id=org_id,
                ):
                    result = func(*args, **kwargs)

                # Log successful completion (white color with └─ prefix)
                logger.info(f"{LogColors.GRAY}│ {LogColors.WHITE}└─TOOL OK    {LogColors.GRAY}│{LogColors.WHITE} {tool_name}{LogColors.RESET}")

                return result

            except Exception as e:
                # Determine error type from exception name
                error_class = e.__class__.__name__

                if 'ValidationError' in error_class:
                    status = 'validation_error'
                    error_type = 'validation'
                elif 'TallyfyError' in error_class or 'Tallyfy' in error_class:
                    status = 'tallyfy_error'
                    error_type = 'tallyfy_api'
                else:
                    status = 'error'
                    error_type = 'unknown'

                # Track error
                mcp_tool_errors_total.labels(
                    tool_name=tool_name,
                    error_type=error_type
                ).inc()

                # Log error with context (warning level — @handle_tallyfy_errors already logs at ERROR)
                duration_ms = (time.time() - start_time) * 1000
                error_msg = str(e)[:200]  # First 200 chars
                logger.warning(f"✗ TOOL ERROR │ {tool_name:30} │ {duration_ms:6.1f}ms │ {error_type}={error_msg} │ user={user_id} │ org={org_id}")

                # Log token fingerprint on 401 errors (debug level — diagnostic detail)
                if '401' in error_msg and api_key:
                    token_hint = api_key[-8:] if len(api_key) > 8 else "***"
                    logger.debug(f"✗ AUTH 401   │ token=...{token_hint} │ org={org_id}")

                raise

            finally:
                # Track request metrics
                duration = time.time() - start_time
                mcp_requests_total.labels(
                    tool_name=tool_name,
                    status=status
                ).inc()
                mcp_request_duration_seconds.labels(
                    tool_name=tool_name
                ).observe(duration)

        return wrapper
    return decorator


def track_tallyfy_api_call(operation: str):
    """
    Decorator to track Tallyfy SDK API calls.

    Args:
        operation: Name of the API operation (e.g., 'get_tasks', 'create_process')

    Returns:
        Decorated function with API metrics tracking

    Example:
        @track_tallyfy_api_call("get_tasks")
        def fetch_tasks(sdk, org_id):
            return sdk.tasks.list(org_id)
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            start_time = time.time()
            status = 'success'

            try:
                result = func(*args, **kwargs)
                return result

            except Exception as e:
                status = 'error'
                logger.error(f"Tallyfy API call '{operation}' failed: {e}")
                raise

            finally:
                duration = time.time() - start_time
                tallyfy_api_calls_total.labels(
                    operation=operation,
                    status=status
                ).inc()
                tallyfy_api_duration_seconds.labels(
                    operation=operation
                ).observe(duration)

        return wrapper
    return decorator


# ============================================================================
# Context Managers
# ============================================================================

class track_connection:
    """
    Context manager to track active connections.

    Example:
        with track_connection():
            # connection handling code
            pass
    """
    def __enter__(self):
        mcp_active_connections.inc()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        mcp_active_connections.dec()
        return False


# ============================================================================
# Manual Metric Functions
# ============================================================================

def record_tool_success(tool_name: str, duration: float):
    """Record a successful tool execution."""
    mcp_requests_total.labels(tool_name=tool_name, status='success').inc()
    mcp_request_duration_seconds.labels(tool_name=tool_name).observe(duration)


def record_tool_error(tool_name: str, error_type: str, duration: float):
    """Record a failed tool execution."""
    mcp_requests_total.labels(tool_name=tool_name, status='error').inc()
    mcp_tool_errors_total.labels(tool_name=tool_name, error_type=error_type).inc()
    mcp_request_duration_seconds.labels(tool_name=tool_name).observe(duration)


def increment_active_connections():
    """Increment active connection count."""
    mcp_active_connections.inc()


def decrement_active_connections():
    """Decrement active connection count."""
    mcp_active_connections.dec()


# ============================================================================
# Authentication Metric Functions (JWT validation)
# ============================================================================

def record_jwt_validation(status: str):
    """
    Record a JWT validation attempt.

    Args:
        status: Validation status (success, failed, invalid_token)
    """
    jwt_validation_total.labels(status=status).inc()


def record_jwt_audience_class(audience_class: str):
    """
    Record the audience class of one signature-verified JWT.

    Changes no behaviour: the caller counts and then returns exactly what it
    would have returned anyway. See the counter's declaration above for why
    the label vocabulary must stay closed.

    Args:
        audience_class: One of utils.tallyfy_auth_provider.AUDIENCE_CLASSES
    """
    jwt_audience_class_total.labels(audience_class=audience_class).inc()


def record_downstream_token_exchange(mode: str, outcome: str):
    """Record one downstream-token decision.

    Changes no behaviour: every caller counts and then returns exactly what it
    would have returned anyway, in every mode. See the counter's declaration
    above for why the label vocabulary must stay closed and why `off` is counted.

    Args:
        mode: one of utils.downstream_token._VALID_MODES
        outcome: one of utils.downstream_token._OUTCOMES
    """
    downstream_token_exchange_total.labels(mode=mode, outcome=outcome).inc()
