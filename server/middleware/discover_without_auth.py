"""Answer ``server/discover`` without a bearer token (#1317, part of #1238).

Why this exists
---------------
The MCP spec (2026-07-28) lets a client call ``server/discover`` before any
other request, to learn which protocol versions, capabilities and identity a
server offers. FastMCP wraps the WHOLE streamable-http route in
``RequireAuthMiddleware`` (``fastmcp/server/http.py``), so a client that asks
before it has a token is refused with 401. On production that happened 47 to 84
times a day from 2026-09-06 to 2026-09-15. With a token the method already
works: the ``mcp`` SDK registers a default handler for it.

The owner decided on 2026-09-15 that the method is exempt from auth, recorded
on tallyfy/mcp#1238. It returns nothing that ``/health`` and
``/.well-known/mcp/server-card.json`` do not already publish without a token.

What it does
------------
Wraps the transport endpoint. A request reaches the UNWRAPPED streamable app
only when every one of these holds:

* it is an HTTP POST to a transport path;
* it carries no ``Authorization`` header, so a caller that did send a token is
  still verified exactly as before;
* it carries an ``MCP-Protocol-Version`` header that the SDK routes to its
  sessionless entry, which is any value not in ``HANDSHAKE_PROTOCOL_VERSIONS``;
* its body is a single JSON-RPC 2.0 object (never a batch) with a string or
  integer ``id`` and ``method`` exactly ``"server/discover"``.

Everything else goes to the protected app. Reusing the SDK's own handler means
the answer with and without a token cannot drift apart.

Things that are easy to get wrong
---------------------------------
* **Mirror the SDK's era rule, and import its set rather than copying it.**
  ``mcp/server/streamable_http_manager.py`` sends a request to the sessionless
  path only when ``mcp-protocol-version`` is present and not a handshake
  version. Without that header the request is the session-based era, where
  ``server/discover`` is not a method and anything but ``initialize`` needs a
  session. Exempting it anyway turns its 401 OAuth challenge into a 400
  ``Missing session ID`` that tells the client nothing, which is a regression
  for exactly the clients this change is meant to help. Measured on the first
  cut of this module, 2026-09-15. ``mcp`` 2.1.1 (the production lock) and 2.2.0
  apply the identical rule from the same ``mcp_types.version`` import.
* **Match the parsed method, never the raw body.** A substring test on the
  bytes would let a batch that includes ``server/discover`` beside a
  ``tools/call``, or a tool call whose arguments mention the method, straight
  past auth.
* **Replay every byte that was read.** The wrapper has to read the body to
  decide, so whichever app receives the request gets the buffered messages
  first and then the live ``receive``. Losing a chunk turns a valid request
  into a malformed one.
* **Cap what is read before auth.** An unauthenticated caller controls the
  body size, so the wrapper stops reading at ``MAX_DISCOVER_BODY_BYTES`` and
  hands the rest to the protected app untouched. A real discover request is a
  few dozen bytes.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, MutableMapping

from mcp.shared.inbound import MCP_PROTOCOL_VERSION_HEADER
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from middleware.auth_error import MCP_TRANSPORT_PATHS

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

DISCOVER_METHOD = "server/discover"

# A real discover request is under 100 bytes. The cap bounds how much an
# unauthenticated caller can make this wrapper buffer before auth runs.
MAX_DISCOVER_BODY_BYTES = 8192

__all__ = [
    "DISCOVER_METHOD",
    "HANDSHAKE_PROTOCOL_VERSIONS",
    "MAX_DISCOVER_BODY_BYTES",
    "MCP_TRANSPORT_PATHS",
    "DiscoverWithoutAuth",
    "is_single_discover_request",
    "speaks_the_sessionless_era",
]


def is_single_discover_request(body: bytes) -> bool:
    """True only for one JSON-RPC 2.0 request whose method is ``server/discover``."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("jsonrpc") != "2.0":
        return False
    if payload.get("method") != DISCOVER_METHOD:
        return False
    request_id = payload.get("id")
    # A notification has no id and a null id is not a request. bool is a
    # subclass of int in Python, so it has to be excluded explicitly.
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
        return False
    return True


def _has_authorization(scope: Scope) -> bool:
    return any(name.lower() == b"authorization" for name, _ in scope.get("headers") or [])


def speaks_the_sessionless_era(scope: Scope) -> bool:
    """True when the SDK would route this request to its sessionless entry.

    Same test ``streamable_http_manager`` applies: the version header is
    present and is not one of the handshake-era versions.
    """
    wanted = MCP_PROTOCOL_VERSION_HEADER.encode("ascii")
    for name, value in scope.get("headers") or []:
        if name.lower() == wanted:
            return value.decode("latin-1") not in HANDSHAKE_PROTOCOL_VERSIONS
    return False


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers") or []:
        if name.lower() == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _replay(buffered: list[Message], receive: Receive) -> Receive:
    pending = list(buffered)

    async def replay() -> Message:
        if pending:
            return pending.pop(0)
        return await receive()

    return replay


class DiscoverWithoutAuth:
    """Route a no-token ``server/discover`` around the auth wrapper."""

    def __init__(self, protected_app: ASGIApp, open_app: ASGIApp) -> None:
        self.protected_app = protected_app
        self.open_app = open_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or scope.get("path") not in MCP_TRANSPORT_PATHS
            or _has_authorization(scope)
            or not speaks_the_sessionless_era(scope)
        ):
            await self.protected_app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > MAX_DISCOVER_BODY_BYTES:
            await self.protected_app(scope, receive, send)
            return

        buffered: list[Message] = []
        total = 0
        complete = False
        while True:
            message = await receive()
            buffered.append(message)
            if message.get("type") != "http.request":
                break
            total += len(message.get("body", b""))
            if total > MAX_DISCOVER_BODY_BYTES:
                break
            if not message.get("more_body", False):
                complete = True
                break

        body = b"".join(
            m.get("body", b"") for m in buffered if m.get("type") == "http.request"
        )
        if complete and is_single_discover_request(body):
            target = self.open_app
        else:
            target = self.protected_app
        await target(scope, _replay(buffered, receive), send)
