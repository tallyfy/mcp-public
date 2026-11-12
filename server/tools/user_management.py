"""
User Management Tools
Tools for managing organization users
"""

import logging
from typing import Any, Dict, List, Optional

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations

from metrics import track_tool_execution
from utils.auth_context import TALLYFY_API_BASE_URL, get_authenticated_credentials
from utils.fastmcp_errors import handle_tallyfy_errors
from utils.fastmcp_types import (
    GenericDict,
    InvitationMessage,
    OptionalBool,
    OptionalGuestName,
    OptionalString,
    PageNumber,
    PageOffset,
    UserEmail,
    UserId,
    UserName,
    UserRole,
)
from utils.pagination import fetch_single_page
from utils.sdk_client import TallyfySDK
from utils.sdk_serializer import (
    serialize_dataclass,
    serialize_paginated_response,
)

logger = logging.getLogger(__name__)


def _get_guest(sdk, org_id: str, email: str) -> Dict[str, Any]:
    """Read a guest through `sdk.users.get_guest` and serialise it.

    This used to GET the guest raw, because the SDK model could not hold what
    the tool returns: `GuestDetails.from_dict` read `disabled_on`, a key api-v2
    has never sent (#590), and `GuestDetails` had no field for `cadence_days`,
    `associated_members`, `last_city`, `last_country` or the pivot's
    `last_accessed_at`, nor `Guest` for the numeric `id`. SDK 3.2.1 types all of
    them (tallyfy/sdk#33, #169, #264), so the read goes through the public SDK
    method, as tallyfy/mcp#1545 requires, and the answer is unchanged.

    `guest_id` is the guest's access code, which the SDK takes from `link`. It
    is not the numeric `id`: measured live on the test org 2026-09-01,
    `get_guest_tasks(guest_id="48418")`, the numeric id, answered 404 while the
    code from the same guest's `link` answered 200. The tool's description tells
    callers to chain it into `get_guest_tasks`.

    Two things the model carries never reach the answer, so it matches what the
    raw read returned:
    - `details.disabled_on`, the SDK's deprecated alias of `disabled_at`;
    - `extra`, the model's store for keys it has no field for, which MCP must
      not read; `serialize_dataclass` drops it at every depth (#1545).

    `is_disabled` makes the live state explicit. `disabled_at` IS that state:
    `GuestService::disable` sets it and `GuestService::enable` nulls it while
    stamping `reactivated_at`, so `disabled_by` and `reactivated_at` are
    breadcrumbs that survive the opposite operation. `serialize_dataclass`
    strips nulls, so an enabled guest's `disabled_at` vanishes, and absence
    alone could not be told from "the tool cannot see the field". A `False`
    survives. `details.status` is NOT the indicator: neither operation touches
    it. When the guest has no details (no pivot row in this organization), no
    `is_disabled` is emitted at all, because `False` there would report an
    unreadable guest as enabled.
    """
    return _guest_content(sdk.users.get_guest(org_id, email))


def _guest_content(guest: Any) -> Dict[str, Any]:
    """Serialise a Guest the SDK returned, as every guest tool answers it.

    `get_guest`, `create_guest` and `update_guest` all answer through
    GuestsController with GuestTransformer and the organization set
    (createGuest, show and updateGuest in
    app/Http/Controllers/Accounts/GuestsController.php), so one shape serves
    all three; see `_get_guest` for what is dropped and why `is_disabled` is
    added. The SDK never hands back ``None`` here: an answer it cannot read
    raises (``TallyfyUnexpectedResponseError``, or ``TallyfyCreatedButUnreadError``
    after the create), and the error handler tells the caller to read back
    rather than repeat the write.
    """
    content = serialize_dataclass(guest)
    details = content.get("details")
    if isinstance(details, dict):
        details.pop("disabled_on", None)
    if guest.details is not None:
        content["is_disabled"] = bool(guest.details.disabled_at)
    return content


