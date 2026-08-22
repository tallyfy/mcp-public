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
"""

import json
import logging
from typing import Any, Awaitable, Callable, MutableMapping

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

# RFC 6750 error code. invalid_token is what every MCP client keys its OAuth
# retry off, and it is the honest reading of an upstream "Unauthenticated.":
# the token presented is expired, revoked, or no longer accepted.
CHALLENGE_ERROR = "invalid_token"

_MAX_DESCRIPTION_CHARS = 300


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


def flag_downstream_auth_failure(api_message: str = "") -> bool:
    """Record that the Tallyfy API refused this request's credentials.

    Called from ``utils.fastmcp_errors.handle_tallyfy_errors`` on a downstream
    401. Returns True when the flag was actually written, so a caller (and a
    test) can tell "signalled" from "there was no HTTP request to signal on".

    Never raises. A tool that cannot reach its HTTP request must still return
    its ToolError rather than dying inside the error handler.
    """
    try:
        from fastmcp.server.dependencies import get_http_request

        request = get_http_request()
        request.scope[DOWNSTREAM_AUTH_FAILURE_SCOPE_KEY] = _clean_for_header(api_message)
        return True
    except Exception as exc:  # pragma: no cover - defensive
        # RuntimeError("No active HTTP request found.") is the expected miss on
        # stdio, in-process FastMCP clients, and unit tests that call a
        # decorated tool directly.
        logger.debug("No HTTP request to flag for downstream auth failure: %s", exc)
        return False


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
