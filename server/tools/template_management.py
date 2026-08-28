"""
Template Management Tools
Tools for managing templates, steps, and template health
"""

import logging
import re
from typing import Any

from email_validator import validate_email, EmailNotValidError
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
from tallyfy import TallyfySDK
from mcp.types import ToolAnnotations
from utils.fastmcp_errors import handle_tallyfy_errors
from utils.auth_context import get_authenticated_credentials, TALLYFY_API_BASE_URL
from utils.fastmcp_types import (
    TemplateId,
    TemplateTitle,
    StepId,
    FieldId,
    StepDescription,
    StepPosition,
    OptionalString,
    OptionalBool,
    PageNumber,
    GenericDict,
    GenericList,
)
from utils.sdk_serializer import (
    serialize_dataclass,
    compact_result,
    compact_dict_list_field,
    window_longest_text,
    MAX_RESULT_BYTES,
    TRUNCATION_MARKER_PREFIX,
)
from utils.pagination import fetch_single_page
from metrics import track_tool_execution


def _is_tallyfy_id(value: str) -> bool:
    return bool(re.fullmatch(r'[0-9a-f]{32}', value.lower()))


def _resolve_template_folder_name_to_id(sdk, org_id: str, folder_name: str) -> str:
    """Resolve a folder name to its ID by searching template folders (including nested children)."""
    try:
        folders = sdk.folders.get_folders(org_id, folder_type='checklist')
        if folders:
            match = _find_folder_by_name(folders, folder_name)
            if match:
                return str(match.id)
        raise ToolError(
            f"Template folder '{folder_name}' not found. Use get_template_folders to see available folders."
        )
    except ToolError:
        raise
    except Exception:
        return folder_name


def _find_folder_by_name(folders, name: str):
    """Recursively search folders and children for a name match."""
    for f in folders:
        if hasattr(f, "name") and f.name and f.name.lower() == name.lower():
            return f
        if hasattr(f, "children") and f.children:
            match = _find_folder_by_name(f.children, name)
            if match:
                return match
    return None


# Keys api-v2's CreateStepRequest carries a rule for, so they survive
# StepControllerNew::create's onlyValidatedFields() and actually reach the step.
# 'position' is included because this tool honours it via a follow-up reorder,
# not because the create endpoint accepts it — it does not.
logger = logging.getLogger(__name__)

_STEP_CREATE_KEYS = frozenset({
    "title", "summary", "step_type", "webhook", "max_assignable", "position",
    "assignees", "guests", "groups", "tags",
    "deadline", "start_date",
    "allow_guest_owners", "skip_start_process", "can_complete_only_assignees",
    "everyone_must_complete", "prevent_guest_comment", "is_soft_start_date",
    "role_changes_every_time", "assign_run_starter", "top_secret",
    "send_chromeless",
})

# Bytes reserved for the JSON list brackets a single-step result is returned in,
# plus slack for transport framing. Measured: bounding the step dict alone
# produced a 25,002-byte list against a 25,000-byte ceiling.
#
# Placed here rather than above _STEP_CREATE_KEYS because a sibling branch
# inserts at that exact line; two insertions at one point conflict on merge.
_RESULT_CONTAINER_ALLOWANCE = 128

# Keys that reach the API and are then silently dropped, each with the reason and
# the path that does work. Naming them beats the old behaviour, where the SDK
# whitelist discarded anything it did not recognise and returned 2xx.
# 'description' is deliberately absent: it is mapped to 'summary' before the
# unknown-key check runs, so an entry here would be unreachable.
_STEP_REJECTED_KEYS = {
    "alias": (
        "steps have no settable alias at creation — CreateStepRequest has no rule "
        "for it, so the API discards it"
    ),
    "roles": (
        "roles cannot be set at step creation — CreateStepRequest has no rule for "
        "it, so the API discards it"
    ),
    "captures": (
        "use add_form_field_to_step instead, which normalizes option shapes that "
        "the raw create path does not"
    ),
}


# Keys api-v2's UpdateStepRequest carries a rule for, so they survive
# StepControllerNew::update's onlyValidatedFields() and actually reach the step.
#
# Derived by reading app/Http/Requests/Steps/UpdateStepRequest::rules() on
# origin/production, NOT copied from _STEP_CREATE_KEYS. The two contracts differ
# in BOTH directions and rule 27 says only the rules array answers "does api-v2
# accept X": create carries 'tags' and update does not, while update carries
# 'bp_to_launch' and the three 'ai_*' keys that the create whitelist never
# forwards. Harmonising the two sets would silently drop a caller's value.
_STEP_UPDATE_KEYS = frozenset({
    "title", "summary", "step_type", "webhook", "max_assignable",
    "assignees", "guests", "groups",
    "deadline", "start_date",
    "allow_guest_owners", "skip_start_process", "can_complete_only_assignees",
    "everyone_must_complete", "prevent_guest_comment", "is_soft_start_date",
    "role_changes_every_time", "assign_run_starter", "top_secret",
    "send_chromeless", "bp_to_launch",
    "ai_assigned", "ai_allowed_app_keys", "ai_on_uncertainty",
})

# Keys that reach the update endpoint and then do nothing, each with the reason
# and the path that does work. Rejecting them by name beats letting a caller
# believe a write landed (rules 25 and 27).
_STEP_UPDATE_REJECTED_KEYS = {
    # THE MOST DANGEROUS PAYLOAD THIS ENDPOINT TAKES, and it looks like the most
    # innocent. UpdateStepRequest::rules() on origin/production carries
    #     if (count($this->all()) === 1 && $this->has('position')) { return $rules; }
    # so a body of exactly {"position": N} skips the title requirement AND the
    # captures rules, and still reaches StepBuilder::build, which detaches every
    # assignee, group and guest. Refusing 'position' in ANY combination is what
    # makes that shape unrepresentable here rather than merely unlikely.
    # Reordering has its own route and controller (StepReorderController,
    # routes/api.php:436), which is what reorder_step already calls.
    "position": (
        "use reorder_step instead. A body of exactly {'position': N} is the single "
        "most dangerous payload this endpoint takes: UpdateStepRequest returns early "
        "for it, so the title requirement never applies, and it still detaches every "
        "assignee, group and guest. Reordering has its own endpoint, which is what "
        "reorder_step calls"
    ),
    "tags": (
        "UpdateStepRequest has no rule for it (CreateStepRequest does), so the "
        "API discards it on an update"
    ),
    "folders": (
        "validated but consumed by nothing on the step update path — neither "
        "StepBuilder, StepService nor StepControllerNew reads it"
    ),
    "captures": (
        "use add_form_field_to_step or update_form_field instead, which "
        "normalize option shapes that the raw update path does not"
    ),
    "alias": (
        "steps have no settable alias — UpdateStepRequest has no rule for it, "
        "so the API discards it"
    ),
    "roles": (
        "roles cannot be set through a step update — UpdateStepRequest has no "
        "rule for it, so the API discards it"
    ),
}

# The three buckets whose ABSENCE from a step PUT means "detach everyone", not
# "leave that dimension alone". StepBuilder::build (app/Step/StepBuilder.php:37
# on origin/production) unconditionally calls
#   saveAssignees(Assignees::newFromArray(Arr::only($data, ['assignees','guests','groups'])))
# and BaseAssignees::newFromArray reads `$data['users'] ?? $data['assignees'] ?? []`,
# so an omitted key yields an EMPTY SET, Assignees::modify diffs it against
# current and calls every existing member "removed", and
# AssignableTrait::saveAssignees detaches them. HTTP 200, no error.
#
# api-v2 origin/master FIXES this (commit 29dc8ff7a / PR #9587, 2026-07-24 —
# `assigneesFromPartial()` treats an omitted key as "leave alone"), but that fix
# is NOT deployed to production, so every tool here must keep re-sending the
# buckets. See tallyfy/api-v2#10052.
_STEP_ASSIGNEE_BUCKETS = ("assignees", "guests", "groups")

# STEP deadline direction codes, from api-v2 app/Step/Deadline.php:20 and :22
# (OPTION_FROM = 'from', OPTION_BEFORE = 'prior_to'). These are the only two
# values any consumer understands.
_STEP_DEADLINE_OPTIONS = ("from", "prior_to")

# All four travel together: CreateStepRequest and UpdateStepRequest both mark
# value/unit/option/step 'required_with:deadline', so a partial dict is a 422.
_STEP_DEADLINE_KEYS = ("value", "unit", "option", "step")

# 'after' and 'before' are the LABELS the Tallyfy UI puts on those two codes, not
# storable values: client-v2 services/step.service.ts::getDeadlineOptions maps
# key 'from' -> title 'after' and key 'prior_to' -> title 'before'.
#
# They are mapped rather than forwarded because a wrong option is SILENT.
# CreateStepRequest.php:39 is 'deadline.option' => 'required_with:deadline' with
# no enum (contrast :65, which does use `in:` where the API wants a whitelist),
# so any string is accepted with a 201. Then Helpers/tenant.php:175-182 switches
# on exactly 'from' and 'prior_to' with NO default arm, so no offset is applied,
# and UpdateChildrenTasksDeadlines.php:97-104 does Arr::get({from: add,
# prior_to: sub}, option) and returns early on a miss, so the deadline never
# re-anchors when the anchor step completes. Pass-through is therefore not the
# safe default it usually is: nothing downstream ever reports the mistake.
_STEP_DEADLINE_OPTION_ALIASES = {"after": "from", "before": "prior_to"}


def _normalize_step_deadline(step_data: dict) -> None:
    """Canonicalise deadline.option in place, or raise naming the two valid codes.

    Scoped deliberately to 'option'. The sibling keys fail LOUDLY already: a
    missing one is a 422 from CreateStepRequest's required_with rules, and a bad
    'unit' raises out of Deadline::validate()'s whitelist. 'option' is the only
    key of the four that api-v2 accepts, stores and then ignores.
    """
    deadline = step_data.get("deadline")
    if not isinstance(deadline, dict) or "option" not in deadline:
        return

    option = deadline["option"]
    if not isinstance(option, str):
        raise ToolError(
            f"deadline.option must be a string, got {type(option).__name__}. "
            f"Valid values: {' or '.join(_STEP_DEADLINE_OPTIONS)}."
        )

    key = option.strip().lower()
    if key in _STEP_DEADLINE_OPTIONS:
        resolved = key
    elif key in _STEP_DEADLINE_OPTION_ALIASES:
        resolved = _STEP_DEADLINE_OPTION_ALIASES[key]
    else:
        raise ToolError(
            f"deadline.option {option!r} is not a value Tallyfy stores. Use "
            f"'from' (the UI shows this as \"after\" the anchor) or 'prior_to' "
            f"(shown as \"before\" it). api-v2 accepts any string here and then "
            f"applies no offset at all, so nothing would report this."
        )

    if resolved != option:
        deadline = dict(deadline)
        deadline["option"] = resolved
        step_data["deadline"] = deadline


def _normalize_step_payload(step_data: dict, valid_keys, rejected_keys) -> dict:
    """Map caller-friendly aliases onto api-v2's field names and reject the rest.

    'description' is accepted and mapped to 'summary' because the tool documented
    'description' for a long time and callers learned it from the tool itself.
    Everything else outside `valid_keys` raises, naming the offending keys —
    a key that is silently dropped turns a caller mistake into a successful no-op
    with an empty step to show for it.

    The key set is a PARAMETER because create and update are different api-v2
    contracts (rule 27). One implementation with two vocabularies cannot drift
    the way two copies would (rule 16).
    """
    normalized = dict(step_data)

    # 'description' is an accepted alias, not an error — but only when it is not
    # competing with an explicit 'summary', which would make intent ambiguous.
    if "description" in normalized:
        if "summary" in normalized:
            raise ToolError(
                "step_data has both 'summary' and 'description'. They are the same "
                "field; pass only 'summary'."
            )
        normalized["summary"] = normalized.pop("description")

    unknown = sorted(set(normalized) - valid_keys)
    if unknown:
        details = [
            f"'{key}': {rejected_keys[key]}" if key in rejected_keys
            else f"'{key}': not a step field"
            for key in unknown
        ]
        raise ToolError(
            "step_data contains keys the API will discard, so nothing was sent. "
            + "; ".join(details)
            + ". Valid keys: "
            + ", ".join(sorted(valid_keys))
        )

    _normalize_step_deadline(normalized)

    return normalized


def _normalize_step_data(step_data: dict) -> dict:
    """Normalize a step CREATE payload against CreateStepRequest's rules."""
    return _normalize_step_payload(
        step_data, _STEP_CREATE_KEYS, _STEP_REJECTED_KEYS
    )


def _normalize_step_update_data(step_data: dict) -> dict:
    """Normalize a step UPDATE payload against UpdateStepRequest's rules."""
    normalized = _normalize_step_payload(
        step_data, _STEP_UPDATE_KEYS, _STEP_UPDATE_REJECTED_KEYS
    )
    _require_complete_step_deadline(normalized)
    return normalized


def _require_complete_step_deadline(step_data: dict) -> None:
    """Raise if a deadline is present but incomplete, before any network call.

    api-v2 answers a partial deadline with a 422, which is legible but costs a
    round trip. `_normalize_step_deadline` cannot cover this: it returns early
    when 'option' is absent, so a dict missing 'step' reaches the wire untouched.
    """
    deadline = step_data.get("deadline")
    if deadline is None:
        return
    if not isinstance(deadline, dict):
        raise ToolError(
            f"deadline must be a dict carrying "
            f"{', '.join(_STEP_DEADLINE_KEYS)}, got "
            f"{type(deadline).__name__}."
        )
    missing = [key for key in _STEP_DEADLINE_KEYS if key not in deadline]
    if missing:
        raise ToolError(
            f"To change a deadline, send all four keys together: "
            f"{{'value': 3, 'unit': 'days', 'option': 'from', 'step': 'start_run'}}. "
            f"Yours is missing {', '.join(missing)}. Read the current deadline with "
            f"get_template_steps, then restate every key including the ones you are "
            f"not changing. If you did not mean to touch the deadline at all, omit "
            f"'deadline' and the existing one is left alone. (api-v2 marks all four "
            f"of {', '.join(_STEP_DEADLINE_KEYS)} 'required_with:deadline', so a "
            f"partial deadline is rejected outright.)"
        )


def _assert_deadline_option_is_storable(payload: dict) -> None:
    """Last check on the OUTGOING body, immediately before the PUT.

    `_normalize_step_deadline` guards the caller's INPUT. This guards the bytes
    that leave, which is a different question once a payload is assembled from
    more than one source. It is the assertion that makes "this tool cannot emit
    an unstorable deadline direction" true of the request rather than of one
    code path.

    api-v2 accepts any string for deadline.option, stores it, and then applies no
    offset at all (rule 29), so nothing downstream would ever report a mistake
    here — which is exactly why the check belongs on our side of the wire.
    """
    deadline = payload.get("deadline")
    if not isinstance(deadline, dict) or "option" not in deadline:
        return
    option = deadline["option"]
    if option not in _STEP_DEADLINE_OPTIONS:
        raise ToolError(
            f"Refusing to send deadline.option {option!r}: Tallyfy stores only "
            f"{' and '.join(repr(o) for o in _STEP_DEADLINE_OPTIONS)}. api-v2 "
            f"would accept this with a 200 and then never apply the offset."
        )


def _fetch_step_for_update(sdk, org_id: str, template_id: str, step_id: str):
    """Return (endpoint, current step dict) for a step about to be updated."""
    endpoint = f"organizations/{org_id}/checklists/{template_id}/steps/{step_id}"
    current = sdk._make_request("GET", endpoint)
    step = current.get("data", current) if isinstance(current, dict) else {}
    return endpoint, step


def _deadline_anchors(steps: list, step_id: str) -> list:
    """Other steps whose deadline is anchored to ``step_id``.

    This is the SECOND thing api-v2 refuses a delete for, and the only one no
    existing tool reports. It maps to `Step::hasDeadlineDependents`
    (api-v2 app/Models/Step.php:263-266), which is:

        $this->whereRaw("deadline ->> 'step' = ?", [$this->id])->exists()

    Two properties of that query decide how this is written.

    Compare ids on the TRANSFORMED payload. `StepTransformer.php:33` re-emits the
    key as `timelineID($step->deadline['step'])`, so what arrives here is a
    timeline id, and both sides of the comparison have been through the same
    transform. That is why this takes already-fetched steps rather than querying.

    It is NOT scoped by checklist_id. api-v2 therefore checks the whole tenant
    while this scan is template-scoped, so a step in ANOTHER template anchored to
    this one is a FALSE NEGATIVE here: the pre-flight reports clean and the delete
    is still refused. That gap is why the tool description calls a clean
    pre-flight strong evidence rather than a guarantee.
    """
    anchors = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        if str(step.get("id", "")) == str(step_id):
            continue  # a step anchored to itself is not a dependent
        deadline = step.get("deadline")
        if not isinstance(deadline, dict):
            continue
        if str(deadline.get("step", "")) != str(step_id):
            continue
        anchors.append({
            "step_id": step.get("id"),
            "step_title": step.get("title"),
            "position": step.get("position"),
            "deadline": {
                "value": deadline.get("value"),
                "unit": deadline.get("unit"),
                "option": deadline.get("option"),
            },
        })
    return anchors


def _preserved_step_fields(step: dict, step_id: str) -> dict:
    """Build the MINIMUM every step PUT must carry, and nothing else.

    Two categories, each for a stated reason, and deliberately no third:

    - 'title', because UpdateStepRequest sets `$rules['title'] = 'required|max:600'`
      on every update that is not a bare position-only payload. Omitting it is a
      422 even when the caller is changing something unrelated.
    - the three assignee buckets, because omitting one DETACHES everyone in it
      (see _STEP_ASSIGNEE_BUCKETS).

    Everything else is left out on purpose. `StepBuilder::editStep` applies
    `Arr::only($data, $this->model->fields)`, so for summary, step_type, deadline,
    start_date, the booleans and bp_to_launch an absent key genuinely means "leave
    alone"; and `AddCapturesToStep` guards on `is_array($this->captures)`, so an
    absent 'captures' is a no-op rather than a wipe. Echoing them back would buy
    nothing and cost two things: it would widen the lost-update window to every
    field a concurrent editor might have touched, and it would launder a value
    already stored wrong straight back into the database — there are 62 production
    step rows carrying the unstorable deadline.option "after" (rule 29), and a
    read-modify-write that re-sent the deadline would rewrite each of them intact.

    A bucket that is missing or not a list means the READ failed, so this aborts
    rather than sending []. A genuinely empty list is legitimate and passes
    through: StepTransformer emits all three keys unconditionally, so present-and-
    empty is the real shape of an unassigned step (rule 10, verified live).
    """
    title = step.get("title") or ""
    if not title:
        raise ToolError(
            "Could not read the step's current title, which the API requires "
            "on every step update. Verify template_id and step_id are correct."
        )

    payload = {"title": title}
    for field in _STEP_ASSIGNEE_BUCKETS:
        value = step.get(field)
        if not isinstance(value, list):
            raise ToolError(
                f"Step {step_id} did not return its current '{field}', so "
                f"updating it would detach every assignee. Nothing was sent."
            )
        payload[field] = list(value)
    return payload


def register_template_management_tools(mcp):
    """Register all template management tools with the MCP server"""

    @mcp.tool(
        name="get_template",
        description=f"""Get a template (checklist) by its ID or name with full details.

MANDATORY: You MUST provide either 'template_id' OR 'template_name'. Calling with empty parameters WILL FAIL.

CORRECT usage examples:
- get_template(template_id="a1b2c3d4e5f6789012345678901234ef") - 32-char hex ID from a previous result
- get_template(template_name="Employee Onboarding") - when you know the template name

WRONG usage (will fail):
- get_template() - NO! Missing required parameter
- get_template(template_id="", template_name="") - NO! Must provide a value for one

If you don't have a template_id or name, use search_for_templates(query="...") first to find templates, or use get_all_templates() to list all templates.

PREFER A NARROWER TOOL when you know what you want. This returns the whole template in one
payload, so it hits the size ceiling soonest and is the most likely to come back trimmed:
- steps, or a step's id: get_template_steps
- automation rules: analyze_template_automations
- kick-off (prerun) fields: get_kickoff_fields

SIZE: if the response carries "_truncated", later steps were dropped and are NOT in it. If it
carries "_withheld", a named field was removed whole and is NOT in it either. The trim
reaches the step list only, so no marker is not a promise of completeness.
If any text CONTAINS "{TRUNCATION_MARKER_PREFIX}", that string is only part of the real value.
Never write a value carrying that marker back to Tallyfy; re-read it first with
get_template_steps(step_id=..., full_text=True).""",
        tags=["templates", "blueprints", "read-only"],
        annotations=ToolAnnotations(
            title="Get template",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_template")
    @handle_tallyfy_errors("get template")
    def get_template(
        template_id: OptionalString = "",
        template_name: OptionalString = "",
    ) -> GenericDict:
        """
        Get a template (checklist) by its ID or name with full details.

        Args:
            template_id: Template (checklist) ID (provide this OR template_name, not both)
            template_name: Template (checklist) name (provide this OR template_id, not both)

        Returns:
            Template object, with its step list trimmed when the payload
            exceeds the result ceiling
        """
        if not template_id.strip() and not template_name.strip():
            raise ToolError("Either template_id or template_name must be provided")

        if template_id.strip() and template_name.strip():
            raise ToolError("Only one of template_id or template_name should be provided")

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            if template_name.strip():
                template = sdk.templates.get_template(org_id, template_name=template_name.strip())
            else:
                template = sdk.templates.get_template(org_id, template_id=template_id.strip())
            if not template:
                return ToolResult(content={}, structured_content=None)
            # Whole-template reads have no size control of their own: compact_result
            # only trims list-shaped results. Route through the ONE shared trimmer so
            # an oversize template loses steps visibly, with a _truncated marker,
            # instead of being handed over the 25KB ceiling silently.
            return ToolResult(
                content=compact_dict_list_field(
                    serialize_dataclass(template), "steps", item_label="steps"
                ),
                structured_content=None
            )

    @mcp.tool(
        name="get_all_templates",
        description="Get templates (checklists) with full details including prerun fields, automated actions, linked tasks, and metadata. Returns 20 per page. Use page=2, page=3, etc. for more. meta.total_pages shows total page count. Optional: filter by folder name or folder ID.",
        tags=["templates", "blueprints", "read-only", "management"],
        annotations=ToolAnnotations(
            title="Get all templates",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_all_templates")
    @handle_tallyfy_errors("get all templates")
    def get_all_templates(page: PageNumber = 1, folder: OptionalString = None) -> GenericDict:
        """
        Get templates (checklists) with full details.

        Args:
            page: Page number to fetch (default: 1)
            folder: Optional folder ID (32-char hex) or folder name to filter templates by folder

        Returns:
            Dict with 'data' (list of templates) and 'meta' (pagination info)
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            kwargs = {}
            if folder:
                if not _is_tallyfy_id(folder):
                    folder = _resolve_template_folder_name_to_id(sdk, org_id, folder)
                kwargs["folder"] = folder
            return ToolResult(
                content=fetch_single_page(
                    sdk.templates.get_all_templates, org_id,
                    page=page,
                    compact_fields=["guidance"],
                    **kwargs,
                ),
                structured_content=None
            )

    @mcp.tool(
        name="get_step_dependencies",
        description="""Read-only: analyze which automations affect when this step appears in the workflow. Inspects all template rules referencing the step as condition trigger OR action target.

RETURN: {step_info: {id,title,position,summary}, dependencies: {incoming: [{step_id,step_title,condition_type,automation_id,description}], outgoing: [{step_id,step_title,action_type,automation_id,description}], field_dependencies: [{field_label,expected_value,condition_type,automation_id,description}], conditional_visibility: [{action_type:"show_step"|"hide_step",automation_id,description}], deadline_anchors: [{step_id,step_title,position,deadline:{value,unit,option}}]}, complexity_analysis: {score:0-100, level:"Low"|"Medium"|"High", total_dependencies, incoming_count, outgoing_count, field_dependencies_count, visibility_conditions_count, deadline_anchor_count}, recommendations: [advisory strings], template_id}

BEFORE DELETING: `deadline_anchors` plus incoming/outgoing are the TWO delete blockers. See delete_step.

KEY: `conditional_visibility` gives automation_ids of show/hide rules; read them with `analyze_template_automations` or `get_step_visibility_conditions`.

USE CASES: "What does this step depend on?"→incoming · "What does this step trigger?"→outgoing · "Which fields gate it?"→field_dependencies · "Is visibility conditional?"→conditional_visibility · "Should this step be split?"→complexity_analysis.level

EXAMPLE: get_step_dependencies(template_id="58c03f...", step_id="9bc2...") → {step_info:{title:"Manager approval",position:4}, dependencies:{incoming:[{step_title:"Submit request",condition_type:"task_completed"}], outgoing:[{step_title:"Notify employee",action_type:"send_email"}], field_dependencies:[{field_label:"Amount",expected_value:">1000"}]}, complexity_analysis:{score:35,level:"Medium"}, recommendations:["Consider extracting the amount-gate"]}

REQUIRED: 'template_id' AND 'step_id' (both 32-char hex). Never call without both.""",
        tags=["templates", "workflow", "analysis", "automation", "read-only"],
        annotations=ToolAnnotations(
            title="Get step dependencies",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_step_dependencies")
    @handle_tallyfy_errors("analyze step dependencies")
    def get_step_dependencies(template_id: TemplateId, step_id: StepId) -> GenericDict:
        """
        Analyze which automations affect when this step appears.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID to analyze (REQUIRED - 32-character hex string)

        Returns:
            Dictionary containing dependency analysis
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.get_step_dependencies(org_id, template_id, step_id)
            payload = serialize_dataclass(result) if result else {}
            if not isinstance(payload, dict):
                return ToolResult(content=payload, structured_content=None)

            dependencies = payload.setdefault("dependencies", {})
            complexity = payload.setdefault("complexity_analysis", {})

            # One extra round trip, DELIBERATELY. The SDK's dependency walk does
            # not look at deadlines, and re-implementing it here to save a call
            # would create a second copy that drifts from the first.
            #
            # This is best-effort ON PURPOSE. Before it was added, this tool
            # returned the automation dependencies it had already fetched; a
            # transient failure on this second call must not take those away.
            # But it must not report a clean anchor check either, so on failure
            # the count is LEFT UNSET and the reason is stated. An absent count
            # means "not checked"; 0 means "checked, and there are none".
            try:
                template = sdk._make_request(
                    "GET",
                    f"organizations/{org_id}/checklists/{template_id}",
                    params={"with": "steps"},
                )
                # The block below is what separates "checked" from "not checked",
                # which is the whole contract stated just above. It replaces two
                # unguarded lines, and every shape it now rejects used to fall
                # through to `raw_steps = None`, which `_normalize_steps` answers
                # with [] -- so the tool reported a confident
                # `deadline_anchor_count: 0` about a payload it had never read.
                # Raising routes them into the `except` below, which already IS
                # the unavailable path, so one place builds the marker and the
                # two cannot drift.
                if not isinstance(template, dict):
                    raise ValueError(
                        f"template read returned {type(template).__name__}, not an object"
                    )
                data = template.get("data", template)
                # `data` being a LIST is deliberately NOT rejected: that is what
                # an empty template answers with, and an empty collection
                # provably holds no step anchored anywhere, so 0 is a real
                # answer there. Flipping it would make every empty template read
                # as unchecked, which is worse than the gap being closed here.
                raw_steps = data.get("steps") if isinstance(data, dict) else data
                # Anything `_normalize_steps` would answer [] for WITHOUT having
                # read a list. Absent and null `steps` both land here, and so
                # does a `steps` that is neither a list nor a Fractal envelope.
                # After the normalizer runs, "[] because empty" and "[] because
                # unparseable" are indistinguishable, so the check has to be
                # here rather than on its result.
                if not (
                    isinstance(raw_steps, list)
                    or (isinstance(raw_steps, dict) and "data" in raw_steps)
                ):
                    raise ValueError(
                        "template read carried no readable 'steps' include "
                        f"(got {type(raw_steps).__name__})"
                    )

                # `?with=steps` is a Fractal INCLUDE, and ChecklistTransformer
                # builds it with $this->collection() while its siblings use
                # $this->map(). AppServiceProvider registers a bare `new Manager`
                # and never calls setSerializer, so Fractal's default
                # DataArraySerializer is in force and the include arrives as
                # {"data": [...]}, NOT as a flat list. Reading the key directly
                # yields [] on every real response. The SDK already solves this
                # in exactly one place, so use that rather than a second copy.
                #
                # `.analysis`, NOT the facade. `sdk.templates` is a
                # `TemplateManager` whose MRO is [TemplateManager, object]: it
                # does NOT inherit `TemplateManagerBase`. It is a facade that
                # re-exports a hand-written list of PUBLIC methods onto three
                # sub-managers, and `_normalize_steps` is private, so it is not
                # on that list. `sdk.templates._normalize_steps(...)` raises
                # AttributeError, which the `except` below then swallows into
                # the unavailable path -- i.e. the feature never runs at all.
                # `.analysis` is the sub-manager that OWNS this call: it is the
                # only module in the SDK that uses `_normalize_steps`, in
                # `get_step_dependencies` and `get_step_visibility_conditions`,
                # on this very payload. There is no public equivalent --
                # `TemplateManagerBase` exposes zero public methods, and
                # `get_template_steps` is a different endpoint plus a second
                # round trip that returns dataclasses rather than raw dicts.
                steps = sdk.templates.analysis._normalize_steps(raw_steps)
                anchors = _deadline_anchors(steps, step_id)
            except Exception as exc:  # noqa: BLE001 - degrade, never fail the call
                logger.warning(
                    "Deadline-anchor pre-flight failed for step %s: %s", step_id, exc
                )
                if isinstance(dependencies, dict):
                    dependencies["deadline_anchors_unavailable"] = (
                        "The deadline-anchor check could not run, so this result does "
                        "NOT tell you whether other steps are anchored to this one. "
                        "Treat it as unchecked, not as clear."
                    )
                return ToolResult(content=payload, structured_content=None)

            if isinstance(dependencies, dict):
                dependencies["deadline_anchors"] = anchors
            if isinstance(complexity, dict):
                complexity["deadline_anchor_count"] = len(anchors)

            return ToolResult(content=payload, structured_content=None)

    @mcp.tool(
        name="suggest_step_deadline",
        description="""Retrieve step details with template context to recommend an appropriate deadline.

Returns the step's title, summary, position in the workflow, current deadline (if any), assignees,
and the total number of steps in the template.

Use this data to suggest a reasonable deadline by considering:
- Step complexity (review/approve steps are quick; document creation takes longer)
- Position in workflow (early steps may need faster turnaround)
- Dependencies and assignee count
- Whether the step has form fields that require data gathering

DEADLINE SHAPE: all four keys travel together. api-v2 marks value, unit, option and step
'required_with:deadline', so omitting any one of them is a 422.
- value: a number.
- unit: 'minutes', 'hours', 'days', 'weeks' or 'months' (singular forms accepted too).
- option: the DIRECTION, not the anchor. 'from' = after the anchor, 'prior_to' = before it.
- step: the ANCHOR. 'start_run' means process launch; any other step ID anchors to that
  step. Launch-relative is step='start_run', NOT option='from'.
Default deadline is value=1, unit='day', option='from', step='start_run'.

This tool only SUGGESTS. To apply a suggestion, pass the same four-key dict to update_step as step_data={'deadline': {...}}.

REQUIRED: Both 'template_id' and 'step_id' must be provided (32-character hex strings). Never call this without both parameters.""",
        tags=["templates", "workflow", "analysis", "deadlines", "read-only"],
        annotations=ToolAnnotations(
            title="Suggest step deadline",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("suggest_step_deadline")
    @handle_tallyfy_errors("suggest step deadline")
    def suggest_step_deadline(template_id: TemplateId, step_id: StepId) -> GenericDict:
        """
        Retrieve step details with template context for deadline recommendation.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID to analyze (REQUIRED - 32-character hex string)

        Returns:
            Dictionary with step data and template context for deadline analysis
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            template = sdk.templates.get_template(org_id, template_id=template_id)
            if not template:
                raise ToolError("Template not found")

            steps = template.steps or []
            target_step = None
            for step in steps:
                if step.id == step_id:
                    target_step = step
                    break

            if not target_step:
                raise ToolError(f"Step {step_id} not found in template {template_id}")

            return ToolResult(
                content={
                    'step': serialize_dataclass(target_step),
                    'template_title': template.title,
                    'total_steps': len(steps),
                    'template_id': template_id,
                },
                structured_content=None
            )

    @mcp.tool(
        name="add_assignees_to_step",
        description="""Add assignees (users or guests or both) to a specific step in a template.

REQUIRED: 'template_id' (32-char hex), 'step_id' (32-char hex), and 'assignees'.

'assignees' accepts the following format:
Dict with 'users' and/or 'guests' keys (to add guests by email):
assignees: {"users": [10026], "guests": ["alice@example.com"]}
assignees: {"guests": ["alice@example.com"]}
assignees: {"users": [10026]}

THIS IS THE RECIPIENT LIST FOR AN EMAIL STEP. A step of type 'email' or
'expiring_email' has NO separate "recipient", "to" or "send_to" field, and no
tool sets one. Its assignees ARE the "To" line. Tallyfy says so in the interface
by renaming that step's Assign tab to "To" for exactly those two step types. The
subject is the step's 'title' and the body is its 'summary', both set through
update_step. Asked who an email goes to, answer with this list: "email this to
Dana" means adding Dana here.

TWO EMAIL TYPES, named differently in the UI:
  'email' = "Email Draft". A HUMAN clicks SEND; it does not send itself.
  'expiring_email' = "Email Auto-Send". Goes out on the deadline, self-completes.
Going out on its own? 'expiring_email'. Reviewed first? 'email'.

This APPENDS: the tool reads the step first and re-sends its existing members,
guests and groups alongside the additions, which the API would otherwise clear
on an update that omits them. Existing assignees are never removed. To REPLACE
or clear a bucket, use update_step and pass the full list, or an empty one.

SEPARATELY, every step carries 'allow_guest_owners', which Tallyfy defaults to
TRUE. That flag is what shows the "Guest" slot on a step, so a brand new step
offers one whether or not a guest was ever added. Assigning members here does
NOT turn it off, and no MCP tool clears it; only the Assign tab in the browser
does, as a side effect of choosing certain assignee modes. To stop a step
offering a guest slot, send 'allow_guest_owners': False through update_step.

Never call this without all three parameters.""",
        tags=["templates", "workflow", "write", "management", "assignees"],
        annotations=ToolAnnotations(
            title="Add assignees to step",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("add_assignees_to_step")
    @handle_tallyfy_errors("add assignees to step")
    def add_assignees_to_step(
        template_id: TemplateId,
        step_id: StepId,
        assignees: Any,
    ) -> GenericDict:
        """
        Add assignees to a specific step in a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID to add assignees to (REQUIRED - 32-character hex string)
            assignees: A dict with 'users' and/or 'guests' keys, e.g.
                       {"users": [10026, 64878], "guests": ["alice@example.com"]}.
                       'users' takes numeric user IDs, 'guests' takes email addresses.
                       A bare list is REJECTED with a ToolError, so wrap IDs as
                       {"users": [10026, 64878]} rather than passing [10026, 64878].

        Returns:
            Dictionary containing updated step information
        """
        # Normalise to a plain list of user IDs + optional guests list
        # The SDK expects add_assignees_to_step(..., assignees: List[int], guests: Optional[List[str]])
        # LLMs often pass stringified JSON or bare values — coerce gracefully
        if isinstance(assignees, str):
            import json
            try:
                assignees = json.loads(assignees)
            except (json.JSONDecodeError, ValueError):
                raise ToolError('assignees must be a dict like {"users": [10026]} or {"guests": ["alice@example.com"]} or {"users": [10026], "guests": ["alice@example.com"]}')

        if not isinstance(assignees, dict):
            raise ToolError('assignees must be a dict like {"users": [10026]} or {"guests": ["alice@example.com"]} or {"users": [10026], "guests": ["alice@example.com"]}')

        # `or []` on both: an explicit null means "none given", not a type error.
        # Strict guidance in the description, lenient parsing here.
        user_ids = assignees.get('users') or []
        guest_emails = assignees.get('guests') or []

        if not isinstance(user_ids, list):
            raise ToolError("assignees['users'] must be a list of numeric user IDs")
        for user_id in user_ids:
            if not isinstance(user_id, int):
                raise ToolError(f"User ID {user_id!r} must be an integer")

        if not isinstance(guest_emails, list):
            raise ToolError("assignees['guests'] must be a list of email addresses")
        validated_guests = []
        for guest_email in guest_emails:
            if not isinstance(guest_email, str):
                raise ToolError(f"Guest email {guest_email!r} must be a string")
            try:
                # check_deliverability=False — syntax only, NO DNS lookup. The SDK
                # path this replaces used the library default (True), which resolves
                # MX records on every call: a network round-trip plus a transient
                # failure mode inside a tool call, and it rejected this tool's OWN
                # documented example (alice@example.com has a null MX by RFC 2606).
                # api-v2 is the authority on whether a guest address is acceptable.
                validated_guests.append(
                    validate_email(guest_email, check_deliverability=False).normalized
                )
            except EmailNotValidError as exc:
                raise ToolError(f"Invalid email address: {exc}")

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # READ-MODIFY-WRITE, done here rather than via the SDK. The SDK's
            # templates.add_assignees_to_step() (tallyfy/template_management/
            # automation.py:248-254) builds {title, assignees, guests} with NO
            # 'groups' key at all, and StepBuilder::build (app/Step/StepBuilder.php:37)
            # unconditionally calls
            #   saveAssignees(Assignees::newFromArray(Arr::only($data, ['assignees','guests','groups'])))
            # where BaseAssignees::newFromArray (app/Domain/Owners/BaseAssignees.php:24)
            # reads `$data['groups'] ?? []` — an ABSENT key is an EMPTY SET, not
            # "leave alone". So ADDING one member silently DETACHED every group
            # from the step. Same mechanism as edit_description_on_step above.
            endpoint = f"organizations/{org_id}/checklists/{template_id}/steps/{step_id}"
            current = sdk._make_request("GET", endpoint)
            step = current.get("data", current) if isinstance(current, dict) else {}

            title = step.get("title") or ""
            if not title:
                raise ToolError(
                    "Could not read the step's current title, which the API requires "
                    "on every step update. Verify template_id and step_id are correct."
                )

            # StepTransformer.php:33-35 emits all three buckets unconditionally, so a
            # missing or malformed bucket means the READ failed, not that the step is
            # unassigned — and sending [] on a failed read is the wipe this guard
            # exists to prevent. An empty list is legitimate and passes through.
            payload = {"title": title}
            for field in ("assignees", "guests", "groups"):
                value = step.get(field)
                if not isinstance(value, list):
                    raise ToolError(
                        f"Step {step_id} did not return its current '{field}', so "
                        f"adding an assignee would detach every assignee. "
                        f"Nothing was sent."
                    )
                payload[field] = list(value)

            # Append-not-replace, preserving existing order (a set would reorder
            # nondeterministically between runs).
            for user_id in user_ids:
                if user_id not in payload["assignees"]:
                    payload["assignees"].append(user_id)
            for guest_email in validated_guests:
                if guest_email not in payload["guests"]:
                    payload["guests"].append(guest_email)

            response = sdk._make_request("PUT", endpoint, data=payload)
            result = response.get("data", response) if isinstance(response, dict) else response
            return ToolResult(content=result or {}, structured_content=None)

    @mcp.tool(
        name="edit_description_on_step",
        description="""Edit the description/summary of a specific step in a template. The description supports HTML — use this to add rich instructions, checklists, or converted document content to a step. When a user wants to convert a document to step instructions, read the document content yourself and write the HTML here.

Only the description changes: this tool reads the step first and re-sends its
existing title and assignees (members, guests and groups), which the API would
otherwise clear on an update that omits them. To change who a step is assigned to,
use add_assignees_to_step instead.

REQUIRED: 'template_id' (32-char hex), 'step_id' (32-char hex), and 'description' (new text, HTML allowed). Never call this without all three parameters.""",
        tags=["templates", "workflow", "write", "management", "editing"],
        annotations=ToolAnnotations(
            title="Edit step description",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("edit_description_on_step")
    @handle_tallyfy_errors("edit step description")
    def edit_description_on_step(
        template_id: TemplateId,
        step_id: StepId,
        description: StepDescription
    ) -> GenericDict:
        """
        Edit the description/summary of a specific step in a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID to edit description for (REQUIRED - 32-character hex string)
            description: New description/summary text for the step (REQUIRED)

        Returns:
            Dictionary containing updated step information
        """
        if not isinstance(description, str) or not description.strip():
            raise ToolError("description cannot be empty")

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # READ-MODIFY-WRITE — the SDK's edit_description_on_step() sends only
            # {title, summary}, and an omitted assignee bucket means "detach
            # everything" on production (see _STEP_ASSIGNEE_BUCKETS). Editing a
            # description therefore wiped every assignee off the step. Re-send the
            # current sets: the diff comes out empty and saveAssignees returns
            # before touching the pivot tables.
            #
            # _preserved_step_fields is shared with update_step on purpose. Two
            # tools writing the same endpoint with two copies of this logic is
            # exactly the sibling drift rule 16 is about; one helper cannot drift.
            endpoint, step = _fetch_step_for_update(
                sdk, org_id, template_id, step_id
            )
            payload = _preserved_step_fields(step, step_id)
            payload["summary"] = description

            response = sdk._make_request("PUT", endpoint, data=payload)
            result = response.get("data", response) if isinstance(response, dict) else response
            return ToolResult(content=serialize_dataclass(result) if result else {}, structured_content=None)

    @mcp.tool(
        name="update_step",
        description="""Edit an EXISTING step in place, keeping its id, automations, form fields and history. To RENAME a step, pass 'title'. Also use this to change its deadline, start date, type or instructions. Never delete_step then add_step_to_template to edit: that mints a NEW id and orphans every automation pointing at the old one.

REQUIRED: 'template_id' (32-char hex), 'step_id' (32-char hex), and 'step_data', a dict of ONLY the fields to change. A field you omit is left alone; pass an empty list to CLEAR one. An unknown key is refused with the full list of valid ones.

step_data keys:
  - 'title': the step name. This is the rename.
  - 'summary': HTML instructions for the assignee ('description' is an alias).
  - 'deadline': dict {'value': int, 'unit': 'days', 'option': 'from', 'step': 'start_run'}. All four travel TOGETHER; a partial deadline is refused. 'option' is the DIRECTION and takes exactly TWO values: 'from' (shown in the UI as "after") or 'prior_to' (shown as "before"). Those two words are display labels, never values. 'step' is the ANCHOR: 'start_run' means process launch, else another step's id.
  - 'start_date': dict {'value': int, 'unit': 'days'}. INERT ON ITS OWN. Every step is born "start anytime" (is_soft_start_date defaults TRUE), which ignores start_date. To make it bite, send 'is_soft_start_date': False in the SAME call. 'value' must be 1 or more, so it cannot be cleared with 0.
  - 'step_type': 'task', 'approval', 'expiring', 'email' (draft) or 'expiring_email' (auto-sends)
  - 'assignees': member IDs (ints), 'guests': emails, 'groups': group IDs. On an 'email' or 'expiring_email' step these ARE the email's "To" line, not merely who is responsible. See add_assignees_to_step.
  - also: 'allow_guest_owners', 'is_soft_start_date', 'everyone_must_complete', 'can_complete_only_assignees', 'assign_run_starter', 'webhook', 'max_assignable'

To MOVE a step use reorder_step; for questions use add_form_field_to_step.

Never call this without all three parameters.""",
        tags=["templates", "workflow", "write", "management", "editing", "deadlines"],
        annotations=ToolAnnotations(
            title="Update a step in place",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("update_step")
    @handle_tallyfy_errors("update step")
    def update_step(
        template_id: TemplateId,
        step_id: StepId,
        step_data: GenericDict
    ) -> GenericDict:
        """
        Edit an existing step in place without changing its id.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID to update (REQUIRED - 32-character hex string)
            step_data: Only the fields to change (REQUIRED - must be non-empty)

        Returns:
            Dictionary containing the updated step, carrying the SAME id
        """
        if not isinstance(step_data, dict) or not step_data:
            raise ToolError(
                "step_data must be a non-empty dict naming the fields to change, "
                "e.g. {'deadline': {'value': 3, 'unit': 'days', "
                "'option': 'from', 'step': 'start_run'}}. Nothing was sent."
            )

        changes = _normalize_step_update_data(step_data)

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # READ-MODIFY-WRITE, and the READ is not optional. See
            # _preserved_step_fields for exactly what is preserved and why the
            # list is deliberately short.
            endpoint, step = _fetch_step_for_update(
                sdk, org_id, template_id, step_id
            )
            payload = _preserved_step_fields(step, step_id)

            # The caller's values win over the preserved ones, so passing
            # 'title' renames and passing 'assignees': [] genuinely clears.
            payload.update(changes)

            # Last gate on the bytes that leave, not on the input.
            _assert_deadline_option_is_storable(payload)

            response = sdk._make_request("PUT", endpoint, data=payload)
            result = response.get("data", response) if isinstance(response, dict) else response
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None,
            )

    @mcp.tool(
        name="add_step_to_template",
        description="""Add a new step to a template. Call this repeatedly after create_template, one call per step, in order. When building from a user description or document, break the workflow into logical steps and call this for each.

REQUIRED: 'template_id' (32-char hex) and 'step_data' (dict with 'title'). An unknown key is refused with the full list of valid ones.

step_data keys:
  - 'title': step name (REQUIRED)
  - 'summary': HTML instructions for the assignee ('description' is an alias)
  - 'position': 1-based order. Steps always append, so this tool issues a follow-up reorder. Omit when adding in order.
  - 'deadline': dict {'value': int, 'unit': 'days', 'option': 'from', 'step': 'start_run'} (all four required together). 'option' is the DIRECTION and takes exactly TWO values: 'from' (shown in the UI as "after") or 'prior_to' (shown as "before"). Those two words are display labels, never values. 'step' is the ANCHOR, not the direction.
  - 'start_date': dict {'value': int, 'unit': 'days'}. INERT unless you also send 'is_soft_start_date': False. Every new step is born "start anytime", which ignores start_date.
  - 'assignees': member IDs (ints), 'guests': emails, 'groups': group IDs. On an 'email' or 'expiring_email' step these ARE the email's "To" line; there is no separate recipient field.
  - 'allow_guest_owners': DEFAULTS TO TRUE, so every new step offers a Guest slot whether or not you pass guests. Send False if it should not.
  - 'step_type': one of 5 values (default 'task'):
      'task'           completed by the assignee
      'approval'       approve/reject decision. MUST be used for any approval, review or sign-off step: it is what enables the 'approved' and 'rejected' automation conditions, which will not fire without it.
      'expiring'       auto-completes at the deadline
      'email'          "Email Draft": a human clicks SEND
      'expiring_email' "Email Auto-Send": sends at deadline, self-completes

Never call this without both parameters.""",
        tags=["templates", "workflow", "write", "management", "creation"],
        annotations=ToolAnnotations(
            title="Add step to template",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("add_step_to_template")
    @handle_tallyfy_errors("add step to template")
    def add_step_to_template(template_id: TemplateId, step_data: GenericDict) -> GenericDict:
        """
        Add a new step to a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_data: Dictionary containing step data including title, summary, position, etc. (REQUIRED - must include 'title')

        Returns:
            Dictionary containing created step information
        """
        if 'title' not in step_data:
            raise ToolError("step_data must contain 'title' field")
        if not step_data['title']:
            raise ToolError("step_data.title must not be empty")

        step_data = _normalize_step_data(step_data)
        position = step_data.pop("position", None)

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.add_step_to_template(org_id, template_id, step_data)

            if position is None:
                return ToolResult(
                    content=serialize_dataclass(result), structured_content=None
                )

            # api-v2 discards position at creation TWICE over: CreateStepRequest has
            # no rule for it, so onlyValidatedFields() strips it, and
            # StepBuilder::buildBasicStep unsets it explicitly ("when creating a step
            # it always goes at the end of the step list"). Honour the documented
            # parameter with the follow-up call the API actually requires.
            #
            # This is a no-op in the common case: a template built in order appends
            # each step at the position it asked for, and set_step_index's UPDATE
            # carries "where position is distinct from new_position", so no row
            # changes. It only moves rows when inserting mid-flow, which is exactly
            # the case that silently failed before.
            step_id = result.get("id") if isinstance(result, dict) else None
            if not step_id:
                return ToolResult(
                    content=serialize_dataclass(result), structured_content=None
                )

            try:
                reordered = sdk.templates.reorder_step(
                    org_id, template_id, step_id, position
                )
            except Exception as exc:
                # Deliberately not raising: the step EXISTS. Raising here reads as
                # "the call failed" and invites a retry that creates a duplicate
                # step. Report the partial success instead so the caller can decide.
                result = dict(result)
                result["_position_warning"] = (
                    f"Step created, but reordering it to position {position} failed "
                    f"({exc}). The step is at the end of the template. Call "
                    f"reorder_step to move it."
                )
                return ToolResult(
                    content=serialize_dataclass(result), structured_content=None
                )

            # Return the REORDER response, not the create response. Both come from
            # StepTransformer, which emits 'position' => (int) $step->position
            # unconditionally, so the create payload carries the append-time position
            # and would report a slot the step no longer occupies — the exact
            # class of untrue-but-2xx reporting this tool is being fixed for.
            #
            # Caveat worth knowing: api-v2 clamps server-side without refreshing the
            # model. StepService::reorderStep assigns and saves, then the saved hook
            # runs set_step_index, which clamps via least(greatest(_index,1), n) in
            # raw SQL. The in-memory model keeps the REQUESTED value, so asking for a
            # position beyond the step count lands the step at the end while the
            # response still echoes what was asked for. Filed separately.
            #
            # serialize_dataclass on EVERY return arm deliberately: it is the
            # sanitization chokepoint (#170/#326), translating internal field_type
            # codes under captures. Applying it to only the reordered arm would make
            # the returned shape depend on whether the caller passed a position.
            return ToolResult(
                content=serialize_dataclass(reordered or result),
                structured_content=None,
            )

    @mcp.tool(
        name="suggest_kickoff_fields",
        description="""Retrieve template data with existing kickoff fields and step context to recommend new kickoff fields.

Returns the template title, summary, existing prerun/kickoff fields, and step titles/summaries.

Use this data to suggest kickoff fields that would help initialize the workflow by considering:
- What information the steps will need (client names, project details, dates, budgets)
- What existing kickoff fields already capture (avoid duplicates)
- The template's domain and purpose (inferred from title, summary, and step content)
- Field types: text, textarea, date, dropdown, multiselect, radio, file, table, assignees_form
  (there is NO `number` and NO `checkbox` field type — add_kickoff_field rejects both)

REQUIRED: 'template_id' (32-character hex string). Never call this without the template_id parameter.""",
        tags=["templates", "workflow", "analysis", "kickoff", "read-only"],
        annotations=ToolAnnotations(
            title="Suggest kickoff fields",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("suggest_kickoff_fields")
    @handle_tallyfy_errors("suggest kickoff fields")
    def suggest_kickoff_fields(template_id: TemplateId) -> GenericDict:
        """
        Retrieve template data with existing kickoff fields for field suggestions.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)

        Returns:
            Dictionary with template metadata, existing prerun fields, and step summaries
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            template = sdk.templates.get_template(org_id, template_id=template_id)
            if not template:
                raise ToolError("Template not found")

            existing_prerun = [serialize_dataclass(f) for f in template.prerun] if template.prerun else []
            steps_summary = []
            if template.steps:
                for s in template.steps:
                    steps_summary.append({
                        'id': s.id,
                        'title': s.title,
                        'summary': s.summary,
                    })

            return ToolResult(
                content={
                    'template_id': template_id,
                    'template_title': template.title,
                    'template_summary': template.summary,
                    'existing_kickoff_fields': existing_prerun,
                    'steps': steps_summary,
                },
                structured_content=None
            )

    @mcp.tool(
        name="get_kickoff_fields",
        description="Get all kickoff/prerun fields for a template. REQUIRED: 'template_id' (32-character hex string). Never call this without the template_id parameter.",
        tags=["templates", "kickoff", "prerun", "forms", "read-only"],
        annotations=ToolAnnotations(
            title="Get kickoff fields",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_kickoff_fields")
    @handle_tallyfy_errors("get kickoff fields")
    def get_kickoff_fields(template_id: TemplateId) -> GenericList:
        """
        Get all kickoff/prerun fields for a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)

        Returns:
            List of kickoff field objects
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # Get the template which includes prerun fields
            template = sdk.templates.get_template(org_id, template_id=template_id)
            if template and template.prerun:
                return ToolResult(
                    content=compact_result([serialize_dataclass(field) for field in template.prerun]),
                    structured_content=None
                )
            return ToolResult(content=[], structured_content=None)

    @mcp.tool(
        name="get_template_steps",
        description=f"""Get all steps for a template in order. USE THIS instead of get_template when the user asks about steps.

MANDATORY: 'template_id' (32-char hex string) is required.

USE THIS TOOL when the user asks:
- "What are the steps in [template]?"
- "List the steps of [template]"
- "Show me the steps for [template]"
- Any question about a template's steps or structure
- Finding a step's ID before editing or assigning it

WORKFLOW: If you don't have the template_id yet:
1. Call search_for_templates(query="<template name>") to get the template_id
2. Then call get_template_steps(template_id="<id>")

CORRECT usage:
- get_template_steps(template_id="abc123...") - every step, long text shortened
- get_template_steps(template_id="abc123...", step_id="def456...") - one step
- get_template_steps(template_id="abc123...", step_id="def456...", full_text=True) - that
  step's text in full, across as many calls as it takes

READING LONG TEXT: to keep all steps in one response, long text is shortened and marked
"{TRUNCATION_MARKER_PREFIX} ...]". A marked value is NOT the full text. Never write one back;
you would overwrite whatever was cut. Re-read that one step with full_text=True first.
'full_text' needs a 'step_id' (it is refused without one) because one step's text can be
paged and a whole template of them cannot.

Text longer than one response is delivered IN PARTS. Each part says which characters it
covers and names the next offset, e.g. "characters 0 to 24461 of 60000 ... call again with
text_offset=24461". Keep calling with the offset you were given and join the parts in
order; the final part says "This is the LAST part." Do not write a part back on its own.

DO NOT call get_template just to read its steps — use this tool instead.""",
        tags=["templates", "steps", "workflow", "read-only"],
        annotations=ToolAnnotations(
            title="Get template steps",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_template_steps")
    @handle_tallyfy_errors("get template steps")
    def get_template_steps(
        template_id: TemplateId,
        step_id: OptionalString = "",
        full_text: bool = False,
        text_offset: int = 0,
    ) -> GenericList:
        """
        Get all steps for a template in order, or one step in full.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Return only this step. Required when full_text is True.
            full_text: Return the step's text in full, in parts if it does not
                fit one response. Refused without step_id: the cap is what keeps
                a whole template under the 25KB result ceiling, so lifting it
                for a list would re-create the truncation this flag exists to
                escape.
            text_offset: Character offset to resume from, taken from the marker
                on the previous part. Only meaningful with full_text.

        Returns:
            List of step objects with id, title, position, and other step properties
        """
        wanted = step_id.strip()
        if text_offset and not full_text:
            raise ToolError(
                "text_offset only means anything with full_text=True. Without it "
                "the text is capped per string, not paged."
            )
        if text_offset < 0:
            raise ToolError(f"text_offset must be 0 or more, got {text_offset}.")
        if full_text and not wanted:
            raise ToolError(
                "full_text=True requires a step_id. Call "
                "get_template_steps(template_id=...) first to find the step you want, "
                "then re-read that one step with full_text=True."
            )

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            steps = sdk.templates.get_template_steps(org_id, template_id)
            if wanted:
                steps = [st for st in steps if str(getattr(st, "id", "")) == wanted]
                if not steps:
                    raise ToolError(
                        f"Step '{wanted}' was not found in template '{template_id}'. "
                        "Call get_template_steps(template_id=...) to list the step ids."
                    )
            if full_text:
                # Lifting the cap alone is NOT enough and returning the result
                # here would re-create the very defect this flag exists to fix: a
                # 60,000-char summary encodes to ~60KB, the client cuts anything
                # past ~30KB with no signal, and compact_result would label the
                # single item "Showing 1 of 1 items", which reads as complete.
                # So the value is delivered across calls, and every part says
                # which characters it covers.
                whole = serialize_dataclass(steps[0], max_string_length=None)
                try:
                    return ToolResult(
                        # The window is budgeted for the LIST it is returned in.
                        # Bounding the dict alone left the encoded list 2 bytes
                        # over, because "[" and "]" are not free; the margin also
                        # covers the framing this result is wrapped in.
                        content=[window_longest_text(
                            whole, offset=text_offset,
                            max_bytes=MAX_RESULT_BYTES - _RESULT_CONTAINER_ALLOWANCE,
                        )],
                        structured_content=None,
                    )
                except ValueError as exc:
                    raise ToolError(str(exc)) from exc

            return ToolResult(
                content=compact_result([serialize_dataclass(st) for st in steps]),
                structured_content=None
            )

    @mcp.tool(
        name="assess_template_health",
        description=f"""Retrieve a template's data for a comprehensive health assessment.

Use this data to evaluate template health across these dimensions:
- Metadata quality: Does it have a clear title, summary, and guidance?
- Step clarity: Do steps have descriptive titles and summaries? Are any too vague?
- Form completeness: Do steps that need data collection have appropriate form fields?
- Automation efficiency: Are automation rules well-structured? Any conflicts or redundancies?
- Deadline configuration: Do time-sensitive steps have reasonable deadlines?
- Workflow structure: Is the step count manageable? Is the flow logical?

Provide an overall health rating (excellent/good/fair/poor/critical) with specific recommendations.

RETURNS: the template payload, trimmed when large — top-level keys include `id`, `title`, `summary`, `steps[]`, `automated_actions[]`, `prerun[]` (kickoff fields), and metadata. Synthesize this into a `health_rating` (one of: excellent, good, fair, poor, critical) plus a `recommendations` list (string array of specific, actionable improvements). The tool returns RAW data — the LLM is responsible for the rating + recommendations synthesis.

SIZE: if the response carries "_truncated", later steps were dropped and are NOT in it; if it
carries "_withheld", a named field was removed whole. The trim reaches the step list only, so
no marker is not a promise of completeness. Rate only what arrived, and say the assessment is
partial whenever a marker is present. If any text CONTAINS "{TRUNCATION_MARKER_PREFIX}", that
string is only part of the real value: never write it back, re-read that one step first with
get_template_steps(step_id=..., full_text=True).

REQUIRED: 'template_id' (32-character hex string). Never call this without the template_id parameter.""",
        tags=["templates", "workflow", "analysis", "health", "read-only", "optimization"],
        annotations=ToolAnnotations(
            title="Assess template health",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("assess_template_health")
    @handle_tallyfy_errors("assess template health")
    def assess_template_health(template_id: TemplateId) -> GenericDict:
        """
        Retrieve a template's data for health assessment.

        Args:
            template_id: Template ID to assess (REQUIRED - 32-character hex string)

        Returns:
            Dictionary with the template data for comprehensive analysis, trimmed
            through compact_dict_list_field when it exceeds the result ceiling
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            template = sdk.templates.get_template(org_id, template_id=template_id)
            if not template:
                raise ToolError("Template not found")

            # Same size control as get_template: one shared trimmer, not a third
            # binary search. See compact_dict_list_field's docstring.
            return ToolResult(
                content=compact_dict_list_field(
                    serialize_dataclass(template), "steps", item_label="steps"
                ),
                structured_content=None
            )

    @mcp.tool(
        name="update_template",
        description="""Update a template's metadata (title, summary, settings).

REQUIRED: 'template_id' (32-char hex) plus at least one property to update.

Updatable fields: title, summary, guidance, icon, alias, webhook, is_public, is_featured,
auto_naming, folderize_process, allow_launcher_change_name, is_pinned, default_folder,
kickoff_title, kickoff_description.

Safe to call with only the fields you want to change — this tool reads the template
first and re-sends its existing permissions ('users' and 'groups'), which the API
would otherwise clear on any update that omits them.

To CHANGE who can access the template, pass the FULL replacement list, e.g.
users=[20059, 20033] or groups=[] — these replace, they do not append.

CORRECT usage:
  update_template(template_id="abc123...", template_data={"title": "New Template Name"})
  update_template(template_id="abc123...", template_data={"summary": "Updated", "is_public": True})

Never call this without template_id.""",
        tags=["templates", "blueprints", "write", "management", "configuration"],
        annotations=ToolAnnotations(
            title="Update template",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("update_template")
    @handle_tallyfy_errors("update template")
    def update_template(template_id: TemplateId, template_data: GenericDict) -> GenericDict:
        """
        Update a template's metadata and settings.

        Args:
            template_id: Template ID to update (REQUIRED - 32-character hex string)
            template_data: Dict of fields to update. Allowed keys: title, summary, guidance,
                icon, alias, webhook, is_public, is_featured, auto_naming, folderize_process,
                allow_launcher_change_name, is_pinned, default_folder, kickoff_title,
                kickoff_description, users, groups (REQUIRED - must contain at least one field)

        Returns:
            Updated template object
        """
        if not template_data:
            raise ToolError("template_data must include at least one field to update (e.g. title, summary)")

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # READ-MODIFY-WRITE. Two independent reasons the current state must be
            # merged in before sending:
            #   1. The API requires 'title' on every update request.
            #   2. ChecklistService::update (app/Services/ChecklistService.php:209)
            #      unconditionally calls
            #      saveAssignees(Assignees::newFromArray(Arr::only($data, ['users','groups'])))
            #      and AssignableTrait::saveAssignees (app/Models/Concerns/AssignableTrait.php:101-124)
            #      has NO empty-set guard — it detaches whatever is not in the payload.
            #      So a partial PUT that omits users/groups WIPES the template's
            #      permissions. Re-sending the current sets makes the diff empty,
            #      which short-circuits before any detach.
            template_data = dict(template_data)
            needs_current = (
                "title" not in template_data
                or "users" not in template_data
                or "groups" not in template_data
            )
            if needs_current:
                current = sdk.templates.get_template(org_id, template_id=template_id)
                if not current:
                    raise ToolError(
                        f"Could not read template {template_id} to preserve its existing "
                        f"permissions, so this partial update was not sent. Retry, or pass "
                        # The nosec must sit on a line INSIDE the string node, so it
                        # goes at the end of the last literal rather than after the
                        # closing paren -- placed outside, it suppresses nothing and
                        # merely looks like it does.
                        f"title, users and groups explicitly to set them outright."  # nosec B608 - not SQL; a ToolError message containing the word "update". There is no database in the MCP tools layer.
                    )
                if "title" not in template_data and getattr(current, "title", None):
                    template_data["title"] = current.title

                # Re-send the CURRENT permissions so the server-side diff is empty.
                #
                # Only a positively-read list counts. `getattr(current, "users", None)
                # or []` would collapse "unknown" and "genuinely empty" into the same
                # [], and an explicit [] is NOT a safe default here: api-v2 treats a
                # present key as authoritative, so sending [] CLEARS every permission —
                # exactly the wipe this block exists to prevent. If we cannot read the
                # current value we must not guess; fail loudly instead of silently
                # destroying data.
                for field in ("users", "groups"):
                    if field in template_data:
                        continue  # caller is setting it explicitly; respect that
                    value = getattr(current, field, None)
                    if not isinstance(value, list):
                        raise ToolError(
                            f"Template {template_id} did not return its current "
                            f"'{field}', so a partial update would wipe them. Nothing "
                            f"was sent. Pass '{field}' explicitly to set it outright."  # nosec B608 - not SQL either; same shape as the message above.
                        )
                    template_data[field] = list(value)

            result = sdk.templates.update_template_metadata(org_id, template_id, **template_data)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="clone_template",
        description="""Clone (duplicate) a template with a new name.

REQUIRED: 'template_id' (32-char hex) and 'new_name' (string).

The clone copies steps, form fields and automation rules. Permissions are handled
by the API's own clone semantics and are NOT controllable from here — there is no
parameter to opt in or out.

CORRECT usage:
  clone_template(template_id="a1b2c3d4e5f6789012345678901234ef", new_name="Employee Onboarding v2")

Never call this without both required parameters.""",
        tags=["templates", "blueprints", "write", "management", "clone", "duplicate"],
        annotations=ToolAnnotations(
            title="Clone template",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("clone_template")
    @handle_tallyfy_errors("clone template")
    def clone_template(
        template_id: TemplateId,
        new_name: TemplateTitle,
    ) -> GenericDict:
        """
        Clone (duplicate) a template with a new name.

        Args:
            template_id: Template ID to clone (REQUIRED - 32-character hex string)
            new_name: Name for the new template copy (REQUIRED - max 250 characters)

        Returns:
            New template object (the clone)
        """
        if not new_name or not new_name.strip():
            raise ToolError("new_name cannot be empty")

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.duplicate_template(
                org_id, template_id, new_name.strip(),tenant=org_id,
            )
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="create_template",
        meta={
            "openai/toolInvocation/invoking": "Building your template...",
            "openai/toolInvocation/invoked": "Template created",
        },
        description="""Create a new template (the reusable recipe - Tallyfy's checklist/blueprint
object). Step 1 of the build chain: then add_step_to_template per step,
add_form_field_to_step for in-step data, add_kickoff_field for pre-launch data,
create_automation_rule for if-then logic, and offer launch_process for a test run
named after a real example.

BEFORE calling this: mirror the user's goal in one plain-English sentence, sketch
the likely steps, and ask only the 2-3 questions that change the design (where
does this data live today; how many per month and how much do they vary; who does
what). Never create silently from a vague ask. And check the object type: a
TEMPLATE is the recipe, a PROCESS is one launched instance - "run onboarding for
Jane" means launch_process on an existing template, not this tool.

VARIANTS: when two workflows share most steps, build ONE template plus a kickoff
field that captures the variant plus step-level show/hide automation rules -
NOT near-duplicate templates. Low overlap: separate templates.

REQUIRED: 'title' (short and noun-like: "Employee Onboarding", not a sentence).
Optional: 'type' ('procedure' for multi-step workflows, 'form' for data
collection, 'document' for reference docs), 'summary', 'guidance', 'starred'.
Never call this without title.""",
        tags=["templates", "blueprints", "write", "create"],
        annotations=ToolAnnotations(
            title="Create template",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("create_template")
    @handle_tallyfy_errors("create template")
    def create_template(
        title: TemplateTitle,
        type: OptionalString = "procedure",
        summary: OptionalString = None,
        guidance: OptionalString = None,
        starred: OptionalBool = None,
    ) -> GenericDict:
        """
        Create a new template.

        Args:
            title: Template title (REQUIRED)
            type: Template type ('procedure', 'form', 'document') (default: 'procedure')
            summary: Template description (optional)
            guidance: Guidance text for template users (optional)
            starred: Star the template (optional)

        Returns:
            Created template object
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.create_template(
                org_id, title,
                type=type,
                summary=summary,
                guidance=guidance,
                starred=starred,
            )
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="delete_template",
        description="""ARCHIVE a template. REQUIRED: 'template_id' (32-char hex).

This is a RECOVERABLE soft delete, NOT a permanent one. The template is archived
(hidden from default template lists) and its steps, form fields and automation rules
are preserved. Tallyfy exposes a restore endpoint, so an archived template can be
brought back — reassure the user rather than warning them the action is irreversible.

References to the template from folders and similar relations ARE removed, and the
response lists what was detached under `deleted_references`.

Permanently purging a template is a separate admin-only API operation that this tool
does not perform, and it still requires the template to be archived first.

Never call this without template_id.""",
        tags=["templates", "blueprints", "write", "delete"],
        annotations=ToolAnnotations(
            title="Delete template",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("delete_template")
    @handle_tallyfy_errors("delete template")
    def delete_template(template_id: TemplateId) -> GenericDict:
        """
        Archive a template (recoverable soft delete).

        Hits DELETE /organizations/{org}/checklists/{id}, which api-v2 routes to
        ChecklistsControllerNew::destroy -> ChecklistService::archiveProcess ->
        Checklist::archive(), i.e. a soft delete that sets deleted_at. A companion
        `PUT restore` endpoint exists, so this is NOT permanent.

        Args:
            template_id: Template ID to archive (REQUIRED - 32-character hex string)

        Returns:
            Result of the archive operation, including `deleted_references`
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.delete_template(org_id, template_id)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="delete_step",
        description="""Delete a step from a template PERMANENTLY. REQUIRED: 'template_id' (32-char hex) and 'step_id' (32-char hex).

Unlike archiving a template, this is a true hard delete with no restore endpoint. It cannot be undone.

THE API BLOCKS THE DELETE INSTEAD OF CASCADING. Orphaned automation rules are NOT pruned
server-side. The request is REJECTED with an error if either of these holds:
  - any automation rule references the step (as a rule's `conditionable_id`, or as the
    target of a then-action) → "Cannot delete this step because there are rules dependent on it."
  - any other step's deadline is anchored to this step → "Cannot delete this step because
    other steps have deadlines that depend on it."

So you MUST clear the dependents FIRST to preserve or retarget them. Run
`get_step_dependencies(template_id, step_id)` and read exactly these keys:
  - dependencies.incoming and dependencies.outgoing -> automation rules pointing here.
    Retarget with `update_automation_rule`, or remove with `delete_automation_rule`.
  - dependencies.deadline_anchors -> other steps whose deadline is anchored to this one,
    each carrying step_id, step_title, position and its deadline. Re-anchor each with
    `update_step`, sending all four deadline keys.
  - complexity_analysis.deadline_anchor_count -> 0 when nothing is anchored here.
Only when both are clear will the delete succeed.

A CLEAN PRE-FLIGHT IS STRONG EVIDENCE, NOT A GUARANTEE. Two blockers are invisible to it:
SOFT-DELETED then-actions, which appear in no template read; and anchors in ANOTHER
template, because api-v2 checks the whole organization while this scan sees one template.
If `deadline_anchors_unavailable` is present the check did not run, which is not a clean
result. Treat any refusal as real; act on its message rather than retrying.

Never call this without both parameters.""",
        tags=["templates", "steps", "write", "delete"],
        annotations=ToolAnnotations(
            title="Delete step",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("delete_step")
    @handle_tallyfy_errors("delete step")
    def delete_step(template_id: TemplateId, step_id: StepId) -> GenericDict:
        """
        Delete a step from a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID to delete (REQUIRED - 32-character hex string)

        Returns:
            Result of the deletion operation
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.delete_step(org_id, template_id, step_id)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="clone_step",
        description="Clone (duplicate) a step within a template. REQUIRED: 'template_id' (32-char hex) and 'step_id' (32-char hex). Creates an exact copy of the step including form fields and assignees. Never call this without both parameters.",
        tags=["templates", "steps", "write", "clone"],
        annotations=ToolAnnotations(
            title="Clone step",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("clone_step")
    @handle_tallyfy_errors("clone step")
    def clone_step(template_id: TemplateId, step_id: StepId) -> GenericDict:
        """
        Clone (duplicate) a step within a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID to clone (REQUIRED - 32-character hex string)

        Returns:
            New step object (the clone)
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.clone_step(org_id, template_id, step_id)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="reorder_step",
        description="Move a step to a new position in a template. REQUIRED: 'template_id' (32-char hex), 'step_id' (32-char hex), and 'position' (1-based integer >= 1; the first step is position 1, not 0). Never call this without all three parameters.",
        tags=["templates", "steps", "write", "reorder"],
        annotations=ToolAnnotations(
            title="Reorder step",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("reorder_step")
    @handle_tallyfy_errors("reorder step")
    def reorder_step(
        template_id: TemplateId,
        step_id: StepId,
        position: StepPosition,
    ) -> GenericDict:
        """
        Move a step to a new position in a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID to move (REQUIRED - 32-character hex string)
            position: New position for the step (REQUIRED - 1-BASED integer >= 1;
                the first step is position 1, not 0)

        Returns:
            Updated step object with new position
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.reorder_step(org_id, template_id, step_id, position)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )