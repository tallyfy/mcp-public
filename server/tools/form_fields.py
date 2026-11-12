"""
Form Field Management Tools
Tools for managing form fields in templates
"""

from typing import Annotated, Final, List, Literal, Optional, Union

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations
from pydantic import Field
from tallyfy import TallyfyError, TallyfyIndeterminateError

from metrics import track_tool_execution
from utils.auth_context import TALLYFY_API_BASE_URL, get_authenticated_credentials
from utils.authoring_payloads import (
    KickoffFieldCreateData,
    KickoffFieldUpdateData,
    StepFormFieldCreateData,
    StepFormFieldUpdateData,
    documented_payload,
)
from utils.fastmcp_errors import handle_tallyfy_errors, is_answered_not_found
from utils.fastmcp_types import (
    FieldId,
    FieldIdList,
    FieldOrderList,
    FieldPosition,
    GenericDict,
    GenericList,
    StepId,
    TemplateId,
)
from utils.field_value_encoding import option_pairs
from utils.sdk_client import TallyfySDK
from utils.sdk_reads import required_list
from utils.sdk_serializer import model_document, serialize_dataclass

# The field types this server will CREATE. api-v2's `BaseCapture::$field_types`
# accepts ten, including `email`; `email` is deliberately excluded here because the
# client has no renderer for it, so a field made that way is invisible and broken for
# the customer (issue #439, closed 2026-06-28). Do NOT add it back just because the
# API tolerates it.
#
# Module level on purpose. These were four identical literals inside four functions,
# which is the copy-per-surface pattern that left #627 broken on its siblings, and it
# recurred: `_validate_task_form_fields` in `tools/task_management.py` shipped with no
# whitelist at all, so `email` was creatable on a one-off task while every template
# path blocked it. A shared constant cannot drift the way four copies did.
ALLOWED_FIELD_TYPES = {
    "text", "textarea", "date", "dropdown", "multiselect",
    "radio", "file", "table", "assignees_form",
}

# User-friendly names mapped to the canonical API `field_type`. Checked BEFORE the
# whitelist so a caller may use either.
FIELD_TYPE_ALIASES = {
    "assignee": "assignees_form",
    "assignee_picker": "assignees_form",
    "member": "assignees_form",
    "member_picker": "assignees_form",
}


# The ONLY `field_data` keys that can reach Tallyfy on a STEP form field. Both SDK
# builders copy exactly these out of the payload and DROP everything else:
# `FormFieldCRUD.add_form_field_to_step` (tallyfy/form_fields_management/crud_operations.py
# lines 43-58) and `.update_form_field` (lines 101-112), the latter discarding each
# unknown key with `logger.warning("Ignoring unknown form field: ...")` that no MCP caller
# ever sees. The write then returns HTTP 200 and the value is simply never stored, which is
# exactly issue #623: guidance text sent as `description` vanished on every update while
# `last_updated` advanced, so the tool reported success and the Client UI showed nothing.
# The two SDK allowlists are identical today, so one constant serves both paths.
#
# The SDK half is filed separately as tallyfy/sdk#32 and needs a PyPI release. Rejecting
# here at the tool boundary is the server-side half: it converts a silent partial write
# into an error the caller can act on, and it is sufficient for the user-visible bug.
#
# This does NOT apply to the kickoff (prerun) paths. Those never touch these builders -
# `add_kickoff_field` / `update_kickoff_field` put the field dict into the template's
# `prerun` LIST and PUT it via `update_template_metadata`, whose own allowlist filters
# TOP-LEVEL template keys and forwards the nested list verbatim. So a kickoff key reaches
# api-v2 and is answered there, which matches #623's report that kickoff guidance persists
# correctly. Deliberate asymmetry, not a missed sibling.
_STEP_FIELD_DATA_KEYS = frozenset({
    "field_type", "label", "required", "position", "guidance", "options",
    "default_value", "default_value_enabled", "columns", "collect_time",
    "use_wysiwyg_editor", "field_validation", "prefix", "suffix", "settings", "list",
})

# Wrong names callers actually reach for, mapped to the key that does the job. Naming the
# ONE right key beats making the model pick from sixteen, and `description` is the exact
# miss in #623 - it was even advertised as accepted by this module's own "nothing to
# update" error text until that issue.
_FIELD_DATA_KEY_ALIASES = {
    "description": "guidance",
    "help_text": "guidance",
    "name": "label",
    "type": "field_type",
    "validation": "field_validation",
}


def _reject_unknown_field_data_keys(field_data, tool_name):
    """Raise a ToolError naming every `field_data` key that would be silently discarded.

    Fails BEFORE any API call, on caller-supplied data only, so nothing is written and the
    caller gets a correction it can apply on the next turn. A non-dict argument passes
    through untouched - Pydantic already rejects those at the signature.
    """
    if not isinstance(field_data, dict):
        return
    unknown = sorted(key for key in field_data if key not in _STEP_FIELD_DATA_KEYS)
    if not unknown:
        return
    message = (
        f"{tool_name}: field_data contains {len(unknown)} key(s) Tallyfy does not accept: "
        f"{', '.join(unknown)}. Those would be DISCARDED while the write still reported "
        f"success, so nothing was sent. Accepted keys: "
        f"{', '.join(sorted(_STEP_FIELD_DATA_KEYS))}."
    )
    renames = [
        f"'{key}' -> '{_FIELD_DATA_KEY_ALIASES[key]}'"
        for key in unknown
        if key in _FIELD_DATA_KEY_ALIASES
    ]
    if renames:
        message += " Did you mean " + "; ".join(renames) + "?"
    raise ToolError(message)


def _normalize_option_dicts(options):
    """Normalize option dicts to the ONE shape api-v2 accepts: {id: int, text: str}.

    `label` is a common alias api-v2 has no rule for, so an option carrying only `label`
    422s ("options.N.text field is required"); rename it to `text`. Also auto-fill a
    sequential integer id where one is missing (api-v2 `options.*.id` is required|integer).
    Mutates the dicts in place; a non-list argument or non-dict entry passes through
    untouched. Centralized so all four option-carrying paths (add/update x step/kickoff)
    stay in sync - the copy-per-surface pattern is exactly what left #627 broken on siblings.
    """
    if not isinstance(options, list):
        return
    for i, option in enumerate(options, start=1):
        if isinstance(option, dict):
            if "text" not in option and "label" in option:
                option["text"] = option.pop("label")
            if "id" not in option:
                option["id"] = i


def _normalize_column_dicts(columns):
    """Auto-fill a sequential integer id on each table-column dict missing one (api-v2
    `columns.*.id` is required|integer). Mutates in place; non-list/non-dict pass through.
    Shared by every path that accepts a `columns` list, for the same reason as options.
    """
    if not isinstance(columns, list):
        return
    for i, column in enumerate(columns, start=1):
        if isinstance(column, dict) and "id" not in column:
            column["id"] = i


# ---------------------------------------------------------------------------
# Moving a field onto the kick-off form and off it (tallyfy/mcp#1666)
#
# api-v2#11403 gave the field move route a kick-off target and added a route that
# moves a kick-off field into a step. Both go through the SDK's
# form_fields.move_form_field_to_kickoff and .move_kickoff_field_to_step
# (tallyfy/sdk#227, SDK 4.0); until then they were the one direct api-v2 call
# here. The step to step move calls the SDK's own move_form_field.
#
# The contract, read from api-v2 origin/production 59bd9962 on 2026-10-29:
# - MoveCaptureRequest: 'target' => 'nullable|in:step,kickoff', 'target_step_id'
#   => 'exclude_if:target,kickoff|required|exists:steps,...', 'position' =>
#   'required|integer|between:1,9999'.
# - MovePrerunRequest: 'target_step_id' => 'required|exists:steps,...',
#   'position' => 'required|integer|between:1,9999'.
# - FormFieldMoveService::move keeps the row and its timeline_id (the public id),
#   clamps the position to the end of the target list, renumbers both lists, and
#   re-types the live version's automations. The answer is the moved field in the
#   shape of its new kind: PrerunTransformer ('checklist_id') on the kick-off form,
#   CaptureTransformer ('step_id') on a step.
# ---------------------------------------------------------------------------

