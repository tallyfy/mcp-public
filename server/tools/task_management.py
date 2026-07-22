"""
Task Management Tools
Tools for managing tasks and task assignments
"""

import logging
from typing import List, Dict, Any, Optional, Union

from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
from tallyfy import TallyfySDK, TaskOwners
from tallyfy.models import Task
from mcp.types import ToolAnnotations
from utils.date_utils import DateExtractor
from utils.fastmcp_errors import handle_tallyfy_errors
from utils.auth_context import get_authenticated_credentials, TALLYFY_API_BASE_URL
from utils.fastmcp_types import (
    OptionalUserId,
    OptionalGuestId,
    ProcessId,
    TaskId,
    TaskTitle,
    NaturalLanguageInput,
    OptionalString,
    OptionalInt,
    OptionalBool,
    PageNumber,
)
from utils.sdk_serializer import serialize_dataclass, serialize_task
from utils.pagination import fetch_single_page
from metrics import track_tool_execution


_OWNER_BUCKETS = ("users", "guests", "groups")


def _completed_owner_buckets(sdk, endpoint: str, task_id: str,
                             owners: Dict[str, Any]) -> Dict[str, Any]:
    """Fill in any owner bucket the caller left out, so the API keeps it.

    Both task update paths reach the same gate:
    TaskFactory::buildFromRequest and TaskFactory::editBasicTask
    (app/Domain/Task/TaskFactory.php:108-110, the one-off route via
    OneOffTaskService::edit:87) test ``is_array(Arr::get($data, 'owners'))``,
    so omitting `owners` WHOLESALE is safe and stays safe. The danger is the
    PARTIAL shape: once `owners` is present, BaseAssignees::newFromArray
    (app/Domain/Owners/BaseAssignees.php:24) reads each bucket as
    ``$data['users'] ?? []``, so any bucket the caller left out becomes an
    EMPTY SET rather than "leave alone". Task::saveAssignees
    (app/Models/Task.php:2054, :2058, :2060-2063) then detaches every user,
    guest and group in the missing buckets.

    This tool's own documented example, owners={"users":[123],"guests":[]},
    omits `groups` and so silently detached every group from the task.

    The fix is to COMPLETE a partially-supplied `owners`, not to always send
    one. Buckets the caller did supply pass through verbatim, including a
    deliberate empty list (that is a real "unassign everyone here" request).
    Only the absent buckets are read back and re-sent.

    TaskTransformer.php:49-52 emits all three buckets unconditionally, so a
    missing or non-list bucket on the READ means the read failed, and sending
    [] on a failed read is exactly the wipe this guard exists to prevent.
    Abort instead of guessing.
    """
    missing = [b for b in _OWNER_BUCKETS if b not in owners]
    if not missing:
        return owners

    current = sdk._make_request("GET", endpoint)
    task = current.get("data", current) if isinstance(current, dict) else {}
    current_owners = task.get("owners")
    if not isinstance(current_owners, dict):
        raise ToolError(
            f"Task {task_id} did not return its current owners, so updating "
            f"only part of 'owners' would detach every assignee in the "
            f"buckets you left out ({', '.join(missing)}). Nothing was sent."
        )

    completed = dict(owners)
    for bucket in missing:
        value = current_owners.get(bucket)
        if not isinstance(value, list):
            raise ToolError(
                f"Task {task_id} did not return its current '{bucket}', so "
                f"updating 'owners' without it would detach everything in "
                f"that bucket. Nothing was sent."
            )
        completed[bucket] = list(value)
    return completed


# ---------------------------------------------------------------------------
# taskdata value shapes
# ---------------------------------------------------------------------------
#
# `taskdata` is a flat dict keyed by the form field's `id` (the API returns the
# capture's timeline_id under that name — CaptureTransformer.php:12), whose
# value is passed STRAIGHT into validation with no unwrapping:
# TaskRequestValidator::validateFormFieldsValues (app/Http/Requests/Tasks/
# TaskRequestValidator.php:31-38) iterates `$taskdata as $captureTimelineId =>
# $values` and hands `$values` to validateFormField.
#
# There is NO {"value": ...} envelope, but the failure mode is NOT uniform —
# verified by live round-trip, not by reading the validator:
#   - text, textarea, date, dropdown, radio, multiselect, assignees_form
#     REJECT a wrapper with a 422 (a dict is not a scalar; a dropdown needs id+text).
#   - `email` ACCEPTS it with a 200 and stores {"value": "..."} VERBATIM. This is
#     silent corruption and is exactly the failure class of the prerun incident.
#   - `file` returns a 500 (Task.php:1654 foreachs the value).
#
# The per-type shapes below are the switch arms of
# app/Http/Requests/Captures/FormValuesValidator.php:20-119. Note that:
#   - text/textarea/date are THREE different rules, not one: text is is_scalar
#     (ints/bools stored as-is), textarea is strictly is_string (12345 -> 422),
#     date must be a parseable string (an epoch int -> 422).
#   - dropdown and radio are deliberately asymmetric: dropdown validates {id,text}
#     as a pair, radio only checks the scalar is one of the option texts.
#   - `file` and `email` have no case in the switch, but "unvalidated" does NOT
#     mean "accepts anything" — see the wrapper behaviour above. `file` in
#     particular needs a LIST of file objects or the storage layer 500s.
#   - multiselect entries need "selected": true for RENDERING, not validation:
#     VariableReplacement.php:270 skips any entry without it, so the field renders
#     EMPTY wherever it is used as a variable. (`must_all_checked` is NOT the
#     reason — it only fires from MarkTaskCompleteRequest.php:52, never on this path.)
_TASKDATA_SHAPE_HELP = """FORM FIELD VALUES ('taskdata') — dict keyed by the form field's id. Send the value ITSELF:
a {"value":...} wrapper is REJECTED by most types, stored VERBATIM by email (silent
corruption), and 500s file. Never send one.
  text        -> scalar e.g. "Acme Corp"  ·  textarea -> string only (12345 -> 422)
  date        -> "2026-03-01 09:00:00" (epoch int -> 422)  ·  email -> "a@b.com"
  file        -> LIST of objects, never a scalar:
                 [{"url":"uploads/x.pdf","name":"x.pdf","source":"url"}]
  radio       -> the chosen option's TEXT as a bare scalar  e.g. "Approved"
  dropdown    -> {"id":<int option_id>,"text":"<option label>"}  BOTH keys; id is the option's INTEGER id, must match an option
  multiselect -> [{"id":<int option_id>,"text":"<label>","selected":true}, ...]
                 ALWAYS set "selected":true — entries without it render EMPTY as a variable
  table       -> a list with EXACTLY one entry per configured column
  assignees_form -> {"users":[20059],"guests":["a@b.com"],"groups":["<group_id>"]}"""


