"""Sentry configuration for MCP Server component."""

import sys
import logging
import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from constants import SENTRY_ENABLED, SENTRY_DSN, SENTRY_ENVIRONMENT, SENTRY_RELEASE, SENTRY_TRACES_SAMPLE_RATE, SENTRY_PROFILES_SAMPLE_RATE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exception-tree helpers.
#
# These exist because `ignore_errors` and any `exc_info[0].__name__` check are
# both blind to an ExceptionGroup, and this server produces them routinely: an
# ASGI/anyio task group wraps whatever its children raised.
#
# Measured, because the naive fix looks correct and does nothing. With
# `ignore_errors=["ClientDisconnect"]` configured:
#
#     bare ClientDisconnect               -> 0 events   (suppressed)
#     ExceptionGroup[ClientDisconnect]    -> 1 event    (NOT suppressed)
#     unrelated RuntimeError              -> 1 event    (control)
#
# `Client._is_ignored_error` reads `exc_info[0]` only, which for a raised group
# is the group itself. MCP-SERVER-5M arrives with
# `mechanism.is_exception_group: true` and `ClientDisconnect` as a CHILD, so an
# `ignore_errors` entry would never have matched it.
# ---------------------------------------------------------------------------

# Exception type names that are never actionable. Matched by name rather than by
# import so this module does not have to import anyio or starlette just to build
# a denylist, and so a dependency shuffle cannot turn a suppression into a
# silent import error.
NOISE_EXCEPTION_TYPES = frozenset({
    # Client closed the browser tab, navigated away, or the tunnel dropped the
    # connection mid-stream. Normal for an SSE server.
    "ClientDisconnect",   # starlette.requests
    "ClosedResourceError",  # anyio
})


def iter_exception_tree(exc):
    """Yield ``exc`` and every exception nested inside it, groups included."""
    seen = set()
    stack = [exc]
    found = []
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        found.append(current)
        stack.extend(getattr(current, "exceptions", None) or ())
    return found


def leaf_exceptions(exc):
    """The exceptions that actually failed, with grouping containers removed.

    An ``ExceptionGroup`` is packaging, not a fault, so it must not be the thing
    a suppression decision is made about.
    """
    return [
        e for e in iter_exception_tree(exc)
        if not isinstance(e, BaseExceptionGroup)
    ]


def is_pure_noise(exc) -> bool:
    """True only when EVERY leaf is a known-noise type.

    Deliberately ``all`` rather than ``any``. A group carrying a real failure
    alongside a disconnect is a real failure, and dropping it because one child
    was noise would be a silent loss. An empty leaf list returns False, so an
    exception shape this cannot decode stays visible.
    """
    leaves = leaf_exceptions(exc)
    if not leaves:
        return False
    return all(type(e).__name__ in NOISE_EXCEPTION_TYPES for e in leaves)


def is_unknown_tool_call(exc) -> bool:
    """True when a client asked for a tool this server does not expose.

    FastMCP raises ``NotFoundError`` from its own middleware chain, before any
    of our decorators run, so nothing in `utils/fastmcp_errors.py` ever sees it
    and it reaches Sentry as an unhandled-looking error. It is a CALLER fault
    of exactly the class #694 says not to page on: an external client, usually
    a directory listing that is out of date, calling a tool we removed.

    Narrow on purpose. It matches only FastMCP's own ``NotFoundError`` carrying
    an "Unknown tool" message, so a ``NotFoundError`` meaning "this template does
    not exist" is untouched.
    """
    for e in leaf_exceptions(exc):
        cls = type(e)
        if cls.__name__ != "NotFoundError":
            continue
        if not (cls.__module__ or "").startswith("fastmcp"):
            continue
        if "unknown tool" in str(e).lower():
            return True
    return False


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

    # Drop the uvicorn lifespan shutdown traceback. On SIGTERM uvicorn cancels
    # the lifespan task, and Starlette's `lifespan` is sitting in `await
    # receive()`, so the CancelledError unwinds through uvicorn's own error
    # logger at ERROR level. It is what an orderly shutdown looks like.
    #
    # It is a LATENT path, not ongoing noise. Measured 2026-08-05: 2 events
    # total, 6 seconds apart on 2026-07-21, both belonging to the single
    # deploy that replaced the pre-#591 container; 28 successful staging
    # deploys since produced none. So a quiet dashboard is not evidence this
    # guard works. It is here because the shutdown path is unchanged.
    #
    # This has to be matched HERE rather than via `ignore_errors`, and the
    # reason is not the ExceptionGroup problem above. MCP-SERVER-5Q carries
    # `hasException: 0`: uvicorn formats the traceback into the log MESSAGE, so
    # there is no `exc_info` in the hint for `ignore_errors` to inspect at all.
    # Adding "CancelledError" to that list would change nothing.
    #
    # Deliberately narrow: uvicorn's logger, plus BOTH the lifespan frame and
    # the CancelledError. A CancelledError from anywhere else, or any other
    # uvicorn error, still reaches Sentry.
    #
    # Note the asymmetry with `host/sentry_config.py`, which DOES list
    # "CancelledError" in `ignore_errors` (MCP-45). That is not copied here on
    # purpose: it was added for the host's own `periodic_cleanup()` task, and a
    # blanket cancellation filter on this process would also hide cancellations
    # during real request handling.
    if event.get("logger") == "uvicorn.error":
        msg = (event.get("logentry") or {}).get("message") or event.get("message", "")
        if "CancelledError" in msg and "in lifespan" in msg:
            return None

    # Drop client-disconnect noise: ClosedResourceError (anyio) and
    # ClientDisconnect (starlette), raised when a client closes the tab or the
    # connection drops mid-stream. Expected in an SSE server; no DB or Redis
    # client in this codebase produces either from a real error.
    #
    # Walks the exception TREE rather than reading exc_info[0].__name__. The old
    # check did the latter and is blind to an ExceptionGroup, which is the shape
    # these actually arrive in: MCP-SERVER-5M carries
    # `mechanism.is_exception_group: true` with ClientDisconnect as a child, so
    # a name check on the outermost type never matches. `ignore_errors` has the
    # identical blind spot, which is why this is here and not in that list.
    exc_info = hint.get("exc_info") if hint else None
    raised = exc_info[1] if exc_info and len(exc_info) > 1 else None
    if raised is not None:
        if is_pure_noise(raised):
            return None

        # A client calling a tool this server does not expose is the caller's
        # fault, not an outage. Dropped here rather than demoted because
        # FastMCP raises it inside its own middleware, upstream of every
        # decorator that could have lowered a log level. The signal is not lost:
        # the call still fails for the caller and FastMCP still logs it.
        if is_unknown_tool_call(raised):
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
