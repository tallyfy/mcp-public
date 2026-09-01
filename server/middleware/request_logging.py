"""
Request logging middleware for MCP Server

Provides detailed request/response logging with:
- MCP protocol inspection (method, tool name)
- Session tracking and visual grouping
- User/org context extraction from JWT
- Sentry transaction tracking
- Color-coded terminal output
- A durable off-box record of every failure (#890)

The last one is why this file is the wiring point rather than somewhere tidier.
This middleware already computes the whole answer to "what failed, for whom" -
org, user, tool name, HTTP status, the MCP-level isError flag, the error text and
the duration - and then writes it only to stdout, which a deploy destroys.
Shipping the values it already has costs one call; recomputing them anywhere
else would mean re-parsing the request body and re-peeking the response.
"""

import json
import logging
import os
import time

import sentry_sdk
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response, StreamingResponse

import metrics
from utils.org_id_middleware import ORG_ID_HEADERS, get_org_id
from constants import MCP_SESSION_TIMEOUT
from durable_event_log import (
    EVENT_AUTH_FAILURE,
    EVENT_TOOL_ERROR,
    EVENT_UPSTREAM_REJECTED,
    ORG_SOURCE_HEADER,
    ORG_SOURCE_NONE_NO_BEARER,
    ORG_SOURCE_NONE_NO_ORG_CLAIM,
    ORG_SOURCE_NONE_UNPARSABLE_BEARER,
    ORG_SOURCE_SESSION_STORE,
    ORG_SOURCE_TOKEN_CLAIM,
    ORG_SOURCE_TOOL_ARGUMENT,
    SYNTHETIC_HEADER,
    SYNTHETIC_TRUTHY,
    TRANSPORT_DIRECT,
    TRANSPORT_NONE,
    TRANSPORT_OAUTH,
    TRANSPORT_UNKNOWN,
    get_event_log,
)

# The org_id header spellings OrgIdMiddleware accepts, as lowercase str (it
# holds them as bytes). Imported rather than re-listed so a spelling added there
# is automatically recognised here - the same reasoning utils/auth_context.py
# gives for its own copy of this line.
_ORG_ID_HEADER_NAMES = frozenset(name.decode("ascii") for name in ORG_ID_HEADERS)


# Maximum bytes the middleware will buffer to inspect a tools/call response
# for MCP-level errors (HTTP 200 + isError:true). Anything past this streams
# straight through. See issue #233.
TOOLS_CALL_INSPECT_BUFFER_BYTES = 65536


async def _peek_response_body(body_iterator, max_bytes: int):
    """Read up to ``max_bytes`` from an async chunk iterator.

    Returns ``(head, drained, overflow_chunk)`` where:
      - ``head`` is bytes accumulated up to (but never beyond) ``max_bytes``
      - ``drained`` is True iff the iterator finished within the cap
      - ``overflow_chunk`` is the *remainder* of the chunk that broke the cap
        (empty bytes if ``drained``); the caller is responsible for emitting it.

    Implements the bounded buffer described in issue #233 — the middleware must
    not materialize the full body in memory when the response exceeds the cap.
    """
    head = b""
    overflow = b""
    async for chunk in body_iterator:
        if len(head) + len(chunk) <= max_bytes:
            head += chunk
            continue
        capacity = max_bytes - len(head)
        if capacity > 0:
            head += chunk[:capacity]
        overflow = chunk[capacity:]
        return head, False, overflow
    return head, True, overflow


# ANSI color codes for terminal output
class Colors:
    """ANSI color codes for colored terminal output"""
    RESET = '\033[0m'
    BOLD = '\033[1m'

    # Status code colors
    GREEN = '\033[92m'   # 2xx success
    BLUE = '\033[94m'    # 3xx redirect
    YELLOW = '\033[93m'  # 4xx client error
    RED = '\033[91m'     # 5xx server error
    CYAN = '\033[96m'    # Info
    GRAY = '\033[90m'    # Muted

    @staticmethod
    def status_color(status_code: int) -> str:
        """Get color based on HTTP status code"""
        if 200 <= status_code < 300:
            return Colors.GREEN
        elif 300 <= status_code < 400:
            return Colors.BLUE
        elif 400 <= status_code < 500:
            return Colors.YELLOW
        else:
            return Colors.RED


# Session tracking for visual grouping of MCP requests
_mcp_sessions: dict[str, dict] = {}  # session_id -> {"user_id": str, "last_activity": float, "short_id": str}
_pending_sessions: dict[str, float] = {}  # user_id -> timestamp (for sessions just initialized)
_SESSION_TIMEOUT = MCP_SESSION_TIMEOUT

# Colors for different users (cycling through)
_USER_COLORS = [
    '\033[38;5;39m',   # Blue
    '\033[38;5;208m',  # Orange
    '\033[38;5;141m',  # Purple
    '\033[38;5;49m',   # Teal
    '\033[38;5;204m',  # Pink
    '\033[38;5;227m',  # Yellow
]


def _get_user_color(user_id: str) -> str:
    """Get a consistent color for a user based on their ID."""
    if not user_id:
        return Colors.GRAY
    # Simple hash to pick a color
    color_idx = hash(user_id) % len(_USER_COLORS)
    return _USER_COLORS[color_idx]


def _get_short_session_id(session_id: str) -> str:
    """Get a short 4-char identifier from session ID."""
    if not session_id:
        return "----"
    # Use last 4 chars as they're more likely to be unique
    return session_id[-4:] if len(session_id) >= 4 else session_id.ljust(4, '-')


def resolve_transport(auth_header, jwt_claims):
    """How the credential arrived: ``oauth``, ``direct``, ``none`` or ``unknown``.

    Derived by the SERVER from what was presented (#996 AC2). It is deliberately
    not ``X-Client-Type``, which is free text the caller picks and which read
    ``direct`` on all 39 rows of the burst that started #996 while also reading
    ``issue890-verify`` on the two rows that gave that burst away.

    The discriminator is the ``mcp_scopes`` claim, and it is the one this
    codebase already relies on: ``McpAccessTokenService::issue`` mints it on
    every OAuth-brokered MCP token, and a raw Tallyfy session token forwarded by
    chat.tallyfy.com or the desktop AI shell carries no such claim
    (``utils.auth_context.get_mcp_scopes`` documents both halves).

    The claims come from OrgIdMiddleware's UNVERIFIED decode, which is correct
    here and would not be for an authorization decision: the question is what
    shape of credential the caller presented, not whether it was any good. On a
    401 the answer is still wanted, and by then there is nothing verified left
    to read.
    """
    if not auth_header or "bearer" not in auth_header.lower():
        return TRANSPORT_NONE
    if not jwt_claims:
        # A bearer arrived and did not decode as a JWT, or the path skipped
        # OrgIdMiddleware entirely. Either way this row cannot say.
        return TRANSPORT_UNKNOWN
    return TRANSPORT_OAUTH if "mcp_scopes" in jwt_claims else TRANSPORT_DIRECT


def resolve_claimed_org(*, resolved_org, resolved_source, jwt_claims, has_auth):
    """The organization the caller NAMED, and where the name came from (#996 AC1).

    Returns ``(claimed_org_id, org_id_source)``. Never asserts that anything was
    verified: on an auth failure nothing was, which is why the shipper blanks the
    ``org_id`` column for that event and keeps only this pair.

    When no organization can be named at all the source is one of the ``none_*``
    reasons, because "953 of 995 rows name nobody" is only actionable once the
    rows say which kind of nobody. Three kinds exist and they want different
    fixes: no credential was sent, one was sent and was not a JWT, or one was
    sent and simply named no organization.
    """
    org = (resolved_org or "").strip()
    if org and org != "unknown":
        return org, resolved_source
    claim = (jwt_claims or {}).get("org_id")
    if claim:
        return str(claim), ORG_SOURCE_TOKEN_CLAIM
    if not has_auth:
        return None, ORG_SOURCE_NONE_NO_BEARER
    if not jwt_claims:
        return None, ORG_SOURCE_NONE_UNPARSABLE_BEARER
    return None, ORG_SOURCE_NONE_NO_ORG_CLAIM


