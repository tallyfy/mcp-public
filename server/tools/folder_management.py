"""
Folder Management Tools
Tools for discovering and managing organization folders
"""

from fastmcp.tools.tool import ToolResult
from fastmcp.exceptions import ToolError
from tallyfy import TallyfySDK
from mcp.types import ToolAnnotations
from utils.fastmcp_errors import handle_tallyfy_errors
from utils.auth_context import get_authenticated_credentials, TALLYFY_API_BASE_URL
from utils.fastmcp_types import (
    FolderId,
    FolderName,
    FolderObjectId,
    FolderType,
    OptionalString,
    GenericDict,
    GenericList,
)
from utils.sdk_serializer import serialize_dataclass, compact_result
from metrics import track_tool_execution


# Folders are typed at creation time and the type is immutable afterwards.
# api-v2's FolderService::normalizeFolderTypeToClass (app/Services/FolderService.php:18-21)
# maps ONLY the literal 'run' to a process folder — every other value, including
# a missing one, becomes a template (Checklist) folder. A process can therefore
# only be filed into a folder that was created with folder_type='run':
# FolderObjectRequest (app/Http/Requests/Folders/FolderObjectRequest.php) requires
# the target folder to exist with folder_type = Run::class.
_FOLDER_TYPE_ALIASES = {
    # process folders
    "run": "run",
    "runs": "run",
    "process": "run",
    "processes": "run",
    # template folders
    "checklist": "checklist",
    "checklists": "checklist",
    "template": "checklist",
    "templates": "checklist",
    "blueprint": "checklist",
    "blueprints": "checklist",
}


def _normalize_folder_type(folder_type: str | None) -> str:
    """Resolve a caller-supplied folder_type to the API's 'checklist' | 'run'."""
    if not folder_type:
        return "checklist"
    resolved = _FOLDER_TYPE_ALIASES.get(str(folder_type).strip().lower())
    if resolved is None:
        raise ToolError(
            f"folder_type must be 'checklist' (template folder) or 'run' "
            f"(process folder) — got {folder_type!r}"
        )
    return resolved


def register_folder_management_tools(mcp):
    """Register all folder management tools with the MCP server"""

    @mcp.tool(
        name="get_template_folders",
        description=(
            "Get all template/blueprint folders in the organization. "
            "Returns folders that contain templates (folder_type='checklist'). "
            "Use returned folder IDs with launch_process(folders=[<folder_id>]) to launch into a folder. "
            "Results include nested child folders sorted by position. Optional: filter by name with 'q'."
        ),
        tags=["folders", "organization", "read-only", "discovery"],
        annotations=ToolAnnotations(
            title="Get template folders",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_template_folders")
    @handle_tallyfy_errors("get template folders")
    def get_template_folders(q: OptionalString = None) -> GenericList:
        """
        Get all template/blueprint folders in the organization.

        Args:
            q: Optional search query to filter folders by name

        Returns:
            List of template folder objects with id, name, and children
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            folders = sdk.folders.get_folders(org_id, q=q, folder_type='checklist')
            return ToolResult(
                content=compact_result([serialize_dataclass(f) for f in folders]) if folders else [],
                structured_content=None
            )

    @mcp.tool(
        name="get_process_folders",
        description=(
            "Get all process/run folders in the organization. "
            "Returns folders that contain active processes (folder_type='run'). "
            "Use returned folder IDs with get_organization_runs(folder=<folder_id>) to filter processes by folder. "
            "Results include nested child folders sorted by position. Optional: filter by name with 'q'."
        ),
        tags=["folders", "organization", "read-only", "discovery"],
        annotations=ToolAnnotations(
            title="Get process folders",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_process_folders")
    @handle_tallyfy_errors("get process folders")
    def get_process_folders(q: OptionalString = None) -> GenericList:
        """
        Get all process/run folders in the organization.

        Args:
            q: Optional search query to filter folders by name

        Returns:
            List of process folder objects with id, name, and children
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            folders = sdk.folders.get_folders(org_id, q=q, folder_type='run')
            return ToolResult(
                content=compact_result([serialize_dataclass(f) for f in folders]) if folders else [],
                structured_content=None
            )

    @mcp.tool(
        name="create_folder",
        description="""Create a new folder in the organization.

REQUIRED: 'name' (folder name, max 32 chars).

Optional:
- 'folder_type': 'checklist' (DEFAULT — a folder that holds templates/blueprints)
  or 'run' (a folder that holds processes). Also accepts the aliases
  template/blueprint -> checklist and process -> run.
- 'parent_id': 32-char hex ID of a parent folder, for nesting. The parent MUST
  be the same folder_type.

CHOOSING folder_type MATTERS — it is fixed at creation and cannot be changed later:
- To file PROCESSES into it (add_object_to_folder(object_type="run"), or
  get_organization_runs(folder=...)), you MUST create it with folder_type="run".
  A default 'checklist' folder will reject processes.
- To file TEMPLATES into it (add_object_to_folder(object_type="template")), use
  the default folder_type="checklist".

CORRECT usage:
  create_folder(name="Q1 2026", folder_type="run")          # holds processes
  create_folder(name="HR Templates")                        # holds templates
  create_folder(name="Payroll", folder_type="run", parent_id="7c9e6679742540de944be07fc1f90ae7")

Never call this without name.""",
        tags=["folders", "organization", "write"],
        annotations=ToolAnnotations(
            title="Create folder",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("create_folder")
    @handle_tallyfy_errors("create folder")
    def create_folder(
        name: FolderName,
        folder_type: FolderType = "checklist",
        parent_id: OptionalString = None,
    ) -> GenericDict:
        """
        Create a new folder.

        Args:
            name: Folder name (REQUIRED, max 32 characters)
            folder_type: 'checklist' for a template folder (default) or 'run' for a
                process folder. Immutable after creation — a process can only be
                added to a folder created with folder_type='run'.
            parent_id: Parent folder ID for nested folders (optional, must be the
                same folder_type)

        Returns:
            Created folder object
        """
        resolved_type = _normalize_folder_type(folder_type)

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # The SDK's create_folder() cannot send folder_type (it posts only
            # name + parent_id), which is why every SDK-created folder came back
            # as a template folder. Post the body directly instead.
            endpoint = f"organizations/{org_id}/folders"
            body = {"name": name, "folder_type": resolved_type}
            if parent_id is not None:
                body["parent_id"] = parent_id

            response = sdk._make_request("POST", endpoint, data=body)
            folder = response.get("data", response) if isinstance(response, dict) else response
            return ToolResult(
                content=serialize_dataclass(folder) if folder else {},
                structured_content=None
            )

    @mcp.tool(
        name="update_folder",
        description="Update a folder's name or parent. REQUIRED: 'folder_id'. Plus at least one optional field. Never call this without folder_id.",
        tags=["folders", "organization", "write"],
        annotations=ToolAnnotations(
            title="Update folder",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("update_folder")
    @handle_tallyfy_errors("update folder")
    def update_folder(
        folder_id: FolderId,
        name: OptionalString = None,
        parent_id: OptionalString = None,
    ) -> GenericDict:
        """
        Update a folder's name or parent.

        Args:
            folder_id: Folder ID (REQUIRED)
            name: New folder name (optional)
            parent_id: New parent folder ID (optional)

        Returns:
            Updated folder object
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            folder = sdk.folders.update_folder(
                org_id, folder_id,
                name=name,
                parent_id=parent_id,
            )
            return ToolResult(
                content=serialize_dataclass(folder) if folder else {},
                structured_content=None
            )

    @mcp.tool(
        name="delete_folder",
        description="Delete a folder permanently. REQUIRED: 'folder_id'. Contents are NOT deleted — processes/templates are moved out first. Never call this without folder_id.",
        tags=["folders", "organization", "write"],
        annotations=ToolAnnotations(
            title="Delete folder",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("delete_folder")
    @handle_tallyfy_errors("delete folder")
    def delete_folder(folder_id: FolderId) -> GenericDict:
        """
        Delete a folder.

        Args:
            folder_id: Folder ID to delete (REQUIRED)

        Returns:
            Result of the deletion operation
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.folders.delete_folder(org_id, folder_id)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="add_object_to_folder",
        description="""Add a process or template to a folder.

REQUIRED: 'folder_id' (32-char hex), 'object_id' (32-char hex ID of the process or
template), and 'object_type' — 'run' for processes, 'checklist'/'template' for templates.

THE FOLDER'S TYPE MUST MATCH object_type. Folder type is fixed at creation:
- object_type='run' requires a folder created with create_folder(..., folder_type='run').
  Passing a template folder fails with "No such a folder exists in Processes."
  Use get_process_folders() to find valid targets.
- object_type='template'/'checklist' requires a template folder (the create_folder default).
  Use get_template_folders() to find valid targets.

Returns a relation whose integer id is what remove_object_from_folder takes.

Never call this without all three parameters.""",
        tags=["folders", "organization", "write"],
        annotations=ToolAnnotations(
            title="Add object to folder",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("add_object_to_folder")
    @handle_tallyfy_errors("add object to folder")
    def add_object_to_folder(
        folder_id: FolderId,
        object_id: str,
        object_type: str,
    ) -> GenericDict:
        """
        Add an object (process or template) to a folder.

        Args:
            folder_id: Folder ID (REQUIRED)
            object_id: ID of the process or template to add (REQUIRED)
            object_type: Type of object — 'run' for processes, 'checklist' for templates (REQUIRED)

        Returns:
            Created folder-object relation
        """
        # Normalize: "template" is an alias for "checklist"
        if object_type == "template":
            object_type = "checklist"
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.folders.add_object_to_folder(org_id, folder_id, object_id, object_type)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="remove_object_from_folder",
        description="""Remove an object from a folder using the folder-object relation ID.

REQUIRED: 'folder_object_id' — a positive INTEGER (e.g. 12345), NOT a 32-char hex ID.
This is the id of the folder-membership row itself, not the id of the process or
template inside the folder. It is returned by add_object_to_folder and appears as
the 'id' of the entries returned when listing a folder's objects.

CORRECT:   remove_object_from_folder(folder_object_id=12345)
WRONG:     remove_object_from_folder(folder_object_id="7c9e6679742540de944be07fc1f90ae7")  # that's the process id

Never call this without folder_object_id.""",
        tags=["folders", "organization", "write"],
        annotations=ToolAnnotations(
            title="Remove object from folder",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("remove_object_from_folder")
    @handle_tallyfy_errors("remove object from folder")
    def remove_object_from_folder(folder_object_id: FolderObjectId) -> GenericDict:
        """
        Remove an object from a folder.

        Args:
            folder_object_id: Folder-object relation ID (REQUIRED — positive integer
                from core.folder_objects.id, not the process/template hex ID)

        Returns:
            Result of the removal operation
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # The SDK type-guards this argument with isinstance(str); the API path
            # segment is the integer id, so stringify at the boundary.
            result = sdk.folders.remove_object_from_folder(org_id, str(folder_object_id))
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )
