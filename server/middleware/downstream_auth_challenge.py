"""
Turn a downstream Tallyfy 401 into a transport-level 401 challenge.

WHY THIS EXISTS
---------------
A tool call that fails because api-v2 rejected the caller's access token used
to reach the client as **HTTP 200 with ``isError: true``** inside the JSON-RPC
result. That is the only shape FastMCP can produce from inside a tool: every
exception raised by a tool, ``ToolError`` included, is caught by the MCP SDK's
``call_tool`` handler and converted into ``CallToolResult(isError=True)``
(``mcp/server/lowlevel/server.py``, ``except Exception as e: return
self._make_error_result(str(e))``). Only ``UrlElicitationRequiredError``
escapes, and even that becomes a JSON-RPC error, still carried on a 200.

MCP clients re-run their OAuth flow on a transport-level **401**, and on
nothing else. The MCP authorization spec (2025-06-18) is explicit:

    "Invalid or expired tokens MUST receive a HTTP 401 response."
    "MCP servers MUST use the HTTP header WWW-Authenticate when returning a
     401 Unauthorized to indicate the location of the resource server metadata
     URL as described in RFC 9728 Section 5.1."
    "MCP clients MUST be able to parse WWW-Authenticate headers and respond
     appropriately to HTTP 401 Unauthorized responses from the MCP server."

The reference client implements exactly that per request, not only on the
handshake: ``mcp/client/auth/oauth2.py`` reacts to ``response.status_code ==
401`` by reading ``resource_metadata`` off ``WWW-Authenticate``, running the
whole OAuth flow, and re-yielding the original request once with the new
token. So a 200 told the client the call had succeeded, nothing ever
re-authenticated, and the connector sat there connected and useless until a
human reconnected it by hand. Measured on a live customer: two 401s from
api-v2 five seconds apart, then no further calls that day (issue #652).

HOW THE SIGNAL CROSSES THE LAYERS
---------------------------------
Only the ASGI layer owns the HTTP status, and by the time a tool runs the
stack is several frames below it. The signal therefore travels on the ASGI
``scope`` dict, which is one object shared by reference from the outermost
middleware down to the Starlette ``Request`` a tool reaches through
``fastmcp.server.dependencies.get_http_request()``: the streamable-http
transport passes the live request through as
``ServerMessageMetadata(request_context=request)`` and the low-level server
publishes it as ``RequestContext.request``.

The timing works because the server runs ``json_response=True``
(``server/server.py``), so the transport blocks on the JSON-RPC reply and
sends nothing until the tool has finished. Verified end to end rather than
reasoned about: see ``tests/integration/test_downstream_auth_challenge.py``.

FAIL-SAFE BY CONSTRUCTION
-------------------------
Every way this can fail leaves today's behaviour in place rather than breaking
a working call. If the flag never arrives (SSE streaming mode, a Docket
background task, stdio, a direct in-process call) nothing is rewritten and the
client still receives the descriptive ``isError`` result, because
``handle_tallyfy_errors`` raises its ``ToolError`` either way.

SCOPED TO 401, DELIBERATELY
---------------------------
A 403 is not included even when its message reads like auth. api-v2 answers
403 for business rules too, and re-authenticating cannot fix those, so
challenging on a 403 would send a client round the OAuth loop over a correct
rejection it can do nothing about. That is #592 arriving through a new door.

AND NOT EVERY 401 EITHER (#652, second pass)
--------------------------------------------
Some 401s from api-v2 are about the OBJECT, not the credential: a guest thread
that is not yours, an explicit-user-access middleware, an organization without
SAML. Re-authenticating cannot fix one of those, so challenging on it is #592
one status code down. ``utils.fastmcp_errors.is_credential_401`` decides, on a
DENYLIST: unknown means challenge, because an allowlist would silently stop
challenging the day api-v2 adds a message nobody has read, and that failure is
invisible -- the connector simply goes quiet.

THE CIRCUIT BREAKER, WHICH IS WHAT MAKES THE DENYLIST AFFORDABLE
---------------------------------------------------------------
A denylist can over-challenge. Over-challenging is a loop: the client
re-authenticates, gets a token api-v2 refuses for the same reason, calls again,
is challenged again. A real customer registered TWELVE OAuth clients this way
over one week.

So a session gets N challenges (default 3) and then stops being challenged
until a call actually succeeds with the credential presented upstream. One
challenge is the ordinary expired-token case; two covers the benign race where
two calls are in flight when the token expires; a fourth consecutive challenge
with no successful upstream call in between is, by construction, a loop.

🔴 A SUPPRESSED CALL STAYS HTTP 200. It must never answer 401.
``AuthErrorMiddleware`` turns EVERY 401 on a transport path into an OAuth
challenge, so emitting one here would re-arm the exact loop being broken. The
explanation travels in the tool's own error text instead.

🔴 "A SUCCESSFUL CALL IN BETWEEN" MEANS A REQUEST THAT PRESENTED THE CREDENTIAL
UPSTREAM. This is the single thing most likely to be built wrong. ``initialize``
and ``tools/list`` never touch api-v2 and therefore ALWAYS succeed on a dead
credential; resetting on those defeats the breaker completely while every other
assertion in the suite still passes. The reset is gated on
``CREDENTIAL_PRESENTED_SCOPE_KEY``, which only
``utils.auth_context.get_authenticated_credentials`` sets, and additionally on
this request having seen no downstream auth failure at all.
"""

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, MutableMapping, Optional

logger = logging.getLogger(__name__)


# The MCP streamable-http transport, served at '/' and aliased at '/mcp' and
# '/mcp/' (see server.py). Deliberately the same tuple AuthErrorMiddleware
# uses: a tool can only run on one of these paths, so a flag on any other path
# means something has gone wrong and the response is left alone.
MCP_TRANSPORT_PATHS = ("/", "/mcp", "/mcp/")

# Key this middleware reads off the ASGI scope. Namespaced because the scope is
# shared with Starlette, FastMCP and the MCP SDK. The setter lives in this file
# with its only consumer so the two cannot drift apart, the same reasoning
# org_id_middleware.py gives for keeping PROTECTED_RESOURCE_METADATA_PATH
# beside the challenge that serves it.
DOWNSTREAM_AUTH_FAILURE_SCOPE_KEY = "tallyfy.downstream_auth_failure"

#: Set whenever a downstream 401 was OBSERVED, whether or not it was challenged.
#: The reset below keys off its ABSENCE, so a suppressed challenge cannot reset
#: the counter that suppressed it -- which would restart the loop on every other
#: call and make the breaker look like it works while doing nothing.
DOWNSTREAM_AUTH_SEEN_SCOPE_KEY = "tallyfy.downstream_auth_seen"

#: Set by ``utils.auth_context.get_authenticated_credentials`` and by nothing
#: else. Its presence is what separates "a call reached api-v2 with this
#: credential" from "a protocol message the MCP server answered by itself".
CREDENTIAL_PRESENTED_SCOPE_KEY = "tallyfy.credential_presented"

# RFC 6750 error code. invalid_token is what every MCP client keys its OAuth
# retry off, and it is the honest reading of an upstream "Unauthenticated.":
# the token presented is expired, revoked, or no longer accepted.
CHALLENGE_ERROR = "invalid_token"

_MAX_DESCRIPTION_CHARS = 300

# ---------------------------------------------------------------------------
# Challenge decisions. A CLOSED vocabulary: it is a Prometheus label, and it is
# also what handle_tallyfy_errors branches on, so a stray string would be both a
# new series and an unhandled branch.
# ---------------------------------------------------------------------------
CHALLENGE_ISSUED_KNOWN = "issued_known_credential"
CHALLENGE_ISSUED_UNCLASSIFIED = "issued_unclassified"
CHALLENGE_SUPPRESSED_NOT_CREDENTIAL = "suppressed_not_credential"
CHALLENGE_SUPPRESSED_CIRCUIT_OPEN = "suppressed_circuit_open"
CHALLENGE_NO_REQUEST = "no_request"

CHALLENGE_DECISIONS = (
    CHALLENGE_ISSUED_KNOWN,
    CHALLENGE_ISSUED_UNCLASSIFIED,
    CHALLENGE_SUPPRESSED_NOT_CREDENTIAL,
    CHALLENGE_SUPPRESSED_CIRCUIT_OPEN,
    CHALLENGE_NO_REQUEST,
)

#: Decisions on which a 401 challenge is actually written to the scope.
CHALLENGE_ISSUED_DECISIONS = frozenset({
    CHALLENGE_ISSUED_KNOWN,
    CHALLENGE_ISSUED_UNCLASSIFIED,
})

# ---------------------------------------------------------------------------
# Per-session challenge budget
# ---------------------------------------------------------------------------

#: Consecutive challenges allowed on one MCP session with no successful
#: credential-presenting call in between. One is the ordinary expired-token
#: case. Two covers the benign race where two calls are already in flight when
#: the token expires. A FOURTH is a loop.
DEFAULT_MAX_CHALLENGES_PER_SESSION = 3

#: Env override. ``0`` disables suppression entirely (every 401 is challenged).
MAX_CHALLENGES_ENV = "MCP_AUTH_CHALLENGE_MAX_PER_SESSION"

#: Same LRU + TTL shape as ``tallyfy_auth_provider._user_org_ids`` and
#: ``request_logging._mcp_sessions``. This process serves every MCP user, so an
#: unbounded dict keyed on session identity is a memory leak with a slow fuse.
_MAX_TRACKED_SESSIONS = 1000

#: One access-token lifetime (api-v2 ``config/mcp.php``), deliberately: a
#: session quiet for longer than its credential could have lived has nothing
#: left to say about that credential.
_SESSION_TTL_SECONDS = 3600.0

_challenge_counts: "OrderedDict[str, tuple]" = OrderedDict()  # session_id -> (count, last_seen)
_challenge_lock = threading.Lock()


def _max_challenges_per_session() -> int:
    """Read the budget fresh on every call.

    Deliberately not a module constant, matching ``downstream_token._mode()``:
    a constant is captured at import, which makes the value untestable without
    a module reload and a runtime flip impossible.

    ⚠️ Empty string, negative and unparseable ALL read as the default, not as
    zero. ``os.getenv(name, default)`` returns ``""`` for a variable that is set
    but empty, so an env line reading ``MCP_AUTH_CHALLENGE_MAX_PER_SESSION=""``
    looks exactly like documentation of the default while silently disabling the
    breaker. This repo has been bitten by that idiom twice already
    (``ENFORCE_JWT_AUDIENCE``, ``MCP_TOOL_SCOPE_ENFORCEMENT``).
    """
    raw = (os.getenv(MAX_CHALLENGES_ENV) or "").strip()
    if not raw:
        return DEFAULT_MAX_CHALLENGES_PER_SESSION
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; using the default of %d",
            MAX_CHALLENGES_ENV, raw, DEFAULT_MAX_CHALLENGES_PER_SESSION,
        )
        return DEFAULT_MAX_CHALLENGES_PER_SESSION
    if value < 0:
        logger.warning(
            "%s=%r is negative; using the default of %d",
            MAX_CHALLENGES_ENV, raw, DEFAULT_MAX_CHALLENGES_PER_SESSION,
        )
        return DEFAULT_MAX_CHALLENGES_PER_SESSION
    return value


def _prune_locked(now: float) -> None:
    """Drop expired entries, then evict oldest until under the cap.

    Caller holds ``_challenge_lock``.
    """
    for session_id in [
        sid for sid, (_, seen) in _challenge_counts.items()
        if now - seen > _SESSION_TTL_SECONDS
    ]:
        _challenge_counts.pop(session_id, None)

    while len(_challenge_counts) > _MAX_TRACKED_SESSIONS:
        _challenge_counts.popitem(last=False)


def _consume_challenge_budget(session_id: str) -> bool:
    """Charge one challenge to this session. False means the circuit is open."""
    limit = _max_challenges_per_session()
    if limit <= 0:
        return True

    now = time.time()
    with _challenge_lock:
        _prune_locked(now)
        count, _ = _challenge_counts.get(session_id, (0, now))
        if count >= limit:
            # Refresh last_seen so an actively-looping session is not evicted
            # by TTL and handed a fresh budget it has not earned.
            _challenge_counts[session_id] = (count, now)
            _challenge_counts.move_to_end(session_id)
            return False
        _challenge_counts[session_id] = (count + 1, now)
        _challenge_counts.move_to_end(session_id)
        _prune_locked(now)
        return True


def note_upstream_success(session_id: Optional[str]) -> None:
    """Clear a session's challenge count after a real upstream success.

    Called from the middleware, never from a tool. See the module docstring for
    why "success" is narrower than "HTTP 2xx".
    """
    if not session_id:
        return
    with _challenge_lock:
        _challenge_counts.pop(session_id, None)


def reset_challenge_state() -> None:
    """Drop all tracked sessions. For tests only."""
    with _challenge_lock:
        _challenge_counts.clear()


def _session_id_from_scope(scope: MutableMapping[str, Any]) -> Optional[str]:
    """The ``mcp-session-id`` request header off a raw ASGI scope."""
    try:
        for name, value in scope.get("headers") or []:
            if name.lower() == b"mcp-session-id":
                return value.decode("latin-1") or None
    except Exception:  # pragma: no cover - defensive
        return None
    return None


def note_credential_presented() -> None:
    """Record that this request resolved a credential to present upstream.

    Called once from ``utils.auth_context.get_authenticated_credentials``, which
    is the single chokepoint every tool's credential passes through.

    Never raises: an authenticated request must not fail because a bookkeeping
    flag could not be written.
    """
    try:
        from fastmcp.server.dependencies import get_http_request

        get_http_request().scope[CREDENTIAL_PRESENTED_SCOPE_KEY] = True
    except Exception as exc:  # pragma: no cover - stdio / in-process / unit test
        logger.debug("No HTTP request to mark credential-presented on: %s", exc)


def _clean_for_header(text: str) -> str:
    """Make an upstream message safe to place in a header value.

    ``_extract_api_message`` has already stripped leaked internals. This is the
    narrower transport concern: a CR or LF reaching a header value is response
    splitting, and an unbounded upstream string is a header nobody can parse.
    """
    collapsed = " ".join(str(text).split())
    if len(collapsed) > _MAX_DESCRIPTION_CHARS:
        collapsed = collapsed[:_MAX_DESCRIPTION_CHARS].rstrip() + "..."
    return collapsed


def build_challenge_description(api_message: str) -> str:
    """The error_description a client and its user will read on the 401."""
    detail = _clean_for_header(api_message)
    if detail:
        return (
            f"The Tallyfy API rejected this access token ({detail}). "
            "Re-authenticate the MCP connector and retry."
        )
    return (
        "The Tallyfy API rejected this access token. "
        "Re-authenticate the MCP connector and retry."
    )


def circuit_open_notice(session_id: Optional[str]) -> str:
    """The parenthetical a suppressed 401 carries in its ToolError text.

    This is the ONLY channel a suppressed challenge has. The response stays
    HTTP 200 on purpose (see the module docstring), so if this text does not
    say what to do, nothing does.

    It names the action that actually works. A token refresh cannot help: the
    customer whose incident produced this had refreshed repeatedly and
    registered twelve OAuth clients. Removing and re-adding the connector is
    what performs a fresh registration.
    """
    from durable_event_log import session_ref

    ref = session_ref(session_id)
    limit = _max_challenges_per_session()
    return (
        f"(The Tallyfy API has rejected this connector's access token {limit} "
        "times in a row on this MCP session, and re-authenticating did not "
        "change the answer, so this server has stopped asking your client to "
        "re-authenticate. Retrying will not help. Remove the Tallyfy connector "
        "in your MCP client and add it again -- that performs a new OAuth "
        "client registration, which a token refresh cannot do. If it still "
        f"fails, contact support@tallyfy.com and quote session reference {ref}.)"
    )


def flag_downstream_auth_failure(
    api_message: str = "",
    credential_failure: bool = True,
    known_message: bool = False,
) -> str:
    """Decide what to do about a downstream 401, and record the decision.

    Called from ``utils.fastmcp_errors.handle_tallyfy_errors``. Writes the
    challenge flag ONLY when a challenge is actually to be issued.

    ⚠️ Returns one of ``CHALLENGE_DECISIONS`` -- a string, not a bool. It used
    to return True/False for "was the flag written", and a caller that treats
    the new value as a bool sees every decision as truthy, including both
    suppressions. The type change is deliberate so that caller fails loudly at
    review rather than silently at runtime.

    Never raises. A tool that cannot reach its HTTP request must still return
    its ToolError rather than dying inside the error handler.

    Args:
        api_message: what api-v2 actually said, for the challenge description.
        credential_failure: ``utils.fastmcp_errors.is_credential_401``'s verdict.
        known_message: whether that verdict came from a body we have read the
            emitter for. Labels the metric only; changes no behaviour.
    """
    try:
        from fastmcp.server.dependencies import get_http_request

        request = get_http_request()
    except Exception as exc:
        # RuntimeError("No active HTTP request found.") is the expected miss on
        # stdio, in-process FastMCP clients, and unit tests that call a
        # decorated tool directly.
        logger.debug("No HTTP request to flag for downstream auth failure: %s", exc)
        return _record_decision(CHALLENGE_NO_REQUEST)

    # Marked whatever we decide, so the middleware's reset cannot fire on a
    # request that saw a 401 -- including one whose challenge was suppressed.
    request.scope[DOWNSTREAM_AUTH_SEEN_SCOPE_KEY] = True

    if not credential_failure:
        logger.info(
            "Downstream 401 is about the object rather than the credential; "
            "not challenging (re-authenticating could not fix it)"
        )
        return _record_decision(CHALLENGE_SUPPRESSED_NOT_CREDENTIAL)

    session_id = request.headers.get("mcp-session-id")
    if session_id and not _consume_challenge_budget(session_id):
        logger.warning(
            "Suppressing a downstream-401 challenge: this MCP session has "
            "already been challenged %d times with no successful upstream call "
            "in between, so challenging again is a loop, not a recovery",
            _max_challenges_per_session(),
        )
        return _record_decision(CHALLENGE_SUPPRESSED_CIRCUIT_OPEN)

    request.scope[DOWNSTREAM_AUTH_FAILURE_SCOPE_KEY] = _clean_for_header(api_message)
    return _record_decision(
        CHALLENGE_ISSUED_KNOWN if known_message else CHALLENGE_ISSUED_UNCLASSIFIED
    )


def _record_decision(decision: str) -> str:
    """Count one decision and hand it back. Must never break a request.

    ⚠️ The swallow is deliberate and the log level is NOT (#1015). Recording a
    metric must never take down a request, so the except stays. But it used to
    log at DEBUG, which production does not emit, and the result was a counter
    that could stop recording in complete silence: nothing in the process said
    so, no test could notice (the suite replaces this module with a stub), and
    on a dashboard a broken counter is indistinguishable from one that has
    simply never fired. WARNING is the difference between a failure that is
    swallowed and a failure that is hidden.
    """
    try:
        from metrics import record_auth_challenge

        record_auth_challenge(decision)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Could not record auth-challenge decision %r: %s. The challenge "
            "decision itself is unaffected, but mcp_server_auth_challenge_total "
            "is now under-counting and must not be read as authoritative.",
            decision, exc,
        )
    return decision


def _initialize_challenge_metric_series() -> None:
    """Publish every decision series at zero, at import, so the metric is legible.

    See ``metrics.initialize_auth_challenge_series`` for why: a labelled counter
    with no children emits no samples, so "never fired" and "not wired" look the
    same, and #1015 exists because nobody could tell which one production was in.

    Never raises. This runs at import of a middleware the server cannot start
    without, so a metrics problem must not be able to stop the server booting --
    the same fail-safe reasoning the module docstring gives for everything else
    here. It logs at WARNING rather than DEBUG for the reason above.
    """
    try:
        from metrics import initialize_auth_challenge_series

        initialize_auth_challenge_series(CHALLENGE_DECISIONS)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Could not pre-create mcp_server_auth_challenge_total series: %s. "
            "The challenge path still works; the metric will be absent until "
            "its first increment, so an empty reading proves nothing.", exc,
        )


_initialize_challenge_metric_series()


class DownstreamAuthChallengeMiddleware:
    """Rewrite a 2xx MCP response into a 401 when a tool hit a downstream 401.

    A pure ASGI middleware rather than a ``BaseHTTPMiddleware`` because it has
    to see ``http.response.start`` in order to replace the status, and because
    it must not buffer the body of every healthy response to do so.

    It emits a plain OAuth error body and lets ``AuthErrorMiddleware``, which
    wraps it, attach the ``WWW-Authenticate`` header carrying the RFC 9728
    ``resource_metadata`` pointer. That middleware is the single place in this
    server that builds the challenge header, and this change keeps it that way.
    """

    def __init__(self, app: Callable):
        self.app = app

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http" or scope.get("path") not in MCP_TRANSPORT_PATHS:
            await self.app(scope, receive, send)
            return

        # A fresh scope per request is the ASGI contract, so this only matters
        # if something upstream ever recycles one.
        scope.pop(DOWNSTREAM_AUTH_FAILURE_SCOPE_KEY, None)
        scope.pop(DOWNSTREAM_AUTH_SEEN_SCOPE_KEY, None)
        scope.pop(CREDENTIAL_PRESENTED_SCOPE_KEY, None)

        state = {"rewritten": False}

        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            if state["rewritten"]:
                # Our replacement is already on the wire; drop the original
                # body frames rather than appending them to it.
                return

            if message.get("type") == "http.response.start":
                detail = scope.get(DOWNSTREAM_AUTH_FAILURE_SCOPE_KEY)
                status = message.get("status", 200)
                # Only ever downgrade a success. An upstream response that is
                # already an error carries its own, more specific, reason.
                if detail is not None and 200 <= status < 300:
                    state["rewritten"] = True
                    await _send_challenge(send, detail)
                    return

                # A genuine upstream success closes the circuit breaker.
                #
                # 🔴 ALL THREE CONDITIONS ARE LOAD-BEARING. A 2xx alone is
                # satisfied by `initialize` and `tools/list`, which never touch
                # api-v2 and so succeed happily on a dead credential -- so
                # resetting on a bare 2xx defeats the breaker entirely while
                # every other assertion in the suite still passes. The
                # credential-presented key is what proves the request actually
                # reached api-v2, and the auth-seen key is what stops a
                # SUPPRESSED challenge (which also leaves a 200) from resetting
                # the very counter that suppressed it.
                #
                # A 404 or a 422 from api-v2 counts as success here, correctly:
                # the edge would have 401'd first, so the credential was
                # accepted. Only a 2xx reaches this branch anyway.
                if (
                    200 <= status < 300
                    and scope.get(CREDENTIAL_PRESENTED_SCOPE_KEY)
                    and not scope.get(DOWNSTREAM_AUTH_SEEN_SCOPE_KEY)
                ):
                    note_upstream_success(_session_id_from_scope(scope))

            await send(message)

        await self.app(scope, receive, send_wrapper)


async def _send_challenge(send: Callable, api_message: str) -> None:
    """Send the 401 body AuthErrorMiddleware will decorate."""
    description = build_challenge_description(api_message)
    body = json.dumps(
        {"error": CHALLENGE_ERROR, "error_description": description}
    ).encode("utf-8")

    logger.warning(
        "Downstream Tallyfy API rejected the access token; answering the MCP "
        "request with a 401 challenge so the client re-authenticates"
    )

    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})