def request_is_synthetic(request):
    """True when the caller declared itself test traffic (#996 AC5).

    One dedicated header, checked for an explicit truthy value. Absent means
    real, so an unlabelled harness is treated as a customer - the direction that
    gets investigated rather than the direction that gets ignored.
    """
    try:
        value = request.headers.get(SYNTHETIC_HEADER, "")
    except Exception:
        return False
    return str(value).strip().lower() in SYNTHETIC_TRUTHY


def _org_source_from_context(request):
    """Whether an org in the ContextVar came from a header or the session store.

    ``OrgIdMiddleware`` writes both into one ContextVar and ``get_org_id()``
    cannot tell them apart, which ``utils/auth_context.py`` says in as many
    words. The request is still here though, so the header half is directly
    observable: a header present means the header is what set it, and its
    absence leaves the per-user store as the only remaining way the value got
    there.
    """
    try:
        for name in request.headers.keys():
            if name.lower() in _ORG_ID_HEADER_NAMES:
                return ORG_SOURCE_HEADER
    except Exception:
        pass
    return ORG_SOURCE_SESSION_STORE


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log HTTP requests with timing, status, and authentication info."""

    async def dispatch(self, request: StarletteRequest, call_next):
        start_time = time.time()

        # Extract request info
        method = request.method
        path = request.url.path

        # Extract auth info (without exposing full token)
        auth_header = request.headers.get("authorization", "")
        has_auth = "Bearer" in auth_header
        # Get org_id from context
        org_id = get_org_id() or 'unknown'
        # Which of the three possible origins produced the value in `org_id`.
        # Tracked at every assignment rather than reconstructed at the end,
        # because by then the three are indistinguishable.
        org_id_source = _org_source_from_context(request)

        # Extract MCP session ID for request grouping
        mcp_session_id = request.headers.get("mcp-session-id", "")

        # Read pre-decoded JWT claims from OrgIdMiddleware (P2-I — single decode per request)
        from utils.org_id_middleware import get_jwt_claims
        claims = get_jwt_claims()
        user_id = None
        if claims:
            user_id = claims.get('sub') or claims.get('user_id') or claims.get('uid')

        # Extract MCP protocol info for requests to the MCP transport paths.
        # The transport is served at '/' and aliased at '/mcp' + '/mcp/'
        # (see server.py), so all three must extract mcp_method / tool name.
        mcp_method = None
        mcp_tool_name = None
        is_mcp_root = path in ("/", "/mcp", "/mcp/") and method in ["POST", "GET"]

        if is_mcp_root and method == "POST":
            try:
                # Read and cache body for MCP protocol inspection
                body = await request.body()
                if body:
                    try:
                        json_body = json.loads(body)
                        mcp_method = json_body.get("method", "")
                        # Extract tool name and org_id for tools/call requests
                        if mcp_method == "tools/call":
                            params = json_body.get("params", {})
                            mcp_tool_name = params.get("name", "unknown")
                            # Extract org_id from tool arguments (more reliable than header for MCP)
                            arguments = params.get("arguments", {})
                            body_org_id = arguments.get("org_id", "")
                            if body_org_id:
                                org_id = body_org_id
                                org_id_source = ORG_SOURCE_TOOL_ARGUMENT
                    except json.JSONDecodeError:
                        pass
            except Exception:
                pass

        # Determine if this is a new session (for visual grouping)
        is_new_session = False
        is_known_session = False
        short_session_id = "----"

        if mcp_method == "initialize":
            # initialize always starts a new session
            is_new_session = True
            # Mark this user as having a pending session (for notifications/initialized)
            if user_id:
                _pending_sessions[user_id] = start_time
        elif mcp_session_id:
            short_session_id = _get_short_session_id(mcp_session_id)

            # Check if we've seen this session before
            if mcp_session_id in _mcp_sessions:
                is_known_session = True
            else:
                # New session ID - check if it's from a user with a pending session
                # (this handles notifications/initialized right after initialize)
                if user_id and user_id in _pending_sessions:
                    # This is continuation after initialize, not a new session
                    is_known_session = True
                    del _pending_sessions[user_id]
                else:
                    is_new_session = True

            # Update session tracking
            _mcp_sessions[mcp_session_id] = {
                "user_id": user_id,
                "last_activity": start_time,
                "short_id": short_session_id
            }

            # Cap session dict size to prevent unbounded memory growth
            MAX_SESSIONS = 1000
            if len(_mcp_sessions) > MAX_SESSIONS:
                # Evict oldest sessions
                sorted_sessions = sorted(
                    _mcp_sessions.items(),
                    key=lambda x: x[1].get('last_activity', 0)
                )
                for sid, _ in sorted_sessions[:len(_mcp_sessions) - MAX_SESSIONS]:
                    del _mcp_sessions[sid]

            # Clean up stale sessions and pending sessions
            stale_threshold = start_time - _SESSION_TIMEOUT
            stale_sessions = [sid for sid, info in _mcp_sessions.items()
                           if info["last_activity"] < stale_threshold]
            for sid in stale_sessions:
                del _mcp_sessions[sid]

            # Clean up old pending sessions (> 5 seconds)
            old_pending = [uid for uid, t in _pending_sessions.items()
                         if start_time - t > 5.0]
            for uid in old_pending:
                del _pending_sessions[uid]

        # Track active connection
        metrics.increment_active_connections()

        # For tools/call requests, log the entry BEFORE processing so it appears before tool execution logs
        tools_call_logged = False
        tools_call_prefix = ""
        if mcp_method == "tools/call" and mcp_tool_name:
            tools_call_logged = True
            user_color = _get_user_color(user_id) if user_id else Colors.GRAY
            if is_new_session:
                session_tag = f"{user_color}[new ]{Colors.RESET}"
                tools_call_prefix = f"{Colors.CYAN}┌─{Colors.RESET}{session_tag}"
            elif is_known_session:
                session_tag = f"{user_color}[{short_session_id}]{Colors.RESET}"
                tools_call_prefix = f"{Colors.GRAY}│ {Colors.RESET}{session_tag}"
            else:
                tools_call_prefix = "        "
            display_path = f"mcp:tools/call({mcp_tool_name})"
            context_parts = []
            if user_id:
                context_parts.append(f"user={user_id}")
            if org_id:
                context_parts.append(f"org={org_id}")
            context_str = " │ ".join(context_parts) if context_parts else ""
            logging.info(f"{tools_call_prefix} {Colors.CYAN}{method:6}{Colors.RESET} {display_path:40} │ {context_str}")

        # Skip Sentry transaction for /metrics endpoint (high frequency monitoring traffic)
        should_trace = path != "/metrics"
        transaction = None

        try:
            # Start Sentry transaction for performance monitoring (skip /metrics)
            if should_trace:
                transaction = sentry_sdk.start_transaction(
                    op="http.server",
                    name=f"{method} {path}"
                )
                transaction.set_tag("method", method)
                transaction.set_tag("path", path)
                transaction.set_tag("has_auth", has_auth)
                if org_id:
                    # Only set first 8 chars for privacy
                    transaction.set_tag("org_id", org_id)
                if mcp_method:
                    transaction.set_tag("mcp_method", mcp_method)
                if mcp_tool_name:
                    transaction.set_tag("mcp_tool", mcp_tool_name)
                transaction.__enter__()

            # Extract X-Client-Type header for Sentry tagging
            client_type = request.headers.get("x-client-type", "direct")
            sentry_sdk.set_tag("client_type", client_type)

            try:
                # Process request
                response = await call_next(request)
                status = response.status_code

                # For tools/call requests, inspect response body for MCP-level errors
                # MCP returns HTTP 200 but includes isError:true in the body for validation failures
                mcp_error = False
                mcp_error_msg = None
                if mcp_method == "tools/call" and status == 200:
                    # Inspect response body for MCP-level errors (HTTP 200 + isError:true).
                    # Memory cap (issue #233): peek up to TOOLS_CALL_INSPECT_BUFFER_BYTES
                    # for inspection. If the body fits, re-emit via Response; otherwise
                    # switch to StreamingResponse so further chunks bypass middleware memory.
                    head_buf, body_drained, overflow_first_chunk = await _peek_response_body(
                        response.body_iterator, TOOLS_CALL_INSPECT_BUFFER_BYTES
                    )

                    # Only attempt error detection on small responses —
                    # large responses (>64KB) are almost certainly successful tool results
                    if body_drained and head_buf:
                        try:
                            # Parse SSE format - extract JSON from "data: " line
                            # Response is in SSE format: "event: message\r\ndata: {json}\r\n\r\n"
                            response_text = head_buf.decode('utf-8')
                            json_data = None

                            for line in response_text.split('\n'):
                                line = line.strip()
                                if line.startswith('data: '):
                                    json_data = line[6:]  # Remove "data: " prefix
                                    break

                            if json_data is None:
                                # NOT SSE. server.py:258 builds the app with
                                # json_response=True, so FastMCP answers every
                                # tools/call with content-type application/json
                                # and no framing. Parsing only `data: ` lines
                                # therefore detected NOTHING on any deployment
                                # of this server: mcp_error could never be True,
                                # and an HTTP 200 carrying isError:true - the
                                # #652 shape, the one #890 was filed about - was
                                # invisible to every consumer of this flag,
                                # including the durable record.
                                #
                                # Measured on live staging 2026-08-22: a failing
                                # tools/call returned HTTP/2 200,
                                # content-type: application/json, isError true,
                                # and this middleware logged it with no error
                                # marker at all.
                                #
                                # The SSE branch is kept because json_response
                                # is a server.py argument that can be changed
                                # back, and a parser that understands only the
                                # framing in use today is how this bug happened.
                                json_data = response_text.strip()

                            if json_data:
                                response_json = json.loads(json_data)

                                # Check for JSON-RPC level error first
                                if "error" in response_json:
                                    mcp_error = True
                                    error_obj = response_json["error"]
                                    mcp_error_msg = error_obj.get("message", str(error_obj))[:100]
                                else:
                                    # Check for tool error in result
                                    result = response_json.get("result", {})
                                    if isinstance(result, dict) and result.get("isError") is True:
                                        mcp_error = True
                                        # Extract error message from content
                                        content = result.get("content", [])
                                        if content and isinstance(content, list):
                                            for item in content:
                                                if isinstance(item, dict) and item.get("type") == "text":
                                                    mcp_error_msg = item.get("text", "")[:100]
                                                    break
                        except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as e:
                            logging.debug(f"Failed to parse tools/call response: {e}")

                    if body_drained:
                        # Whole body is in head_buf — safe to re-emit as a single Response
                        response = Response(
                            content=head_buf,
                            status_code=response.status_code,
                            headers=dict(response.headers),
                            media_type=response.media_type
                        )
                    else:
                        # Body exceeded cap — stream the head + overflow + remainder
                        # without holding the full body in middleware memory.
                        original_iterator = response.body_iterator

                        async def _passthrough(head=head_buf, overflow=overflow_first_chunk,
                                               tail=original_iterator):
                            yield head
                            if overflow:
                                yield overflow
                            async for chunk in tail:
                                yield chunk

                        # Drop content-length: we no longer know the byte total here.
                        passthrough_headers = {
                            k: v for k, v in response.headers.items()
                            if k.lower() != "content-length"
                        }
                        response = StreamingResponse(
                            _passthrough(),
                            status_code=response.status_code,
                            headers=passthrough_headers,
                            media_type=response.media_type,
                        )

                # Calculate duration AFTER body is fully read (includes tool execution time)
                duration = time.time() - start_time

                # Get org_id from context after OrgIdMiddleware has set it
                org_id_from_context = get_org_id()
                if org_id_from_context:
                    org_id = org_id_from_context
                    org_id_source = _org_source_from_context(request)
                # Record transaction data
                if should_trace and transaction:
                    transaction.set_data("duration_ms", duration * 1000)
                    transaction.set_data("status_code", status)
                    if mcp_error:
                        transaction.set_data("mcp_error", True)
            except Exception:
                duration = time.time() - start_time
                # Exception propagates to framework — LoggingIntegration captures it
                raise
        finally:
            # Close Sentry transaction if it was created
            if should_trace and transaction:
                transaction.__exit__(None, None, None)

            # Decrement active connection
            metrics.decrement_active_connections()

        # Known scanner/bot paths - demote to DEBUG level when unauthenticated
        _SCANNER_PATTERNS = {
            '.env', '.git', 'wp-login', 'wp-admin', 'xmlrpc.php',
            'wlwmanifest', '.php', 'wp-includes', 'wp-content',
            'cgi-bin', '.asp', '.aspx', 'phpmyadmin', 'adminer',
        }
        is_scanner = (
            not has_auth
            and org_id == 'unknown'
            and any(pattern in path.lower() for pattern in _SCANNER_PATTERNS)
        )

        # Filter out noise:
        # 1. /sse endpoint 404s (expected - SSE not implemented)
        # 2. DELETE 400s (expired session cleanup - normal behavior)
        # 3. /metrics endpoint 200s (successful Prometheus scrapes - high frequency)
        # 4. Scanner/bot probes from unauthenticated sources
        # 5. OAuth discovery probes (POST / → 401 without MCP body — RFC 9728 flow)
        is_sse_404 = path == "/sse" and status == 404
        is_delete_400 = method == "DELETE" and status == 400
        is_metrics_200 = path == "/metrics" and status == 200
        is_health_200 = path == "/health" and status == 200
        is_favicon = path == "/favicon.ico"
        is_oauth_discovery = (
            is_mcp_root and status == 401 and not mcp_method
        )

        if is_scanner:
            logging.debug(f"{Colors.GRAY}[scanner] {method:6} {path:40} │ {status:>3} │ {duration*1000:6.1f}ms{Colors.RESET}")
        elif is_oauth_discovery:
            # RFC 9728: client probes resource to trigger WWW-Authenticate discovery — expected noise
            logging.debug(f"{Colors.GRAY}[oauth-discovery] {method:6} {path:40} │ {status:>3} │ {duration*1000:6.1f}ms{Colors.RESET}")
        elif is_sse_404:
            # Completely suppress /sse 404s - this is expected noise
            pass
        elif is_delete_400:
            # Log DELETE 400s at debug level only
            logging.debug(f"{Colors.GRAY}{method:6} {path:20} │ {status} │ {duration*1000:6.1f}ms │ session_cleanup{Colors.RESET}")
        elif is_metrics_200:
            # Completely suppress successful /metrics requests - high frequency monitoring traffic
            pass
        elif is_health_200:
            pass
        elif is_favicon:
            pass
        else:
            # Build context parts for display
            context_parts = []

            if user_id:
                context_parts.append(f"user={user_id}")

            if org_id:
                context_parts.append(f"org={org_id}")
            elif has_auth:
                context_parts.append("authenticated")
            else:
                context_parts.append("no-auth")

            context_str = " │ ".join(context_parts) if context_parts else "no-context"

            # Color-code status - use RED for MCP errors even if HTTP 200
            if mcp_error:
                color = Colors.RED
                display_status = "ERR"
            else:
                color = Colors.status_color(status)
                display_status = str(status)

            # Determine display path - show MCP method for root requests
            if is_mcp_root and mcp_method:
                if mcp_tool_name:
                    display_path = f"mcp:{mcp_method}({mcp_tool_name})"
                else:
                    display_path = f"mcp:{mcp_method}"
            else:
                display_path = path

            # Get user color for visual distinction between different users
            user_color = _get_user_color(user_id) if user_id else Colors.GRAY

            # Visual grouping prefix for MCP requests with session tag
            if is_new_session:
                # New session starts with a header line
                session_tag = f"{user_color}[new ]{Colors.RESET}"
                prefix = f"{Colors.CYAN}┌─{Colors.RESET}{session_tag}"
            elif is_known_session:
                # Continuation of existing session - show short session ID
                session_tag = f"{user_color}[{short_session_id}]{Colors.RESET}"
                prefix = f"{Colors.GRAY}│ {Colors.RESET}{session_tag}"
            else:
                # Non-MCP request or unknown context
                prefix = "        "  # 8 spaces to align with session tags

            # Build the log message
            log_msg = (
                f"{prefix} {color}{method:6}{Colors.RESET} {display_path:40} │ "
                f"{color}{display_status:>3}{Colors.RESET} │ "
                f"{duration*1000:6.1f}ms │ {context_str}"
            )

            # Add MCP error message if present
            if mcp_error and mcp_error_msg:
                log_msg += f"\n{Colors.GRAY}│ {Colors.RESET}        {Colors.RED}└─ {mcp_error_msg}{Colors.RESET}"

            # Use warning level for MCP errors (error-level Sentry events come from
            # @handle_tallyfy_errors — the single source of truth for tool errors)
            if mcp_error:
                logging.warning(log_msg)
            elif tools_call_logged:
                # For tools/call, log a brief completion line with status and duration
                # Use │ for completion line (not ┌─) since this is a continuation
                if is_new_session:
                    completion_prefix = f"{Colors.GRAY}│ {Colors.RESET}{user_color}[new ]{Colors.RESET}"
                elif is_known_session:
                    completion_prefix = f"{Colors.GRAY}│ {Colors.RESET}{user_color}[{short_session_id}]{Colors.RESET}"
                else:
                    completion_prefix = "        "
                logging.info(
                    f"{completion_prefix} {color}{method:6}{Colors.RESET} {'└─ completed':40} │ "
                    f"{color}{display_status:>3}{Colors.RESET} │ "
                    f"{duration*1000:6.1f}ms"
                )
            else:
                logging.info(log_msg)

        # Durable record (#890). Everything above this line goes to stdout and
        # dies with the container; this call is the only thing that outlives a
        # deploy. It is deliberately the LAST statement before the response is
        # returned, so it can never change what the caller receives.
        claimed_org_id, claimed_org_source = resolve_claimed_org(
            resolved_org=org_id,
            resolved_source=org_id_source,
            jwt_claims=claims,
            has_auth=has_auth,
        )
        self._record_failure_durably(
            mcp_error=mcp_error,
            mcp_error_msg=mcp_error_msg,
            status=status,
            org_id=org_id,
            user_id=user_id,
            tool_name=mcp_tool_name,
            mcp_method=mcp_method,
            duration=duration,
            session_id=mcp_session_id,
            client_type=client_type,
            claimed_org_id=claimed_org_id,
            org_id_source=claimed_org_source,
            transport=resolve_transport(auth_header, claims),
            synthetic=request_is_synthetic(request),
            is_noise=(
                is_scanner
                or is_oauth_discovery
                or is_sse_404
                or is_delete_400
                or is_favicon
            ),
        )

        return response

    @staticmethod
    def _record_failure_durably(
        *,
        mcp_error: bool,
        mcp_error_msg,
        status: int,
        org_id,
        user_id,
        tool_name,
        mcp_method,
        duration: float,
        session_id,
        client_type,
        claimed_org_id=None,
        org_id_source=None,
        transport=None,
        synthetic: bool = False,
        is_noise: bool,
    ) -> None:
        """Ship one failure to the off-box record. Never raises, never blocks.

        Only FAILURES travel. A successful tool call is already counted by
        ``mcp_server_requests_total``, answers no incident question, and would
        multiply the volume landing in a table shared with the whole estate.

        ``mcp_error`` is checked BEFORE ``status``, and that order is the point
        of the whole change: a tool rejected by api-v2 comes back as HTTP 200
        with ``isError:true`` inside the result (#652), so a status-code-only
        rule records nothing for the exact failure mode that prompted #890.

        ``org_id`` is what the request resolved and ``claimed_org_id`` is what
        the caller named. On an auth failure the shipper drops the first and
        keeps the second, because a 401 verified nothing (#996). That demotion
        deliberately does NOT happen here: putting it in the shipper means the
        next call site inherits it without knowing the rule exists.
        """
        try:
            if is_noise:
                # Scanner probes, RFC-9728 discovery 401s, /sse 404s and expired
                # session DELETEs. Each is expected, high-volume, and answers
                # nothing - the same set the display logic already suppresses.
                return
            if mcp_error:
                event = EVENT_TOOL_ERROR
            elif status in (401, 403):
                event = EVENT_AUTH_FAILURE
            elif status >= 400:
                event = EVENT_UPSTREAM_REJECTED
            else:
                return

            get_event_log().emit(
                event,
                org_id=org_id,
                user_id=user_id,
                tool_name=tool_name,
                mcp_method=mcp_method,
                status_code=status,
                duration_ms=int(duration * 1000),
                error_message=mcp_error_msg,
                session_id=session_id,
                client_type=client_type,
                claimed_org_id=claimed_org_id,
                org_id_source=org_id_source,
                transport=transport,
                synthetic=synthetic,
            )
        except Exception as exc:  # pragma: no cover - emit already swallows
            logging.debug("event_log wiring failed (%s)", type(exc).__name__)
