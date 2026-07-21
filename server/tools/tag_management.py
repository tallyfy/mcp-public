"""
Tag Management Tools
Tools for discovering and managing organization tags
"""
from fastmcp.tools.tool import ToolResult
from tallyfy import TallyfySDK
from mcp.types import ToolAnnotations
from utils.fastmcp_errors import handle_tallyfy_errors
from utils.auth_context import get_authenticated_credentials, TALLYFY_API_BASE_URL
from utils.fastmcp_types import (
    TagId,
    TagTitle,
    TemplateId,
    ProcessId,
    OptionalString,
    OptionalBool,
    GenericDict,
    GenericList,
)
from utils.sdk_serializer import serialize_dataclass, compact_result
from metrics import track_tool_execution


def register_tag_management_tools(mcp):
    """Register all tag management tools with the MCP server"""

    @mcp.tool(
        name="get_tags",
        description="Get all tags in the organization. Use tags to filter processes via get_organization_runs(tag=<tag_id>). All parameters are optional.",
        tags=["tags", "organization", "read-only", "discovery"],
        annotations=ToolAnnotations(
            title="Get tags",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_tags")
    @handle_tallyfy_errors("get tags")
    def get_tags(
        q: OptionalString = None,
        status: OptionalString = None,
        auto_generated: OptionalBool = None,
    ) -> GenericList:
        """
        Get all tags in the organization.

        Args:
            q: Optional search query to filter tags by name
            status: Optional status filter
            auto_generated: Optional filter — True for auto-generated tags only, False for manual only

        Returns:
            List of tag objects with id, title, color, and usage counts
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            tags = sdk.tags.get_tags(org_id, q=q, status=status, auto_generated=auto_generated)
            return ToolResult(
                content=compact_result([serialize_dataclass(t) for t in tags]) if tags else [],
                structured_content=None
            )

    @mcp.tool(
        name="create_tag",
        description="Create a new tag in the organization. REQUIRED: 'title' (tag name). Optional: 'color' (hex color code like '#FF5733'). Never call this without title.",
        tags=["tags", "organization", "write"],
        annotations=ToolAnnotations(
            title="Create tag",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("create_tag")
    @handle_tallyfy_errors("create tag")
    def create_tag(title: TagTitle, color: OptionalString = None) -> GenericDict:
        """
        Create a new tag.

        Args:
            title: Tag title/name (REQUIRED)
            color: Optional hex color code (e.g., '#FF5733')

        Returns:
            Created tag object
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            tag = sdk.tags.create_tag(org_id, title, color=color)
            return ToolResult(
                content=serialize_dataclass(tag) if tag else {},
                structured_content=None
            )

    @mcp.tool(
        name="update_tag",
        description="Update a tag's title or color. REQUIRED: 'tag_id'. Plus at least one optional field. Never call this without tag_id.",
        tags=["tags", "organization", "write"],
        annotations=ToolAnnotations(
            title="Update tag",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("update_tag")
    @handle_tallyfy_errors("update tag")
    def update_tag(
        tag_id: TagId,
        title: OptionalString = None,
        color: OptionalString = None,
    ) -> GenericDict:
        """
        Update a tag's title or color.

        Args:
            tag_id: Tag ID (REQUIRED)
            title: New tag title (optional)
            color: New hex color code (optional)

        Returns:
            Updated tag object
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            tag = sdk.tags.update_tag(org_id, tag_id, title=title, color=color)
            return ToolResult(
                content=serialize_dataclass(tag) if tag else {},
                structured_content=None
            )

    @mcp.tool(
        name="delete_tag",
        description="Delete a tag from the organization permanently. REQUIRED: 'tag_id'. This action cannot be undone. Never call this without tag_id.",
        tags=["tags", "organization", "write"],
        annotations=ToolAnnotations(
            title="Delete tag",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("delete_tag")
    @handle_tallyfy_errors("delete tag")
    def delete_tag(tag_id: TagId) -> GenericDict:
        """
        Delete a tag.

        Args:
            tag_id: Tag ID to delete (REQUIRED)

        Returns:
            Result of the deletion operation
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.tags.delete_tag(org_id, tag_id)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="tag_template",
        description="Assign a tag to a template. REQUIRED: 'template_id' (32-char hex) and 'tag_id'. Never call this without both parameters.",
        tags=["tags", "templates", "write"],
        annotations=ToolAnnotations(
            title="Tag template",
            readOnlyHint=False,
            destructiveHint=False,
            # NOT idempotent. The pivot row IS deduped (TagsChecklistsRepository::create
            # returns the existing row, plus a UNIQUE index on (subject_id, tag_id)), but
            # TagChecklistService::storeTagChecklist dispatches 'tag.created' and
            # 'checklist.update' UNCONDITIONALLY, outside that guard. Because the activity
            # feed only dedupes verb='updated', each repeat call appends a new audit row.
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("tag_template")
    @handle_tallyfy_errors("tag template")
    def tag_template(template_id: TemplateId, tag_id: TagId) -> GenericDict:
        """
        Assign a tag to a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            tag_id: Tag ID to assign (REQUIRED)

        Returns:
            Result of the tagging operation
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.tags.tag_template(org_id, template_id, tag_id)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="untag_template",
        description="Remove a tag from a template. REQUIRED: 'tag_id' and 'template_id'. Never call this without both parameters.",
        tags=["tags", "templates", "write"],
        annotations=ToolAnnotations(
            title="Untag template",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("untag_template")
    @handle_tallyfy_errors("untag template")
    def untag_template(tag_id: TagId, template_id: TemplateId) -> GenericDict:
        """
        Remove a tag from a template.

        Args:
            tag_id: Tag ID to remove (REQUIRED - 32-character hex string)
            template_id: Template ID the tag is attached to (REQUIRED - 32-character hex string)

        Returns:
            Result of the untagging operation
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.tags.untag_template(org_id, tag_id, template_id=template_id)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="tag_process",
        description="Assign a user-defined tag to a running process. REQUIRED: 'run_id' (32-char hex) and 'tag_id'. Multiple tags per process are allowed — call this tool repeatedly with the same run_id and different tag_ids to apply more than one. Re-tagging with an already-applied tag_id does NOT create a duplicate tag, but it is not a full no-op: it re-fires the tag event and appends an audit-trail entry, so avoid redundant calls. Tags are used for filtering (search_for_processes), grouping in dashboards, and organizing processes by team/department/category. To create a new tag first, use create_tag; to remove a tag, use untag_process. Never call this without both parameters.",
        tags=["tags", "processes", "write"],
        annotations=ToolAnnotations(
            title="Tag process",
            readOnlyHint=False,
            destructiveHint=False,
            # NOT idempotent — same reason as tag_template. For runs the service ALSO
            # calls Run::recalcSearchVector() unconditionally on every repeat call.
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("tag_process")
    @handle_tallyfy_errors("tag process")
    def tag_process(run_id: ProcessId, tag_id: TagId) -> GenericDict:
        """
        Assign a tag to a running process.

        Args:
            run_id: Process (run) ID to tag (REQUIRED - 32-character hex string)
            tag_id: Tag ID to assign (REQUIRED)

        Returns:
            Result of the tagging operation
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.tags.tag_process(org_id, run_id, tag_id)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="untag_process",
        description="Remove a tag from a running process. REQUIRED: 'run_id' (32-char hex) and 'tag_id'. Never call this without both parameters.",
        tags=["tags", "processes", "write"],
        annotations=ToolAnnotations(
            title="Untag process",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("untag_process")
    @handle_tallyfy_errors("untag process")
    def untag_process(run_id: ProcessId, tag_id: TagId) -> GenericDict:
        """
        Remove a tag from a running process.

        Args:
            run_id: Process (run) ID to untag (REQUIRED - 32-character hex string)
            tag_id: Tag ID to remove (REQUIRED)

        Returns:
            Result of the untagging operation
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.tags.untag_process(org_id, run_id, tag_id)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )
