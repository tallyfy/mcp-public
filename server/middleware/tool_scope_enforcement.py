"""Enforce a token's ``mcp_scopes`` at the tool boundary (tallyfy/mcp#559).

This is a FastMCP tool middleware, not an ASGI one, and that is forced rather
than chosen: the thing being authorised is a TOOL NAME, and the tool name only
exists inside the JSON-RPC body. An ASGI middleware would have to read and
re-inject the request stream to see it, which breaks streaming for every request
in order to gate a minority of them.

The consequence is worth stating plainly, because ``utils/org_id_middleware.py``
already ships ``build_www_authenticate_header(error="insufficient_scope", ...)``
and it would be the RFC 6750 answer here. It is not used: by the time a tool name
is known, the HTTP status for this response is already 200 and the MCP transport
is committed to returning a JSON-RPC result. A denial therefore arrives the way
every other tool failure does -- ``isError: true`` with a message -- and the
message names the missing scope so a client or an operator can act on it. The
challenge header stays where it belongs, on the 401 that a missing or invalid
token gets, which is a different failure at a different layer.

Modes -- ``MCP_TOOL_SCOPE_ENFORCEMENT``
---------------------------------------
``enforce`` (default) | ``log`` | ``off``.

The name is deliberately NOT api-v2's ``MCP_OAUTH_TOKEN_SCOPE_ENFORCEMENT``.
Two flags with the same name in two services, gating two different layers of the
same feature, is how somebody reads the wrong one and concludes enforcement is on
-- this repo has already lost fifteen months to exactly that with
``ENFORCE_JWT_AUDIENCE`` and ``MCP_ENFORCE_JWT_AUDIENCE``.

``enforce`` is the default because api-v2's equivalent already defaults to
enforce, so a scoped token is being gated at the API regardless; shipping this
one in ``log`` would mean the two halves disagreed about whether the feature is
live. ``log`` exists for a new environment that wants to measure first: it emits
the WARNING a denial would emit and then allows the call.

``os.getenv(name, "enforce")`` returns ``""`` for a variable that is SET BUT
EMPTY, so an ``.env`` line reading ``MCP_TOOL_SCOPE_ENFORCEMENT=""`` -- which
looks like documentation of the default -- would silently disable the gate under
that idiom. ``enforcement_mode`` handles the empty string explicitly, and an
unrecognised value falls back to ``enforce`` rather than to ``off``. Both are
tested.

What is NOT gated, and must never become gated
----------------------------------------------
A token with no ``mcp_scopes`` claim passes through in every mode. See
``utils/tool_scopes.py`` for why that is load-bearing rather than lenient.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

from utils.auth_context import get_mcp_scopes
from utils.tool_scopes import ScopeDecision, decide

logger = logging.getLogger(__name__)

ENFORCEMENT_ENV_VAR = "MCP_TOOL_SCOPE_ENFORCEMENT"
DEFAULT_MODE = "enforce"
VALID_MODES = frozenset({"enforce", "log", "off"})


def enforcement_mode() -> str:
    """Read the mode, treating unset, empty and unrecognised values as ``enforce``.

    Read per call rather than at import so an operator can change it with a
    container restart and so tests can drive every branch.
    """
    raw = os.getenv(ENFORCEMENT_ENV_VAR)
    if raw is None or not raw.strip():
        # Covers BOTH "unset" and "set to the empty string". `os.getenv(name,
        # "enforce")` would return "" for the second and skip the default.
        return DEFAULT_MODE

    value = raw.strip().lower()
    if value not in VALID_MODES:
        logger.warning(
            "%s=%r is not one of %s; falling back to %r",
            ENFORCEMENT_ENV_VAR,
            raw,
            sorted(VALID_MODES),
            DEFAULT_MODE,
        )
        return DEFAULT_MODE
    return value


def denial_message(tool_name: str, decision: ScopeDecision) -> str:
    """The text a denied caller sees. Names what is missing, so it is actionable."""
    if decision.reason == "unmapped_tool":
        return (
            f"'{tool_name}' has no declared scope requirement on this server, so "
            f"it cannot be called with a scope-limited access token. This is a "
            f"server-side gap, not a permission you can grant: report it against "
            f"tallyfy/mcp."
        )

    missing = ", ".join(sorted(decision.missing))
    plural = "permissions" if len(decision.missing) > 1 else "permission"
    return (
        f"This application was not granted the '{missing}' {plural} needed to "
        f"call '{tool_name}'. Required scope: {missing}. Re-authorize the "
        f"connection and approve it, or use a tool covered by the scopes you "
        f"already hold."
    )


def _log_decision(tool_name: str, decision: ScopeDecision, mode: str) -> None:
    """Mirror api-v2's ``Log::warning('MCP token scope check failed', ...)``.

    In ``log`` mode this IS the output -- it is what "emits what would be denied
    without denying" means -- so it carries the same fields a denial does.
    """
    logger.warning(
        "MCP tool scope check failed | mode=%s | tool=%s | reason=%s | "
        "required=%s | missing=%s",
        mode,
        tool_name,
        decision.reason,
        sorted(decision.required),
        sorted(decision.missing),
    )


class ToolScopeEnforcementMiddleware(Middleware):
    """Refuse a tool call the caller's ``mcp_scopes`` do not cover."""

    async def on_call_tool(self, context, call_next):
        mode = enforcement_mode()
        if mode == "off":
            return await call_next(context)

        tool_name = _tool_name(context)
        if tool_name is None:
            # No name means this is not a shape we can authorise. Allowing it
            # would be a bypass, so refuse rather than guess.
            raise ToolError(
                "Tool call rejected: the request carried no tool name, so its "
                "required permissions could not be determined."
            )

        decision = decide(tool_name, get_mcp_scopes())
        if decision.allowed:
            return await call_next(context)

        _log_decision(tool_name, decision, mode)

        if mode == "log":
            # Audit-only: observe the population before enforcing on it.
            return await call_next(context)

        raise ToolError(denial_message(tool_name, decision))


def _tool_name(context) -> Optional[str]:
    """The tool name off a FastMCP middleware context, or None.

    ``context.message`` is an ``mcp.types.CallToolRequestParams`` in every shape
    fastmcp produces today. Read defensively anyway: this function decides
    whether a call is authorisable at all, so an AttributeError raised here would
    surface as a 500 on a request that should have been a clean refusal.
    """
    message = getattr(context, "message", None)
    name = getattr(message, "name", None)
    if isinstance(name, str) and name:
        return name
    return None
