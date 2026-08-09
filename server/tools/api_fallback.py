"""Tallyfy API fallback tools — ``tallyfy_api_read`` and ``tallyfy_api_write``.

Gives Claude an escape hatch: if no specific tool matches the user's
request, Claude can issue a direct HTTP call against any Tallyfy REST
API endpoint, subject to:

- Feature flag: ``MCP_ENABLE_API_FALLBACK=true`` (OFF by default in prod
  until the per-org whitelist warms up).
- Path validation against the live OpenAPI spec (``tallyfy_spec_cache``).
- Block-list of admin / oauth / metrics surfaces (``tallyfy_endpoint_allowlist``).
- Audit logging for every write call.

**Why two tools and not one.** This used to be a single ``tallyfy_api_call``
with a ``method`` parameter accepting GET through DELETE. Anthropic's
Connectors Directory review rules reject exactly that shape: a tool that
accepts both safe and unsafe HTTP methods must be split into a read-only
tool and one or more write tools, and saying so in the description does
not satisfy the rule. Splitting also lets the annotations tell the truth,
so the read tool can run without a confirmation prompt while the write
tool always asks. See https://claude.com/docs/connectors/building/review-criteria

Issues: #171 (original), #651 (the split)  |  Plan: §VIII / §V.4
"""
from __future__ import annotations

import json
import logging
import os
from typing import Annotated, Any, Dict, Literal, Optional

import httpx
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
from mcp.types import ToolAnnotations
from pydantic import Field

from constants import TALLYFY_API_BASE_URL
from utils.auth_context import (
    get_authenticated_credentials,
    get_jwt_scopes,
    get_user_id_from_token,
)
from utils.fastmcp_errors import handle_tallyfy_errors
from utils.tallyfy_endpoint_allowlist import check as allowlist_check
from utils.tallyfy_spec_cache import SPEC_CACHE
from metrics import track_tool_execution


logger = logging.getLogger(__name__)
_audit_logger = logging.getLogger("tallyfy_api_call_audit")


# Environment flag. Start OFF in prod; flip per-org via MCP_FEATURE_WHITELIST_ORGS.
_ENABLED = os.getenv("MCP_ENABLE_API_FALLBACK", "false").lower() == "true"


# Deliberately split. Never merge these back into one union on one tool:
# a single tool exposing both sets is an automatic rejection from the
# Anthropic Connectors Directory (see the module docstring and #651).
ReadMethod = Literal["GET"]
WriteMethod = Literal["POST", "PUT", "PATCH", "DELETE"]

# The public Tallyfy API reference. Anthropic's review rules require a tool
# that accepts caller-built endpoint paths to name or link its target API.
TALLYFY_API_DOCS_URL = "https://api.tallyfy.com/docs/index"


ApiCallPath = Annotated[str, Field(
    description=(
        "Tallyfy REST API path starting with '/'. Concrete IDs fine: "
        "'/organizations/abc123/users' matches the template "
        "'/organizations/{org}/users' in the live OpenAPI spec. "
        "Use curly-brace placeholders (e.g. '{org}') only if you "
        "genuinely don't know the value — the server substitutes "
        "the authenticated org_id for '{org}' automatically."
    ),
    examples=["/organizations/abc123/users", "/me"],
    min_length=1,
)]

ApiCallBody = Annotated[Optional[Dict[str, Any]], Field(
    default=None,
    description=(
        "JSON request body for POST/PUT/PATCH. MUST be omitted for DELETE "
        "unless the endpoint genuinely accepts a body there."
    ),
)]

ApiCallQuery = Annotated[Optional[Dict[str, Any]], Field(
    default=None,
    description="Query-string parameters as a flat dict (primitives or lists).",
)]


def _substitute_path_params(path: str, org_id: str) -> str:
    """Replace the ``{org}`` placeholder with the authenticated org_id.

    Other placeholders are left untouched — Claude must supply them.
    """
    return path.replace("{org}", org_id)


def _serialize_response(resp: httpx.Response) -> Dict[str, Any]:
    """Turn an httpx.Response into a dict suitable for ToolResult.content."""
    try:
        body: Any = resp.json()
    except Exception:
        body = resp.text
    return {
        "status_code": resp.status_code,
        "headers": {k.lower(): v for k, v in resp.headers.items() if k.lower() in (
            "content-type",
            "content-length",
            "x-request-id",
            "x-tallyfy-version",
        )},
        "body": body,
    }


def _audit(
    *,
    tool: str,
    method: str,
    path: str,
    status: int,
    org_id: str,
    user_id: Optional[str],
    destructive: bool,
) -> None:
    """One structured audit log per API call."""
    _audit_logger.info(
        json.dumps({
            "tool": tool,
            "method": method,
            "path": path,
            "status": status,
            "org_id": org_id,
            "user_id": user_id,
            "destructive": destructive,
        })
    )


async def _execute(
    *,
    tool_name: str,
    method: str,
    path: str,
    body: Optional[Dict[str, Any]],
    query: Optional[Dict[str, Any]],
) -> ToolResult:
    """Shared implementation behind both fallback tools.

    Read and write differ only in which methods their signature accepts.
    Everything after that (spec validation, the block-list, the HTTP call,
    audit logging) is identical by construction rather than by imitation,
    so the two tools cannot drift apart.
    """
    if not _ENABLED:
        raise ToolError(
            f"{tool_name} is disabled. Set MCP_ENABLE_API_FALLBACK=true "
            "to enable it for this deployment."
        )

    api_key, org_id = get_authenticated_credentials()
    user_id = get_user_id_from_token()
    method_u = method.upper()

    # Substitute {org} before spec lookup so the template match works.
    resolved_path = _substitute_path_params(path, org_id)

    # Spec validation — loaded at startup; refreshed hourly.
    # On-demand load as fallback: if the startup event didn't fire in time,
    # refresh now (blocking this call once) rather than failing immediately.
    if not SPEC_CACHE.is_loaded():
        await SPEC_CACHE.refresh_once()
    if not SPEC_CACHE.is_loaded():
        raise ToolError(
            "Tallyfy OpenAPI spec cache is not yet loaded. Retry in a moment."
        )
    endpoint = SPEC_CACHE.get_endpoint(method_u, resolved_path)
    if endpoint is None:
        raise ToolError(
            f"No {method_u} {resolved_path} in Tallyfy OpenAPI spec. "
            f"Paths are case-sensitive and include base path '/'. "
            f"Confirm the endpoint exists at {TALLYFY_API_DOCS_URL}"
        )

    # Allowlist + scope gate. The scopes come from the VERIFIED access token
    # for this request. They used to be a hardcoded empty tuple here, which
    # meant the gate never saw the caller at all (#746).
    gate = allowlist_check(method_u, resolved_path, jwt_scopes=get_jwt_scopes())
    if not gate.allowed:
        if gate.reason == "blocked":
            raise ToolError(
                f"Path {resolved_path} is in the block-list "
                "(/support, /auth, /oauth, /metrics, /health, /ready, /debug)"
            )
        raise ToolError(
            f"Access denied: {gate.reason}. Required scope: {gate.required_scope}"
        )

    # Issue the HTTP call.
    url = f"{TALLYFY_API_BASE_URL}{resolved_path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Tallyfy-Client": "APIClient",
        "Accept": "application/json",
    }
    if body is not None and method_u in ("POST", "PUT", "PATCH"):
        headers["Content-Type"] = "application/json"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method_u,
                url,
                headers=headers,
                json=body if method_u in ("POST", "PUT", "PATCH") else None,
                params=query,
            )
    except httpx.RequestError as e:
        logger.warning("%s network error: %s", tool_name, e)
        raise ToolError(f"Network error calling Tallyfy API: {e}") from e

    if gate.is_destructive:
        _audit(
            tool=tool_name,
            method=method_u,
            path=resolved_path,
            status=resp.status_code,
            org_id=org_id,
            user_id=user_id,
            destructive=True,
        )

    return ToolResult(
        content=_serialize_response(resp),
        structured_content=None,
    )


_SHARED_RULES = (
    "Paths under /support, /auth, /oauth, /metrics, /health, /ready and "
    "/debug are BLOCKED and return an error. This is a PATH-PREFIX block "
    "list and nothing more: it does NOT restrict administrative actions, "
    "because api-v2 gates those by middleware on ordinary org-scoped paths "
    "(user role changes, disable, delete, approve, reject, tag delete, "
    "organization roles) rather than behind an /admin prefix. Those remain "
    "reachable here, limited only by the caller's own permissions. The "
    "path MUST exist "
    "in the live Tallyfy OpenAPI spec (auto-refreshed hourly); confirm it "
    f"at {TALLYFY_API_DOCS_URL}. The '{{org}}' placeholder is substituted "
    "with the authenticated org_id, so pass it literally. Other path "
    "params (template_id, user_id) you supply yourself. "
    "Returns {status_code, headers, body}."
)


def register_api_fallback_tool(mcp):
    """Register the read and write API fallback tools with the MCP server."""

    @mcp.tool(
        name="tallyfy_api_read",
        description=(
            "READ-ONLY FALLBACK. Issues a GET against any Tallyfy REST API "
            "endpoint when no specific tool fits. This tool cannot modify "
            "anything; use tallyfy_api_write for that.\n\n"
            "Prefer a specific tool (search_*, get_*) whenever one matches. "
            "Only reach for this after confirming none does.\n\n"
            f"{_SHARED_RULES}\n\n"
            "EXAMPLE PATHS (illustrative; confirm against the live spec):\n"
            "- /organizations/{org}/runs (list processes)\n"
            "- /organizations/{org}/checklists/{template_id} (fetch template)\n"
            "- /organizations/{org}/users/{user_id}/tasks (a user's tasks)\n"
            "- /organizations/{org}/checklists/{template_id}/steps/{step_id}/captures "
            "(step form fields)"
        ),
        tags={"fallback", "generic", "advanced", "read-only"},
        annotations=ToolAnnotations(
            title="Read any Tallyfy API endpoint",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
            idempotentHint=True,
        ),
        output_schema=None,
    )
    @track_tool_execution("tallyfy_api_read")
    @handle_tallyfy_errors("fallback API read")
    async def tallyfy_api_read(
        path: ApiCallPath,
        query: ApiCallQuery = None,
        method: ReadMethod = "GET",
    ) -> ToolResult:
        """Fallback: GET an arbitrary Tallyfy API endpoint."""
        return await _execute(
            tool_name="tallyfy_api_read",
            method=method,
            path=path,
            body=None,
            query=query,
        )

    @mcp.tool(
        name="tallyfy_api_write",
        description=(
            "WRITE FALLBACK. Issues a POST, PUT, PATCH or DELETE against any "
            "Tallyfy REST API endpoint when no specific tool fits. This tool "
            "CHANGES OR DELETES DATA. To read, use tallyfy_api_read.\n\n"
            "MANDATORY: call ask_user_question FIRST and confirm the exact "
            "action with the user. Never issue a write without explicit "
            "confirmation. Prefer a specific tool (create_*, update_*, "
            "delete_*) whenever one matches.\n\n"
            "The body argument is raw JSON for the endpoint; shape it per the "
            "Tallyfy API docs. Every call here is audit-logged.\n\n"
            f"{_SHARED_RULES}\n\n"
            "EXAMPLE PATHS (illustrative; confirm against the live spec):\n"
            "- POST /organizations/{org}/runs (launch a process)\n"
            "- PUT /organizations/{org}/checklists/{template_id} (update a template)"
        ),
        tags={"fallback", "generic", "advanced", "write"},
        annotations=ToolAnnotations(
            title="Write to any Tallyfy API endpoint",
            readOnlyHint=False,
            destructiveHint=True,
            openWorldHint=True,
            idempotentHint=False,
        ),
        output_schema=None,
    )
    @track_tool_execution("tallyfy_api_write")
    @handle_tallyfy_errors("fallback API write")
    async def tallyfy_api_write(
        method: WriteMethod,
        path: ApiCallPath,
        body: ApiCallBody = None,
        query: ApiCallQuery = None,
    ) -> ToolResult:
        """Fallback: POST/PUT/PATCH/DELETE an arbitrary Tallyfy API endpoint."""
        return await _execute(
            tool_name="tallyfy_api_write",
            method=method,
            path=path,
            body=body,
            query=query,
        )
