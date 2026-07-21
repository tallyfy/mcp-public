"""
FastMCP Error Handling Utilities
Standardized error handling patterns for MCP tools
"""

import re
from functools import wraps
from typing import Any
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


_MAX_FIELD_ERRORS = 12
_MAX_FIELD_ERROR_CHARS = 900


def _flatten_detail(detail: Any) -> list:
    """
    Flatten one ``errors`` value into a list of plain strings.

    Laravel nests unpredictably here: a plain string, a list of strings, or —
    for keyed payloads such as ``tasks`` and ``prerun`` — a list of dicts whose
    values are themselves lists. Recursing keeps Python list reprs out of the
    message the caller reads.
    """
    if isinstance(detail, str):
        return [detail]
    if isinstance(detail, dict):
        out = []
        for key, value in detail.items():
            out.extend(f"{key}: {msg}" for msg in _flatten_detail(value))
        return out
    if isinstance(detail, (list, tuple)):
        out = []
        for item in detail:
            out.extend(_flatten_detail(item))
        return out
    return [str(detail)]


def _format_field_errors(errors: Any) -> str:
    """
    Flatten a Laravel ``errors`` block into a compact, agent-readable string.

    Laravel returns ``{"message": ..., "errors": {"<field.path>": ["msg", ...]}}``
    where ``message`` is often just "The given data was invalid." and every
    actionable detail — which field, what was wrong — lives only in ``errors``.
    Dropping it leaves the caller with nothing to correct, so we surface it.

    Values may be a list of strings, a bare string, or (for nested payloads such
    as ``tasks``) a list of dicts keyed by ID, so each shape is handled.
    """
    if not isinstance(errors, dict) or not errors:
        return ""

    parts = []
    for field, detail in list(errors.items())[:_MAX_FIELD_ERRORS]:
        messages = _flatten_detail(detail)

        joined = "; ".join(str(m) for m in messages if m)
        if joined:
            parts.append(f"{field}: {joined}")

    if not parts:
        return ""

    remaining = len(errors) - _MAX_FIELD_ERRORS
    if remaining > 0:
        parts.append(f"(+{remaining} more)")

    rendered = _sanitize_api_error(" | ".join(parts))
    if len(rendered) > _MAX_FIELD_ERROR_CHARS:
        rendered = rendered[:_MAX_FIELD_ERROR_CHARS].rstrip() + "…"
    return rendered


# Markers that mean a 403 really is an authentication or authorization failure
# rather than a domain rule. Kept deliberately narrow: anything not matched here
# keeps its own specific message with no re-authentication hint bolted on.
_AUTH_STYLE_MARKERS = (
    "unauthenticated",
    "unauthorized",
    "token",
    "expired",
    "invalid credentials",
    "access denied",
    "permission denied",
    "forbidden",
    "audience",
)


def _extract_primary_message(error: TallyfyError) -> str:
    """
    Extract only the primary API message for auth-style classification.

    Unlike _extract_api_message, this does NOT append field errors — preventing
    unrelated field text (containing substrings like 'token' or 'expired') from
    influencing the auth classification. It also correctly treats an empty body
    after the SDK prefix as empty (not as the full SDK string).
    """
    response_data = getattr(error, "response_data", None)

    if isinstance(response_data, dict) and "message" in response_data:
        return _sanitize_api_error(response_data["message"])

    raw = str(error)
    match = re.match(r"API request failed with status \d+:\s*(.*)", raw)
    if match:
        body = match.group(1).strip()
        return _sanitize_api_error(body) if body else ""
    return _sanitize_api_error(raw)


def _is_auth_style_message(error: TallyfyError) -> bool:
    """
    Decide whether a 403 is an auth failure (hint helps) or a business rule
    (hint actively misleads, see #592).

    A 403 with no message at all AND no field errors is treated as auth-style,
    preserving the old behaviour for the opaque case where the caller has
    nothing else to go on.  When ``response_data`` carries an ``errors`` block
    the 403 is a business-rule rejection even if ``message`` is blank.
    """
    message = _extract_primary_message(error)
    if not message or not message.strip():
        response_data = getattr(error, "response_data", None)
        if isinstance(response_data, dict) and response_data.get("errors"):
            return False
        return True
    lowered = message.lower()
    return any(marker in lowered for marker in _AUTH_STYLE_MARKERS)


def _extract_api_message(error: TallyfyError) -> str:
    """
    Extract a clean, user-facing message from a TallyfyError.

    The SDK formats messages as:
      "API request failed with status 400: <actual message>"
    This strips the technical prefix and returns just the API message.
    If response_data contains a 'message' key, prefer that, and append the
    per-field ``errors`` block when present so the caller can self-correct.

    Internal system details (SQL queries, stack traces, file paths) are
    stripped before returning — the full error is already in logs/Sentry.
    """
    response_data = getattr(error, "response_data", None)

    # The per-field ``errors`` block is the only part that names the offending
    # field, so it must survive regardless of which source supplies the message.
    # Some api-v2 responses (custom ResourceExceptions) carry `errors` with no
    # `message` at all — dropping them there would defeat the whole point.
    field_errors = ""
    if isinstance(response_data, dict):
        field_errors = _format_field_errors(response_data.get("errors"))

    if isinstance(response_data, dict) and "message" in response_data:
        message = _sanitize_api_error(response_data["message"])
    else:
        # Strip the SDK's "API request failed with status NNN: " prefix
        raw = str(error)
        match = re.match(r"API request failed with status \d+:\s*(.+)", raw)
        message = _sanitize_api_error(match.group(1) if match else raw)

    if field_errors:
        return f"{message} [{field_errors}]"
    return message


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
                    # Attach the API response body so it's visible in Sentry
                    # (the HTTP breadcrumb's `reason` field gets server-side
                    # scrubbed to [Filtered]; this context survives scrubbing)
                    response_data = getattr(e, "response_data", None)
                    sentry_sdk.set_context("api_response", {
                        "status_code": status,
                        "message": _extract_api_message(e),
                        "response_body": str(response_data)[:2000] if response_data else None,
                    })
                    # logger.error triggers Sentry via LoggingIntegration — no explicit capture needed
                    logger.error(f"Tallyfy API error in {operation_name}: {e}")

                # 401, and 403s that actually look like auth failures, mean the OAuth
                # token is expired or carries an audience the Tallyfy API rejects, so a
                # re-authentication hint helps. A 403 is ALSO how api-v2 refuses a
                # business rule ("Cannot disable guest with incomplete tasks"), and
                # appending the hint there told users to re-authenticate over a correct,
                # specific rejection they could do nothing about (#592). Trust a specific
                # domain message; only add the hint when the message reads like auth.
                if status == 401 or (status == 403 and _is_auth_style_message(e)):
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
