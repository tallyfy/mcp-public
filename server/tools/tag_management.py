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
    PageNumber,
    PageSize,
)
from utils.sdk_serializer import serialize_dataclass, compact_result
from metrics import track_tool_execution


def register_tag_management_tools(mcp):
    """Register all tag management tools with the MCP server"""

    @mcp.tool(
        name="get_tags",
        description=(
            "Get ONE PAGE of the organization's tags. Use tags to filter processes via "
            "get_organization_runs(tag=<tag_id>). All parameters are optional. "
            "PAGINATION: this returns at most 'per_page' tags (default 100, which is also the "
            "API maximum), NOT every tag the organization owns, and the response carries no "
            "total count. So read a full page as 'there is probably more': if the list you get "
            "back holds exactly per_page items, call again with page=2, then page=3, until a "
            "page comes back with fewer than per_page items or empty. An organization with 110 "
            "tags returns 100 items on page=1 and 10 on page=2. If you are hunting for one tag "
            "you already know the name of, q=<substring> is faster and cheaper than paging."
        ),
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
        page: PageNumber = 1,
        per_page: PageSize = 100,
        q: OptionalString = None,
        status: OptionalString = None,
        auto_generated: OptionalBool = None,
    ) -> GenericList:
        """
        Get one page of the organization's tags.

        The Tallyfy tags endpoint is paginated and caps a page at 100 items. Before
        issue #589 this tool exposed no page controls at all, so every caller was
        pinned to the SDK's default first page of 100 and any tag beyond that was
        unreachable through MCP. An org holding 110 tags looked like an org holding
        100, with no signal that anything was missing.

        The return shape is deliberately unchanged: a bare LIST of tag objects, the
        same as before. Wrapping it in {data, meta} would have handed the caller a
        total count, but it is an unversioned breaking change for every external MCP
        client already parsing the list, so the page-exhaustion rule lives in the tool
        description instead.

        Args:
            page: 1-based page number (default: 1)
            per_page: Items per page (default: 100, which is the API maximum). A full
                page means there are probably more; request the next page.
            q: Optional search query to filter tags by name
            status: Optional status filter
            auto_generated: Optional filter — True for auto-generated tags only, False for manual only

        Returns:
            List of tag objects with id, title, color, and usage counts. This is one
            page, not the whole set.
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            tags = sdk.tags.get_tags(
                org_id,
                page=page,
                per_page=per_page,
                q=q,
                status=status,
                auto_generated=auto_generated,
            )
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
        description="Assign a tag to a template. REQUIRED: 'template_id' (32-char hex) and 'tag_id'. ACTIVE TEMPLATES ONLY: an archived template cannot be tagged. Tallyfy resolves the subject with a lookup that skips archived (soft-deleted) templates, so the call fails with the validation error 'Subject not found'. That is the identical message a wrong or unknown template_id produces, so read it as 'archived OR unknown', never as proof the template does not exist. get_template does NOT settle it: that read skips archived templates too and answers 404 in both cases. Only an archived listing tells them apart (GET organizations/<org>/checklists?archived=only), and un-archiving is PUT organizations/<org>/checklists/<template_id>/restore. Neither has a first-class tool here, so reach for tallyfy_api_read / tallyfy_api_write if they are enabled, or ask the user to restore the template in Tallyfy. Never call this without both parameters.",
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

        Only ACTIVE templates can be tagged. api-v2 validates the subject through the
        custom `tag_subject` rule (AddTagChecklistRequest::rules() ->
        HelpersManager::loadCustomValidationRules()), whose Checklist branch is
        `Checklist::where('id', aliveID($subject))->exists()`. `aliveID()` runs
        `DB::table('classes')->where('timeline_id', $tid)->whereNull('deleted_at')
        ->first(['id'])`, so an archived template has deleted_at set and is found by
        nothing; called without $strict it returns null instead of throwing, and the
        branch then evaluates `Checklist::where('id', null)->exists()`, which is false.
        Both paths land on the rule's single generic message 'Subject not found' - the
        same message a wrong or unknown template_id produces. The clearer 'Cannot update
        archived processes.' that the same request class carries lives in postValidate(),
        which BaseRequest only reaches once the rules have passed, and it covers the Run
        branch alone, so it can never fire here.

        get_template cannot break the tie either. Its GET /checklists/{id} goes through
        ChecklistsRepository::findByTimeline, which is
        `whereNull('checklists.deleted_at')->firstOrFail()`, so an archived template 404s
        exactly as an unknown one does. The archived listing (?archived=only, which api-v2
        serves via `onlyTrashed()`) is what distinguishes them, and PUT .../restore is what
        makes the tag call work. Neither is exposed as a first-class tool from this server.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string).
                Must be an ACTIVE template; an archived one fails with 'Subject not found'.
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
        description="Assign a user-defined tag to a running process. REQUIRED: 'run_id' (32-char hex) and 'tag_id'. Multiple tags per process are allowed — call this tool repeatedly with the same run_id and different tag_ids to apply more than one. Re-tagging with an already-applied tag_id does NOT create a duplicate tag, but it is not a full no-op: it re-fires the tag event and appends an audit-trail entry, so avoid redundant calls. Tags are used for filtering (search_for_processes), grouping in dashboards, and organizing processes by team/department/category. To create a new tag first, use create_tag; to remove a tag, use untag_process. ACTIVE PROCESSES ONLY: an archived process cannot be tagged. Tallyfy validates the subject with a query that excludes archived (soft-deleted) processes, so the call fails with the validation error 'Subject not found'. That is the identical message a wrong or unknown run_id produces, so read it as 'archived OR unknown', never as proof the process does not exist. Confirm with get_process, and call reactivate_process first if the process is archived. Never call this without both parameters.",
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

        Only ACTIVE processes can be tagged. api-v2 validates the subject through the
        custom `tag_subject` rule (AddTagChecklistRequest::rules() ->
        HelpersManager::loadCustomValidationRules(), which runs
        `Run::where('id', $subject)->exists()`). `Run` inherits SoftDeletes via BaseModel
        and archiving a process is a soft delete, so an archived process is excluded and
        the rule fails with its generic message 'Subject not found' - the same message a
        wrong or unknown run_id produces. The clearer 'Cannot update archived processes.'
        that the same request class carries lives in postValidate(), which BaseRequest
        only reaches once the rules have passed, so it never fires on this path. The
        caller therefore cannot tell "archived" from "no such process" by the error alone;
        the tool description tells them to check get_process and reactivate_process.

        Args:
            run_id: Process (run) ID to tag (REQUIRED - 32-character hex string).
                Must be an ACTIVE process; an archived one fails with 'Subject not found'.
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