def _guest_after_write(sdk, org_id: str, email: str) -> Dict[str, Any]:
    """Read a guest back after a disable or enable that already succeeded.

    `sdk.users.disable_guest` and `enable_guest` both return a bare `True`, so
    the tools built on them answered `True` while their docstrings promised an
    "Updated guest object with disabled status". api-v2 does return the guest
    from both routes; the SDK simply discards the body.

    Never fatal, for the same reason as `task_management._task_after_write`: the
    write has already happened, so raising here would report a landed change as
    a failure and invite a retry. A failed read-back degrades to the plain
    acknowledgement rather than to a wrong answer about the guest's state.
    """
    try:
        return _get_guest(sdk, org_id, email)
    except Exception as exc:  # noqa: BLE001 - degraded read, never fatal
        logger.warning("Post-write guest read-back failed for %s: %s", email, exc)
        return {}


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

SCOPE: returns ALL members regardless of status (active, invited, and disabled),
so meta.total is the FULL member roster, not an active-member count. Each record
carries a 'status' of 'active', 'invited' or 'disabled'; filter on it yourself
when the user wants only active people. Guests are not members and are never
returned here. This total will normally be LARGER than the 'users_count'
reported by get_organization, which counts active members only.

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
    def get_organization_users(with_groups: OptionalBool = False, page: PageNumber = 1, page_offset: PageOffset = 0) -> GenericDict:
        """
        Get organization members with full profile data.

        Returns every member regardless of status. No status filter is sent, and
        api-v2's OrganizationUsersRepository::query() is the unfiltered
        get_tenant()->users() relation, so active, invited and disabled members
        are all included. Compare with get_organization's 'users_count', which
        counts active members only.

        Args:
            with_groups: Include user groups data (default: False)
            page: Page number to fetch (default: 1)

        Returns:
            Dict with 'data' (list of users, each carrying a 'status' of
            'active', 'invited' or 'disabled') and 'meta' (pagination info)
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            return ToolResult(
                content=fetch_single_page(
                    sdk.users.get_organization_users,
                    org_id,
                    page=page, page_offset=page_offset or 0,
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
        message: InvitationMessage = None,
    ) -> Optional[GenericDict]:
        """
        Invite a member to your organization.

        Args:
            email: Email address of the user to invite (REQUIRED - must be valid email)
            first_name: First name of the user (REQUIRED - must not be empty)
            last_name: Last name of the user (REQUIRED - must not be empty)
            role: User role - 'light', 'standard', or 'admin' (default: 'light')
            message: Custom invitation message (optional, max 5000 characters)

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
        description="Get a single user by their numeric ID. REQUIRED: 'user_id' (positive integer).",
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
        description="""Create a new guest (external collaborator) in the organization.

REQUIRED: 'email' (valid email). That is the only required field.

Optional but STRONGLY PREFERRED: 'first_name', 'last_name' (max 200 chars each). The API
accepts a guest with neither, and a nameless guest shows as just an email address
everywhere in Tallyfy. Supply them when you know them; do NOT invent placeholders and do
not stall to interrogate the user for names you were not given.

Optional: 'phone_1' (primary phone, max 20 chars), 'phone_2' (secondary phone,
max 20 chars), 'company_name' (max 200 chars).

NOTE the phone parameter names: the API stores two numbered phone fields,
'phone_1' and 'phone_2'. There is no plain 'phone' field: a value sent as
'phone' is silently discarded and the guest is created without it.

CORRECT usage:
  create_guest(email="alice@vendor.com", first_name="Alice", last_name="Smith")
  create_guest(email="bob@vendor.com", first_name="Bob", last_name="Jones",
               phone_1="+1 314 555 0100", company_name="Vendor Inc")
  create_guest(email="carol@vendor.com")   # legal: email is the only required field""",
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
        first_name: OptionalGuestName = None,
        last_name: OptionalGuestName = None,
        phone_1: OptionalString = None,
        phone_2: OptionalString = None,
        company_name: OptionalString = None,
    ) -> GenericDict:
        """
        Create a new guest in the organization.

        Args:
            email: Guest's email address (REQUIRED — the only required field)
            first_name: Guest's first name (optional, max 200 chars; omitted if blank)
            last_name: Guest's last name (optional, max 200 chars; omitted if blank)
            phone_1: Guest's primary phone number (optional, max 20 chars)
            phone_2: Guest's secondary phone number (optional, max 20 chars)
            company_name: Guest's company name (optional, max 200 chars)

        Returns:
            Created guest object
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # SDK 4.0's create_guest sends exactly the keys
            # CreateGuestRequest::rules() names (tallyfy/sdk#168): phone_1 and
            # phone_2, not the singular "phone" api-v2 drops at 201. A blank
            # name is left out rather than posted as "", because names are
            # nullable at the API (#622 item 8) and "" would store an empty
            # name where the customer expects none.
            guest = sdk.users.create_guest(
                org_id, email,
                first_name=first_name,
                last_name=last_name,
                phone_1=phone_1,
                phone_2=phone_2,
                company_name=company_name,
            )
            return ToolResult(content=_guest_content(guest), structured_content=None)

    @mcp.tool(
        name="update_guest",
        description="""Set which organization members a guest is associated with.

