"""
Group Management Tools
Tools for managing organization groups
"""

from fastmcp.tools.tool import ToolResult
from fastmcp.exceptions import ToolError
from tallyfy import TallyfySDK
from mcp.types import ToolAnnotations
from utils.fastmcp_errors import handle_tallyfy_errors
from utils.auth_context import get_authenticated_credentials, TALLYFY_API_BASE_URL
from typing import List, Optional

from utils.fastmcp_types import (
    GroupId,
    GroupName,
    OptionalBool,
    OptionalString,
    PageNumber,
    GenericDict,
    GenericList,
)
from utils.sdk_serializer import serialize_dataclass, compact_result
from utils.pagination import fetch_single_page
from metrics import track_tool_execution


def register_group_management_tools(mcp):
    """Register all group management tools with the MCP server"""

    @mcp.tool(
        name="get_organization_guests",
        description="Get all organization guests with full profile data. All parameters are optional. Use 'with_stats' (true/false) to include guest activity statistics. PAGINATION: Returns 20 results per page. Use page=2, page=3, etc. for more. meta.total_pages shows total page count.",
        tags={"users", "organization", "guests", "read-only"},
        annotations=ToolAnnotations(
            title="Get organization guests",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_organization_guests")
    @handle_tallyfy_errors("get organization guests")
    def get_organization_guests(with_stats: OptionalBool = False, page: PageNumber = 1) -> GenericDict:
        """
        Get guests in an organization with full profile data.

        Args:
            with_stats: Include guest statistics (default: False)
            page: Page number to fetch (default: 1)

        Returns:
            Dict with 'data' (list of guests) and 'meta' (pagination info)
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            return ToolResult(
                content=fetch_single_page(
                    sdk.users.get_organization_guests,
                    org_id,
                    page=page,
                    with_stats=with_stats,
                ),
                structured_content=None
            )

    @mcp.tool(
        name="get_organization_guests_list",
        description="Get all organization guests with minimal profile data for listing. Returns data with pagination metadata.",
        tags={"users", "organization", "guests", "read-only", "minimal"},
        annotations=ToolAnnotations(
            title="Get organization guests list",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_organization_guests_list")
    @handle_tallyfy_errors("get organization guests list")
    def get_organization_guests_list() -> GenericDict:
        """
        Get organization guests with minimal data.

        Returns:
            Dict with 'data' (list of guests) and 'meta' (pagination info)
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # This endpoint returns a flat list without pagination — fetch once
            guests_list = sdk.users.get_organization_guests_list(org_id)
            return ToolResult(
                content=compact_result({"data": serialize_dataclass(guests_list.data), "count": len(guests_list.data) if guests_list.data else 0}),
                structured_content=None
            )

    @mcp.tool(
        name="get_groups",
        description="Get all groups in the organization. Use group IDs to filter processes via get_organization_runs(groups=<group_id>) or assign tasks. All parameters are optional.",
        tags=["groups", "organization", "read-only", "discovery"],
        annotations=ToolAnnotations(
            title="Get groups",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_groups")
    @handle_tallyfy_errors("get groups")
    def get_groups(q: OptionalString = None) -> GenericList:
        """
        Get all groups in the organization.

        Args:
            q: Optional search query to filter groups by name

        Returns:
            List of group objects with id, name, description, and member counts
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            groups = sdk.groups.get_groups(org_id, q=q)
            return ToolResult(
                content=compact_result([serialize_dataclass(g) for g in groups]) if groups else [],
                structured_content=None
            )

    @mcp.tool(
        name="get_group",
        description="Get a single group by its ID. REQUIRED: 'group_id'. Never call this without group_id.",
        tags=["groups", "organization", "read-only"],
        annotations=ToolAnnotations(
            title="Get group",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_group")
    @handle_tallyfy_errors("get group")
    def get_group(group_id: GroupId) -> GenericDict:
        """
        Get a single group by ID.

        Args:
            group_id: Group ID (REQUIRED)

        Returns:
            Group object with details
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            group = sdk.groups.get_group(org_id, group_id)
            return ToolResult(
                content=serialize_dataclass(group) if group else {},
                structured_content=None
            )

    @mcp.tool(
        name="create_group",
        description="""Create a new group (team) in the organization.

REQUIRED: 'name' (group name, max 200 chars, must be unique in the organization)
AND 'description' (non-empty string). The API rejects a create without a
description, so ask the user for one — or pass a short factual summary of the
group's purpose — rather than omitting it.

Optional: 'members' (list of numeric member user IDs), 'guests' (list of guest emails).

CORRECT usage:
  create_group(name="Finance", description="Finance department approvers")
  create_group(name="Onboarding buddies", description="Volunteers who mentor new hires",
               members=[20059, 20033])

Never call this without both name and description.""",
        tags=["groups", "organization", "write"],
        annotations=ToolAnnotations(
            title="Create group",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("create_group")
    @handle_tallyfy_errors("create group")
    def create_group(
        name: GroupName,
        description: str,
        members: Optional[List[int]] = None,
        guests: Optional[List[str]] = None,
    ) -> GenericDict:
        """
        Create a new group in the organization.

        Args:
            name: Group name (REQUIRED — max 200 chars, unique per organization)
            description: Group description (REQUIRED — api-v2's CreateGroupRequest
                declares 'description' => 'required|string', so a create without
                one is rejected with a 422)
            members: List of numeric user IDs to add as members (optional)
            guests: List of guest emails to add as members (optional)

        Returns:
            Created group object
        """
        if not description or not description.strip():
            raise ToolError(
                "description is required and cannot be empty — the Tallyfy API "
                "rejects a group create without one. Provide a short description "
                "of the group's purpose."
            )

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            group = sdk.groups.create_group(
                org_id, name,
                description=description,
                members=members,
                guests=guests,
            )
            return ToolResult(
                content=serialize_dataclass(group) if group else {},
                structured_content=None
            )

    @mcp.tool(
        name="update_group",
        description="Update a group's name, description, or members. REQUIRED: 'group_id'. Plus at least one optional field to update. Never call this without group_id.",
        tags=["groups", "organization", "write"],
        annotations=ToolAnnotations(
            title="Update group",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("update_group")
    @handle_tallyfy_errors("update group")
    def update_group(
        group_id: GroupId,
        name: OptionalString = None,
        description: OptionalString = None,
        members: Optional[List[int]] = None,
        guests: Optional[List[str]] = None,
    ) -> GenericDict:
        """
        Update a group's details.

        Args:
            group_id: Group ID (REQUIRED)
            name: New group name (optional)
            description: New group description (optional)
            members: Updated list of numeric user IDs (optional)
            guests: Updated list of guest emails (optional)

        Returns:
            Updated group object
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            group = sdk.groups.update_group(
                org_id, group_id,
                name=name,
                description=description,
                members=members,
                guests=guests,
            )
            return ToolResult(
                content=serialize_dataclass(group) if group else {},
                structured_content=None
            )

    @mcp.tool(
        name="delete_group",
        description="Delete a group from the organization permanently. REQUIRED: 'group_id'. This action cannot be undone. Never call this without group_id.",
        tags=["groups", "organization", "write"],
        annotations=ToolAnnotations(
            title="Delete group",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("delete_group")
    @handle_tallyfy_errors("delete group")
    def delete_group(group_id: GroupId) -> GenericDict:
        """
        Delete a group from the organization.

        Args:
            group_id: Group ID to delete (REQUIRED)

        Returns:
            Result of the deletion operation
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.groups.delete_group(org_id, group_id)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )