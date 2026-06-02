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