REQUIRED: 'email' (the guest to update) and 'associated_members' (list of numeric
member user IDs, pass [] to clear). 'associated_members' REPLACES the whole list,
it does not append.

THIS TOOL CANNOT EDIT A GUEST'S PROFILE. The update-guest endpoint accepts only the
guest's email (to identify them) and associated_members; the API validates nothing
else, so first_name, last_name, phone and company_name CANNOT be changed here: a
request carrying them is accepted with a success status and those values are
silently discarded. Do not tell the user a name or phone was updated.

To change a guest's name/phone/company today: delete the guest and re-create them
with create_guest, or edit the guest in the Tallyfy web UI.

CORRECT usage:
  update_guest(email="alice@vendor.com", associated_members=[20059, 20033])
  update_guest(email="alice@vendor.com", associated_members=[])   # clear

Use get_guest(email=...) to read a guest's current profile.""",
        tags={"users", "guests", "write"},
        annotations=ToolAnnotations(
            title="Set guest's associated members",
            readOnlyHint=False,
            # destructiveHint (#1020, per #653's rule): associated_members is REQUIRED and replaces the whole list, so every call
            # replaces and [] clears the association outright.
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("update_guest")
    @handle_tallyfy_errors("update guest")
    def update_guest(
        email: UserEmail,
        associated_members: Optional[List[int]] = None,
    ) -> GenericDict:
        """
        Set the organization members associated with a guest.

        Only ``associated_members`` is editable. api-v2's UpdateGuestRequest
        (app/Http/Requests/Guests/UpdateGuestRequest.php) declares just
        ``email`` (required|exists) and ``associated_members``; the controller
        passes ``$request->onlyValidatedFields()`` — i.e. ``validator->validated()``
        (BaseRequest.php:139-141) — into the service, which strips every key the
        rules do not mention. Profile fields sent here would be dropped without
        an error, so this tool does not advertise them.

        Args:
            email: Guest's email address (REQUIRED)
            associated_members: Full replacement list of numeric member user IDs
                (REQUIRED. Pass [] to clear the association)

        Returns:
            Updated guest object
        """
        if associated_members is None:
            raise ToolError(
                "associated_members is required. It is the only field this endpoint "
                "can change. Pass a list of numeric member user IDs, or [] to clear. "
                "A guest's first_name / last_name / phone / company_name cannot be "
                "updated through the API; re-create the guest or edit them in the "
                "Tallyfy web UI instead."
            )

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # The one field the route stores (tallyfy/sdk#169), and the only
            # one this tool sends.
            guest = sdk.users.update_guest(
                org_id, email, associated_members=associated_members
            )
            return ToolResult(content=_guest_content(guest), structured_content=None)

    @mcp.tool(
        name="disable_guest",
        description="Disable a guest account. REQUIRED: 'email' (valid email). This prevents the guest from accessing the organization.",
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
            The guest as it now stands, read back after the write, with
            `is_disabled` true. An empty dict means the disable itself
            succeeded and only the confirming read failed; it does NOT mean
            the guest is still enabled.
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            sdk.users.disable_guest(org_id, email)
            # The SDK returns a bare `True`, so without this read-back the tool
            # answers `True` while its own docstring promises a guest object
            # carrying the new state. #590 is about exactly that gap.
            return ToolResult(
                content=_guest_after_write(sdk, org_id, email),
                structured_content=None
            )

    @mcp.tool(
        name="enable_guest",
        description="Re-enable a disabled guest account. REQUIRED: 'email' (valid email).",
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
            The guest as it now stands, read back after the write, with
            `is_disabled` false. An empty dict means the enable itself
            succeeded and only the confirming read failed.
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            sdk.users.enable_guest(org_id, email)
            # Sibling of disable_guest (rule 16). Same bare `True` from the SDK,
            # same read-back, so `is_disabled` flips back to False visibly.
            return ToolResult(
                content=_guest_after_write(sdk, org_id, email),
                structured_content=None
            )

    @mcp.tool(
        name="get_guest",
        description="""Get a single guest's profile by email address.

Returns name, contact info, last accessed time, and whether the guest is
currently disabled.

WHETHER A GUEST IS DISABLED: read the top-level 'is_disabled' boolean. It is
always present and it is the answer. 'details.disabled_at' carries the
timestamp when there is one.

Do NOT read 'details.status' for this. It is a separate value that
disable_guest and enable_guest never touch, and it is often empty.
'details.disabled_by' and 'details.reactivated_at' are history, not current
state: each one survives the opposite operation, so a guest who was disabled
and then re-enabled still carries a disabled_by.

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
            Dict with guest profile data (email, name, last_accessed_at,
            details), plus a top-level `is_disabled` boolean derived from
            `details.disabled_at`. `is_disabled` is omitted only when the
            response carried no `details` object at all, which means the read
            could not answer rather than that the guest is enabled.
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            return ToolResult(
                content=_get_guest(sdk, org_id, email),
                structured_content=None
            )

    @mcp.tool(
        name="change_user_role",
        description="Change a user's role in the organization. REQUIRED: 'user_id' (positive integer) and 'role' ('light', 'standard', or 'admin').",
        tags={"users", "admin"},
        annotations=ToolAnnotations(
            title="Change user role",
            readOnlyHint=False,
            # destructiveHint (#1020, per #653's rule): replaces the stored role, and a demotion revokes the access the previous
            # role carried.
            destructiveHint=True,
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
        description="Disable a user account. REQUIRED: 'user_id' (positive integer). This prevents the user from accessing the organization.",
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
        description="Re-enable a disabled user account. REQUIRED: 'user_id' (positive integer).",
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
        description="""Get organization details. No parameters required; the organization is determined from the authentication context.

Returns the organization profile, including 'users_count'.

IMPORTANT: 'users_count' IS AN ACTIVE-MEMBER COUNT, NOT TOTAL HEADCOUNT.
It counts only members with a verified email, an approved membership, an
accepted invite or a prior sign-in, and no disabled flag.

EXCLUDED from 'users_count' but PRESENT in get_organization_users, which is
exactly why the two numbers differ:
- members invited who never accepted and never signed in (status 'invited')
- disabled members (status 'disabled')
- members whose email address is still unverified
- bot and system accounts

Absent from BOTH: guests (they are not members at all) and members who were
removed from the organization.

Do NOT report 'users_count' as the total number of people in the organization.
It will normally be SMALLER than the number of records returned by
get_organization_users, which returns every member of every status. That gap is
correct behaviour, not a data bug, and it is not a reason to retry either tool.
When the user asks for total headcount or a breakdown by status, call
get_organization_users and count its records by their per-record 'status'.""",
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
            Organization object with details. Its 'users_count' is an
            ACTIVE-member count (api-v2 OrganizationTransformer.php:40 calls
            Organization::totalActiveUsers(), which counts
            Organization::activeUsers() at Organization.php:530). Invited,
            disabled and email-unverified members plus bot accounts are
            excluded, so it is smaller than the record count from
            get_organization_users. Guests and removed members are absent from
            both, since Organization::users() at Organization.php:269-296 is a
            members-only relation already filtered on
            organizations_users.deleted_at.
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            org = sdk.organizations.get_organization(org_id)
            return ToolResult(
                content=serialize_dataclass(org) if org else {},
                structured_content=None
            )