def _search_process_by_name(sdk, org_id: str, process_name: str) -> str:
    """
    Resolve a process name to its run_id using the SDK search endpoint.

    Delegates to sdk.searches.search_processes_by_name which calls the Tallyfy
    search API with on="process". Tries an exact case-insensitive match first;
    falls back to a single fuzzy result.

    Args:
        sdk: Active TallyfySDK instance
        org_id: Organization ID
        process_name: Process name or partial name to search for

    Returns:
        run_id of the matching process

    Raises:
        ToolError: If no match found or result is ambiguous
    """
    from tallyfy import TallyfyError
    try:
        result = sdk.search_processes_by_name(org_id, process_name)
        logger.debug("search_processes_by_name(%r) -> %r", process_name, result)
        return result
    except TallyfyError as e:
        raise ToolError(
            f"{e} Use get_organization_runs to browse processes or provide the run_id directly."
        )

logger = logging.getLogger(__name__)

# Global date extractor instance
date_extractor = DateExtractor()


def resolve_user_ids(api_key: str, org_id: str, user_names: List[str], user_emails: List[str]) -> List[int]:
    """Resolve user names and emails to user IDs.

    Lets TallyfyError and other exceptions propagate so @handle_tallyfy_errors
    on the calling tool can categorize them correctly (issue #228).

    Raises ToolError when a name matches multiple users by first-name only
    (closes #153 ambiguity gap — previously silently picked the first match).
    Full-name and email matches always take precedence over first-name fallback,
    so ``user_name="Alex Smith"`` resolves uniquely even when multiple "Alex"s exist.
    """
    if not user_names and not user_emails:
        return []

    with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
        users_list = sdk.users.get_organization_users_list(org_id)
        users = users_list.data
        resolved_ids: List[int] = []

        users_by_email = {user.email.lower(): user.id for user in users if user.email}
        users_by_name = {
            f"{user.first_name} {user.last_name}".lower(): user.id
            for user in users if user.first_name and user.last_name
        }
        users_by_username = {user.username.lower(): user.id for user in users if user.username}

        for email in user_emails:
            if email.lower() in users_by_email:
                resolved_ids.append(users_by_email[email.lower()])

        # Build first-name -> list of user records (NOT first-name -> first id)
        # so we can detect ambiguity instead of silently picking one.
        users_by_first_name: Dict[str, List[Any]] = {}
        for user in users:
            fn = user.first_name.lower() if user.first_name else ""
            if fn:
                users_by_first_name.setdefault(fn, []).append(user)

        for name in user_names:
            name_lower = name.lower()
            if name_lower in users_by_name:
                resolved_ids.append(users_by_name[name_lower])
            elif name_lower in users_by_username:
                resolved_ids.append(users_by_username[name_lower])
            elif name_lower in users_by_first_name:
                candidates = users_by_first_name[name_lower]
                if len(candidates) > 1:
                    rendered = ", ".join(
                        f"{u.first_name} {u.last_name} <{u.email}> (id={u.id})"
                        for u in candidates
                    )
                    raise ToolError(
                        f"Name '{name}' is ambiguous \u2014 {len(candidates)} users match: "
                        f"{rendered}. Provide a more specific user_name (full name), "
                        f"or pass user_email or user_id directly."
                    )
                resolved_ids.append(candidates[0].id)

        return resolved_ids


def resolve_guest_ids(api_key: str, org_id: str, guest_emails: List[str]) -> List[str]:
    """Resolve guest emails to guest IDs via direct lookup.

    Per-guest 404s are swallowed so a missing guest doesn't fail the whole batch,
    but other failures (auth, network, server) propagate to @handle_tallyfy_errors.
    """
    if not guest_emails:
        return []

    from tallyfy.models import TallyfyError
    resolved_ids = []
    with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
        for email in guest_emails:
            try:
                guest = sdk.users.get_guest(org_id, email)
                if guest and guest.guest_id:
                    resolved_ids.append(guest.guest_id)
            except TallyfyError as e:
                if e.status_code == 404:
                    continue
                raise
    return resolved_ids


def resolve_group_ids(api_key: str, org_id: str, group_names: List[str]) -> List[str]:
    """Resolve group names to group IDs.

    Lets TallyfyError and other exceptions propagate so @handle_tallyfy_errors
    on the calling tool can categorize them correctly (issue #228).
    """
    if not group_names:
        return []
    with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
        groups = sdk.groups.get_groups(org_id)
        groups_by_name = {g.name.lower(): g.id for g in groups if g.name}
        return [groups_by_name[n.lower()] for n in group_names if n.lower() in groups_by_name]




def _resolve_user_timezone(api_key: str, org_id: str) -> tuple:
    """Fetch the user's effective timezone from org/user profile.

    Returns (effective_timezone, utc_fallback) where utc_fallback is True
    if no timezone could be determined.
    """
    effective_timezone = None
    try:
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            user = sdk.users.get_current_user_info(org_id)
            org = sdk.organizations.get_organization(org_id)
        if org and getattr(org, 'timezone', None):
            effective_timezone = org.timezone
        elif user and getattr(user, 'timezone', None):
            effective_timezone = user.timezone
        elif user and getattr(user, 'UTC_offset', None):
            effective_timezone = user.UTC_offset
    except Exception as e:
        logger.debug(f"Could not fetch timezone: {e}")
    return effective_timezone, not effective_timezone


def _format_local_deadline(result: dict, parsed_deadline: str, effective_timezone: Optional[str], utc_fallback: bool):
    """Add deadline_local to result dict with timezone conversion."""
    if parsed_deadline and effective_timezone and not utc_fallback:
        try:
            import pytz
            from datetime import datetime as _dt
            tz = pytz.timezone(effective_timezone)
            utc_dt = pytz.utc.localize(_dt.strptime(parsed_deadline, "%Y-%m-%d %H:%M:%S"))
            local_dt = utc_dt.astimezone(tz)
            result["deadline_local"] = local_dt.strftime(f"%Y-%m-%d %H:%M ({effective_timezone})")
        except Exception:
            pass
    elif utc_fallback:
        result["deadline_local"] = (
            f"{parsed_deadline} (UTC — no org/user timezone configured; "
            "update Tallyfy profile settings)"
        )


def register_task_management_tools(mcp):
    """Register all task management tools with the MCP server"""

    @mcp.tool(
        name="get_my_tasks",
        meta={
            "openai/toolInvocation/invoking": "Fetching your tasks...",
            "openai/toolInvocation/invoked": "Tasks loaded",
        },
        description="""Get tasks assigned to the current user. No parameters required.

USE THIS TOOL when user asks:
- "What are my tasks?"
- "Show me my tasks"
- "What do I need to do?"
- "What's assigned to me?"

This tool returns task data including task IDs, run_ids (process IDs), titles, deadlines, and status.
Use the returned data to answer follow-up questions about "those tasks" without making additional tool calls.

IMPORTANT: Tallyfy has no urgency or priority field. For "urgent" tasks, look for
status="overdue" or status="hasproblem" in the returned results.

NOTE: This tool does NOT support status filtering. It returns all tasks for the current user.
To filter by status, retrieve all tasks and filter the results client-side.

PAGINATION: Returns 20 tasks per page. Use page=2, page=3, etc. to retrieve subsequent pages.
meta.total_pages shows how many pages exist. meta.total shows the real count.""",
        tags={"tasks", "workflow", "read-only", "user"},
        annotations=ToolAnnotations(
            title="Get my tasks",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_my_tasks")
    @handle_tallyfy_errors("get my tasks")
    def get_my_tasks(
        page: PageNumber = 1,
    ) -> ToolResult:
        """
        Get tasks assigned to the current user in the organization.

        Args:
            page: Page number to fetch (1-based, default: 1).

        Returns:
            Dict with 'data' (list of tasks) and 'meta' (pagination info).
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            content = fetch_single_page(
                sdk.tasks.get_my_tasks, org_id,
                page=page,
                compact_fields=["step", "run", "taskdata"],
            )
            return ToolResult(content=content, structured_content=None)

    @mcp.tool(
        name="get_user_tasks",
        description="""Get all tasks assigned to a specific organization member (not the current user).

IDENTIFICATION: Provide one of user_id, user_name, or user_email.

CORRECT usage:
- get_user_tasks(user_id=12345) — fastest, no lookup needed
- get_user_tasks(user_name="Zurly Venom") — resolves name to ID automatically
- get_user_tasks(user_email="zurly@example.com") — resolves email to ID automatically

WRONG usage (will fail):
- get_user_tasks() — NO! Must provide at least one identifier

GUEST USERS: This tool is for org members only. For guest tasks, use
get_guest_tasks(guest_email="...") or get_guest_tasks(guest_id="...") instead.

For current user's tasks, use get_my_tasks() instead (no user_id needed).

IMPORTANT: Tallyfy has no "urgent" or "priority" field. For urgent tasks, call with
no status filter and look for status="overdue" or status="hasproblem" in results.

PAGINATION: Returns 20 tasks per page. Use page=2, page=3, etc. for more. meta.total_pages shows total page count.""",
        tags={"tasks", "workflow", "read-only", "user"},
        annotations=ToolAnnotations(
            title="Get user tasks",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_user_tasks")
    @handle_tallyfy_errors("get user tasks")
    def get_user_tasks(
        user_id: OptionalUserId = None,
        user_name: OptionalString = None,
        user_email: OptionalString = None,
        sort_by: OptionalString = "newest",
        status: OptionalString = "all",
        page: PageNumber = 1,
    ) -> ToolResult:
        """
        Get tasks assigned to the given user in the organization.

        Args:
            user_id: Numeric user ID (optional — provide this OR user_name OR user_email)
            user_name: User's full name for automatic ID lookup (optional)
            user_email: User's email address for automatic ID lookup (optional)
            sort_by: Sort order for tasks, e.g. "newest" (default: "newest")
            status: Filter by status, e.g. "all", "active", "completed" (default: "all")
            page: Page number to fetch (1-based, default: 1).

        Returns:
            Dict with 'data' (list of tasks) and 'meta' (pagination info).
        """
        if user_id is None and not user_name and not user_email:
            raise ToolError(
                "At least one of 'user_id' (numeric), 'user_name', or 'user_email' must be provided."
            )

        api_key, org_id = get_authenticated_credentials()

        resolved_user_id = user_id

        if resolved_user_id is None:
            names = [user_name] if user_name else []
            emails = [user_email] if user_email else []
            resolved_ids = resolve_user_ids(api_key, org_id, names, emails)
            if not resolved_ids:
                raise ToolError(
                    f"No user found matching name='{user_name}' or email='{user_email}'. "
                    "Use get_organization_users() to browse available members and their IDs."
                )
            resolved_user_id = resolved_ids[0]

        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            content = fetch_single_page(
                sdk.tasks.get_user_tasks, org_id, resolved_user_id,
                page=page,
                compact_fields=["step", "run", "taskdata"],
                sort_by=sort_by,
                status=status,
            )
            return ToolResult(content=content, structured_content=None)

    @mcp.tool(
        name="get_tasks_for_process",
        description="""Get tasks for a specific process (workflow run).

MANDATORY: You MUST provide either 'process_id', 'run_id', OR 'process_name'. Calling with empty parameters WILL FAIL.

CORRECT usage examples:
- get_tasks_for_process(process_id="abc123") - when you have the process/run ID
- get_tasks_for_process(run_id="abc123") - alias for process_id (consistent with other task tools)
- get_tasks_for_process(process_name="Hiring John Doe") - when you have the process name

WRONG usage (will fail):
- get_tasks_for_process() - NO! Missing required parameter
- get_tasks_for_process(process_id=None) - NO! Must provide a value

If you don't have a process_id/run_id or process_name, use search_for_processes first or extract run_id values from previous get_my_tasks results.

PAGINATION: Returns 20 tasks per page. Use page=2, page=3, etc. for more. meta.total_pages shows total page count.""",
        tags={"tasks", "workflow", "process", "read-only"},
        annotations=ToolAnnotations(
            title="Get tasks for process",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_tasks_for_process")
    @handle_tallyfy_errors("get tasks for process")
    def get_tasks_for_process(
        process_id: OptionalString = None,
        run_id: OptionalString = None,
        process_name: OptionalString = None,
        status: OptionalString = None,
        sort: OptionalString = None,
        owners: OptionalString = None,
        groups: OptionalString = None,
        page: PageNumber = 1,
    ) -> ToolResult:
        """
        Get tasks for a given process (run).

        Args:
            process_id: Process (run) ID to get tasks for (provide this OR run_id OR process_name)
            run_id: Alias for process_id — consistent with run_id used in other task tools
            process_name: Process (run) name to get tasks for (alternative to process_id/run_id)
            status: Filter tasks by status (optional)
            sort: Sort order for tasks (optional)
            owners: Filter by owner IDs (optional)
            groups: Filter by group IDs (optional)
            page: Page number to fetch (1-based, default: 1)

        Returns:
            Dict containing tasks, meta, and process_info
        """
        api_key, org_id = get_authenticated_credentials()

        resolved_process_id = process_id or run_id

        if not resolved_process_id and not process_name:
            raise ToolError("Either process_id (or run_id) or process_name must be provided")

        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            if process_name and not resolved_process_id:
                resolved_process_id = _search_process_by_name(sdk, org_id, process_name)
            result = fetch_single_page(
                sdk.tasks.get_tasks_for_process,
                org_id,
                page=page,
                compact_fields=["step", "taskdata"],
                process_id=resolved_process_id,
                status=status,
                sort=sort,
                owners=owners,
                groups=groups,
            )

            process_info = None
            if resolved_process_id:
                try:
                    runs_list = sdk.tasks.get_organization_runs(org_id)
                    matching_run = next(
                        (run for run in runs_list.data if run.id == resolved_process_id), None
                    )
                    if matching_run:
                        process_info = {
                            "id": matching_run.id,
                            "name": matching_run.name,
                            "status": matching_run.status,
                            "increment_id": matching_run.increment_id,
                        }
                except Exception as e:
                    logger.debug(f"Could not fetch process info for {resolved_process_id}: {e}")
                    process_info = {
                        "id": resolved_process_id,
                        "name": process_name or "Unknown",
                    }

        result["process_info"] = process_info
        return ToolResult(content=result, structured_content=None)

    @mcp.tool(
        name="create_standalone_task",
        description="""Create a standalone (one-off) task with explicit structured fields.

NOTE: Tallyfy internally creates a lightweight process container for every standalone task — this is
expected platform behaviour and the task will still appear as a one-off task (is_oneoff_task: true).

REQUIRED FIELDS: title, deadline, and at least one assignee.

DEADLINE: Accepts natural language — e.g. "April 12 2026 at 3pm", "next Monday at noon",
"tomorrow at 5pm". Resolved to UTC using the org timezone automatically.

TASK TYPE: Accepted values:
- "task" (default) — standard task
- "approval" — approval/reject task
- "expiring" — expiring task
- "email" — email draft task
- "expiring_email" — auto-send email task

ASSIGNEES: Provide one or more of:
- user_names: member full names (e.g. ["John Doe"])
- user_emails: member email addresses
- guest_emails: guest email addresses
- group_names: group names

CORRECT usage:
- create_standalone_task(title="Review budget", deadline="tomorrow at 3pm", user_emails=["john@example.com"])
- create_standalone_task(title="QA test", task_type="approval", deadline="April 12 2026", user_names=["Jane Smith"], description="Routine QA validation")

If the user doesn't specify a deadline or assignee, ASK them before calling the tool.""",
        tags={"tasks", "workflow", "create", "write"},
        annotations=ToolAnnotations(
            title="Create standalone task",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("create_standalone_task")
    @handle_tallyfy_errors("create standalone task")
    def create_standalone_task(
        title: TaskTitle,
        deadline: NaturalLanguageInput,
        description: OptionalString = None,
        task_type: OptionalString = None,
        user_names: Optional[Union[str, List[str]]] = None,
        user_emails: Optional[Union[str, List[str]]] = None,
        guest_emails: Optional[Union[str, List[str]]] = None,
        group_names: Optional[List[str]] = None,
        max_assignable: OptionalInt = None,
        prevent_guest_comment: OptionalBool = None,
    ) -> ToolResult:
        """
        Create a standalone task with explicit structured fields.

        Args:
            title: Task name
            deadline: Deadline as natural language — e.g. "April 12 2026 at 3pm", "next Monday"
            description: Task description/summary (optional)
            string (task|approval|expiring|email|expiring_email)
            task_type: Task type — string (task|approval|expiring|email|expiring_email)
            user_names: Member full names to assign (optional)
            user_emails: Member email addresses to assign (optional)
            guest_emails: Guest email addresses to assign (optional)
            group_names: Group names to assign (optional)
            max_assignable: Maximum number of assignees (optional)
            prevent_guest_comment: Prevent guests from commenting (optional)

        Returns:
            Created Task as a dictionary
        """
        if isinstance(user_names, str):
            user_names = [user_names]
        if isinstance(user_emails, str):
            user_emails = [user_emails]
        if isinstance(guest_emails, str):
            guest_emails = [guest_emails]

        api_key, org_id = get_authenticated_credentials()

        effective_timezone, utc_fallback = _resolve_user_timezone(api_key, org_id)

        parsed_deadline = date_extractor._parse_date_with_fallbacks(deadline, timezone=effective_timezone)
        if not parsed_deadline:
            raise ToolError(
                "Could not parse deadline. Use a format like 'April 12 2026 at 3pm', 'next Monday at noon', or 'tomorrow at 5pm'."
            )

        user_ids = resolve_user_ids(api_key, org_id, user_names or [], user_emails or [])
        group_ids = resolve_group_ids(api_key, org_id, group_names or [])

        if not user_ids and not guest_emails and not group_ids:
            raise ToolError(
                "At least one assignee is required. Provide 'user_names', 'user_emails', 'guest_emails', or 'group_names'."
            )

        owners = TaskOwners(
            users=user_ids or [],
            guests=guest_emails or [],
            groups=group_ids or [],
        )

        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            task = sdk.tasks.create_task(
                org_id=org_id,
                title=title.strip(),
                deadline=parsed_deadline,
                owners=owners,
                description=description.strip() if description else None,
                task_type=task_type or "task",
                max_assignable=max_assignable,
                prevent_guest_comment=prevent_guest_comment,
            )

        result = serialize_task(task) if task else {}
        _format_local_deadline(result, parsed_deadline, effective_timezone, utc_fallback)

        return ToolResult(content=result, structured_content=None)

    @mcp.tool(
        name="complete_task",
        meta={
            "openai/toolInvocation/invoking": "Completing the task...",
            "openai/toolInvocation/invoked": "Task completed",
        },
        description="""Mark a task as complete.

REQUIRED: 'run_id' (32-char hex process ID) and 'task_id' (32-char hex).

APPROVAL TASKS need is_approved:
  If the task's task_type is "approval", you MUST pass is_approved=True (approve)
  or is_approved=False (reject). api-v2 returns HTTP 422 "The is approved field is
  required" if you omit it on an approval task. The requirement is gated on the
  task's OWN task_type, not on its parent step_type (the two can differ). Read
  task_type first via get_tasks_for_process or get_task (both return it).

    task_type="task"           -> do NOT pass is_approved (regular completion)
    task_type="approval"       -> MUST pass is_approved=True or is_approved=False
    task_type="expiring"       -> do NOT pass is_approved (completing acknowledges it)
    task_type="expiring_email" -> do NOT pass is_approved (completing acknowledges it)
    task_type="email"          -> do NOT pass is_approved (regular completion)

  is_approved is honored ONLY for approval tasks. On task/email it is ignored. On
  expiring or expiring_email it is NOT ignored: is_approved=False would record the
  task as EXPIRED instead of ACKNOWLEDGED, so this tool refuses is_approved=False on
  an expiring task. There is no way to mark a task expired through this tool.

CORRECT usage:
  complete_task(run_id="...", task_id="...")                    # regular or expiring task
  complete_task(run_id="...", task_id="...", is_approved=True)  # approve an approval task
  complete_task(run_id="...", task_id="...", is_approved=False) # reject an approval task

Get run_id, task_id, and task_type from get_tasks_for_process() or get_my_tasks().

Never call this without both required parameters.""",
        tags={"tasks", "workflow", "write", "lifecycle"},
        annotations=ToolAnnotations(
            title="Complete task",
            readOnlyHint=False,
            destructiveHint=False,
            # NOT idempotent. Task::complete() (Task.php:1037-1039) returns false when
            # the task is already complete, but TaskService::complete() (TaskService.php:147)
            # DISCARDS that return and still dispatches 'task.completed' (:151). A repeat
            # call therefore re-fires webhooks, assignee emails, watcher digests, the
            # realtime broadcast and an activity-feed row even though status is unchanged.
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("complete_task")
    @handle_tallyfy_errors("complete task")
    def complete_task(
        run_id: ProcessId,
        task_id: TaskId,
        is_approved: OptionalBool = None,
        override_user: OptionalInt = None,
    ) -> ToolResult:
        """
        Mark a task as complete.

        Args:
            run_id: Process (run) ID the task belongs to (REQUIRED - 32-character hex string)
            task_id: Task ID to complete (REQUIRED - 32-character hex string)
            is_approved: Approval decision, honored ONLY for approval-type tasks
                (True = approve, False = reject). Ignored for task/email tasks. For
                an expiring task, is_approved=False is REFUSED because api-v2 would
                record it as EXPIRED instead of ACKNOWLEDGED; complete expiring tasks
                without is_approved.
            override_user: Optional numeric user ID to record as the completing user

        Returns:
            Updated task object with completed status
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            forwarded_is_approved = is_approved
            # is_approved is only part of an APPROVAL task's completion contract.
            # api-v2 Task::complete() (Task.php) ALSO writes it for expiring tasks:
            # `is_approved = ($is_approved !== false)`, so is_approved=False silently
            # flips an expiring task from ACKNOWLEDGED to EXPIRED (a 2xx with data
            # loss). The gate api-v2 uses is the task's own task_type, so resolve it
            # and forward is_approved only for a genuine approval task.
            if is_approved is not None:
                task = sdk.tasks.get_task(org_id, run_id, task_id)
                task_type = getattr(task, "task_type", None) if task else None
                if is_approved is False and task_type in ("expiring", "expiring_email"):
                    raise ToolError(
                        "is_approved=False cannot be used on an expiring task: "
                        "completing an expiring task records it as ACKNOWLEDGED, and "
                        "api-v2 would instead mark it EXPIRED (silent data loss). There "
                        "is no way to mark a task expired through this tool. Call "
                        "complete_task without is_approved to acknowledge it."
                    )
                if task_type is not None and task_type != "approval":
                    # task / email (or expiring completed with is_approved=True):
                    # is_approved is not part of the completion contract, so drop it
                    # rather than send a value the API ignores or misinterprets.
                    forwarded_is_approved = None
            result = sdk.tasks.complete_task(
                org_id, run_id, task_id,
                is_approved=forwarded_is_approved,
                override_user=override_user,
            )
            return ToolResult(
                content=serialize_task(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="reopen_task",
        description="""Reopen a previously completed task. REQUIRED: 'run_id' (32-char hex process ID), 'task_id' (32-char hex), and 'reason' (string explanation for reopening).

⚠️ MANDATORY `reason` PARAMETER:
  - The `reason` parameter is REQUIRED (not optional) — empty/whitespace-only strings raise ToolError.
  - It mirrors the native Tallyfy UI, which requires a reason before reopening.
  - The reason is automatically posted as a comment on the task for audit trail purposes — visible to all task participants and persisted permanently in the run history.
  - Length: keep it concise (1-2 sentences, ideally under 500 characters). Very long reasons are accepted by the API but may be truncated in some UI views.
  - YOU MUST ASK THE USER for the reason before calling this tool. Do NOT invent, assume, or fabricate a reason — that would create a misleading audit trail.

WHY: Reopening a task is a corrective action that affects workflow integrity. The audit comment ensures team members (assignees, owners, observers) understand WHY the task was reopened — preventing confusion ("Was the previous completion wrong? Did requirements change?").

CORRECT usage:
  reopen_task(run_id="abc...", task_id="def...", reason="Incorrect completion, needs review")
  reopen_task(run_id="abc...", task_id="def...", reason="Customer reported issue not addressed; reopening for follow-up.")

WRONG usage (will fail or create a misleading audit):
  reopen_task(run_id="abc...", task_id="def...")                              # MISSING reason → ToolError
  reopen_task(run_id="abc...", task_id="def...", reason="")                   # EMPTY reason → ToolError
  reopen_task(run_id="abc...", task_id="def...", reason="reopening")          # AI-fabricated → misleading audit

Never call this without all three required parameters. Always ask the user to provide the reason.""",
        tags={"tasks", "workflow", "write", "lifecycle"},
        annotations=ToolAnnotations(
            title="Reopen task",
            readOnlyHint=False,
            destructiveHint=False,
            # NOT idempotent: every call posts the `reason` as a NEW audit comment
            # (sdk.threads.add_task_comment below), so calling twice leaves two
            # comments on the task even though the task status is unchanged.
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("reopen_task")
    @handle_tallyfy_errors("reopen task")
    def reopen_task(
        run_id: ProcessId,
        task_id: TaskId,
        reason: str,
    ) -> ToolResult:
        """
        Reopen a previously completed task.

        Args:
            run_id: Process (run) ID the task belongs to (REQUIRED - 32-character hex string)
            task_id: Task ID to reopen (REQUIRED - 32-character hex string)
            reason: Why the task is being reopened (REQUIRED). Posted as a comment on the task for audit trail.

        Returns:
            Updated task object with reopened status
        """
        if not reason.strip():
            raise ToolError(
                "reason is required — the native Tallyfy UI requires a reason "
                "before reopening a task. Please provide an explanation."
            )
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.tasks.reopen_task(org_id, run_id, task_id)
            sdk.threads.add_task_comment(
                org_id, task_id, reason.strip()
            )
            return ToolResult(
                content=serialize_task(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="update_task",
        description="""Update task properties including deadline, assignees, title, or form field values.

REQUIRED: 'run_id' (32-char hex process ID) and 'task_id' (32-char hex).
Plus at least ONE optional field to update.

""" + _TASKDATA_SHAPE_HELP + """

CORRECT usage:
  update_task(run_id="abc...", task_id="def...", deadline="2026-03-01 17:00:00")
  update_task(run_id="abc...", task_id="def...", title="New title", summary="Updated description")
  update_task(run_id="abc...", task_id="def...", owners={"users": [123], "guests": [], "groups": []})

'owners' REPLACES the assignee list, it does not add to it. Pass all three
buckets and list everyone who should remain assigned. Any bucket you leave out
is read back off the task and re-sent unchanged, so it is preserved rather than
detached, but an empty list you DO pass means "unassign everyone here".
  update_task(run_id="abc...", task_id="def...", taskdata={"a1b2c3d4e5f6789012345678901234ef": "Acme Corp"})

Never call this without run_id and task_id.""",
        tags={"tasks", "workflow", "write", "update"},
        annotations=ToolAnnotations(
            title="Update task",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("update_task")
    @handle_tallyfy_errors("update task")
    def update_task(
        run_id: ProcessId,
        task_id: TaskId,
        title: OptionalString = None,
        summary: OptionalString = None,
        deadline: OptionalString = None,
        owners: Optional[Dict[str, Any]] = None,
        taskdata: Optional[Dict[str, Any]] = None,
        status: OptionalString = None,
        position: OptionalInt = None,
        max_assignable: OptionalInt = None,
        top_secret: OptionalBool = None,
        prevent_guest_comment: OptionalBool = None,
        started_at: OptionalString = None,
        task_type: OptionalString = None,
        webhook: OptionalString = None,
    ) -> ToolResult:
        """
        Update task properties.

        Args:
            run_id: Process (run) ID the task belongs to (REQUIRED - 32-character hex string)
            task_id: Task ID to update (REQUIRED - 32-character hex string)
            title: New task title
            summary: New task description
            deadline: New deadline in "YYYY-MM-DD HH:MM:SS" format
            owners: Assignees dict, e.g. {"users": [123, 456], "guests": ["email@x.com"], "groups": []}.
                Replaces the assignee list rather than adding to it. Any of the three
                buckets you omit is read back off the task and re-sent unchanged, so it
                is preserved; a bucket you pass as [] is cleared as you asked.
            taskdata: Form field values, keyed by form field id. The value is shaped by
                the field's type and is sent verbatim. There is no {"value": ...}
                wrapper: most types reject one with a 422, email stores it verbatim
                (silent corruption), and file returns a 500. text/textarea/date/email
                take a bare scalar; file takes a LIST of objects and NEVER a scalar,
                e.g. [{"filename":"x.pdf","url":"uploads/x.pdf","source":"url"}] (the
                key is "filename", not "name"; a bare scalar is an HTTP 500); radio
                takes the option's text as a bare scalar; dropdown takes {"id","text"}
                with the id as the option's integer id; multiselect a list of
                {"id","text","selected":true}; table a list with one entry per column,
                each entry holding that column's row values; assignees_form
                {"users","guests","groups"}.
            status: Task status string
            position: Task position (1-based)
            max_assignable: Maximum number of assignees who must complete the task
            top_secret: Hide task from non-assignees
            prevent_guest_comment: Prevent guests from commenting
            started_at: Task start timestamp in "YYYY-MM-DD HH:MM:SS" format
            task_type: Task type string
            webhook: Webhook URL to notify on task updates

        Returns:
            Updated task object
        """
        if owners is not None and not isinstance(owners, dict):
            raise ToolError(
                "owners must be a dict with 'users', 'guests' and 'groups' keys, "
                'e.g. {"users": [123], "guests": [], "groups": []}'
            )

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # A partially-supplied `owners` detaches every assignee in the
            # buckets it omits. Complete it before sending; see
            # _completed_owner_buckets for the trace.
            if owners is not None:
                owners = _completed_owner_buckets(
                    sdk,
                    f"organizations/{org_id}/runs/{run_id}/tasks/{task_id}",
                    task_id,
                    owners,
                )

            result = sdk.tasks.update_task(
                org_id, run_id, task_id,
                title=title,
                summary=summary,
                deadline=deadline,
                owners=owners,
                taskdata=taskdata,
                status=status,
                position=position,
                max_assignable=max_assignable,
                top_secret=top_secret,
                prevent_guest_comment=prevent_guest_comment,
                started_at=started_at,
                task_type=task_type,
                webhook=webhook,
            )
            return ToolResult(
                content=serialize_task(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="get_task",
        description="Get a single task from a process by ID. REQUIRED: 'run_id' (32-char hex process ID) and 'task_id' (32-char hex). Never call this without both parameters.",
        tags={"tasks", "workflow", "read-only"},
        annotations=ToolAnnotations(
            title="Get task",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_task")
    @handle_tallyfy_errors("get task")
    def get_task(run_id: ProcessId, task_id: TaskId) -> ToolResult:
        """
        Get a single task from a process.

        Args:
            run_id: Process (run) ID the task belongs to (REQUIRED - 32-character hex string)
            task_id: Task ID to retrieve (REQUIRED - 32-character hex string)

        Returns:
            Task object with full details
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.tasks.get_task(org_id, run_id, task_id)
            return ToolResult(
                content=serialize_task(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="get_standalone_task",
        description="Get a standalone (one-off) task by ID. REQUIRED: 'task_id' (32-char hex). Never call this without task_id.",
        tags={"tasks", "workflow", "read-only", "standalone"},
        annotations=ToolAnnotations(
            title="Get standalone task",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_standalone_task")
    @handle_tallyfy_errors("get standalone task")
    def get_standalone_task(task_id: TaskId) -> ToolResult:
        """
        Get a standalone (one-off) task.

        Args:
            task_id: Standalone task ID (REQUIRED - 32-character hex string)

        Returns:
            Task object with full details
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.tasks.get_standalone_task(org_id, task_id)
            return ToolResult(
                content=serialize_task(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="update_standalone_task",
        description="""Update a standalone (one-off) task that was created via create_standalone_task.

Use THIS tool (not update_task) when the task has no run_id or was created as a one-off task.
Use update_task instead when the task belongs to a workflow process run.

REQUIRED: 'task_id' (32-char hex) plus at least ONE field to update.

""" + _TASKDATA_SHAPE_HELP + """

CORRECT usage:
  update_standalone_task(task_id="abc...", deadline="2026-06-01 17:00:00")
  update_standalone_task(task_id="abc...", title="New title", summary="Updated description")
  update_standalone_task(task_id="abc...", owners={"users": [123], "guests": [], "groups": []})
  update_standalone_task(task_id="abc...", taskdata={"a1b2c3d4e5f6789012345678901234ef": "new value"})
  update_standalone_task(task_id="abc...", taskdata={"a1b2c3d4e5f6789012345678901234ef": {"id": 2, "text": "Approved"}})

Never call this without task_id. Do NOT pass a run_id — standalone tasks don't use one.""",
        tags={"tasks", "workflow", "write", "standalone", "update"},
        annotations=ToolAnnotations(
            title="Update standalone task",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("update_standalone_task")
    @handle_tallyfy_errors("update standalone task")
    def update_standalone_task(
        task_id: TaskId,
        title: OptionalString = None,
        summary: OptionalString = None,
        deadline: OptionalString = None,
        owners: Optional[Dict[str, Any]] = None,
        taskdata: Optional[Dict[str, Any]] = None,
        status: OptionalString = None,
        max_assignable: OptionalInt = None,
        top_secret: OptionalBool = None,
        prevent_guest_comment: OptionalBool = None,
        started_at: OptionalString = None,
        task_type: OptionalString = None,
        webhook: OptionalString = None,
    ) -> ToolResult:
        """
        Update a standalone (one-off) task.

        Args:
            task_id: Standalone task ID (REQUIRED - 32-character hex string)
            title: New task title
            summary: New task description
            deadline: New deadline in "YYYY-MM-DD HH:MM:SS" format
            owners: Assignees dict, e.g. {"users": [123, 456], "guests": ["email@x.com"], "groups": []}.
                Replaces the assignee list rather than adding to it. Any of the three
                buckets you omit is read back off the task and re-sent unchanged, so it
                is preserved; a bucket you pass as [] is cleared as you asked.
            taskdata: Form field values, keyed by form field id. The value is shaped by
                the field's type and is sent verbatim. There is no {"value": ...}
                wrapper: most types reject one with a 422, email stores it verbatim
                (silent corruption), and file returns a 500. text/textarea/date/email
                take a bare scalar; file takes a LIST of objects and NEVER a scalar,
                e.g. [{"filename":"x.pdf","url":"uploads/x.pdf","source":"url"}] (the
                key is "filename", not "name"; a bare scalar is an HTTP 500); radio
                takes the option's text as a bare scalar; dropdown takes {"id","text"}
                with the id as the option's integer id; multiselect a list of
                {"id","text","selected":true}; table a list with one entry per column,
                each entry holding that column's row values; assignees_form
                {"users","guests","groups"}.
            status: Task status string
            max_assignable: Maximum number of assignees who must complete the task
            top_secret: Hide task from non-assignees (only assignees and admins can see it)
            prevent_guest_comment: Prevent guests from commenting
            started_at: Task start timestamp in "YYYY-MM-DD HH:MM:SS" format
            task_type: Task type (task, approval, expiring, email, expiring_email)
            webhook: Webhook URL to notify on task updates

        Returns:
            Updated task object
        """
        if owners is not None and not isinstance(owners, dict):
            raise ToolError(
                "owners must be a dict with 'users', 'guests' and 'groups' keys, "
                'e.g. {"users": [123], "guests": [], "groups": []}'
            )

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            current_task = sdk.tasks.get_standalone_task(org_id, task_id)
            current_data = serialize_dataclass(current_task) if current_task else {}

            # Same partial-bucket hazard as update_task: OneOffTaskService::edit
            # routes through TaskFactory::editBasicTask, which hits the identical
            # is_array('owners') gate, so a partially-supplied owners detaches
            # every assignee in the buckets it omits. Only the CALLER-supplied
            # dict needs completing. The `current_data` fallback below is already
            # safe: serialize_dataclass strips only EMPTY buckets, and detaching
            # an already-empty bucket is a no-op.
            if owners is not None:
                owners = _completed_owner_buckets(
                    sdk, f"organizations/{org_id}/tasks/{task_id}", task_id, owners
                )

            result = sdk.tasks.update_standalone_task(
                org_id, task_id,
                title=title if title is not None else current_data.get('title'),
                summary=summary,
                deadline=deadline if deadline is not None else current_data.get('deadline'),
                owners=owners if owners is not None else current_data.get('owners'),
                taskdata=taskdata,
                status=status,
                max_assignable=max_assignable,
                top_secret=top_secret,
                prevent_guest_comment=prevent_guest_comment,
                started_at=started_at,
                task_type=task_type,
                webhook=webhook,
            )
            return ToolResult(
                content=serialize_task(result) if result else {},
                structured_content=None
            )


    @mcp.tool(
        name="complete_kickoff_form",
        description="Complete a process kickoff form to mark it as submitted. REQUIRED: 'run_id' (32-char hex process ID). Never call this without run_id.",
        tags={"tasks", "kickoff", "write"},
        annotations=ToolAnnotations(
            title="Complete kickoff form",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("complete_kickoff_form")
    @handle_tallyfy_errors("complete kickoff form")
    def complete_kickoff_form(run_id: ProcessId) -> ToolResult:
        """
        Complete a process kickoff form.

        Args:
            run_id: Process (run) ID (REQUIRED - 32-character hex string)

        Returns:
            Updated process object
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.tasks.complete_kickoff_form(org_id, run_id)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="get_guest_tasks",
        description="""Get all tasks assigned to a specific guest (external user).

IDENTIFICATION: Provide guest_id or guest_email.

CORRECT usage:
- get_guest_tasks(guest_id="MITxZa1z2f5d81bb53f1da7c7fa95a2cfec5cbc2") — fastest, no lookup needed
- get_guest_tasks(guest_email="guest@example.com") — resolves email to guest_id automatically

WRONG usage (will fail):
- get_guest_tasks() — NO! Must provide at least one identifier

For org member tasks, use get_user_tasks() instead.
For the current user's tasks, use get_my_tasks() (no identifier needed).

IMPORTANT: Tallyfy has no "urgent" or "priority" field. For urgent tasks, call with
no status filter and look for status="overdue" or status="hasproblem" in results.

PAGINATION: Returns 20 tasks per page. Use page=2, page=3, etc. for more. meta.total_pages shows total page count.""",
        tags={"tasks", "workflow", "read-only", "guests"},
        annotations=ToolAnnotations(
            title="Get guest tasks",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_guest_tasks")
    @handle_tallyfy_errors("get guest tasks")
    def get_guest_tasks(
        guest_id: OptionalGuestId = None,
        guest_email: OptionalString = None,
        sort_by: OptionalString = "newest",
        status: OptionalString = "all",
        page: PageNumber = 1,
    ) -> ToolResult:
        """
        Get tasks assigned to the given guest in the organization.

        Args:
            guest_id: Unique guest identifier string (optional — provide this OR guest_email)
            guest_email: Guest's email address for automatic ID lookup (optional)
            sort_by: Sort order for tasks, e.g. "newest" (default: "newest")
            status: Filter by status, e.g. "all", "active", "completed" (default: "all")
            page: Page number to fetch (1-based, default: 1).

        Returns:
            Dict with 'data' (list of tasks) and 'meta' (pagination info).
        """
        if guest_id is None and not guest_email:
            raise ToolError(
                "At least one of 'guest_id' or 'guest_email' must be provided."
            )

        api_key, org_id = get_authenticated_credentials()

        if guest_id is None:
            resolved_ids = resolve_guest_ids(api_key, org_id, [guest_email])
            if not resolved_ids:
                raise ToolError(
                    f"No guest found matching email='{guest_email}'. "
                    "Use get_organization_guests() to browse available guests and their IDs."
                )
            guest_id = resolved_ids[0]

        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            content = fetch_single_page(
                sdk.users.get_guest_tasks, org_id, guest_id,
                page=page,
                compact_fields=["step", "run", "taskdata"],
                sort_by=sort_by,
                status=status,
            )
            return ToolResult(content=content, structured_content=None)