"""
FastMCP Error Handling Utilities
Standardized error handling patterns for MCP tools
"""

import re
from functools import wraps
from fastmcp.exceptions import ToolError
from tallyfy import TallyfyError
import logging
import sentry_sdk

logger = logging.getLogger(__name__)


_LEAKED_INTERNALS_RE = re.compile(
    # SQL errors & queries
    r"SQLSTATE\[|"
    r"\bSELECT\b.*\bFROM\b|"
    r"\bINSERT\b.*\bINTO\b|"
    r"\bUPDATE\b.*\bSET\b|"
    r"\bDELETE\b.*\bFROM\b|"
    r"\bALTER\s+TABLE\b|"
    r"\bCREATE\s+TABLE\b|"
    r"\bDROP\s+TABLE\b|"
    # Laravel / PHP internals
    r"\(Connection:\s*\w+,\s*SQL:|"
    r"\bIlluminate\\|"
    r"\bPDOException\b|"
    r"\bQueryException\b|"
    r"\bErrorException\b|"
    r"in /[^\s]+\.php on line \d+|"
    r"\bvendor/|"
    # PostgreSQL context lines
    r"\bCONTEXT:\s*unnamed portal|"
    r"\bHINT:|"
    r"\bDETAIL:|"
    r"\bFATAL:|"
    # Stack traces (generic, PHP, Python)
    r"Stack trace:|"
    r"#\d+\s+/[^\s]+\.php|"
    r"at /[^\s]+\.py:\d+|"
    r"\bTraceback \(most recent call",
    re.IGNORECASE,
)

_GENERIC_ERROR = "an internal error occurred. Please try again or contact support."


_LEAK_SPLIT_MARKERS = [
    "SQLSTATE", "Stack trace", "Traceback", "Illuminate\\",
    "PDOException", "QueryException", "ErrorException",
    "HINT:", "DETAIL:", "FATAL:", "vendor/",
    "ALTER TABLE", "CREATE TABLE", "DROP TABLE",
    "in /",
]


def _sanitize_api_error(message: str) -> str:
    """Strip leaked internals (SQL, stack traces, file paths) from API error messages."""
    if _LEAKED_INTERNALS_RE.search(message):
        before_leak = message
        for marker in _LEAK_SPLIT_MARKERS:
            before_leak = before_leak.split(marker)[0]
        before_leak = before_leak.strip().rstrip(" ——-:")
        if len(before_leak) > 10:
            return before_leak
        return _GENERIC_ERROR
    return message


def _extract_api_message(error: TallyfyError) -> str:
    """
    Extract a clean, user-facing message from a TallyfyError.

    The SDK formats messages as:
      "API request failed with status 400: <actual message>"
    This strips the technical prefix and returns just the API message.
    If response_data contains a 'message' key, prefer that.

    Internal system details (SQL queries, stack traces, file paths) are
    stripped before returning — the full error is already in logs/Sentry.
    """
    # Prefer the message from response_data if available
    response_data = getattr(error, "response_data", None)
    if isinstance(response_data, dict) and "message" in response_data:
        return _sanitize_api_error(response_data["message"])

    # Strip the SDK's "API request failed with status NNN: " prefix
    raw = str(error)
    match = re.match(r"API request failed with status \d+:\s*(.+)", raw)
    if match:
        return _sanitize_api_error(match.group(1))

    return _sanitize_api_error(raw)


def _build_error_message(operation_name: str, error: TallyfyError) -> str:
    """
    Build a user-facing error message from a TallyfyError.

    Only exposes the API's own message — no HTTP status codes, internal
    error codes, or implementation details. Technical context is already
    captured in logs and Sentry.
    """
    api_msg = _extract_api_message(error)
    return f"Could not {operation_name} — {api_msg}"


def handle_tallyfy_errors(operation_name: str):
    """
    Decorator to standardize Tallyfy API error handling for MCP tools.

    Converts TallyfyError and other exceptions into FastMCP ToolError
    with user-friendly messaging while preserving detailed logging.

    Args:
        operation_name: Human-readable description of the operation

    Returns:
        Decorator function for tool methods

    Example:
        @mcp.tool(...)
        @handle_tallyfy_errors("get organization users")
        def get_organization_users(...):
            # Clean implementation - error handling is automatic
            with TallyfySDK(api_key=api_key) as sdk:
                return sdk.get_organization_users(org_id, with_groups)
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except TallyfyError as e:
                # Demote expected operational errors to WARNING — these are NOT bugs:
                #   400: client-side input missing (e.g. MissingOrgIdError when no
                #        X-Organization-ID header / JWT claim / env var present —
                #        Sentry MCP-4T / issue #511). Not a server bug — the client
                #        request was malformed and the user-facing message tells them
                #        how to fix it.
                #   401/403: expired/invalid JWT tokens
                #   404: resource was deleted (LLM referenced stale ID)
                #   422: request validation failed (e.g. missing field for approval task)
                # These should not create Sentry issues. Only true server errors (5xx)
                # and unexpected client errors are logged at ERROR.
                status = getattr(e, "status_code", None)
                if status in (400, 401, 403, 404, 422):
                    logger.warning(f"{operation_name} returned {status}: {e}")
                else:
                    # Set Sentry tags so LoggingIntegration event has context
                    sentry_sdk.set_tag("operation", operation_name)
                    sentry_sdk.set_tag("error_type", "tallyfy_api")
                    if status:
                        sentry_sdk.set_tag("http_status", str(status))
                    # logger.error triggers Sentry via LoggingIntegration — no explicit capture needed
                    logger.error(f"Tallyfy API error in {operation_name}: {e}")

                # 401/403 from the Tallyfy API means the OAuth token is expired or was
                # issued with an audience restriction (aud) that the Tallyfy API rejects.
                # Append a re-authentication hint so the caller knows what to do.
                if status in (401, 403):
                    api_msg = _extract_api_message(e)
                    raise ToolError(
                        f"Could not {operation_name} — {api_msg} "
                        f"(Your session may be expired or misconfigured. "
                        f"Please re-authenticate the MCP connector and retry.)"
                    )

                # Raise descriptive ToolError with status code + API message
                raise ToolError(_build_error_message(operation_name, e))
            except ToolError:
                # Re-raise ToolError directly (already properly formatted)
                raise
            except Exception as e:
                # Set Sentry tags so LoggingIntegration event has context
                sentry_sdk.set_tag("operation", operation_name)
                sentry_sdk.set_tag("error_type", "unexpected")
                # logger.error triggers Sentry via LoggingIntegration — no explicit capture needed
                logger.error(f"Unexpected error in {operation_name}: {e}", exc_info=True)

                # User-facing message without internal details (type, traceback, etc.)
                # Full context is already captured in the log/Sentry above
                raise ToolError(
                    f"Could not {operation_name} — {_sanitize_api_error(str(e))}"
                )
        return wrapper
    return decorator
