"""Sentry configuration for MCP Server component."""

import sys
import logging
import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from constants import SENTRY_ENABLED, SENTRY_DSN, SENTRY_ENVIRONMENT, SENTRY_RELEASE, SENTRY_TRACES_SAMPLE_RATE, SENTRY_PROFILES_SAMPLE_RATE

logger = logging.getLogger(__name__)


def filter_metrics_transactions(event, hint):
    """
    Filter out /metrics endpoint transactions from Sentry.

    Prometheus scrapes /metrics every few seconds, generating high-volume
    transaction noise that doesn't provide actionable insights.

    Args:
        event: Sentry transaction event dict
        hint: Additional hints about the event

    Returns:
        None to drop the transaction, or the event to send it
    """
    # Check if this is a transaction (not an error event)
    if event.get("type") == "transaction":
        transaction_name = event.get("transaction", "")

        # Drop /metrics transactions (e.g., "GET /metrics")
        if "/metrics" in transaction_name:
            return None

    return event


def init_sentry_server():
    """Initialize Sentry for MCP Server with FastMCP-specific configuration."""
    # Check if Sentry is explicitly disabled
    if SENTRY_ENABLED == "false":
        logger.info("Sentry is disabled for mcp-server (SENTRY_ENABLED=false)")
        return

    if not SENTRY_DSN:
        logger.warning("Sentry DSN not configured for mcp-server, skipping initialization")
        return

    # A 0 rate must reach the SDK as None, not 0.0. With 0.0 the SDK leaves
    # performance tracing *enabled* at 0% sampling: the Starlette/FastMCP
    # integration still opens a transaction on every request, then drops it
    # client-side (an unbilled `client_discard`) — wasted CPU and millions of
    # phantom spans cluttering Sentry's "Usage" view. None disables tracing
    # outright so no transaction is ever created. Errors are unaffected: they
    # ride LoggingIntegration / `sample_rate`, independent of traces_sample_rate.
    traces_sample_rate = SENTRY_TRACES_SAMPLE_RATE or None
    profiles_sample_rate = SENTRY_PROFILES_SAMPLE_RATE or None

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=SENTRY_ENVIRONMENT,
        release=SENTRY_RELEASE,
        integrations=[
            LoggingIntegration(
                level=logging.INFO,
                event_level=logging.ERROR
            ),
            StarletteIntegration(),
        ],
        traces_sample_rate=traces_sample_rate,
        profiles_sample_rate=profiles_sample_rate,
        send_default_pii=False,
        before_send=scrub_tool_arguments,
        before_send_transaction=filter_metrics_transactions,
        # Ignore FastMCP-specific errors
        ignore_errors=[
            "ToolError",  # FastMCP tool errors are handled
            "ValidationError",  # Validation errors are expected
        ],
    )

    # Set global tags
    sentry_sdk.set_tag("component", "mcp-server")
    sentry_sdk.set_tag("python_version", f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

    logger.info(f"✓ Sentry initialized for mcp-server (env={SENTRY_ENVIRONMENT}, release={SENTRY_RELEASE}, traces={traces_sample_rate}, profiles={profiles_sample_rate})")


def scrub_tool_arguments(event, hint):
    """
    Scrub sensitive tool arguments before sending to Sentry.

    Args:
        event: Sentry event dict
        hint: Additional hints about the event

    Returns:
        Scrubbed event dict
    """
    # Drop FastMCP client-disconnect noise — fires when a direct MCP client
    # (ChatGPT, Claude Desktop) drops the HTTP connection before the SSE
    # response is delivered. Not actionable; captured by LoggingIntegration
    # because FastMCP logs it at ERROR level.
    if event.get("logger") == "mcp.server.streamable_http":
        msg = (event.get("logentry") or {}).get("message") or event.get("message", "")
        if "No response message received before stream closed" in msg:
            return None

    # Drop FastMCP's "Error calling tool 'X'" noise. Our @handle_tallyfy_errors
    # decorator already handles Sentry reporting: 400/401/403/404/422 are demoted
    # to WARNING (never reach Sentry), and real errors (5xx/unexpected) are logged
    # at ERROR with proper tags (operation, error_type, http_status). FastMCP then
    # catches the ToolError we raise and re-logs a generic message at ERROR level
    # via its own logger — no status code, no context, not actionable.
    if event.get("logger") == "fastmcp.server.server":
        msg = (event.get("logentry") or {}).get("message") or event.get("message", "")
        if "Error calling tool" in msg:
            return None

    # Drop ClosedResourceError — raised by anyio when a client closes the
    # browser tab or loses the connection mid-stream. This is expected noise
    # in an SSE server; there are no DB/Redis clients in this codebase that
    # could produce the same exception from a real error.
    exc_info = hint.get("exc_info") if hint else None
    if exc_info and exc_info[0] is not None:
        if exc_info[0].__name__ == "ClosedResourceError":
            return None

    # Scrub tool arguments in extra context
    if "extra" in event:
        if "tool_args" in event["extra"]:
            args = event["extra"]["tool_args"]
            if isinstance(args, dict):
                # Redact api_key
                if "api_key" in args and isinstance(args["api_key"], str):
                        args["api_key"] = "[REDACTED]"

                # Redact any other sensitive fields
                for key in list(args.keys()):
                    if any(sensitive in key.lower() for sensitive in ["token", "password", "secret", "credential"]):
                        args[key] = "[REDACTED]"

        # Scrub operation context
        if "operation" in event["extra"]:
            op = event["extra"]["operation"]
            if isinstance(op, dict):
                for key in ["api_key", "token", "password"]:
                    if key in op:
                        op[key] = "[REDACTED]"

    # Scrub request headers
    if "request" in event:
        request = event["request"]
        if "headers" in request:
            sensitive_headers = ["Authorization", "Cookie", "X-Api-Key"]
            for header in sensitive_headers:
                if header in request["headers"]:
                    request["headers"][header] = "[REDACTED]"
                # Also check lowercase
                header_lower = header.lower()
                if header_lower in request["headers"]:
                    request["headers"][header_lower] = "[REDACTED]"

        # Scrub cookies
        if "cookies" in request and request["cookies"]:
            request["cookies"] = {k: "[REDACTED]" for k in request["cookies"]}

    # Scrub contexts
    if "contexts" in event:
        for context_name in list(event["contexts"].keys()):
            context = event["contexts"][context_name]
            if isinstance(context, dict):
                for key in list(context.keys()):
                    if any(sensitive in key.lower() for sensitive in ["api_key", "token", "password", "secret"]):
                        if isinstance(context[key], str) and len(context[key]) > 8:
                            context[key] = f"{context[key][:8]}...[REDACTED]"
                        else:
                            context[key] = "[REDACTED]"

    # Scrub user data
    if "user" in event and isinstance(event["user"], dict):
        # Don't send IP addresses
        if "ip_address" in event["user"]:
            event["user"]["ip_address"] = None


    # Group errors by tool operation + error type to prevent fragmented Sentry issues
    tags = event.get("tags", {})
    # tags can be a list of [key, value] pairs or a dict
    if isinstance(tags, list):
        tags_dict = {item[0]: item[1] for item in tags if isinstance(item, (list, tuple)) and len(item) == 2}
    else:
        tags_dict = tags
    operation_tag = tags_dict.get("operation")
    error_type_tag = tags_dict.get("error_type")
    if operation_tag and error_type_tag:
        event["fingerprint"] = ["{{ default }}", operation_tag, error_type_tag]

    return event



def add_tool_breadcrumb(tool_name: str, operation: str, duration_ms: float = None, success: bool = True):
    """
    Add a breadcrumb for tool execution.

    Args:
        tool_name: Name of the tool
        operation: Operation being performed
        duration_ms: Duration in milliseconds
        success: Whether the operation succeeded
    """
    data = {
        "tool": tool_name,
        "operation": operation,
        "success": success
    }

    if duration_ms is not None:
        data["duration_ms"] = duration_ms

    sentry_sdk.add_breadcrumb(
        category="mcp.tool",
        message=f"Tool: {tool_name} - {operation}",
        level="info" if success else "error",
        data=data
    )