# Final, so a type checker reads each as its literal and accepts the step default
# against MoveTarget.
_MOVE_TARGET_STEP: Final = "step"
_MOVE_TARGET_KICKOFF: Final = "kickoff"

MoveTarget = Annotated[Literal["step", "kickoff"], Field(
    description=(
        "Where the field goes. 'step' (the default) moves it to the step named in "
        "to_step. 'kickoff' moves it onto the template's kick-off form, and to_step "
        "must then be left out."
    ),
)]

# Optional here because target='kickoff' has no step, and local to this module so
# the shared StepId keeps its own description (the #1252 hazard of a shared alias).
# The empty string stays valid and means "left out", like OptionalTaskId.
OptionalTargetStepId = Annotated[Optional[str], Field(
    default=None,
    max_length=32,
    pattern="^(?:[a-f0-9]{32})?$",
    description=(
        "ID of the step to move the field to, as a 32-character hex string. Required "
        "when target is 'step'. Leave it out when target is 'kickoff'."
    ),
    examples=["b828317aab9ff295a6aedf10c6a7450c"],
)]


def _is_not_there(error: BaseException) -> bool:
    """api-v2 answered that the thing is not there: the SDK's TallyfyNotFoundError,
    or a 404 api-v2 sent itself (a plain TallyfyError with status 404)."""
    return is_answered_not_found(error) or getattr(error, "status_code", None) == 404


def _moved_field_document(move, *args):
    """Run one SDK move and give the moved field as a plain dict, or None.

    The dict is the moved field as api-v2 answered it (PrerunTransformer on the
    kick-off form, CaptureTransformer on a step): every typed key, None ones
    included, so "field" in the result is the same object the raw call used to
    return. None when api-v2 answered 2xx without the field, which the SDK
    raises as TallyfyIndeterminateError, and the tool reports as unconfirmed
    exactly as it did before.
    """
    try:
        return model_document(move(*args))
    except TallyfyIndeterminateError:
        return None


def _move_step_field_to_kickoff(sdk, org_id, template_id, from_step, field_id, position):
    """Move a step field onto the template's kick-off form and say where it landed."""
    moved = _moved_field_document(
        sdk.form_fields.move_form_field_to_kickoff,
        org_id, template_id, from_step, field_id, position,
    )
    if moved and moved.get("id") == field_id and moved.get("checklist_id") == template_id:
        landed_at = moved.get("position") or position
        return ToolResult(
            content={
                "success": True,
                "message": (
                    f"Form field '{moved.get('label') or field_id}' moved from step "
                    f"'{from_step}' onto the kick-off form at position {landed_at}. It kept "
                    f"its id '{field_id}', its alias and its settings."
                ),
                "field_id": field_id,
                "from_step": from_step,
                "target": _MOVE_TARGET_KICKOFF,
                "position": landed_at,
                "field": moved,
            },
            structured_content=None,
        )
    return ToolResult(
        content={
            "success": False,
            "message": (
                f"Tallyfy answered without showing field '{field_id}' on the kick-off form, "
                f"so the move is not confirmed. It may still be on step '{from_step}'. Check "
                f"with get_kickoff_fields before trying again."
            ),
            "field_id": field_id,
            "from_step": from_step,
            "target": _MOVE_TARGET_KICKOFF,
        },
        structured_content=None,
    )


def _move_kickoff_field_to_step(sdk, org_id, template_id, field_id, to_step, position):
    """Move a kick-off field into a step of the same template and say where it landed."""
    moved = _moved_field_document(
        sdk.form_fields.move_kickoff_field_to_step,
        org_id, template_id, field_id, to_step, position,
    )
    if moved and moved.get("id") == field_id and moved.get("step_id") == to_step:
        landed_at = moved.get("position") or position
        return ToolResult(
            content={
                "success": True,
                "message": (
                    f"Kick-off field '{moved.get('label') or field_id}' moved into step "
                    f"'{to_step}' at position {landed_at}. It kept its id '{field_id}', its "
                    f"alias and its settings."
                ),
                "field_id": field_id,
                "to_step": to_step,
                "position": landed_at,
                "field": moved,
            },
            structured_content=None,
        )
    return ToolResult(
        content={
            "success": False,
            "message": (
                f"Tallyfy answered without showing field '{field_id}' on step '{to_step}', "
                f"so the move is not confirmed. It may still be on the kick-off form. Check "
                f"with get_kickoff_fields and get_template_steps before trying again."
            ),
            "field_id": field_id,
            "to_step": to_step,
        },
        structured_content=None,
    )


def register_form_fields_tools(mcp):
    """Register all form field management tools with the MCP server"""

    # Convenience aliases that map user-friendly field type names to the
    # canonical API field_type values.  Checked before the whitelist so
    # callers can use either the alias or the real name.
    _FIELD_TYPE_ALIASES = FIELD_TYPE_ALIASES

    @mcp.tool(
        name="add_form_field_to_step",
        description="""Add form fields (text, dropdown, date, etc.) to a step.

REQUIRED: 'template_id', 'step_id', and 'field_data' (dict).

SUPPORTED `field_type` ENUM (9 values, must be one of these exactly):
  - `text`           single-line text input
  - `textarea`       multi-line text input (free-form notes)
  - `date`           date picker (yyyy-mm-dd)
  - `dropdown`       single-select from a list of options
  - `multiselect`    multi-select from options (checkbox-style)
  - `radio`          single-select radio buttons (mutually exclusive)
  - `file`           file upload
  - `table`          tabular data with named columns (REQUIRES `columns` list)
  - `assignees_form` member/assignee picker (the "person" field type)

CONVENIENCE ALIASES (auto-resolved): `assignee`, `assignee_picker`, `member`, `member_picker` -> `assignees_form`.

CONDITIONAL REQUIRED `field_data` KEYS (per field_type):
  - `dropdown` / `multiselect`: REQUIRE `options`: list of {text, optional int id}. `label` aliases `text`.
  - `radio`: REQUIRES `options` with AT LEAST 2 entries: list of {text, optional int id}.
  - `table`: REQUIRES `columns`: list of {label, optional id} (id auto-assigned if omitted)

ALWAYS-REQUIRED `field_data` KEYS: `field_type`, `label`, `required` (bool). Pass `required=False` for an optional field; there is NO default.
Optional: `guidance` (help text), `position`, `field_validation`, `collect_time`, `use_wysiwyg_editor`, `default_value`, `default_value_enabled`, `prefix`, `suffix`, `settings`.

CORRECT usage:
  add_form_field_to_step(template_id="abc...", step_id="def...",
    field_data={"field_type":"dropdown","label":"Priority","required":True,
                "options":[{"id":1,"text":"High"},{"id":2,"text":"Medium"},{"id":3,"text":"Low"}]})

  add_form_field_to_step(template_id="abc...", step_id="def...",
    field_data={"field_type":"text","label":"Customer Name","required":True})

""",
        tags=["forms", "fields", "ui", "write", "management", "configuration"],
        annotations=ToolAnnotations(
            title="Add form field to step",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("add_form_field_to_step")
    @handle_tallyfy_errors("add form field to step")
    def add_form_field_to_step(
        template_id: TemplateId,
        step_id: StepId,
        field_data: documented_payload(StepFormFieldCreateData),
    ) -> GenericDict:
        """
        Add form fields (text, dropdown, date, etc.) to a step.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID (REQUIRED - 32-character hex string)
            field_data: Form field creation data including field_type, label, required, etc. (REQUIRED)
                Supported field_type values:
                    text, textarea, date, dropdown, multiselect, radio,
                    file, table, assignees_form
                Convenience aliases (automatically resolved):
                    assignee -> assignees_form
                    assignee_picker -> assignees_form
                    member -> assignees_form
                    member_picker -> assignees_form

        Returns:
            Created form field object
        """
        # Structural check first: a key the SDK would drop means the caller's intent is
        # already partly lost, whatever else the payload gets right (#623).
        _reject_unknown_field_data_keys(field_data, "add_form_field_to_step")

        # Resolve convenience aliases to canonical API field_type names
        ft = field_data.get("field_type", "")
        if ft and ft in _FIELD_TYPE_ALIASES:
            field_data["field_type"] = _FIELD_TYPE_ALIASES[ft]
            ft = field_data["field_type"]

        # Validate field_type against allowed values (per API's CaptureRequestValidator).
        # DELIBERATELY NARROWER THAN api-v2: BaseCapture::$field_types (BaseCapture.php:183-194)
        # also accepts "email", but the native Tallyfy UI cannot create or render an email
        # field, so a field made this way is invisible/broken for the customer. Blocked here
        # on purpose per issue #439 (closed 2026-06-28) until UI parity exists. Do NOT add
        # "email" back just because the API tolerates it.
        allowed_field_types = ALLOWED_FIELD_TYPES
        if ft and ft not in allowed_field_types:
            raise ToolError(
                f"Invalid field_type '{ft}'. Allowed values: {', '.join(sorted(allowed_field_types))}"
            )

        # `required` is mandatory on every capture write (CaptureRequestValidator.php:24
        # `required|boolean`). The SDK silently defaults an omitted value to True, so a
        # field the user wanted optional is created mandatory, with no error (#630). Fail
        # locally instead. Presence check, NOT truthiness, so an explicit `required=False`
        # is honored rather than re-triggering the default.
        if "required" not in field_data:
            raise ToolError(
                "field_data must include 'required' (bool): whether the field is "
                "mandatory. Pass required=False for an optional field; there is no default."
            )

        # Validate conditional required fields
        if ft == "table" and not field_data.get("columns"):
            raise ToolError("field_type 'table' requires a 'columns' list in field_data")
        if ft in ("dropdown", "radio", "multiselect"):
            options = field_data.get("options")
            if not options:
                raise ToolError(f"field_type '{ft}' requires an 'options' list in field_data")
            for opt in options:
                if isinstance(opt, dict) and "text" not in opt and "label" not in opt:
                    raise ToolError(
                        f"Each option for field_type '{ft}' must have a 'text' (or 'label') field"
                    )
            # api-v2 (CaptureRequestValidator.php:73-74) rejects a radio field with
            # fewer than 2 options. Fail locally rather than as a remote 422.
            if ft == "radio" and len(options) < 2:
                raise ToolError("field_type 'radio' requires at least 2 options")

        # Normalize option/column dicts to the shapes api-v2 requires (#627): rename the
        # `label` alias to `text` and fill sequential ids. Shared helpers keep this identical
        # across every option-carrying path (the copy-per-surface pattern broke #627 before).
        _normalize_option_dicts(field_data.get('options'))
        _normalize_column_dicts(field_data.get('columns'))

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.form_fields.add_form_field_to_step(org_id, template_id, step_id, field_data)
            return ToolResult(
                content=serialize_dataclass(result),
                structured_content=None
            )

    @mcp.tool(
        name="update_form_field",
        description="""Update form field properties, validation, or options on an existing form field.

REQUIRED: 'template_id', 'step_id', 'field_id', and 'field_data' (dict).

UPDATABLE vs IMMUTABLE PROPERTIES:

  UPDATABLE:
    - `label`            human-readable name
    - `field_type`       the field's type. api-v2 changes it in place and reconciles existing rules; do NOT delete then recreate (that hard-deletes every collected value). Changing TO dropdown/radio/multiselect also needs `options`; changing TO table also needs `columns`.
    - `guidance`         help/instruction text shown under the field
    - `required`         mandatory flag (bool)
    - `field_validation` validation rules array (e.g. ["email"] or ["numeric"])
    - `position`         ordering within the step
    - `options`          (dropdown/radio/multiselect ONLY) list of {id:int, text:str}; prefer `update_dropdown_options` for cleaner semantics
    - `columns`          (table fields ONLY)

  IMMUTABLE (CANNOT be changed):
    - `id`               Permanent.
    - `alias`            Server-generated on creation; changing it breaks automation rules and references.

  AUTO-FILLED: The API requires `label`, `field_type`, `required` on every update. If you
  omit any, this tool fetches the current field and fills them in. To update only the
  `guidance`, pass `field_data={"guidance":"..."}` and the others auto-fill.

field_type enum: text, textarea, date, dropdown, multiselect, radio, file, table, assignees_form

CORRECT usage:
  update_form_field(template_id="abc...", step_id="def...", field_id="ghi...",
    field_data={"label":"Customer Name (legal)","required":True})

  # partial, field_type/required auto-filled from current state:
  update_form_field(template_id="abc...", step_id="def...", field_id="ghi...",
    field_data={"guidance":"Use the legal entity name from the contract"})

""",
        tags=["forms", "fields", "ui", "write", "management", "configuration"],
        annotations=ToolAnnotations(
            title="Update form field",
            readOnlyHint=False,
            # destructiveHint (#1020, per #653's rule): label, guidance, field_validation, options and columns each replace the
            # stored value outright.
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("update_form_field")
    @handle_tallyfy_errors("update form field")
    def update_form_field(
        template_id: TemplateId,
        step_id: StepId,
        field_id: FieldId,
        field_data: documented_payload(StepFormFieldUpdateData),
    ) -> GenericDict:
        """
        Update form field properties, validation, options.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID containing the field (REQUIRED - 32-character hex string)
            field_id: Form field ID to update (REQUIRED - 32-character hex string)
            field_data: Dictionary containing updated field properties (REQUIRED)
                - Can include: label, field_type, required, guidance, field_validation, options, etc.
                - Only specified fields will be updated

        Returns:
            Updated form field object data
        """
        if not field_data:
            # `description` used to head this list of examples. It is not a key api-v2
            # accepts on a capture, so the one worked example this error offered steered
            # the model straight into the silent drop of #623. Name `guidance`, which is
            # the key that actually carries help text.
            raise ToolError(
                "field_data must include at least one property to update, for example "
                "'label', 'required', or 'guidance' (the help text shown under a field)."
            )

        # Reject anything the SDK would silently discard, BEFORE the auto-fill fetch and
        # before any write (#623). Runs on caller-supplied keys only, which is the whole
        # point: the auto-fill block below injects label/field_type/required/options/
        # columns, all of them accepted, so gating here can never fail on data the tool
        # itself put there.
        _reject_unknown_field_data_keys(field_data, "update_form_field")

        # When the caller explicitly sets field_type, apply the same guards
        # as add_form_field_to_step: resolve aliases and reject types that
        # would create invisible/broken fields (e.g. "email" per #439).
        _caller_set_field_type = "field_type" in field_data
        # Capture whether the CALLER supplied options/columns BEFORE auto-fill runs. The
        # auto-fill block below injects the stored options/columns into field_data, so a
        # later `"options" in field_data` test cannot tell caller intent from auto-filled
        # data. Gating validation on these flags (plus a real type change) stops an unrelated
        # partial update (e.g. only guidance) from re-validating stored data (Bugbot @393/@385).
        _caller_set_options = "options" in field_data
        _caller_set_columns = "columns" in field_data
        if _caller_set_field_type:
            ft_raw = field_data["field_type"]
            if ft_raw in _FIELD_TYPE_ALIASES:
                field_data["field_type"] = _FIELD_TYPE_ALIASES[ft_raw]
            ft_val = field_data["field_type"]
            allowed_field_types = ALLOWED_FIELD_TYPES
            if ft_val not in allowed_field_types:
                raise ToolError(
                    f"Invalid field_type '{ft_val}'. Allowed values: {', '.join(sorted(allowed_field_types))}"
                )

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # Tallyfy API requires 'label', 'field_type', and 'required' on every update;
            # it also requires 'options' for dropdown/radio/multiselect and 'columns' for
            # table fields (even on partial updates). Fetch the current field to fill in
            # any missing keys the API would reject without.
            required_keys = {"label", "field_type", "required"}
            missing_keys = required_keys - field_data.keys()

            # The field's stored type, captured from the fetched record below. Used to tell
            # a real field_type CHANGE from an echo of the current type (Bugbot @385).
            stored_field_type = None

            # Determine field_type early to check conditional requirements
            ft = field_data.get("field_type")
            needs_options = ft in ("dropdown", "radio", "multiselect") and "options" not in field_data
            needs_columns = ft == "table" and "columns" not in field_data

            if missing_keys or needs_options or needs_columns:
                current = None

                # Try the step's own fields first. A step api-v2 says is not there
                # (404) may mean the field is a kick-off field, so that is the one
                # failure that falls through to the template. Any other failure
                # (a 5xx, an answer SDK 4.0 could not read) is raised as it came:
                # it is not "this field is not on the step" (#1545).
                try:
                    step = sdk.form_fields.get_step(org_id, template_id, step_id)
                except TallyfyError as exc:
                    if not _is_not_there(exc):
                        raise
                    step = None
                if step is not None:
                    # StepTransformer always sends `captures`; None is a read that
                    # did not carry them, not a step without this field.
                    captures = required_list(
                        step.captures, f"the form fields of step {step_id}"
                    )
                    current = next((c for c in captures if c.id == field_id), None)

                # Fallback: kickoff/prerun fields live on the template, not on a step
                if current is None:
                    try:
                        template = sdk.templates.get_template(org_id, template_id=template_id)
                    except TallyfyError as exc:
                        if not _is_not_there(exc):
                            raise
                        template = None
                    if template is not None:
                        prerun = required_list(
                            template.prerun, f"the kick-off fields of template {template_id}"
                        )
                        current = next((f for f in prerun if f.id == field_id), None)

                if current is None:
                    raise ToolError(
                        f"Cannot auto-fill required fields ({', '.join(sorted(missing_keys))}). "
                        f"Failed to fetch current field data for field '{field_id}' on step '{step_id}'. "
                        f"Please include 'label', 'field_type', and 'required' explicitly in field_data."
                    )

                stored_field_type = getattr(current, "field_type", None)

                # SDK 4.0 invents nothing: a key the read did not carry is None
                # (SDK 3.x filled required=True, field_type='text', options=[]).
                # Sending that None would null the stored value, and an unread
                # `options` sent as [] would delete every option, so a missing
                # stored value refuses and names the key to pass (#1545).
                def _stored(key):
                    value = getattr(current, key, None)
                    if value is None:
                        raise ToolError(
                            f"Field '{field_id}' came back without its current "
                            f"'{key}', so it cannot be filled in for you. Nothing was "
                            f"sent. Pass '{key}' explicitly in field_data."
                        )
                    return value

                for key in sorted(missing_keys):
                    field_data[key] = _stored(key)

                # Re-check field_type now that it may have been auto-filled
                ft = field_data.get("field_type")
                if ft in ("dropdown", "radio", "multiselect") and "options" not in field_data:
                    field_data["options"] = list(_stored("options"))
                if ft == "table" and "columns" not in field_data:
                    field_data["columns"] = list(_stored("columns"))

            # Normalize any option/column dicts (caller-supplied or auto-filled) to the
            # shapes api-v2 requires (#627), via the shared helpers. Without this,
            # changing field_type to dropdown/radio/multiselect, or passing a label-keyed
            # option, 422s on options.N.id / options.N.text. Similarly, id-less column
            # dicts on a table field 422 on columns.N.id.
            _normalize_option_dicts(field_data.get("options"))
            _normalize_column_dicts(field_data.get("columns"))

            # On an explicit field_type change, strip type-specific keys incompatible with the
            # new type so stale leftovers from the old type do not reach the API.
            ft_final = field_data.get("field_type")
            if _caller_set_field_type:
                if ft_final not in ("dropdown", "radio", "multiselect"):
                    field_data.pop("options", None)
                if ft_final != "table":
                    field_data.pop("columns", None)

            # Validate the new type's option/column requirements, but ONLY for data the caller
            # is responsible for: a REAL type change (new type differs from the stored type) or
            # caller-supplied options/columns. Echoing the current type, or a partial update
            # that never touched options, must not re-validate stored/auto-filled data - that
            # is the API's authority, and doing so would fail a guidance/label edit on a legacy
            # field (Bugbot @393/@385). `... or []` keeps a null/empty options value from a
            # TypeError and routes it through the presence check (Bugbot @401).
            type_changed = _caller_set_field_type and ft_final != stored_field_type
            if (type_changed or _caller_set_columns) and ft_final == "table" and not field_data.get("columns"):
                raise ToolError("field_type 'table' requires a 'columns' list in field_data")
            if (type_changed or _caller_set_options) and ft_final in ("dropdown", "radio", "multiselect"):
                opts = field_data.get("options") or []
                if not opts:
                    raise ToolError(f"field_type '{ft_final}' requires an 'options' list in field_data")
                if ft_final == "radio" and len(opts) < 2:
                    raise ToolError("field_type 'radio' requires at least 2 options")
                for opt in opts:
                    if isinstance(opt, dict) and "text" not in opt and "label" not in opt:
                        raise ToolError(
                            f"Each option for field_type '{ft_final}' must have a 'text' (or 'label') field"
                        )

            result = sdk.form_fields.update_form_field(org_id, template_id, step_id, field_id, **field_data)
            return ToolResult(
                content=serialize_dataclass(result),
                structured_content=None
            )

    @mcp.tool(
        name="move_form_field",
        description=(
            "Move a form field off a step, either to another step or onto the template's kick-off form. "
            "REQUIRED: 'template_id', 'from_step' (the step the field is on now) and 'field_id'. "
            "To move it to a step, pass 'to_step'. To move it onto the kick-off form, pass target='kickoff' and leave out 'to_step'. "
            "Optional: 'position' (1-BASED integer between 1 and 9999, where the first field is position 1, not 0; defaults to 1; "
            "a number past the end puts the field last). "
            "The field keeps its id, alias and settings, and the automations that use it follow it, so nothing needs re-pointing. "
            "Both lists are renumbered. It works on a template with launched processes; those processes keep the form they started with. "
            "To move a kick-off field into a step, use move_kickoff_field. "
            "Refusals come back as an error carrying Tallyfy's own message: the field is not on 'from_step' (reload the template); "
            "'to_step' is deleted, unknown or in another template; or the template borrows another template's kick-off form, "
            "so the move has to be made on the template that owns it."
        ),
        tags=["forms", "fields", "ui", "write", "management", "positioning"],
        annotations=ToolAnnotations(
            title="Move form field",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("move_form_field")
    @handle_tallyfy_errors("move form field")
    def move_form_field(
        template_id: TemplateId,
        from_step: StepId,
        field_id: FieldId,
        to_step: OptionalTargetStepId = None,
        position: FieldPosition = 1,
        target: MoveTarget = _MOVE_TARGET_STEP,
    ) -> bool:
        """
        Move a form field off a step, to another step or onto the kick-off form.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            from_step: Source step ID (REQUIRED - 32-character hex string)
            field_id: Form field ID to move (REQUIRED - 32-character hex string)
            to_step: Target step ID (32-character hex string). Required when target
                is 'step'; leave it out when target is 'kickoff'.
            position: Position in the target list (1-BASED integer between 1 and 9999;
                the first field is position 1, not 0. Default: 1)
            target: 'step' (default) or 'kickoff'

        Returns:
            Whether the move landed, and where
        """
        # Checked before any call, so a contradictory or incomplete request writes
        # nothing. api-v2 would ignore target_step_id beside target=kickoff
        # (MoveCaptureRequest 'exclude_if:target,kickoff'), so a caller who sent both
        # is told rather than having one of them dropped in silence.
        to_step = to_step or None
        if target == _MOVE_TARGET_KICKOFF and to_step:
            raise ToolError(
                "move_form_field: 'to_step' and target='kickoff' were both given, so it is "
                "not clear where the field should go. Leave out 'to_step' to move it onto the "
                "kick-off form, or leave out target to move it to that step. Nothing was moved."
            )
        if target != _MOVE_TARGET_KICKOFF and not to_step:
            raise ToolError(
                "move_form_field: 'to_step' is required to move a field to a step. To move it "
                "onto the kick-off form instead, pass target='kickoff'. Nothing was moved."
            )

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            if target == _MOVE_TARGET_KICKOFF:
                return _move_step_field_to_kickoff(sdk, org_id, template_id, from_step, field_id, position)
            result = sdk.form_fields.move_form_field(org_id, template_id, from_step, field_id, to_step, position)
            if result:
                return ToolResult(
                    content={
                        "success": True,
                        "message": f"Form field '{field_id}' moved from step '{from_step}' to step '{to_step}' at position {position}.",
                        "field_id": field_id,
                        "from_step": from_step,
                        "to_step": to_step,
                        "position": position,
                    },
                    structured_content=None,
                )
            else:
                return ToolResult(
                    content={
                        "success": False,
                        "message": f"Move failed: field '{field_id}' was NOT moved to step '{to_step}'. The field may still be on the original step '{from_step}'. Verify with get_template_steps.",
                        "field_id": field_id,
                        "from_step": from_step,
                        "to_step": to_step,
                    },
                    structured_content=None,
                )

    @mcp.tool(
        name="delete_form_field",
        description="Delete a form field from a step. REQUIRED: 'template_id', 'step_id', and 'field_id'. This cannot be undone. If an automation rule condition uses the field, Tallyfy refuses the delete with a 403 (\"Cannot delete a form field that is being used in a rule in other steps\") and nothing is removed: edit or delete that rule first. To find it, call analyze_template_automations and look for a condition whose 'conditionable_type' is \"Capture\" and whose 'conditionable_id' equals this field_id. A delete that goes ahead also removes the field's {{alias}} references from this template's text.",
        tags=["forms", "fields", "ui", "write", "management", "deletion"],
        annotations=ToolAnnotations(
            title="Delete form field",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("delete_form_field")
    @handle_tallyfy_errors("delete form field")
    def delete_form_field(
        template_id: TemplateId,
        step_id: StepId,
        field_id: FieldId
    ) -> bool:
        """
        Delete a form field from a step.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID (REQUIRED - 32-character hex string)
            field_id: Form field ID (REQUIRED - 32-character hex string)

        Returns:
            True if deletion was successful
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.form_fields.delete_form_field(org_id, template_id, step_id, field_id)
            return ToolResult(content=result, structured_content=None)

    @mcp.tool(
        name="get_dropdown_options",
        description="Get the options on a dropdown/radio/multiselect field, as {\"id\", \"text\"} objects. REQUIRED: 'template_id', 'step_id', and 'field_id'. The id is needed to WRITE a value: dropdown takes {\"id\",\"text\"}, multiselect a list of {\"id\",\"text\",\"selected\":true}, radio the bare text. Options the API could not have issued (missing an id or a text) are OMITTED rather than given a made-up id, so an empty list means none were usable and NOT that the field has no options - do not feed an empty result back into update_dropdown_options, which would replace the real list.",
        tags=["forms", "fields", "ui", "read-only", "dropdown", "options"],
        annotations=ToolAnnotations(
            title="Get dropdown options",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_dropdown_options")
    @handle_tallyfy_errors("get dropdown options")
    def get_dropdown_options(
        template_id: TemplateId,
        step_id: StepId,
        field_id: FieldId
    ) -> GenericList:
        """
        Get the options on a choice field, with their ids.

        The SDK's `get_dropdown_options` flattens options to a list of TEXT and
        drops every id, which makes a dropdown value unconstructible from the
        one tool whose whole job is reading options: `FormValuesValidator.php:23`
        requires BOTH `id` and `text` and requires them to resolve to the same
        option. So this reads `get_dropdown_options_with_ids`, which keeps the
        pairs (tallyfy/sdk#172). A field not on the step is the SDK's
        TallyfyNotFoundError (404), and a step read without its form fields is
        refused rather than read as no options.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID (REQUIRED - 32-character hex string)
            field_id: Form field ID (REQUIRED - 32-character hex string)

        Returns:
            List of {"id", "text"} option objects, in their configured order.
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            stored = sdk.form_fields.get_dropdown_options_with_ids(
                org_id, template_id, step_id, field_id
            )

            # Same helper the write path resolves against, so what this tool
            # reports and what `update_task` will accept cannot drift.
            options = option_pairs(stored)
            return ToolResult(content=options, structured_content=None)

    @mcp.tool(
        name="update_dropdown_options",
        description="""Replace the options list on a dropdown/radio/multiselect form field.

REQUIRED: 'template_id', 'step_id', 'field_id', and 'options' (list).

ACCEPTED `options` FORMATS (pass EITHER; the tool normalizes both):

  Format A - STRING ARRAY (simplest, recommended for new options):
    options=["High", "Medium", "Low"]
    The tool auto-converts each string to {id: <sequential>, text: <string>}.
    IDs are assigned 1, 2, 3, ... in input order.

  Format B - DICT ARRAY with explicit ids and text:
    options=[
      {"id": 1, "text": "High"},
      {"id": 2, "text": "Medium"},
      {"id": 3, "text": "Low"}
    ]
    Use this when you need to preserve specific IDs (e.g. matching external
    system codes, or keeping IDs stable across updates). `label` is accepted
    as an alias for `text` and is renamed automatically.

REPLACEMENT vs APPEND: This tool REPLACES the entire options list; it does NOT
append. To add an option, fetch existing options with `get_dropdown_options`,
add the new entry to the array, and pass the full updated list back.

CAUTION: Removing or changing the IDs of existing options can affect already-collected
form data (existing process runs reference options by ID). Prefer adding new options
with new IDs over deleting/renumbering existing ones.

CORRECT usage:
  update_dropdown_options(
    template_id="abc...", step_id="def...", field_id="ghi...",
    options=["Approved", "Rejected", "Needs Review"]
  )

  update_dropdown_options(
    template_id="abc...", step_id="def...", field_id="ghi...",
    options=[{"id": 100, "text": "Approved"}, {"id": 200, "text": "Rejected"}]
  )

""",
        tags=["forms", "fields", "ui", "write", "dropdown", "options", "configuration"],
        annotations=ToolAnnotations(
            title="Update dropdown options",
            readOnlyHint=False,
            # destructiveHint (#1020, per #653's rule): replaces the entire options list rather than appending, and dropping or
            # renumbering an option id changes what already-collected data points at.
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("update_dropdown_options")
    @handle_tallyfy_errors("update dropdown options")
    def update_dropdown_options(
        template_id: TemplateId,
        step_id: StepId,
        field_id: FieldId,
        options: Union[List[str], List[GenericDict]],
    ) -> bool:
        """
        Update dropdown options (for external data integration).

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID (REQUIRED - 32-character hex string)
            field_id: Form field ID (REQUIRED - 32-character hex string)
            options: List of option strings or dicts (REQUIRED).
                     Strings are auto-converted to {id, text} format; a dict's
                     `label` key is renamed to `text`.

        Returns:
            True if the update was successful
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.form_fields.update_dropdown_options(org_id, template_id, step_id, field_id, options)
            return ToolResult(content=result, structured_content=None)

    @mcp.tool(
        name="suggest_form_fields_for_step",
        description="""Retrieve step details with existing form fields to recommend additional fields.

Returns the step's title, summary, type, and existing form fields (captures), plus the template title for context.

Use this data to suggest relevant form fields by considering:
- The step's purpose (inferred from title and summary)
- What fields already exist (avoid duplicates)
- Common patterns: approval steps need decision dropdowns, contact steps need text fields for email/phone,
  document steps need file uploads, financial steps need amount fields
- Supported field types: text, textarea, date, dropdown, multiselect, radio, file, table, assignees_form

RECOMMENDATION OUTPUT: After analyzing the returned data, produce a list of recommended fields where each entry includes:
- field_type (one of: text, textarea, date, dropdown, multiselect, radio, file, table, assignees_form)
- label (suggested human-readable label for the field)
- why (one-sentence rationale tying the suggestion back to step purpose, e.g. "approval steps need a decision dropdown so the assignee's verdict is captured structurally" or "contact-collection steps need a text field for email capture and downstream notifications")
- required (true/false based on whether the data is essential)

To add suggested fields, use add_form_field_to_step with the recommended field_data.

REQUIRED: 'template_id' and 'step_id'.""",
        tags=["forms", "fields", "ui", "read-only", "suggestions", "analysis"],
        annotations=ToolAnnotations(
            title="Suggest form fields",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("suggest_form_fields_for_step")
    @handle_tallyfy_errors("suggest form fields for step")
    def suggest_form_fields_for_step(
        template_id: TemplateId,
        step_id: StepId
    ) -> GenericDict:
        """
        Retrieve step details with existing form fields for field suggestions.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID to analyze (REQUIRED - 32-character hex string)

        Returns:
            Dictionary with step data, existing fields, and template context
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            template = sdk.templates.get_template(org_id, template_id=template_id)

            # None is "not read" (SDK 4.0): neither a missing step nor a step
            # with no fields may be reported from it (#1545).
            steps = required_list(template.steps, f"the steps of template {template_id}")
            target_step = None
            for step in steps:
                if step.id == step_id:
                    target_step = step
                    break

            if not target_step:
                raise ToolError(f"Step {step_id} not found in template {template_id}")

            existing_fields = [
                serialize_dataclass(c)
                for c in required_list(target_step.captures, f"the form fields of step {step_id}")
            ]

            return ToolResult(
                content={
                    'step': serialize_dataclass(target_step),
                    'existing_fields': existing_fields,
                    'template_title': template.title,
                    'template_id': template_id,
                },
                structured_content=None
            )

    @mcp.tool(
        name="reorder_form_fields",
        description="Reorder form fields in a step. REQUIRED: 'template_id', 'step_id', and 'field_order'. Pass 'field_order' as EITHER a list of field IDs in the desired order (positions assigned 1, 2, 3, ... by list order), OR a list of {capture_id, position} objects when you need explicit non-sequential positions.",
        tags=["forms", "fields", "write", "reorder"],
        annotations=ToolAnnotations(
            title="Reorder form fields",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("reorder_form_fields")
    @handle_tallyfy_errors("reorder form fields")
    def reorder_form_fields(
        template_id: TemplateId,
        step_id: StepId,
        field_order: FieldOrderList,
    ) -> GenericDict:
        """
        Reorder form fields in a step.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID (REQUIRED - 32-character hex string)
            field_order: Ordered list of field IDs (32-char hex strings), OR a list of
                {capture_id, position} dicts for explicit positions (REQUIRED). Both
                shapes are normalized to {capture_id, position} for the API.

        Returns:
            Updated step with reordered fields
        """
        api_key, org_id = get_authenticated_credentials()
        # Normalize field_order items to use capture_id (API requirement).
        # Preserve position if provided — API requires both capture_id and position.
        normalized_order = []
        for i, item in enumerate(field_order, start=1):
            if isinstance(item, dict):
                field_id = item.get('id') or item.get('capture_id')
                normalized = {'capture_id': field_id, 'position': item.get('position', i)}
                normalized_order.append(normalized)
            elif isinstance(item, str):
                normalized_order.append({'capture_id': item, 'position': i})
            else:
                normalized_order.append(item)
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.form_fields.reorder_form_fields(org_id, template_id, step_id, normalized_order)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    # ------------------------------------------------------------------
    # Kickoff / prerun field helpers
    # ------------------------------------------------------------------

    def _get_template_and_prerun(sdk, org_id, template_id):
        """Read the template for a kick-off form write.

        Returns ``(title, prerun, template)``: the template's title, its kick-off
        fields as the dicts a template write sends back (``PrerunField.to_payload()``:
        every writable key the read carried, so ``columns``, ``settings``,
        ``default_value``, ``prefix``, ``suffix`` and the rest survive; never the
        read-only ``alias``, ``increment_id`` or timestamps), and the SDK
        ``Template`` for ``_preserved_assignment_buckets``.

        A template write REPLACES the kick-off form (``Preruns::save()`` deletes a
        field the list leaves out), so a ``prerun`` the read did not carry (None,
        SDK 4.0) is refused: sending ``[]`` or a partial list would wipe the form.
        ChecklistTransformer always sends ``prerun`` and ``title``
        (ChecklistTransformer.php:44, 51).
        """
        template = sdk.templates.get_template(org_id, template_id=template_id)
        fields = required_list(
            template.prerun, f"the kick-off fields of template {template_id}"
        )
        if not template.title:
            raise ToolError(
                f"Template {template_id} did not return its title, which every "
                f"template write must carry. Nothing was sent."
            )
        return template.title, [field.to_payload() for field in fields], template

    def _preserved_assignment_buckets(template, template_id):
        """Return the template's CURRENT users/groups to re-send with a kick-off form write.

        Until 2026-08-29 an ABSENT key on a template PUT was an EMPTY SET on
        api-v2 production (ChecklistService::update ran
        ``Assignees::newFromArray(Arr::only($data, ['users','groups']))``), so a
        kickoff-field PUT carrying only {title, prerun} stripped every member and
        group off the template, HTTP 200, no error. Production now keeps an
        omitted bucket (``assigneesFromPartial``, api-v2 29dc8ff7a, merged to
        production in e14804637). Re-sending the current lists is correct under
        both, and an unchanged list makes ``saveAssignees`` return before it
        touches anything.

        ChecklistTransformer emits both keys unconditionally via
        wrappedAssignees(), so None (SDK 4.0: not read) means the READ failed,
        and sending [] on a failed read would clear them. Abort instead of
        guessing. A genuinely empty list is legitimate and passes straight through.
        """
        buckets = {}
        for field in ("users", "groups"):
            value = getattr(template, field)
            if not isinstance(value, list):
                raise ToolError(
                    f"Template {template_id} did not return its current '{field}', "
                    f"so changing only the kickoff form would detach every member "
                    f"and group. Nothing was sent."
                )
            buckets[field] = list(value)
        return buckets

    @mcp.tool(
        name="add_kickoff_field",
        description="""Add a form field to a template's kickoff (prerun) form.

Kickoff fields are filled out BEFORE a process starts; they collect initialization data
(e.g. customer name, start date, priority). This is different from step form fields which
are filled out DURING the process.

WHAT BELONGS HERE: only what is ALWAYS needed to start and is known at submission, plus
whatever names or routes the process. Everything else belongs on the step where that work
happens (add_form_field_to_step). Porting a long paper or Google form wholesale into kickoff
recreates the problem the customer came with; split it by section instead.

REQUIRED: 'template_id' and 'field_data' (dict).

SUPPORTED `field_type` ENUM (same as step fields):
  text, textarea, date, dropdown, multiselect, radio, file, table, assignees_form

CONVENIENCE ALIASES: assignee, assignee_picker, member, member_picker → assignees_form

CONDITIONAL REQUIRED `field_data` KEYS:
  - dropdown / multiselect: REQUIRE `options`, a list of {text, optional integer id}. `label` is accepted as an alias for `text`.
  - radio: REQUIRES `options` with AT LEAST 2 entries, a list of {text, optional integer id}.
  - table: REQUIRES `columns`, a list of {label, optional id}

ALWAYS-REQUIRED `field_data` KEYS: `field_type`, `label`, `required` (bool). Pass `required=False` for an optional field; there is NO default.
Optional: guidance, position, collect_time, use_wysiwyg_editor, field_validation,
          default_value, default_value_enabled, prefix, suffix, settings.

CORRECT usage:
  add_kickoff_field(template_id="abc...",
    field_data={"field_type":"text","label":"Customer Name","required":true})

  add_kickoff_field(template_id="abc...",
    field_data={"field_type":"dropdown","label":"Priority","required":true,
                "options":[{"id":1,"text":"High"},{"id":2,"text":"Medium"},{"id":3,"text":"Low"}]})

""",
        tags=["forms", "fields", "kickoff", "prerun", "write", "configuration"],
        annotations=ToolAnnotations(
            title="Add kickoff field",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("add_kickoff_field")
    @handle_tallyfy_errors("add kickoff field")
    def add_kickoff_field(
        template_id: TemplateId,
        field_data: documented_payload(KickoffFieldCreateData),
    ) -> GenericDict:
        """
        Add a form field to a template's kickoff (prerun) form.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            field_data: Field definition dict (REQUIRED). Must include field_type, label, required

        Returns:
            Updated template object with the new kickoff field
        """
        if not field_data.get("label"):
            raise ToolError("field_data must include 'label'")
        if not field_data.get("field_type"):
            raise ToolError("field_data must include 'field_type'")
        # `required` is mandatory on every capture write (CaptureRequestValidator.php:24).
        # The kickoff path forwards field_data verbatim into the prerun array, so an
        # omitted `required` reaches api-v2 absent and 422s. Fail locally and identically
        # to add_form_field_to_step (#630). Presence check, not truthiness, so an explicit
        # `required=False` is honored.
        if "required" not in field_data:
            raise ToolError(
                "field_data must include 'required' (bool): whether the field is "
                "mandatory. Pass required=False for an optional field; there is no default."
            )

        ft = field_data.get("field_type", "")
        if ft in _FIELD_TYPE_ALIASES:
            field_data["field_type"] = _FIELD_TYPE_ALIASES[ft]
            ft = field_data["field_type"]

        # Same deliberate narrowing as add_form_field_to_step: "email" is valid in api-v2
        # but has no native-UI equivalent, so it stays blocked per issue #439.
        allowed_field_types = ALLOWED_FIELD_TYPES
        if ft not in allowed_field_types:
            raise ToolError(f"Invalid field_type '{ft}'. Allowed values: {', '.join(sorted(allowed_field_types))}")

        if ft == "table" and not field_data.get("columns"):
            raise ToolError("field_type 'table' requires a 'columns' list in field_data")
        if ft in ("dropdown", "radio", "multiselect") and not field_data.get("options"):
            raise ToolError(f"field_type '{ft}' requires an 'options' list in field_data")
        # Every option needs a text (or its `label` alias). This is the guard the other three
        # option-carrying paths already carry (add_form_field_to_step, update_form_field,
        # update_kickoff_field) and the one this path was missing - rule 16, swept one sibling
        # short. Without it a text-less option passes the presence check above, collects an id
        # from _normalize_option_dicts below, and 422s remotely on `options.0.text field is
        # required` instead of failing here with a name the caller can act on.
        if ft in ("dropdown", "radio", "multiselect"):
            for opt in field_data.get("options") or []:
                if isinstance(opt, dict) and "text" not in opt and "label" not in opt:
                    raise ToolError(
                        f"Each option for field_type '{ft}' must have a 'text' (or 'label') field"
                    )
        # api-v2 (CaptureRequestValidator.php:73-74) rejects a radio field with < 2 options.
        if ft == "radio" and len(field_data.get("options") or []) < 2:
            raise ToolError("field_type 'radio' requires at least 2 options")

        # Normalize option/column dicts to the shapes api-v2 requires (#627) via the shared
        # helpers, so a {label}-only or id-less option/column does not 422.
        _normalize_option_dicts(field_data.get("options"))
        _normalize_column_dicts(field_data.get("columns"))

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            title, existing, template = _get_template_and_prerun(sdk, org_id, template_id)
            existing.append(field_data)
            result = sdk.update_template_metadata(
                org_id, template_id, title=title, prerun=existing,
                **_preserved_assignment_buckets(template, template_id),
            )
            return ToolResult(
                content=serialize_dataclass(result),
                structured_content=None
            )

    @mcp.tool(
        name="update_kickoff_field",
        description="""Update a kickoff (prerun) field on a template.

REQUIRED: 'template_id', 'field_id', and 'field_data' (dict).

UPDATABLE PROPERTIES:
  label, guidance, required (bool), position, options (dropdown/radio/multiselect),
  columns (table), collect_time, use_wysiwyg_editor, field_validation,
  default_value, default_value_enabled, prefix, suffix, settings.

IMMUTABLE: id, alias - cannot be changed. `field_type` CAN be changed in place (send the new type plus any options/columns it needs); do NOT delete and recreate, which drops collected data.

AUTO-FILLED: This tool fetches the current field and merges your changes in, so you
only need to pass the properties you want to change.

CORRECT usage:
  update_kickoff_field(template_id="abc...", field_id="def...",
    field_data={"required": true})

  update_kickoff_field(template_id="abc...", field_id="def...",
    field_data={"label": "Updated Name", "guidance": "Enter the legal entity name"})

""",
        tags=["forms", "fields", "kickoff", "prerun", "write", "configuration"],
        annotations=ToolAnnotations(
            title="Update kickoff field",
            readOnlyHint=False,
            # destructiveHint (#1020, per #653's rule): label, guidance, options, columns and default_value each replace the stored
            # value outright.
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("update_kickoff_field")
    @handle_tallyfy_errors("update kickoff field")
    def update_kickoff_field(
        template_id: TemplateId,
        field_id: FieldId,
        field_data: documented_payload(KickoffFieldUpdateData),
    ) -> GenericDict:
        """
        Update a kickoff (prerun) field on a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            field_id: Kickoff field ID to update (REQUIRED - 32-character hex string)
            field_data: Properties to update (REQUIRED)

        Returns:
            Updated template object
        """
        if not field_data:
            raise ToolError("field_data must include at least one property to update")

        # When the caller explicitly sets field_type, apply the same guards
        # as add_kickoff_field: resolve aliases and reject types that would
        # create invisible/broken fields (e.g. "email" per #439).
        _caller_set_field_type = "field_type" in field_data
        _caller_set_options = "options" in field_data
        _caller_set_columns = "columns" in field_data
        if _caller_set_field_type:
            ft_raw = field_data["field_type"]
            if ft_raw in _FIELD_TYPE_ALIASES:
                field_data["field_type"] = _FIELD_TYPE_ALIASES[ft_raw]
            ft_val = field_data["field_type"]
            allowed_field_types = ALLOWED_FIELD_TYPES
            if ft_val not in allowed_field_types:
                raise ToolError(
                    f"Invalid field_type '{ft_val}'. Allowed values: {', '.join(sorted(allowed_field_types))}"
                )

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            title, existing, template = _get_template_and_prerun(sdk, org_id, template_id)

            target = None
            for f in existing:
                if f.get("id") == field_id:
                    target = f
                    break

            if target is None:
                raise ToolError(
                    f"Kickoff field '{field_id}' not found on template '{template_id}'. "
                    f"Use get_kickoff_fields to list available fields."
                )

            # The field's stored type, captured BEFORE the merge, so a real field_type CHANGE
            # is told apart from an echo of the current type (Bugbot @385).
            stored_field_type = target.get("field_type")

            # Normalize any option/column dicts the caller supplied to the shapes
            # api-v2 requires (#627) via the shared helpers, so a label-keyed or
            # id-less option/column does not 422.
            _normalize_option_dicts(field_data.get("options"))
            _normalize_column_dicts(field_data.get("columns"))

            target.update(field_data)

            # On an explicit field_type change, strip type-specific keys incompatible with the
            # new type (they would otherwise survive the merge and pollute the PUT payload).
            ft_final = target.get("field_type")
            if _caller_set_field_type:
                if ft_final not in ("dropdown", "radio", "multiselect"):
                    target.pop("options", None)
                if ft_final != "table":
                    target.pop("columns", None)

            # Validate the new type's option/column requirements on the merged state, but ONLY
            # for data the caller is responsible for: a REAL type change (new type differs from
            # stored) or caller-supplied options/columns. Echoing the current type, or a partial
            # update that never touched options, must not re-validate stored/merged data - that
            # is the API's authority (Bugbot @393/@385). `... or []` keeps a null/empty options
            # value from a TypeError and routes it through the presence check (Bugbot @401).
            type_changed = _caller_set_field_type and ft_final != stored_field_type
            if (type_changed or _caller_set_columns) and ft_final == "table" and not target.get("columns"):
                raise ToolError("field_type 'table' requires a 'columns' list in field_data")
            if (type_changed or _caller_set_options) and ft_final in ("dropdown", "radio", "multiselect"):
                opts = target.get("options") or []
                if not opts:
                    raise ToolError(f"field_type '{ft_final}' requires an 'options' list in field_data")
                if ft_final == "radio" and len(opts) < 2:
                    raise ToolError("field_type 'radio' requires at least 2 options")
                for opt in opts:
                    if isinstance(opt, dict) and "text" not in opt and "label" not in opt:
                        raise ToolError(
                            f"Each option for field_type '{ft_final}' must have a 'text' (or 'label') field"
                        )

            result = sdk.update_template_metadata(
                org_id, template_id, title=title, prerun=existing,
                **_preserved_assignment_buckets(template, template_id),
            )
            return ToolResult(
                content=serialize_dataclass(result),
                structured_content=None
            )

    @mcp.tool(
        name="delete_kickoff_field",
        description="""Delete a kickoff (prerun) field from a template.

REQUIRED: 'template_id' and 'field_id'.

WARNING: Deleting a kickoff field removes it permanently and drops all collected data
for that field across existing process runs. This cannot be undone.

If an automation rule condition uses the field, Tallyfy refuses the delete with a 403
("Cannot delete the kick off field because it is part of a rule.") and nothing is
removed: edit or delete that rule first. To find it, call analyze_template_automations
and look for a condition whose 'conditionable_type' is "Prerun" and whose
'conditionable_id' equals this field_id.

A delete that goes ahead also removes the field's {{alias}} references from this
template's text. A template that borrows this kick-off form keeps its references.

""",
        tags=["forms", "fields", "kickoff", "prerun", "write", "deletion"],
        annotations=ToolAnnotations(
            title="Delete kickoff field",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("delete_kickoff_field")
    @handle_tallyfy_errors("delete kickoff field")
    def delete_kickoff_field(
        template_id: TemplateId,
        field_id: FieldId,
    ) -> GenericDict:
        """
        Delete a kickoff (prerun) field from a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            field_id: Kickoff field ID to delete (REQUIRED - 32-character hex string)

        Returns:
            Success status
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # api-v2 answers 204 and the SDK returns True; a refusal (403 when an
            # automation uses the field, 404) is raised with its status.
            sdk.form_fields.delete_kickoff_field(org_id, template_id, field_id)
            return ToolResult(content={"success": True}, structured_content=None)

    @mcp.tool(
        name="move_kickoff_field",
        description=(
            "Move a field off the template's kick-off form into one of its steps. "
            "REQUIRED: 'template_id', 'field_id' (the kick-off field's id, from get_kickoff_fields) and 'to_step'. "
            "Optional: 'position' (1-BASED integer between 1 and 9999, where the first field is position 1, not 0; defaults to 1; "
            "a number past the end puts the field last). "
            "The field keeps its id, alias and settings, and the automations that use it follow it, so nothing needs re-pointing. "
            "Both lists are renumbered. It works on a template with launched processes; those processes keep the form they started with. "
            "To move a step field onto the kick-off form, use move_form_field with target='kickoff'. "
            "Refusals come back as an error carrying Tallyfy's own message: the field is not on this template's kick-off form "
            "(reload the template); 'to_step' is deleted, unknown or in another template; or the template borrows another "
            "template's kick-off form, so the move has to be made on the template that owns it."
        ),
        tags=["forms", "fields", "kickoff", "prerun", "write", "positioning"],
        annotations=ToolAnnotations(
            title="Move kickoff field into a step",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("move_kickoff_field")
    @handle_tallyfy_errors("move kickoff field")
    def move_kickoff_field(
        template_id: TemplateId,
        field_id: FieldId,
        to_step: StepId,
        position: FieldPosition = 1,
    ) -> GenericDict:
        """
        Move a kick-off (prerun) field into a step of the same template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            field_id: Kick-off field ID to move (REQUIRED - 32-character hex string)
            to_step: Step ID to move it into (REQUIRED - 32-character hex string)
            position: Position in the step (1-BASED integer between 1 and 9999;
                the first field is position 1, not 0. Default: 1)

        Returns:
            Whether the move landed, and where
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            return _move_kickoff_field_to_step(sdk, org_id, template_id, field_id, to_step, position)

    @mcp.tool(
        name="reorder_kickoff_fields",
        description="""Reorder kickoff (prerun) fields on a template.

REQUIRED: 'template_id' and 'field_order' (list of field IDs in desired order).

The API assigns position sequentially based on array order: first ID gets position 1,
second gets position 2, etc. Fields not included in field_order are appended at the end
in their original relative order.

CORRECT usage:
  reorder_kickoff_fields(template_id="abc...",
    field_order=["field_id_3", "field_id_1", "field_id_2"])

""",
        tags=["forms", "fields", "kickoff", "prerun", "write", "reorder"],
        annotations=ToolAnnotations(
            title="Reorder kickoff fields",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("reorder_kickoff_fields")
    @handle_tallyfy_errors("reorder kickoff fields")
    def reorder_kickoff_fields(
        template_id: TemplateId,
        field_order: FieldIdList,
    ) -> GenericDict:
        """
        Reorder kickoff (prerun) fields on a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            field_order: List of field IDs in desired order (REQUIRED)

        Returns:
            Updated template object with reordered kickoff fields
        """
        if not field_order:
            raise ToolError("field_order must be a non-empty list of field IDs")

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            title, existing, template = _get_template_and_prerun(sdk, org_id, template_id)

            fields_by_id = {f.get("id"): f for f in existing if f.get("id")}

            # Update each item's `position` field as we rebuild the list — Tallyfy's
            # API derives display order from the `position` integer on each prerun
            # item, NOT from array index alone. The sibling `reorder_form_fields`
            # tool above writes explicit `{capture_id, position}` pairs for the same
            # reason. BugBot finding on PR #496: "reorders array but ignores position
            # field on items; UI may still show old order" — addressed by writing
            # positions explicitly here.
            reordered = []
            for pos, fid in enumerate(field_order, start=1):
                if fid in fields_by_id:
                    item = fields_by_id.pop(fid)
                    item["position"] = pos
                    reordered.append(item)
                else:
                    raise ToolError(
                        f"Field '{fid}' not found in template kickoff fields. "
                        f"Use get_kickoff_fields to list available fields."
                    )

            # Append any fields the caller omitted from field_order, preserving their
            # relative order and continuing the position sequence.
            next_pos = len(reordered) + 1
            for f in existing:
                if f.get("id") in fields_by_id:
                    f["position"] = next_pos
                    reordered.append(f)
                    next_pos += 1

            result = sdk.update_template_metadata(
                org_id, template_id, title=title, prerun=reordered,
                **_preserved_assignment_buckets(template, template_id),
            )
            return ToolResult(
                content=serialize_dataclass(result),
                structured_content=None
            )

    @mcp.tool(
        name="get_kickoff_dropdown_options",
        description="""Get dropdown/radio/multiselect options for a kickoff (prerun) field.

REQUIRED: 'template_id' and 'field_id'.

Returns the options array for the specified kickoff field. Only works on fields with
field_type dropdown, radio, or multiselect; returns an error for other field types.

CORRECT usage:
  get_kickoff_dropdown_options(template_id="abc...", field_id="def...")

""",
        tags=["forms", "fields", "kickoff", "prerun", "read-only", "dropdown", "options"],
        annotations=ToolAnnotations(
            title="Get kickoff dropdown options",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_kickoff_dropdown_options")
    @handle_tallyfy_errors("get kickoff dropdown options")
    def get_kickoff_dropdown_options(
        template_id: TemplateId,
        field_id: FieldId,
    ) -> GenericDict:
        """
        Get dropdown/radio/multiselect options for a kickoff (prerun) field.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            field_id: Kickoff field ID (REQUIRED - 32-character hex string)

        Returns:
            Options list for the specified field
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            _, existing, _ = _get_template_and_prerun(sdk, org_id, template_id)

            target = None
            for f in existing:
                if f.get("id") == field_id:
                    target = f
                    break

            if target is None:
                raise ToolError(
                    f"Kickoff field '{field_id}' not found on template '{template_id}'. "
                    f"Use get_kickoff_fields to list available fields."
                )

            ft = target.get("field_type", "")
            if ft not in ("dropdown", "radio", "multiselect"):
                raise ToolError(
                    f"Field '{field_id}' has field_type '{ft}', options are only "
                    f"available for dropdown, radio, or multiselect fields."
                )

            return ToolResult(
                content=target.get("options", []),
                structured_content=None
            )

