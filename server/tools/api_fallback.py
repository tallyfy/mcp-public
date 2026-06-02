"""Universal Tallyfy API fallback tool — ``tallyfy_api_call``.

Gives Claude an escape hatch: if no specific tool matches the user's
request, Claude can issue a direct HTTP call against any Tallyfy REST
API endpoint — subject to:

- Feature flag: ``MCP_ENABLE_API_FALLBACK=true`` (OFF by default in prod
  until the per-org whitelist warms up).
- Path validation against the live OpenAPI spec (``tallyfy_spec_cache``).
- Block-list of admin / oauth / metrics surfaces (``tallyfy_endpoint_allowlist``).
- Destructive-action hint (POST/PUT/PATCH/DELETE) — the system prompt
  instructs Claude to ``ask_user_question`` before calling this tool
  with a non-GET method.
- Audit logging for every non-GET call.

Issue: #171  |  Plan: §VIII / §V.4
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


HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


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
        "JSON request body for POST/PUT/PATCH. MUST be omitted for GET and "
        "DELETE unless the endpoint genuinely accepts a body there."
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
            "tool": "tallyfy_api_call",
            "method": method,
            "path": path,
            "status": status,
            "org_id": org_id,
            "user_id": user_id,
            "destructive": destructive,
        })
    )


def register_api_fallback_tool(mcp):
    """Register ``tallyfy_api_call`` with the MCP server."""

    @mcp.tool(
        name="tallyfy_api_call",
        description=(
            "UNIVERSAL FALLBACK — call ANY Tallyfy REST API endpoint when "
            "no specific tool fits. Path MUST exist in the live OpenAPI "
            "spec (auto-refreshed hourly). The ``{org}`` placeholder is "
            "auto-substituted with the authenticated org_id.\n\n"
            "USAGE RULES (MANDATORY):\n"
            "1. Prefer specific tools (search_*, get_*, create_*, etc.) "
            "whenever one matches — only reach for this tool after "
            "confirming no specific tool works.\n"
            "2. For POST/PUT/PATCH/DELETE, call ``ask_user_question`` "
            "FIRST to confirm the destructive action with the user. Never "
            "issue a write without explicit user confirmation.\n"
            "3. Paths under /admin, /support, /auth, /oauth, /metrics, "
            "/health, /ready, /debug are BLOCKED and will return an error.\n"
            "4. The body argument is raw JSON for the endpoint — shape it "
            "per the Tallyfy API docs.\n"
            "5. Returns {status_code, headers, body}.\n\n"
            "EXAMPLE PATHS (illustrative only — confirm against the live spec):\n"
            "- GET /organizations/{org}/runs — list processes\n"
            "- GET /organizations/{org}/checklists/{template_id} — fetch template\n"
            "- GET /organizations/{org}/users/{user_id}/tasks — user's tasks\n"
            "- POST /organizations/{org}/runs — launch process (write — confirm first)\n"
            "- GET /organizations/{org}/checklists/{template_id}/steps/{step_id}/captures — step form fields\n"
            "Note: {org} is auto-substituted; pass the literal placeholder. Other path params (template_id, user_id, etc.) you provide explicitly."
        ),
        tags={"fallback", "generic", "advanced"},
        annotations=ToolAnnotations(
            title="Universal Tallyfy API fallback",
            readOnlyHint=False,
            destructiveHint=True,
            openWorldHint=True,
            idempotentHint=False,
        ),
        output_schema=None,
    )
    @track_tool_execution("tallyfy_api_call")
    @handle_tallyfy_errors("fallback API call")
    async def tallyfy_api_call(
        method: HttpMethod,
        path: ApiCallPath,
        body: ApiCallBody = None,
        query: ApiCallQuery = None,
    ) -> ToolResult:
        """Fallback: call an arbitrary Tallyfy API endpoint."""
        if not _ENABLED:
            raise ToolError(
                "tallyfy_api_call is disabled. Set MCP_ENABLE_API_FALLBACK=true "
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
                f"Confirm the endpoint exists at https://api.tallyfy.com/docs/index"
            )

        # Allowlist + scope gate.
        gate = allowlist_check(method_u, resolved_path, jwt_scopes=())
        if not gate.allowed:
            if gate.reason == "blocked":
                raise ToolError(
                    f"Path {resolved_path} is in the block-list "
                    "(/admin, /support, /oauth, /metrics, etc.)"
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
            logger.warning("tallyfy_api_call network error: %s", e)
            raise ToolError(f"Network error calling Tallyfy API: {e}") from e

        if gate.is_destructive:
            _audit(
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
