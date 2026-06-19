"""
Process Management Tools
Tools for managing processes and runs
"""

import re
from typing import Any, Dict, List, Optional

from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
from tallyfy import TallyfySDK
from mcp.types import ToolAnnotations
from utils.fastmcp_errors import handle_tallyfy_errors
from utils.auth_context import get_authenticated_credentials, TALLYFY_API_BASE_URL
from utils.fastmcp_types import (
    TemplateId,
    ProcessId,
    OptionalString,
    OptionalBool,
    OptionalInt,
    PageNumber,
    GenericDict,
    GenericList,
)
from utils.sdk_serializer import serialize_dataclass
from utils.pagination import fetch_single_page
from metrics import track_tool_execution


def _is_tallyfy_id(value: str) -> bool:
    """Return True if value looks like a 32-char hex Tallyfy ID."""
    return bool(re.fullmatch(r'[0-9a-f]{32}', value.lower()))


def _resolve_folder_name_to_id(sdk, org_id: str, folder_name: str) -> str:
    """Resolve a folder name to its ID by searching process folders. Returns original value if no match found."""
    try:
        folders = sdk.folders.get_folders(org_id, folder_type='run')
        if folders:
            for f in folders:
                if hasattr(f, "name") and f.name and f.name.lower() == folder_name.lower():
                    return str(f.id)
        raise ToolError(
            f"Folder '{folder_name}' not found. Use get_process_folders to see available process folders."
        )
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(
            f"Could not look up folder '{folder_name}': {e}. "
            f"Provide a folder ID directly, or use get_process_folders to list available folders."
        )


def register_process_management_tools(mcp):
    """Register all process management tools with the MCP server"""

    @mcp.tool(
        name="get_organization_runs",
        description="""Get workflow processes (runs) in the organization. All parameters are optional — call with no parameters to get all runs, or use filters to narrow results.

FILTERS: status, archived, starred, checklist_id (template_id), tag, folder, groups, owners, run_type, me (bool)
RUN STATUS VALUES: "active", "problem", "delayed", "complete"
ARCHIVED: Use archived="only" to get archived processes (NOT status="archived").
RUN TYPE VALUES: "procedure", "form", "document"
FOLDER: Pass folder ID or folder name (name is auto-resolved to ID).
OWNERS: Pass numeric user IDs. Filters by collaborator presence (run owner OR task assignee) — NOT by strict process creator/started_by. Use owners= to find runs the user is involved in, not runs they personally launched.

PAGINATION: Returns 20 results per page. Use page=2, page=3, etc. to retrieve subsequent pages.
meta.total_pages shows how many pages exist. meta.total shows the real count.""",
        tags={"processes", "workflow", "runs", "read-only"},
        annotations=ToolAnnotations(
            title="Get organization runs",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_organization_runs")
    @handle_tallyfy_errors("get organization runs")
    def get_organization_runs(
        with_data: OptionalString = None,
        form_fields_values: OptionalBool = None,
        owners: OptionalString = None,
        task_status: OptionalString = None,
        groups: OptionalString = None,
        status: OptionalString = None,
        folder: OptionalString = None,
        checklist_id: OptionalString = None,
        starred: OptionalBool = None,
        run_type: OptionalString = None,
        tag: OptionalString = None,
        sort: OptionalString = "-created_at",
        archived: OptionalString = None,
        page: PageNumber = 1,
    ) -> GenericDict:
        """
        Get all processes (runs) in the organization.

        Args:
            with_data: Comma-separated data to include (e.g., 'checklist,tasks,assets,tags')
            form_fields_values: Include form field values
            owners: Filter by numeric user IDs (comma-separated). Matches run owner OR task assignees.
            task_status: Filter by task status ('all', 'in-progress', 'completed')
            groups: Filter by group IDs
            status: Filter by process status ('active', 'problem', 'delayed', 'complete')
            folder: Filter by folder ID or folder name (names are auto-resolved to IDs)
            checklist_id: Filter by template ID
            starred: Filter by starred status
            run_type: Filter by type ('procedure', 'form', 'document')
            tag: Filter by tag ID
            sort: Sort order for results (default: '-created_at' for newest first)
            archived: Filter archived processes ('only' = archived only, 'true' = include archived with active)

        Returns:
            Dict with 'data' (list of runs) and 'meta' (pagination info)
        """
        # Translate status="archived" to the correct API parameter
        if status == "archived":
            archived = "only"
            status = None

        # Validate run_type values upfront so the LLM gets a clear error rather
        # than a silent passthrough that returns misleading results (issue #160).
        if run_type is not None and run_type not in ("procedure", "document", "form"):
            raise ToolError(
                f"Invalid run_type '{run_type}'. Valid values: 'procedure', 'document', 'form'."
            )

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # Resolve folder name to ID if a non-ID value was passed
            if folder and not _is_tallyfy_id(folder):
                folder = _resolve_folder_name_to_id(sdk, org_id, folder)

            return ToolResult(
                content=fetch_single_page(
                    sdk.tasks.get_organization_runs,
                    org_id,
                    page=page,
                    compact_fields=["prerun"],
                    with_data=with_data,
                    form_fields_values=form_fields_values,
                    owners=owners,
                    task_status=task_status,
                    groups=groups,
                    status=status,
                    folder=folder,
                    checklist_id=checklist_id,
                    starred=starred,
                    run_type=run_type,
                    tag=tag,
                    sort=sort,
                    archived=archived,
                ),
                structured_content=None
            )

    @mcp.tool(
        name="launch_process",
        meta={
            "openai/toolInvocation/invoking": "Launching your process...",
            "openai/toolInvocation/invoked": "Process launched",
        },
        description="""Launch a new workflow process (run) from a template.

REQUIRED: 'template_id' (32-char hex) and 'name' (process name string). For the name, generate a short descriptive instance name based on the template name and context (e.g. "Onboarding - Jane Doe", "Q1 Budget Review - Marketing"). Do not ask the user for a name unless they want to specify one.

PRERUN vs STEP-FORM-FIELDS — TWO DIFFERENT FORM SURFACES:

  - `prerun` (optional list): KICKOFF FORM fields — initialization data collected
    BEFORE the workflow starts. These are the form fields the user fills in on
    the launch screen (e.g. "Customer name", "Project ID", "Department").
    Defined at the TEMPLATE level (not on a specific step). Use
    `get_kickoff_fields(template_id)` to discover available kickoff field IDs.

    Format: list of {"<kickoff_field_id>": "<value>"} entries.
    Example: prerun=[{"abc123def456...": "Acme Corp"}, {"xyz789abc...": "2026-01-15"}]

  - Step-level form fields (NOT set here — set later via `update_task`):
    Filled in DURING workflow execution as steps run. To set a step's form-field
    values, complete or update the relevant task with `update_task` and pass
    `taskdata={field_id: value, ...}`. These are NOT prerun fields.

  Rule of thumb: If the user provides data BEFORE launching the workflow,
  it goes in `prerun`. If they provide data WHILE doing tasks, it goes in
  `update_task.taskdata`.

CORRECT usage:
  launch_process(template_id="abc123...", name="Onboarding - Jane Doe")
  launch_process(template_id="abc123...", name="Q1 Review", tags=["tag_id"], folders=["folder_id"])
  launch_process(
    template_id="abc123...",
    name="Onboarding - Acme",
    prerun=[{"<customer_name_field_id>": "Acme Corp"}, {"<start_date_field_id>": "2026-01-15"}],
    owner_id=12345,
    folders=["<folder_id>"]
  )

WRONG usage (will fail):
  launch_process(template_id="abc123...")  ← MISSING name
  launch_process(name="Review")  ← MISSING template_id""",
        tags={"processes", "workflow", "runs", "write", "create", "launch"},
        annotations=ToolAnnotations(
            title="Launch process",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("launch_process")
    @handle_tallyfy_errors("launch process")
    def launch_process(
        template_id: TemplateId,
        name: str,
        summary: OptionalString = None,
        owner_id: OptionalInt = None,
        prerun: Optional[List[Any]] = None,
        tags: Optional[List[str]] = None,
        folders: Optional[List[str]] = None,
        users: Optional[List[int]] = None,
        groups: Optional[List[str]] = None,
        is_public: OptionalBool = None,
        tasks: Optional[Dict[str, Any]] = None,
        roles: Optional[List[str]] = None,
        parent_id: OptionalString = None,
    ) -> GenericDict:
        """
        Launch a new workflow process from a template.

        Args:
            template_id: Template ID to launch from (REQUIRED - 32-character hex string)
            name: Name for the new process run (REQUIRED)
            summary: Optional process description
            owner_id: Optional numeric user ID to set as process owner
            prerun: Optional list of kickoff field values, e.g. [{"<field_id>": "value"}]
            tags: Optional list of tag IDs to attach
            folders: Optional list of folder IDs to place the process in
            users: Optional list of user IDs to assign to the process
            groups: Optional list of group IDs to assign to the process
            is_public: Whether the process is publicly accessible (optional)
            tasks: Task assignment overrides dict (optional)
            roles: List of role IDs to assign (optional)
            parent_id: Parent process ID for sub-processes (optional)

        Returns:
            Launched process (run) object with ID, name, status, and task details
        """
        if not name or not name.strip():
            raise ToolError("name cannot be empty")

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.tasks.launch_process(
                org_id=org_id,
                template_id=template_id,
                name=name.strip(),
                summary=summary,
                owner_id=owner_id,
                prerun=prerun,
                tags=tags,
                folders=folders,
                users=users,
                groups=groups,
                is_public=is_public,
                tasks=tasks,
                roles=roles,
                parent_id=parent_id,
            )
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="get_process",
        description="Get full details for a single process (run) by ID. REQUIRED: 'run_id' (32-char hex). Never call this without run_id.",
        tags={"processes", "workflow", "runs", "read-only"},
        annotations=ToolAnnotations(
            title="Get process",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_process")
    @handle_tallyfy_errors("get process")
    def get_process(run_id: ProcessId) -> GenericDict:
        """
        Get full details for a single process (run).

        Args:
            run_id: Process (run) ID (REQUIRED - 32-character hex string)

        Returns:
            Process object with full details including tasks, status, and metadata
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.tasks.get_process(org_id, run_id)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="update_process",
        description="Update a process name, summary, or starred status. REQUIRED: 'run_id' (32-char hex) plus at least one of: 'name', 'summary', or 'starred'. Never call this without run_id.",
        tags={"processes", "workflow", "runs", "write"},
        annotations=ToolAnnotations(
            title="Update process",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("update_process")
    @handle_tallyfy_errors("update process")
    def update_process(
        run_id: ProcessId,
        name: OptionalString = None,
        summary: OptionalString = None,
        starred: OptionalBool = None,
    ) -> GenericDict:
        """
        Update a process's name, summary, or starred status.

        Args:
            run_id: Process (run) ID (REQUIRED - 32-character hex string)
            name: New process name (optional)
            summary: New process description (optional)
            starred: Star or unstar the process (optional)

        Returns:
            Updated process object
        """
        if name is None and summary is None and starred is None:
            raise ToolError("At least one of name, summary, or starred must be provided")

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.tasks.update_process(
                org_id, run_id,
                name=name,
                summary=summary,
                starred=starred,
            )
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="archive_process",
        description="Archive a completed process (run). REQUIRED: 'run_id' (32-char hex). CAUTION: Archived processes are HIDDEN from default views but NOT deleted — all data, tasks, comments, and form-field captures are preserved. Use reactivate_process(run_id) to restore an archived process to active status. To permanently delete a process you must use the universal API fallback (tallyfy_api_call) since no first-class delete tool is exposed. Archived processes can be retrieved via get_organization_runs(archived='only'). Never call this without run_id.",
        tags={"processes", "workflow", "runs", "write", "archive"},
        annotations=ToolAnnotations(
            title="Archive process",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("archive_process")
    @handle_tallyfy_errors("archive process")
    def archive_process(run_id: ProcessId) -> bool:
        """
        Archive a process (run).

        Args:
            run_id: Process (run) ID to archive (REQUIRED - 32-character hex string)

        Returns:
            True if archived successfully
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            return ToolResult(
                content=sdk.tasks.archive_process(org_id, run_id),
                structured_content=None
            )

    @mcp.tool(
        name="reactivate_process",
        description="Reactivate an archived process (run) to make it active again. REQUIRED: 'run_id' (32-char hex). Never call this without run_id.",
        tags={"processes", "workflow", "runs", "write"},
        annotations=ToolAnnotations(
            title="Reactivate process",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("reactivate_process")
    @handle_tallyfy_errors("reactivate process")
    def reactivate_process(run_id: ProcessId) -> GenericDict:
        """
        Reactivate an archived process (run).

        Args:
            run_id: Process (run) ID to reactivate (REQUIRED - 32-character hex string)

        Returns:
            Updated process object with active status
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.tasks.reactivate_process(org_id, run_id)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="reopen_kickoff_form",
        description="Reopen a completed kickoff form to allow edits. REQUIRED: 'run_id' (32-char hex process ID). Never call this without run_id.",
        tags={"processes", "kickoff", "write"},
        annotations=ToolAnnotations(
            title="Reopen kickoff form",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("reopen_kickoff_form")
    @handle_tallyfy_errors("reopen kickoff form")
    def reopen_kickoff_form(run_id: ProcessId) -> GenericDict:
        """
        Reopen a completed kickoff form.

        Args:
            run_id: Process (run) ID (REQUIRED - 32-character hex string)

        Returns:
            Updated process object
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.tasks.reopen_kickoff_form(org_id, run_id)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )