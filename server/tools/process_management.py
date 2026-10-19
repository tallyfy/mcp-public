"""
Process Management Tools
Tools for managing processes and runs
"""

import re
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from pydantic import Field
from tallyfy import TallyfySDK, TallyfyError
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
    PageOffset,
    GenericDict,
    GenericList,
)
from utils.sdk_serializer import serialize_dataclass
from utils.field_value_encoding import coerce_field_values_safely
from utils.guest_assignment import (
    build_guest_task_overrides,
    normalize_guest_emails,
    resolve_run_starter_user_id,
)
from utils.kickoff_encoding import normalize_keyed_payload
from utils.pagination import fetch_single_page
from utils import scheduled_jobs as sj
from metrics import track_tool_execution


def _is_tallyfy_id(value: str) -> bool:
    """Return True if value looks like a 32-char hex Tallyfy ID."""
    return bool(re.fullmatch(r'[0-9a-f]{32}', value.lower()))


def _preserved_run_assignment_buckets(run: Dict[str, Any], run_id: str) -> Dict[str, list]:
    """Return the run's CURRENT users/groups so a PUT does not wipe them.

    RunService::editRun (app/Services/RunService.php:135) unconditionally calls
      saveAssignees(Assignees::newFromArray(Arr::only($data, ['users','groups'])))
    and BaseAssignees::newFromArray (app/Domain/Owners/BaseAssignees.php:24)
    reads ``$data['users'] ?? []``, so an ABSENT key becomes an EMPTY SET, not
    "leave alone". Assignees::modify then calls every current member and group
    "removed" and AssignableTrait::saveAssignees (app/Models/Concerns/
    AssignableTrait.php:115-124) detaches them. The guarded
    ``if (isset($data['prerun']))`` one line above at :132 is the tell: prerun
    is protected from exactly this, assignees are not.

    A rename carrying only {name} therefore silently strips every member and
    group off a RUNNING process. Proven live on production 2026-07-21: a run
    holding {"users":[10013],"groups":["53a4..."]} was PUT with {"name":"renamed"},
    returned HTTP 200, and read back {"users":[],"groups":[]}. Worse than the
    template variant fixed in #624, because people lose access to work already
    in flight.

    RunTransformer.php:80-81 emits both keys unconditionally via
    wrappedAssignees(), so a missing or non-list bucket means the READ failed,
    and sending [] on a failed read is precisely the wipe this guard exists to
    prevent. Abort instead of guessing. A genuinely empty list is legitimate
    (nobody is assigned) and passes straight through.
    """
    buckets: Dict[str, list] = {}
    for field in ("users", "groups"):
        value = run.get(field)
        if not isinstance(value, list):
            raise ToolError(
                f"Process {run_id} did not return its current '{field}', so "
                f"updating it would detach every member and group from a "
                f"running process. Nothing was sent."
            )
        buckets[field] = list(value)
    return buckets


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
        description="""Get workflow processes (runs) in the organization. All parameters are optional. Call with no parameters to get all runs, or use filters to narrow results.

FILTERS: status, archived, starred, checklist_id (template_id), tag, folder, groups, owners, run_type, me (bool)
RUN STATUS VALUES: "active", "problem", "delayed", "complete", "improvement", "starred", "archived"
ARCHIVED: archived="only" returns archived processes. status="archived" also works: this tool
translates it to archived="only" for you.
RUN TYPE VALUES: "procedure", "form", "document"
FOLDER: Pass folder ID or folder name (name is auto-resolved to ID).
OWNERS: Pass numeric user IDs. Filters by collaborator presence (run owner OR task assignee), NOT by strict process creator/started_by. Use owners= to find runs the user is involved in, not runs they personally launched.

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
        page_offset: PageOffset = 0,
    ) -> GenericDict:
        """
        Get all processes (runs) in the organization.

        Args:
            with_data: Comma-separated data to include (e.g., 'checklist,tasks,assets,tags')
            form_fields_values: Include form field values
            owners: Filter by numeric user IDs (comma-separated). Matches run owner OR task assignees.
            task_status: Filter by task status ('all', 'in-progress', 'completed')
            groups: Filter by group IDs
            status: Filter by process status ('active', 'problem', 'delayed', 'complete',
                'improvement', 'starred', 'archived'). 'archived' is translated to
                archived='only' below.
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
                    page=page, page_offset=page_offset or 0,
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

REQUIRED: 'template_id' and 'name'. Name it after the template plus the
real-world thing it tracks. Generate it; ask only if they want to pick. Launch
ONE process per real-world thing (per hire, per order). At volume the fix is
less LAUNCHING WORK, not fewer processes: repeat this call once per row of
their list, instead of them launching each one by hand.

PEOPLE: 'users' takes member IDs, 'guests' takes EMAILS - never swap them, and
pass both in ONE call. A guest cannot be a member of a process, so 'guests'
assigns them to its TASKS instead.

TWO FORM SURFACES:

  - `prerun` (optional): KICKOFF fields, collected BEFORE the workflow starts.
    Call `get_kickoff_fields(template_id)` for field IDs, types and options.

    ONE OBJECT keyed by each field's `timeline_id` - not a list, not labels:
      prerun={"a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6": "Acme Corp"}

    Values follow the field type. Bare scalar for text/textarea/date/email. For
    dropdown/radio/multiselect pass the option's id OR its exact text
    (multiselect: a list of them) - this tool resolves each, so
    never hand-build {"id","text"} or "selected":true. table: one entry
    per column. file: a list of objects, each with "filename" plus
    id/full_url/url. assignees_form: {"users":[id],"guests":["email"]}.

  - Step-level fields (NOT here): filled DURING execution via `update_task`
    with `taskdata={field_id: value}`.

CORRECT:
  launch_process(template_id="abc...", name="Onboarding - Jane Doe")
  launch_process(template_id="abc...", name="Onboarding - Acme",
    users=[12345], guests=["jane@acme.com"])

WRONG:
  launch_process(template_id="abc...")  <- no name, fails
  launch_process(..., users=["jane@acme.com"])  <- an EMAIL is a guest: guests=[..]
  launch_process(..., prerun={"Customer name": "Acme"})  <- LABEL not timeline_id:
    matches no field, so it is DROPPED - a 201 with an empty kickoff form""",
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
        # Union, not bare Dict: FastMCP validates this signature with Pydantic
        # BEFORE the function body runs, so a bare Dict makes a legacy list
        # payload fail at the schema layer and the normalizer below never
        # executes. Callers built against the old list schema would stay broken.
        # The description steers callers to the object; this only stops a list
        # from being rejected before it can be converted.
        prerun: Optional[Union[Dict[str, Any], List[Any]]] = None,
        tags: Optional[List[str]] = None,
        folders: Optional[List[str]] = None,
        users: Optional[List[int]] = None,
        # Guests are identified by EMAIL, never by a member id, so this cannot
        # be folded into `users` (List[int]) -- Pydantic rejects the email at the
        # schema layer before any tool code runs, which is what made a mixed
        # member-and-guest launch impossible and produced the "different member
        # IDs" answer a customer was given. See utils/guest_assignment.py for
        # why it is not sent as a top-level `guests` key.
        guests: Optional[List[str]] = None,
        groups: Optional[List[str]] = None,
        is_public: OptionalBool = None,
        tasks: Optional[Dict[str, Any]] = None,
        roles: Optional[Union[Dict[str, Any], List[Any]]] = None,
        parent_id: OptionalString = None,
    ) -> GenericDict:
        """
        Launch a new workflow process from a template.

        Args:
            template_id: Template ID to launch from (REQUIRED - 32-character hex string)
            name: Name for the new process run (REQUIRED)
            summary: Optional process description
            owner_id: Optional numeric user ID to set as process owner
            prerun: Optional kickoff field values as ONE object keyed by the field's
                    timeline_id, e.g. {"<timeline_id>": "value"}. A legacy list of
                    single-key objects is accepted and folded into that object.
            tags: Optional list of tag IDs to attach
            folders: Optional list of folder IDs to place the process in
            users: Optional list of NUMERIC member IDs to assign to the process
            guests: Optional list of guest EMAIL ADDRESSES, e.g.
                    ["jane@acme.com"]. Guests are identified by email and never
                    by a member id. A guest cannot be a member of a process
                    (api-v2 has no runs-guests relation at all), so these are
                    assigned to the process's TASKS instead, on every step whose
                    allow_guest_owners is true, alongside whoever the template
                    already assigns. Refused if the template has no steps or no
                    step accepts guest owners, rather than returning a 201 for a
                    process the guest was never put on.
            groups: Optional list of group IDs to assign to the process
            is_public: Whether the process is publicly accessible (optional)
            tasks: Task assignment overrides dict (optional), keyed by step
                   timeline_id. WARNING: api-v2 creates tasks ONLY for the steps
                   named here when this is non-empty, so a partial map launches a
                   truncated process. When 'guests' is set this tool expands the
                   map to cover every step for exactly that reason.
            roles: Optional role assignments as an object keyed by org role ID,
                   e.g. {"<role_id>": {"users": [12345], "guests": [], "groups": []}}
            parent_id: Parent process ID for sub-processes (optional)

        Returns:
            Launched process (run) object with ID, name, status, and task details
        """
        if not name or not name.strip():
            raise ToolError("name cannot be empty")

        # Refuse a malformed address before anything is created, naming the
        # value. api-v2's ValidGuestEmail stays the authority on whether a
        # well-formed address is acceptable (banned host, bot, already a member,
        # SSO domain); this only catches what is obviously not an email.
        guest_emails = normalize_guest_emails(guests, "guests")

        # POST /runs keys both of these by ID; a list silently loses every value.
        prerun = normalize_keyed_payload(prerun, "prerun")
        roles = normalize_keyed_payload(roles, "roles")

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            if guest_emails:
                # One extra GET, and only when guests were actually supplied.
                # The steps carry both the ids every entry must name and the
                # assignees each entry has to re-send.
                tasks = build_guest_task_overrides(
                    sdk.templates.get_template_steps(org_id, template_id),
                    guest_emails,
                    tasks,
                    # Lazy on purpose. Most templates have no step that assigns
                    # the launcher, and this costs a round trip, so it is only
                    # called when a step actually needs it.
                    run_starter_resolver=lambda: resolve_run_starter_user_id(
                        sdk, org_id, template_id
                    ),
                )
            # Correct container, wrong VALUES is a separate 422 class: dropdown
            # needs {"id","text"}, radio the bare text, multiselect a list of
            # objects carrying `selected`. Resolve them against the template's
            # own option lists so an option id or its text both work. Costs one
            # extra GET, and only when kickoff values were actually supplied.
            prerun = coerce_field_values_safely(
                prerun,
                lambda: getattr(
                    sdk.templates.get_template(org_id, template_id=template_id),
                    "prerun",
                    None,
                ),
            )

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
        description="Get full details for a single process (run) by ID. REQUIRED: 'run_id'.",
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
        description="""Update a process name, summary, or starred status.

Only those properties change: this tool reads the process first and re-sends its
existing members and groups, which the API would otherwise detach from the
running process on an update that omits them.

REQUIRED: 'run_id' plus at least one of: 'name', 'summary', or 'starred'.""",
        tags={"processes", "workflow", "runs", "write"},
        annotations=ToolAnnotations(
            title="Update process",
            readOnlyHint=False,
            # destructiveHint (#1020, per #653's rule): name and summary overwrite the stored values, with no history and no undo.
            destructiveHint=True,
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
            # READ-MODIFY-WRITE, done here rather than via the SDK. The SDK's
            # tasks.update_process() (tallyfy/task_management/creation.py:327-335)
            # builds a body of ONLY {name, summary, starred} and has no parameter
            # that could carry users/groups, while RunService::editRun detaches
            # both on any payload that omits them. See
            # _preserved_run_assignment_buckets for the full trace. Reading the
            # run back and re-sending the current sets makes the diff empty, so
            # saveAssignees returns before touching the pivot tables.
            endpoint = f"organizations/{org_id}/runs/{run_id}"
            current = sdk._make_request("GET", endpoint)
            run = current.get("data", current) if isinstance(current, dict) else {}

            payload: Dict[str, Any] = {}
            if name is not None:
                payload["name"] = name
            if summary is not None:
                payload["summary"] = summary
            if starred is not None:
                payload["starred"] = starred
            payload.update(_preserved_run_assignment_buckets(run, run_id))

            response = sdk._make_request("PUT", endpoint, data=payload)
            result = response.get("data", response) if isinstance(response, dict) else response
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="archive_process",
        description="Archive a completed process (run). REQUIRED: 'run_id'. CAUTION: Archived processes are HIDDEN from default views but NOT deleted. All data, tasks, comments, and form-field captures are preserved. Use reactivate_process(run_id) to restore an archived process to active status. To permanently delete a process you must use the universal write fallback (tallyfy_api_write) since no first-class delete tool is exposed. Archived processes can be retrieved via get_organization_runs(archived='only').",
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
        description="Reactivate an archived process (run) to make it active again. REQUIRED: 'run_id'.",
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
        description=(
            "Reopen a completed kickoff form, which UN-COMPLETES the process: it takes an "
            "already-finished run back out of its finished state, not just 'allow edits' on it. "
            "That can shift SLA/deadline calculations and re-trigger completion-based automations. "
            "REQUIRED: 'run_id' (32-char hex process ID)."
        ),
        tags={"processes", "kickoff", "write"},
        annotations=ToolAnnotations(
            title="Reopen kickoff form",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("reopen_kickoff_form")
    @handle_tallyfy_errors("reopen kickoff form")
    def reopen_kickoff_form(run_id: ProcessId) -> GenericDict:
        """
        Reopen a completed kickoff form, un-completing the process it belongs to.

        This reverts an already-completed process out of its finished state, with real
        workflow consequences: SLA/deadline recalculation and any automations that fire on
        completion. It is not merely "allow edits" on an otherwise-unaffected process.

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

    _register_scheduled_job_tools(mcp)


# ---------------------------------------------------------------------------
# Scheduled jobs: launching a template on a schedule (tallyfy/mcp#1603)
#
# HOTFIX EXCEPTION to "Tools reach the Tallyfy API ONLY through public SDK
# methods" (root CLAUDE.md, "Tool Registration", #1545). The SDK has no method
# for api-v2's recurring-jobs routes, which is tallyfy/sdk#199, and a customer
# was told on 2026-10-06 and 2026-10-12 that Claude can schedule his launches.
# Every direct call below goes through _scheduled_job_request, the one place to
# replace when tallyfy/sdk#199 ships. Nothing else in this module calls
# _make_request for scheduled jobs.
# ---------------------------------------------------------------------------

# A job's id is core.recurring_jobs.id, a bigint, and the routes carry
# whereNumber('recurring_job').
ScheduledJobId = Annotated[int, Field(
    ge=1,
    description=(
        "Scheduled job ID, a whole number such as 168, from get_scheduled_jobs or "
        "schedule_process_launch. Not a template or process ID."
    ),
    examples=[168],
)]

_HISTORY_SHOWN = 20
_JOBS_PER_PAGE = 25


def _utc_now() -> datetime:
    """Now, as an aware UTC instant. A function so tests can fix the clock."""
    return datetime.now(timezone.utc)


def _scheduled_job_request(sdk, method: str, path: str, *, params=None, data=None):
    """The one direct api-v2 call the scheduled job tools make. Remove with tallyfy/sdk#199."""
    return sdk._make_request(method, path, params=params, data=data)


def _jobs_path(org_id: str, job_id: Optional[int] = None, suffix: str = "") -> str:
    path = f"organizations/{org_id}/recurring-jobs"
    if job_id is not None:
        path += f"/{int(job_id)}"
    return path + suffix


def _unwrap_data(response: Any) -> Any:
    if isinstance(response, dict) and "data" in response:
        return response["data"]
    return response


def _job_types(sdk, org_id: str) -> List[Dict[str, Any]]:
    rows = _unwrap_data(_scheduled_job_request(sdk, "GET", f"organizations/{org_id}/recurring-job-types"))
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _job_keys_by_id(types: List[Dict[str, Any]]) -> Dict[int, str]:
    """{type id: job_key}. The id comes back as a string here and an integer on a job."""
    keys: Dict[int, str] = {}
    for row in types:
        try:
            keys[int(row.get("id"))] = str(row.get("job_key"))
        except (TypeError, ValueError):
            continue
    return keys


def _launch_type_id(types: List[Dict[str, Any]]) -> int:
    """The launch-process type's id, found by job_key because the ids differ by
    environment: read 2026-10-14, production "2" and staging "3"."""
    for job_id, key in _job_keys_by_id(types).items():
        if key == sj.LAUNCH_JOB_KEY:
            return job_id
    raise ToolError(
        "This organization's Tallyfy offers no launch-process scheduled job, so a "
        "launch cannot be scheduled."
    )


def _org_timezone(sdk, org_id: str) -> Optional[str]:
    organization = sdk.organizations.get_organization(org_id)
    zone = getattr(organization, "timezone", None) if organization is not None else None
    return zone.strip() if isinstance(zone, str) and zone.strip() else None


def _schedule_reading(sdk, org_id: str) -> str:
    """Which clock this API reads start_date and start_time on (utils/scheduled_jobs.py).

    First from any job the caller can see, which costs one read. Only when there
    is none, from a create that can never be saved: it has no `name`, which both
    readings require, so both answer 422 before anything is written. Anything
    else is refused rather than guessed, because a guess moves every run by the
    organization's UTC offset and nothing errors.
    """
    rows = _unwrap_data(_scheduled_job_request(sdk, "GET", _jobs_path(org_id), params={"per_page": 1}))
    if isinstance(rows, list) and rows and isinstance(rows[0], dict) and "schedule" in rows[0]:
        return sj.reading_from_job(rows[0])
    try:
        _scheduled_job_request(sdk, "POST", _jobs_path(org_id), data=sj.READING_PROBE_BODY)
    except TallyfyError as exc:
        body = getattr(exc, "response_data", None)
        errors = body.get("errors") if isinstance(body, dict) else None
        reading = sj.reading_from_probe_errors(errors) if getattr(exc, "status_code", None) == 422 else None
        if reading is not None:
            return reading
        raise ToolError(
            "Could not tell whether this Tallyfy server reads schedule times in UTC or in "
            "the organization's time zone, so nothing was scheduled. "
            f"Tallyfy answered: {exc}"
        ) from exc
    raise ToolError(
        "Tallyfy accepted a scheduled job request that has no name, which it should "
        "always refuse, so nothing was scheduled. Check get_scheduled_jobs for a job "
        "with no name and delete it."
    )


def _read_job(sdk, org_id: str, job_id: int) -> Dict[str, Any]:
    job = _unwrap_data(_scheduled_job_request(sdk, "GET", _jobs_path(org_id, job_id)))
    if not isinstance(job, dict) or "id" not in job:
        raise ToolError(f"Scheduled job {job_id} did not come back as a job, so nothing was changed.")
    return job


def _put_status(sdk, org_id: str, job: Dict[str, Any], status: str) -> Dict[str, Any]:
    try:
        body = sj.update_body_from_job(job, status)
    except sj.ScheduleInputError as exc:
        raise ToolError(str(exc)) from exc
    saved = _unwrap_data(_scheduled_job_request(sdk, "PUT", _jobs_path(org_id, job["id"]), data=body))
    if not isinstance(saved, dict) or saved.get("status") != status:
        got = saved.get("status") if isinstance(saved, dict) else None
        raise ToolError(
            f"Tallyfy answered the change but scheduled job {job['id']} reads back with "
            f"status {got!r}, not {status!r}. Read it with get_scheduled_job."
        )
    return saved


def _register_scheduled_job_tools(mcp):
    """Register the scheduled launch tools (tallyfy/mcp#1603) with the server.

    Called from register_process_management_tools, so they sit in the Process
    Management category: a scheduled launch is a process launch at a later time.
    """

    @mcp.tool(
        name="get_scheduled_job_types",
        description="""List the kinds of scheduled job this organization's Tallyfy offers, and the organization's time zone.

A scheduled job runs on its own at a set time, once or on a repeat. launch-process starts a process from a template: schedule one with schedule_process_launch. create-task makes a one-off task and cannot be scheduled through these tools yet.

Each kind lists the fields its data takes ('validations').

Give every time to schedule_process_launch in the organization's local time, the zone this tool reports. That tool works out how Tallyfy stores it.""",
        tags={"processes", "scheduled-jobs", "read-only"},
        annotations=ToolAnnotations(
            title="Get scheduled job types",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_scheduled_job_types")
    @handle_tallyfy_errors("get scheduled job types")
    def get_scheduled_job_types() -> GenericDict:
        """
        List the kinds of scheduled job, and the organization's time zone.

        Returns:
            {"job_types": [{id, job_key, name, description, validations}],
             "organization_time_zone": str or None, "note": str}
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            types = _job_types(sdk, org_id)
            zone = _org_timezone(sdk, org_id)
        return ToolResult(
            content={
                "job_types": [
                    {
                        "id": row.get("id"),
                        "job_key": row.get("job_key"),
                        "name": row.get("name"),
                        "description": row.get("description"),
                        "validations": row.get("validations"),
                    }
                    for row in types
                ],
                "organization_time_zone": zone,
                "note": (
                    "Give schedule_process_launch the day and time in "
                    f"{zone or 'UTC'}, without converting them."
                ),
            },
            structured_content=None
        )

    @mcp.tool(
        name="get_scheduled_jobs",
        description="""List the organization's scheduled jobs: what each launches or creates, when it runs, and whether it is active, paused or completed.

A member sees only the jobs they created and the launch jobs for templates they can launch; an admin sees all. Set mine=true for only the caller's own jobs.

Each job says which zone its start_date and schedule.start_time are read in (times_are_read_in) and, where Tallyfy reports it, its next run in UTC and local time. For a launch job, data.checklist_id is the template and data.name the name each launched process gets.

PAGINATION: 25 jobs per page. Use page=2, page=3 and so on; meta.total_pages says how many pages exist.""",
        tags={"processes", "scheduled-jobs", "read-only"},
        annotations=ToolAnnotations(
            title="Get scheduled jobs",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_scheduled_jobs")
    @handle_tallyfy_errors("get scheduled jobs")
    def get_scheduled_jobs(page: PageNumber = 1, mine: OptionalBool = None) -> GenericDict:
        """
        List scheduled jobs, 25 per page.

        Args:
            page: Page number, starting from 1.
            mine: true lists only the jobs the caller created (api-v2 created_by=me).

        Returns:
            {"data": [job], "meta": {total, count, current_page, total_pages}}
        """
        params: Dict[str, Any] = {"page": page or 1, "per_page": _JOBS_PER_PAGE}
        if mine:
            params["created_by"] = "me"
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            response = _scheduled_job_request(sdk, "GET", _jobs_path(org_id), params=params)
            keys = _job_keys_by_id(_job_types(sdk, org_id))
            zone = _org_timezone(sdk, org_id)
        rows = _unwrap_data(response)
        rows = rows if isinstance(rows, list) else []
        pagination = {}
        if isinstance(response, dict):
            pagination = ((response.get("meta") or {}).get("pagination") or {})
        return ToolResult(
            content={
                "data": [sj.describe_job(row, tz_name=zone, job_keys=keys) for row in rows if isinstance(row, dict)],
                "meta": {
                    key: pagination.get(key)
                    for key in ("total", "count", "current_page", "total_pages")
                },
            },
            structured_content=None
        )

    @mcp.tool(
        name="get_scheduled_job",
        description="""Read one scheduled job with its run history. REQUIRED: 'job_id' (a whole number, from get_scheduled_jobs).

Returns the job (status, schedule, the zone its times are read in, its next run where Tallyfy reports it) and its newest 20 runs, newest first, each with success, message and when it ran. A failed run's message says why. A one-time job whose run failed is left paused.

A job the caller may not see answers not found, the same as a job that does not exist.""",
        tags={"processes", "scheduled-jobs", "read-only"},
        annotations=ToolAnnotations(
            title="Get scheduled job",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_scheduled_job")
    @handle_tallyfy_errors("get scheduled job")
    def get_scheduled_job(job_id: ScheduledJobId) -> GenericDict:
        """
        Read one scheduled job and its newest runs.

        Args:
            job_id: Scheduled job ID (a whole number).

        Returns:
            The job, with "history" (newest 20 runs) and "history_total".
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            job = _read_job(sdk, org_id, job_id)
            history = _unwrap_data(
                _scheduled_job_request(sdk, "GET", _jobs_path(org_id, job_id, "/history"))
            )
            keys = _job_keys_by_id(_job_types(sdk, org_id))
            zone = _org_timezone(sdk, org_id)
        runs = [row for row in history if isinstance(row, dict)] if isinstance(history, list) else []
        runs.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        described = sj.describe_job(job, tz_name=zone, job_keys=keys)
        described["history"] = runs[:_HISTORY_SHOWN]
        described["history_total"] = len(runs)
        return ToolResult(content=described, structured_content=None)

    # The REQUIRED line names its parameters without quotes. A quoted 'start_date'
    # is how test_tool_descriptions.py finds the STEP start_date, whose
    # is_soft_start_date gate does not exist here: this start_date is the day a
    # job first runs. Writing that gate into this description would teach a key
    # the recurring-jobs API does not take (root CLAUDE.md, rule 25).
    @mcp.tool(
        name="schedule_process_launch",
        meta={
            "openai/toolInvocation/invoking": "Scheduling your launch...",
            "openai/toolInvocation/invoked": "Launch scheduled",
        },
        description="""Schedule a template to launch a process on its own, once or on a repeat. REQUIRED: template_id, process_name, repeat, start_date, time.

Confirm the schedule with the user before calling it.

WHEN, in the organization's LOCAL time (get_scheduled_job_types gives the zone). Do not convert to UTC:
  start_date: day of the first run, YYYY-MM-DD. The first run must still be ahead.
  time: HH:MM, 24 hour.
  repeat: once, hourly, daily, weekly, monthly or yearly.
    weekly runs on start_date's weekday, or on days_of_week (1 Monday to 7 Sunday).
    monthly runs on start_date's day of the month; yearly on its month and day.
  interval: every N hours, days, weeks, months or years (default 1).
  end_date or max_occurrences: when a repeat stops. Not for once.

WHAT: process_name is the name every launched process gets, so name it after what it tracks. prerun optionally fills kickoff fields, keyed by field id as in launch_process.

The reply says which zone Tallyfy reads the time in, the first run in local time, and the next run. A Tallyfy server that still reads schedule times in UTC cannot take a new job from this tool: it says scheduled launches are not available yet and creates nothing.

To change a schedule, schedule a new job and delete the old one.

CORRECT:
  schedule_process_launch(template_id="abc...", process_name="Annual Equipment Review", repeat="yearly", start_date="2027-03-01", time="09:00")
  schedule_process_launch(template_id="abc...", process_name="Q4 Audit", repeat="once", start_date="2026-11-02", time="14:30")

WRONG:
  time="2pm"  <- write 14:00
  repeat="yearly" with a month or day argument  <- both come from start_date""",
        tags={"processes", "scheduled-jobs", "write", "create", "launch"},
        annotations=ToolAnnotations(
            title="Schedule a process launch",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("schedule_process_launch")
    @handle_tallyfy_errors("schedule process launch")
    def schedule_process_launch(
        template_id: TemplateId,
        process_name: str,
        repeat: Literal["once", "hourly", "daily", "weekly", "monthly", "yearly"],
        start_date: str,
        time: str,
        interval: Annotated[Optional[int], Field(ge=1)] = None,
        days_of_week: Optional[List[int]] = None,
        end_date: OptionalString = None,
        max_occurrences: Annotated[Optional[int], Field(ge=1)] = None,
        job_name: OptionalString = None,
        description: OptionalString = None,
        # Union, not bare Dict, for the same reason as launch_process: FastMCP
        # validates the signature before the body runs, so a legacy list would
        # be refused before it could be folded into the object.
        prerun: Optional[Union[Dict[str, Any], List[Any]]] = None,
    ) -> GenericDict:
        """
        Schedule a template launch, once or on a repeat.

        Args:
            template_id: Template to launch (32-character hex ID).
            process_name: Name every launched process gets, up to 300 characters.
                Required and not blank, because a job with no name can launch
                processes with a blank name.
            repeat: once, hourly, daily, weekly, monthly or yearly.
            start_date: Day of the first run, YYYY-MM-DD, in the organization's local time.
            time: Time of day, HH:MM 24 hour, in the organization's local time.
            interval: Every N hours, days, weeks, months or years. Default 1.
            days_of_week: Weekly only. Days to run on, 1 Monday to 7 Sunday.
                Default: start_date's weekday.
            end_date: Repeats only. Last day a run may happen, YYYY-MM-DD.
            max_occurrences: Repeats only. Stop after this many runs.
            job_name: Name of the scheduled job itself, up to 50 characters.
                Default: process_name, cut to 50 characters.
            description: Note on the scheduled job, up to 500 characters.
            prerun: Kickoff field values for each launch, ONE object keyed by
                each field's id, the same shape as launch_process takes.

        Returns:
            The stored job, with first_run_local, times_are_read_in and notes.
        """
        name = (process_name or "").strip()
        if not name:
            raise ToolError(
                "process_name cannot be blank. Every launched process takes this name, and "
                "a job with none launches processes with a blank name."
            )
        if len(name) > sj.PROCESS_NAME_MAX:
            raise ToolError(f"process_name is {len(name)} characters; Tallyfy takes up to {sj.PROCESS_NAME_MAX}.")
        label = (job_name or "").strip() or name[: sj.JOB_NAME_MAX].strip()
        if len(label) > sj.JOB_NAME_MAX:
            raise ToolError(f"job_name is {len(label)} characters; Tallyfy takes up to {sj.JOB_NAME_MAX}.")
        note = (description or "").strip() or None
        if note is not None and len(note) > sj.DESCRIPTION_MAX:
            raise ToolError(f"description is {len(note)} characters; Tallyfy takes up to {sj.DESCRIPTION_MAX}.")
        kickoff = normalize_keyed_payload(prerun, "prerun")

        planning = dict(
            repeat=repeat,
            start_date=start_date,
            time_of_day=time,
            interval=interval,
            days_of_week=days_of_week,
            end_date=end_date,
            max_occurrences=max_occurrences,
        )
        # Refuse a malformed schedule before any call.
        try:
            sj.plan_schedule(**planning, tz_name=None)
        except sj.ScheduleInputError as exc:
            raise ToolError(str(exc)) from exc

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # Create only where api-v2 reads the time in the organization's zone
            # (owner decision 2026-10-15, utils/scheduled_jobs.py). Nothing is
            # converted to UTC, so on the UTC reading nothing is created.
            if _schedule_reading(sdk, org_id) != sj.READING_ORGANIZATION:
                raise ToolError(sj.UTC_CREATE_REFUSAL)
            zone = _org_timezone(sdk, org_id)
            try:
                plan = sj.plan_schedule(**planning, tz_name=zone, now=_utc_now())
            except sj.ScheduleInputError as exc:
                raise ToolError(str(exc)) from exc
            types = _job_types(sdk, org_id)
            type_id = _launch_type_id(types)

            data: Dict[str, Any] = {"checklist_id": template_id, "name": name}
            if kickoff:
                # A scheduled launch does not pass POST /runs' RunRequestValidator:
                # RunService::startRun hands prerun straight to updateCaptureValues.
                # So the values are put in the shape each field needs here, against
                # the template's own options, as launch_process does.
                data["prerun"] = coerce_field_values_safely(
                    kickoff,
                    lambda: getattr(
                        sdk.templates.get_template(org_id, template_id=template_id),
                        "prerun",
                        None,
                    ),
                )

            body: Dict[str, Any] = {
                "name": label,
                "start_date": plan.start_date,
                "recurring_job_type_id": type_id,
                "schedule": plan.schedule,
                "data": data,
            }
            if note is not None:
                body["description"] = note
            if plan.end_date is not None:
                body["end_date"] = plan.end_date
            if plan.max_occurrences is not None:
                body["max_occurrences"] = plan.max_occurrences

            job = _unwrap_data(_scheduled_job_request(sdk, "POST", _jobs_path(org_id), data=body))
        keys = _job_keys_by_id(types)

        if not isinstance(job, dict) or "id" not in job:
            raise ToolError(
                "Tallyfy answered the request but returned no job. Check get_scheduled_jobs "
                "before scheduling again, so the launch is not scheduled twice."
            )

        warnings: List[str] = []
        stored = job.get("data") if isinstance(job.get("data"), dict) else {}
        if stored.get("name") != name:
            warnings.append(
                f"The job stored the process name {stored.get('name')!r}, not {name!r}."
            )
        if stored.get("checklist_id") != template_id:
            warnings.append(
                f"The job stored template {stored.get('checklist_id')!r}, not {template_id!r}."
            )
        if repeat == "once" and "next_occurrence" in job:
            if sj.same_minute(job.get("next_occurrence"), plan.first_run_local) is False:
                warnings.append(
                    "Tallyfy's next run is not the time asked for: "
                    f"{sj.local_string(job.get('next_occurrence'), zone)} against "
                    f"{plan.first_run_local.strftime('%Y-%m-%d %H:%M')} ({plan.zone_name})."
                )

        described = sj.describe_job(job, tz_name=zone, job_keys=keys)
        described["first_run_local"] = (
            f"{plan.first_run_local.strftime('%Y-%m-%d %H:%M')} ({plan.zone_name})"
        )
        described["notes"] = plan.notes
        if warnings:
            described["warnings"] = warnings
        return ToolResult(content=described, structured_content=None)

    @mcp.tool(
        name="pause_scheduled_job",
        description="""Pause a scheduled job so it does not run until it is resumed. REQUIRED: 'job_id'.

Only the status changes; the schedule and what the job launches stay as they are. resume_scheduled_job starts it again. A completed job has finished and cannot be paused.

Returns the job as Tallyfy stored it, with status paused and no next run.""",
        tags={"processes", "scheduled-jobs", "write"},
        annotations=ToolAnnotations(
            title="Pause a scheduled job",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("pause_scheduled_job")
    @handle_tallyfy_errors("pause scheduled job")
    def pause_scheduled_job(job_id: ScheduledJobId) -> GenericDict:
        """
        Pause a scheduled job. PUT takes the whole job, so it is read first and
        sent back as stored with status paused.

        Args:
            job_id: Scheduled job ID (a whole number).

        Returns:
            The stored job.
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            job = _read_job(sdk, org_id, job_id)
            status = job.get("status")
            if status == "completed":
                raise ToolError(sj.FINISHED_MESSAGE)
            changed = status != "paused"
            if changed:
                job = _put_status(sdk, org_id, job, "paused")
            keys = _job_keys_by_id(_job_types(sdk, org_id))
            zone = _org_timezone(sdk, org_id)
        described = sj.describe_job(job, tz_name=zone, job_keys=keys)
        if not changed:
            described["notes"] = ["The job was already paused, so nothing was changed."]
        return ToolResult(content=described, structured_content=None)

    @mcp.tool(
        name="resume_scheduled_job",
        description="""Resume a paused scheduled job. REQUIRED: 'job_id'.

A repeating job picks up at its next run after now; runs it missed while paused are skipped, not made up. A one-time job whose time has passed cannot be resumed: schedule a new one with schedule_process_launch, then delete this one. A completed job has finished and cannot be resumed.

Returns the job as Tallyfy stored it, with its next run where Tallyfy reports it.""",
        tags={"processes", "scheduled-jobs", "write"},
        annotations=ToolAnnotations(
            title="Resume a scheduled job",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("resume_scheduled_job")
    @handle_tallyfy_errors("resume scheduled job")
    def resume_scheduled_job(job_id: ScheduledJobId) -> GenericDict:
        """
        Resume a paused scheduled job. Read first, then sent back as stored
        with status active.

        Args:
            job_id: Scheduled job ID (a whole number).

        Returns:
            The stored job.
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            job = _read_job(sdk, org_id, job_id)
            status = job.get("status")
            if status == "completed":
                raise ToolError(sj.FINISHED_MESSAGE)
            changed = status != "active"
            if changed:
                try:
                    job = _put_status(sdk, org_id, job, "active")
                except TallyfyError as exc:
                    body = getattr(exc, "response_data", None)
                    errors = body.get("errors") if isinstance(body, dict) else None
                    one_time = job.get("max_occurrences") == 1
                    if (
                        getattr(exc, "status_code", None) == 422
                        and one_time
                        and isinstance(errors, dict)
                        and "start_date" in errors
                    ):
                        raise ToolError(
                            f"Scheduled job {job_id} runs once and its time has passed, so it "
                            "cannot be resumed. Schedule a new one with schedule_process_launch, "
                            "then delete this one with delete_scheduled_job. Tallyfy said: "
                            f"{'; '.join(str(m) for m in errors['start_date'])}"
                        ) from exc
                    raise
            keys = _job_keys_by_id(_job_types(sdk, org_id))
            zone = _org_timezone(sdk, org_id)
        described = sj.describe_job(job, tz_name=zone, job_keys=keys)
        if not changed:
            described["notes"] = ["The job was already active, so nothing was changed."]
        return ToolResult(content=described, structured_content=None)

    @mcp.tool(
        name="delete_scheduled_job",
        description="""Delete a scheduled job so it never runs again. REQUIRED: 'job_id'.

Confirm with the user before calling it: these tools cannot bring a deleted job back. Processes the job already launched are not touched. The tool reads the job back afterwards and reports it deleted only when Tallyfy no longer finds it.

To stop a job for a while instead, use pause_scheduled_job.""",
        tags={"processes", "scheduled-jobs", "write", "delete"},
        annotations=ToolAnnotations(
            title="Delete a scheduled job",
            readOnlyHint=False,
            destructiveHint=True,
            # A repeat call finds nothing to delete and answers not found, so it
            # changes nothing further, the same as delete_standalone_task.
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("delete_scheduled_job")
    @handle_tallyfy_errors("delete scheduled job")
    def delete_scheduled_job(job_id: ScheduledJobId) -> GenericDict:
        """
        Delete a scheduled job and read it back. DELETE answers 200 with the job,
        and a GET of it afterwards answers 404 (measured on staging 2026-10-14).

        Args:
            job_id: Scheduled job ID (a whole number).

        Returns:
            {"deleted": True, "job_id": job_id, "name": str or None}
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            removed = _unwrap_data(_scheduled_job_request(sdk, "DELETE", _jobs_path(org_id, job_id)))
            try:
                _scheduled_job_request(sdk, "GET", _jobs_path(org_id, job_id))
            except TallyfyError as exc:
                if getattr(exc, "status_code", None) != 404:
                    raise ToolError(
                        f"Tallyfy answered the delete, but reading scheduled job {job_id} back "
                        f"failed, so whether it is gone is not known: {exc}"
                    ) from exc
            else:
                raise ToolError(
                    f"Tallyfy answered the delete, but scheduled job {job_id} still reads back. "
                    "Read it with get_scheduled_job before trying again."
                )
        return ToolResult(
            content={
                "deleted": True,
                "job_id": job_id,
                "name": removed.get("name") if isinstance(removed, dict) else None,
            },
            structured_content=None
        )