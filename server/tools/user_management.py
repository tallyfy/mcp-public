"""
User Management Tools
Tools for managing organization users
"""

from typing import Optional

from fastmcp.tools.tool import ToolResult
from tallyfy import TallyfySDK
from mcp.types import ToolAnnotations

from utils.fastmcp_types import (
    UserEmail,
    UserName,
    UserRole,
    UserId,
    OptionalString,
    OptionalBool,
    PageNumber,
    GenericDict
)
from utils.fastmcp_errors import handle_tallyfy_errors
from utils.auth_context import get_authenticated_credentials, TALLYFY_API_BASE_URL
from utils.sdk_serializer import serialize_dataclass, serialize_paginated_response
from utils.pagination import fetch_single_page
from metrics import track_tool_execution


def register_user_management_tools(mcp):
    """Register all user management tools with the MCP server"""

    @mcp.tool(
        name="get_me",
        description="""Get the currently authenticated user's profile data. No parameters required.

USE THIS TOOL when user asks:
- "Who am I?"
- "What's my name?"
- "Show my profile"
- "What's my user ID?"
- "What organization am I in?"

Returns the authenticated user's full profile including numeric 'id', 'email', 'first_name', 'last_name', and organization details.""",
        tags={"users", "profile", "read-only", "self"},
        annotations=ToolAnnotations(
            title="Get authenticated user profile",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_me")
    @handle_tallyfy_errors("get authenticated user profile")
    def get_me() -> GenericDict:
        """
        Get the currently authenticated user's profile data.

        Returns:
            Dict with user profile data including id, email, first_name, last_name
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            user = sdk.users.get_current_user_info(org_id)
            result = serialize_dataclass(user) if user else {}
            if result:
                result["organization_id"] = org_id
            return ToolResult(content=result, structured_content=None)

    @mcp.tool(
        name="get_organization_users",
        description="""Get organization members with full profile data. No required parameters.

USE THIS TOOL when user asks:
- "Who are the team members?"
- "List all users"
- "Show me organization members"
- "Find user by name/email" (then search results for the user)

Returns user data including numeric 'id', 'email', 'first_name', 'last_name'.
Use the returned 'id' field when you need to call get_user_tasks(user_id=...).

Optional: Set with_groups=true to include group membership information.
PAGINATION: Returns 20 results per page. Use page=2, page=3, etc. for subsequent pages. meta.total_pages shows how many pages exist.""",
        tags={"users", "organization", "read-only"},
        annotations=ToolAnnotations(
            title="Get organization users",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_organization_users")
    @handle_tallyfy_errors("get organization users")
    def get_organization_users(with_groups: OptionalBool = False, page: PageNumber = 1) -> GenericDict:
        """
        Get organization members with full profile data.

        Args:
            with_groups: Include user groups data (default: False)
            page: Page number to fetch (default: 1)

        Returns:
            Dict with 'data' (list of users) and 'meta' (pagination info)
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            return ToolResult(
                content=fetch_single_page(
                    sdk.users.get_organization_users,
                    org_id,
                    page=page,
                    with_groups=with_groups,
                ),
                structured_content=None
            )

    @mcp.tool(
        name="get_organization_users_list",
        description="Get all organization members with minimal profile data for listing. Returns data with pagination metadata.",
        tags={"users", "organization", "read-only", "minimal"},
        annotations=ToolAnnotations(
            title="Get organization users list",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_organization_users_list")
    @handle_tallyfy_errors("get organization users list")
    def get_organization_users_list() -> GenericDict:
        """
        Get all organization members with minimal data for listing.

        Returns:
            Dict with 'data' (list of users) and 'meta' (pagination info)
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # SDK returns UsersList with .data and .meta attributes
            users_list = sdk.users.get_organization_users_list(org_id)
            return ToolResult(
                content=serialize_paginated_response(users_list),
                structured_content=None
            )

    @mcp.tool(
        name="invite_user_to_organization",
        description="""Invite a new member to the organization.

MANDATORY - ALL THREE required:
1. 'email' - Valid email address
2. 'first_name' - User's first name
3. 'last_name' - User's last name

CORRECT usage:
- invite_user_to_organization(email="john@example.com", first_name="John", last_name="Doe")
- invite_user_to_organization(email="jane@example.com", first_name="Jane", last_name="Smith", role="standard")

WRONG usage (will fail):
- invite_user_to_organization(email="john@example.com") - NO! Missing first_name and last_name
- invite_user_to_organization(first_name="John", last_name="Doe") - NO! Missing email

Optional: 'role' (light/standard/admin, defaults to 'light'), 'message' (custom invitation text).
If user doesn't provide all required info, ASK them before calling this tool.""",
        tags={"users", "organization", "invite", "write"},
        annotations=ToolAnnotations(
            title="Invite user to organization",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("invite_user_to_organization")
    @handle_tallyfy_errors("invite user to organization")
    def invite_user_to_organization(
        email: UserEmail,
        first_name: UserName,
        last_name: UserName,
        role: UserRole = "light",
        message: OptionalString = None,
    ) -> Optional[GenericDict]:
        """
        Invite a member to your organization.

        Args:
            email: Email address of the user to invite (REQUIRED - must be valid email)
            first_name: First name of the user (REQUIRED - must not be empty)
            last_name: Last name of the user (REQUIRED - must not be empty)
            role: User role - 'light', 'standard', or 'admin' (default: 'light')
            message: Custom invitation message (optional)

        Returns:
            Dict with user data for the invited user, or None if invitation failed
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            user = sdk.users.invite_user_to_organization(
                org_id, email, first_name, last_name, role, message
            )
            return ToolResult(
                content=serialize_dataclass(user) if user else {},
                structured_content=None
            )

    @mcp.tool(
        name="get_user",
        description="Get a single user by their numeric ID. REQUIRED: 'user_id' (positive integer). Never call this without user_id.",
        tags={"users", "organization", "read-only"},
        annotations=ToolAnnotations(
            title="Get user",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_user")
    @handle_tallyfy_errors("get user")
    def get_user(user_id: UserId) -> GenericDict:
        """
        Get a single user by ID.

        Args:
            user_id: Numeric user ID (REQUIRED)

        Returns:
            User object with profile data
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            user = sdk.users.get_user(org_id, user_id)
            return ToolResult(
                content=serialize_dataclass(user) if user else {},
                structured_content=None
            )

    @mcp.tool(
        name="create_guest",
        description="Create a new guest in the organization. REQUIRED: 'email' (valid email), 'first_name', 'last_name'. Optional: 'phone', 'company_name'. Never call this without the three required parameters.",
        tags={"users", "guests", "write"},
        annotations=ToolAnnotations(
            title="Create guest",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("create_guest")
    @handle_tallyfy_errors("create guest")
    def create_guest(
        email: UserEmail,
        first_name: UserName,
        last_name: UserName,
        phone: OptionalString = None,
        company_name: OptionalString = None,
    ) -> GenericDict:
        """
        Create a new guest in the organization.

        Args:
            email: Guest's email address (REQUIRED)
            first_name: Guest's first name (REQUIRED)
            last_name: Guest's last name (REQUIRED)
            phone: Guest's phone number (optional)
            company_name: Guest's company name (optional)

        Returns:
            Created guest object
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            guest = sdk.users.create_guest(
                org_id, email, first_name, last_name,
                phone=phone, company_name=company_name,
            )
            return ToolResult(
                content=serialize_dataclass(guest) if guest else {},
                structured_content=None
            )

    @mcp.tool(
        name="update_guest",
        description="Update guest details. REQUIRED: 'email' (valid email). Plus at least one optional field to update: 'first_name', 'last_name', 'phone', 'company_name'. Never call this without email.",
        tags={"users", "guests", "write"},
        annotations=ToolAnnotations(
            title="Update guest",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("update_guest")
    @handle_tallyfy_errors("update guest")
    def update_guest(
        email: UserEmail,
        first_name: OptionalString = None,
        last_name: OptionalString = None,
        phone: OptionalString = None,
        company_name: OptionalString = None,
    ) -> GenericDict:
        """
        Update guest details.

        Args:
            email: Guest's email address (REQUIRED)
            first_name: Updated first name (optional)
            last_name: Updated last name (optional)
            phone: Updated phone number (optional)
            company_name: Updated company name (optional)

        Returns:
            Updated guest object
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            guest = sdk.users.update_guest(
                org_id, email,
                first_name=first_name,
                last_name=last_name,
                phone=phone,
                company_name=company_name,
            )
            return ToolResult(
                content=serialize_dataclass(guest) if guest else {},
                structured_content=None
            )

    @mcp.tool(
        name="disable_guest",
        description="Disable a guest account. REQUIRED: 'email' (valid email). This prevents the guest from accessing the organization. Never call this without email.",
        tags={"users", "guests", "admin"},
        annotations=ToolAnnotations(
            title="Disable guest",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("disable_guest")
    @handle_tallyfy_errors("disable guest")
    def disable_guest(email: UserEmail) -> GenericDict:
        """
        Disable a guest account.

        Args:
            email: Guest's email address (REQUIRED)

        Returns:
            Updated guest object with disabled status
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            guest = sdk.users.disable_guest(org_id, email)
            return ToolResult(
                content=serialize_dataclass(guest) if guest else {},
                structured_content=None
            )

    @mcp.tool(
        name="enable_guest",
        description="Re-enable a disabled guest account. REQUIRED: 'email' (valid email). Never call this without email.",
        tags={"users", "guests", "admin"},
        annotations=ToolAnnotations(
            title="Enable guest",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("enable_guest")
    @handle_tallyfy_errors("enable guest")
    def enable_guest(email: UserEmail) -> GenericDict:
        """
        Re-enable a disabled guest account.

        Args:
            email: Guest's email address (REQUIRED)

        Returns:
            Updated guest object with enabled status
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            guest = sdk.users.enable_guest(org_id, email)
            return ToolResult(
                content=serialize_dataclass(guest) if guest else {},
                structured_content=None
            )

    @mcp.tool(
        name="get_guest",
        description="""Get a single guest's profile by email address.

Returns guest profile data including name, contact info, last accessed time, and status.

NOTE: This returns the guest's profile only, not their tasks. To get tasks
assigned to a guest, use get_guest_tasks(guest_email="...") or
get_guest_tasks(guest_id="...") with the guest_id from this response.

CORRECT usage:
- get_guest(email="guest@example.com")
""",
        tags={"users", "guests", "organization", "read-only"},
        annotations=ToolAnnotations(
            title="Get guest by email",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_guest")
    @handle_tallyfy_errors("get guest")
    def get_guest(email: UserEmail) -> GenericDict:
        """
        Get a single guest by their email address.

        Args:
            email: Guest email address (required)

        Returns:
            Dict with guest profile data (email, name, last_accessed_at, details).
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.users.get_guest(org_id, email)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="change_user_role",
        description="Change a user's role in the organization. REQUIRED: 'user_id' (positive integer) and 'role' ('light', 'standard', or 'admin'). Never call this without both parameters.",
        tags={"users", "admin"},
        annotations=ToolAnnotations(
            title="Change user role",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("change_user_role")
    @handle_tallyfy_errors("change user role")
    def change_user_role(user_id: UserId, role: UserRole) -> GenericDict:
        """
        Change a user's role in the organization.

        Args:
            user_id: Numeric user ID (REQUIRED)
            role: New role ('light', 'standard', or 'admin') (REQUIRED)

        Returns:
            Updated user object
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            user = sdk.users.change_user_role(org_id, user_id, role)
            return ToolResult(
                content=serialize_dataclass(user) if user else {},
                structured_content=None
            )

    @mcp.tool(
        name="disable_user",
        description="Disable a user account. REQUIRED: 'user_id' (positive integer). This prevents the user from accessing the organization. Never call this without user_id.",
        tags={"users", "admin"},
        annotations=ToolAnnotations(
            title="Disable user",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("disable_user")
    @handle_tallyfy_errors("disable user")
    def disable_user(user_id: UserId) -> GenericDict:
        """
        Disable a user account.

        Args:
            user_id: Numeric user ID (REQUIRED)

        Returns:
            Updated user object with disabled status
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            user = sdk.users.disable_user(org_id, user_id)
            return ToolResult(
                content=serialize_dataclass(user) if user else {},
                structured_content=None
            )

    @mcp.tool(
        name="enable_user",
        description="Re-enable a disabled user account. REQUIRED: 'user_id' (positive integer). Never call this without user_id.",
        tags={"users", "admin"},
        annotations=ToolAnnotations(
            title="Enable user",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("enable_user")
    @handle_tallyfy_errors("enable user")
    def enable_user(user_id: UserId) -> GenericDict:
        """
        Re-enable a disabled user account.

        Args:
            user_id: Numeric user ID (REQUIRED)

        Returns:
            Updated user object with enabled status
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            user = sdk.users.enable_user(org_id, user_id)
            return ToolResult(
                content=serialize_dataclass(user) if user else {},
                structured_content=None
            )

    @mcp.tool(
        name="get_organization",
        description="Get organization details. No parameters required — organization is determined from authentication context.",
        tags={"organization", "read-only"},
        annotations=ToolAnnotations(
            title="Get organization",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_organization")
    @handle_tallyfy_errors("get organization")
    def get_organization() -> GenericDict:
        """
        Get organization details.

        Returns:
            Organization object with details
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            org = sdk.organizations.get_organization(org_id)
            return ToolResult(
                content=serialize_dataclass(org) if org else {},
                structured_content=None
            )