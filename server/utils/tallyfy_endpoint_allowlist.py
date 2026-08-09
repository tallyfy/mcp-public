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
- **Scope gating, FAIL CLOSED**: a destructive call to a path covered by
  ``_WRITE_SCOPE_RULES`` requires the matching ``mcp.<resource>.write``
  scope in the caller's VERIFIED token. A token carrying no scopes has
  proved no authority, so it is rejected like any other insufficient
  token. There is no legacy carve-out and no opt-out switch.

  This used to read ``if scopes and needed not in scopes``, which
  short-circuited on an empty set, so the membership test never ran and
  every rule in ``_WRITE_SCOPE_RULES`` was unreachable. Paired with a
  hardcoded ``jwt_scopes=()`` at the only call site, the gate could not
  fire at all. Both halves are fixed; see #746.

Issues: #171 (original), #746 (the gate could never fire)
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
# second value is the scope that must be present in the JWT's ``scope`` set.
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
    jwt_scopes: Iterable[str],
) -> AllowlistResult:
    """Return whether the call is allowed; produce a reason if not."""
    method_u = method.upper()
    is_destructive = method_u in _WRITE_METHODS

    if is_blocked(path):
        return AllowlistResult(
            allowed=False,
            reason="blocked",
            is_destructive=is_destructive,
        )

    if is_destructive:
        needed = required_write_scope(path)
        if needed:
            scopes = frozenset(jwt_scopes or ())
            # FAIL CLOSED. An empty scope set is not an exemption: it is a
            # caller that proved no authority, which is exactly the case this
            # gate exists to stop. The former `if scopes and ...` made an empty
            # set unconditionally sufficient, so this branch was dead (#746).
            # Non-destructive methods never reach here, so reads are untouched.
            if needed not in scopes:
                return AllowlistResult(
                    allowed=False,
                    reason=f"scope_missing:{needed}",
                    required_scope=needed,
                    is_destructive=True,
                )

    return AllowlistResult(
        allowed=True,
        is_destructive=is_destructive,
        required_scope=required_write_scope(path),
    )
