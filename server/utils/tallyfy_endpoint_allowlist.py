"""Allowlist + block-list gating for the API fallback tools.

``tallyfy_api_read`` and ``tallyfy_api_write`` both route through this gate.

Policy:
- **Block-list is ABSOLUTE**: any path matching a block pattern is rejected
  regardless of method or scope. Block patterns cover internal/admin
  surfaces (/admin, /support, /webhooks/internal, /auth, /oauth, /metrics,
  /health, /ready, /debug).
- **Method-based destructive hints**: POST/PUT/PATCH/DELETE are always
  flagged destructive — the MCP tool decorator uses this hint to trigger
  Claude's confirmation flow via ``ask_user_question`` (enforced by the
  system prompt, not by this module).
- **Scope gating, FAIL CLOSED for a SCOPED token**: a destructive call to a
  path covered by ``_WRITE_SCOPE_RULES`` requires the matching
  ``mcp.<resource>.write`` scope in the caller's VERIFIED token. An MCP token
  granted an empty scope set has proved no authority, so it is rejected like
  any other insufficient token.

  This used to read ``if scopes and needed not in scopes``, which
  short-circuited on an empty set, so the membership test never ran and
  every rule in ``_WRITE_SCOPE_RULES`` was unreachable. Paired with a
  hardcoded ``jwt_scopes=()`` at the only call site, the gate could not
  fire at all. Both halves were fixed in #746.

Where the scopes come from, and why the parameter was renamed (#856)
--------------------------------------------------------------------
The parameter is ``mcp_scopes`` and it is fed by
``auth_context.get_mcp_scopes()``. It used to be ``jwt_scopes``, fed by
``auth_context.get_jwt_scopes()``, which reads ``AccessToken.scopes`` --
populated by fastmcp's ``JWTVerifier._extract_scopes`` from the ``scope`` /
``scp`` claims. **A Tallyfy MCP token carries neither.**
``McpAccessTokenService::issue`` mints ``scopes: []`` on purpose (that field is
a Passport organization allowlist) and puts the MCP permissions in a separate
``mcp_scopes`` claim. So the old source returned ``()`` for every real Tallyfy
token and the fail-closed branch denied EVERY write to a rule-covered path,
including from a token that genuinely held the required scope.

Measured 2026-08-21 against the exact payload ``McpAccessTokenService::issue``
mints, with a positive control in the same run::

    fastmcp _extract_scopes(real api-v2 payload) -> []
    POSITIVE CONTROL with a 'scope' claim       -> ['mcp.tasks.read', 'mcp.templates.write']
    check(POST checklists, from get_jwt_scopes) -> DENIED (scope_missing:mcp.templates.write)
    check(POST checklists, from mcp_scopes)     -> allowed

The rename is deliberate and is the safety property: a caller still passing
``jwt_scopes=`` now gets a loud ``TypeError`` rather than silently inheriting
the new meaning of ``None`` described below.

THE ABSENT-CLAIM DECISION (#856), stated rather than implied
------------------------------------------------------------
``mcp_scopes=None`` means the token carries NO ``mcp_scopes`` claim, and such a
token is **NOT scope gated here**. ``mcp_scopes=()`` means an MCP token that was
granted nothing, and IS gated.

This matches ``utils.tool_scopes.decide`` exactly, which is the point.
``chat.tallyfy.com`` and the desktop native AI shell forward the user's RAW
Tallyfy session token, which carries no such claim; api-v2's own
``EnforceMcpTokenScopes`` returns early for the same case. Two gates sit in
front of ``tallyfy_api_write`` (the tool-name middleware from #853 and this
per-path one) and having them disagree about the absent claim is the hazard
#856 names: the middleware would pass the call through ungated and this gate
would then refuse it, so Tallyfy's own products would break on the paths in
``_WRITE_SCOPE_RULES`` and nowhere else.

**The alternative that was rejected**: keep treating an absent claim as "no
authority" and deny. That is the stricter reading and it is what this module
did before #856 (by accident, since it could not tell the two states apart).
It was rejected because it 403s Tallyfy's own first-party clients on exactly
the paths this table covers, and because a defence-in-depth gate that
contradicts the primary gate is a source of inconsistent behaviour rather than
extra safety. Nothing is lost: an absent-claim caller is still bounded by the
block-list here, by ``MCP_ENABLE_API_FALLBACK``, and by api-v2's own
permission checks on the token it presents.

WHY THIS GATE STILL EXISTS ALONGSIDE #853 (#856 criterion 4)
-------------------------------------------------------------
#853 gates ``tallyfy_api_write`` on ALL SIX ``.write`` scopes at the tool
boundary, so in ``enforce`` mode any token that reaches this module already
satisfies every rule below. This gate is kept anyway, for three reasons that
are properties of the code rather than opinions:

1. ``MCP_TOOL_SCOPE_ENFORCEMENT`` accepts ``log`` and ``off``
   (``middleware/tool_scope_enforcement.enforcement_mode``). In both of those
   the tool-name gate allows the call. This gate is not controlled by that
   flag, so removing it would leave ZERO scope checking on the fallback tool
   in those modes.
2. The two gates answer different questions. The middleware knows only a tool
   NAME; this module is the only thing that knows a write to
   ``/organizations/{org}/users`` needs ``mcp.users.write`` specifically. If
   #853's all-six requirement is ever relaxed, this is what remains.
3. The block-list half of this module is unconditional and unrelated to
   scopes, so the module is not going away regardless.

Issues: #171 (original), #746 (the gate could never fire),
#856 (it was reading a claim Tallyfy tokens never carry)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import FrozenSet, Iterable, Optional, Tuple


# Absolute block-list — path prefixes that MUST NOT be reachable via the
# fallback tool. Patterns are matched against the normalized path (no
# trailing slash, no query string).
_BLOCKED_PREFIXES: Tuple[re.Pattern, ...] = (
    # NOTE (#622 item 9): this pattern currently matches NOTHING. api-v2 has no
    # prefix('admin') route group — its admin surface is org-scoped and gated by
    # `Route::middleware('admin')` on ordinary paths (users/{id}/role, /disable,
    # /delete, /approve, /reject, tag delete, organization-roles). It is kept as a
    # forward guard in case such a prefix is ever added, NOT as a live restraint,
    # and the tool descriptions must not advertise it as one.
    re.compile(r"^/admin(/|$)"),
    re.compile(r"^/support(/|$)"),
    re.compile(r"^/webhooks/internal(/|$)"),
    re.compile(r"^/auth(/|$)"),
    re.compile(r"^/oauth(/|$)"),
    re.compile(r"^/mcp/oauth(/|$)"),
    re.compile(r"^/metrics(/|$|$)"),
    re.compile(r"^/health(/|$|$)"),
    re.compile(r"^/ready(/|$)"),
    re.compile(r"^/debug(/|$)"),
)

# Scope requirements for writes. The regex matches the normalized path; the
# second value is the scope that must be present in the token's ``mcp_scopes``
# claim. NOT the ``scope`` / ``scp`` claim, which a Tallyfy token never carries
# -- see the module docstring and #856.
_WRITE_SCOPE_RULES: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"^/organizations/[^/]+/users"), "mcp.users.write"),
    (re.compile(r"^/organizations/[^/]+/guests"), "mcp.users.write"),
    (re.compile(r"^/organizations/[^/]+/groups"), "mcp.users.write"),
    (re.compile(r"^/organizations/[^/]+/runs"), "mcp.processes.write"),
    (re.compile(r"^/organizations/[^/]+/tasks"), "mcp.tasks.write"),
    (re.compile(r"^/organizations/[^/]+/(checklists|blueprints)"), "mcp.templates.write"),
    (re.compile(r"^/organizations/[^/]+/automation"), "mcp.automation.write"),
)

_WRITE_METHODS: FrozenSet[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class AllowlistResult:
    """Outcome of a single allowlist check."""

    allowed: bool
    reason: Optional[str] = None       # "blocked" / "scope_missing:X" / None
    required_scope: Optional[str] = None
    is_destructive: bool = False
    # True when the caller presented an ``mcp_scopes`` claim, so the scope
    # branch was actually consulted. False means the token carried no claim and
    # was passed through ungated. Mirrors ``tool_scopes.ScopeDecision.gated``.
    # Read this rather than inferring it from ``allowed``: an allowed result is
    # produced by BOTH a satisfied requirement and an absent claim, and only
    # one of those is a scope check that happened.
    scope_gated: bool = False


def _normalize_path(path: str) -> str:
    norm = path.split("?", 1)[0].rstrip("/")
    if not norm.startswith("/"):
        norm = "/" + norm
    return norm or "/"


def is_blocked(path: str) -> bool:
    norm = _normalize_path(path)
    return any(rx.match(norm) for rx in _BLOCKED_PREFIXES)


def required_write_scope(path: str) -> Optional[str]:
    """Return the scope required to write to this path, or None."""
    norm = _normalize_path(path)
    for rx, scope in _WRITE_SCOPE_RULES:
        if rx.match(norm):
            return scope
    return None


def check(
    method: str,
    path: str,
    mcp_scopes: Optional[Iterable[str]],
) -> AllowlistResult:
    """Return whether the call is allowed; produce a reason if not.

    Args:
        method: The HTTP method the caller wants to issue.
        path: The Tallyfy API path, already ``{org}``-substituted.
        mcp_scopes: The token's ``mcp_scopes`` claim, from
            ``auth_context.get_mcp_scopes()``. ``None`` and an empty iterable
            are DIFFERENT and must not be conflated by any caller:

            * ``None``  -- the token carries no ``mcp_scopes`` claim, so it is
              not MCP-issued and is NOT scope gated. This is what keeps
              chat.tallyfy.com and the desktop AI shell working, and it matches
              ``utils.tool_scopes.decide`` and api-v2's ``EnforceMcpTokenScopes``.
            * ``()``    -- an MCP token granted nothing. Gated, and therefore
              denied on every rule-covered write.

            The parameter was called ``jwt_scopes`` before #856 and ``None``
            meant "deny" there. It was RENAMED rather than redefined so that a
            missed caller fails loudly with a ``TypeError`` instead of silently
            acquiring the opposite behaviour.
    """
    method_u = method.upper()
    is_destructive = method_u in _WRITE_METHODS
    # An absent claim is not an empty grant. See the module docstring.
    is_scoped_token = mcp_scopes is not None

    if is_blocked(path):
        # The block-list is absolute and has nothing to do with scopes, so it
        # is checked before the claim is even consulted.
        return AllowlistResult(
            allowed=False,
            reason="blocked",
            is_destructive=is_destructive,
        )

    if is_destructive and is_scoped_token:
        needed = required_write_scope(path)
        if needed:
            scopes = frozenset(mcp_scopes)
            # FAIL CLOSED for a SCOPED token. An empty grant set is not an
            # exemption: it is a caller that proved no authority, which is
            # exactly the case this gate exists to stop. The former
            # `if scopes and ...` made an empty set unconditionally sufficient,
            # so this branch was dead (#746). Non-destructive methods never
            # reach here, so reads are untouched.
            if needed not in scopes:
                return AllowlistResult(
                    allowed=False,
                    reason=f"scope_missing:{needed}",
                    required_scope=needed,
                    is_destructive=True,
                    scope_gated=True,
                )

    return AllowlistResult(
        allowed=True,
        is_destructive=is_destructive,
        required_scope=required_write_scope(path),
        scope_gated=is_scoped_token,
    )
