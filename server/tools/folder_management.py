"""
Folder Management Tools
Tools for discovering and managing organization folders
"""

from fastmcp.tools.tool import ToolResult
from tallyfy import TallyfySDK
from mcp.types import ToolAnnotations
from utils.fastmcp_errors import handle_tallyfy_errors
from utils.auth_context import get_authenticated_credentials, TALLYFY_API_BASE_URL
from utils.fastmcp_types import (
    FolderId,
    FolderName,
    FolderObjectId,
    OptionalString,
    GenericDict,
    GenericList,
)
from utils.sdk_serializer import serialize_dataclass, compact_result
from metrics import track_tool_execution


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
        description="Create a new folder in the organization. REQUIRED: 'name' (folder name). Optional: 'parent_id' (ID of parent folder for nesting). Never call this without name.",
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
        parent_id: OptionalString = None,
    ) -> GenericDict:
        """
        Create a new folder.

        Args:
            name: Folder name (REQUIRED)
            parent_id: Parent folder ID for nested folders (optional)

        Returns:
            Created folder object
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            folder = sdk.folders.create_folder(org_id, name, parent_id=parent_id)
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
        description="Add a process or template to a folder. REQUIRED: 'folder_id', 'object_id' (32-char hex ID of the process or template), and 'object_type' ('run' for processes, 'checklist' or 'template' for templates). Never call this without all three parameters.",
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
        description="Remove an object from a folder using the folder-object relation ID. REQUIRED: 'folder_object_id' (the ID of the folder-object relationship, not the object ID itself). Never call this without folder_object_id.",
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
            folder_object_id: Folder-object relation ID (REQUIRED)

        Returns:
            Result of the removal operation
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.folders.remove_object_from_folder(org_id, folder_object_id)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )
