"""Documented payload shapes for the template-authoring write parameters.

WHY THIS MODULE EXISTS (#1007)
------------------------------
Ten write parameters spanning the whole Tallyfy template-authoring path used to
publish `{"type": "object"}` and nothing else, because they were annotated with
the shared `GenericDict` alias. A model asked to build a template had to GUESS
the key names, and a wrong guess reads to the customer as a connector that
cannot build a template. The prose description is the only other channel that
could carry the shape and it is out of room: eight of the ten sit within 250
bytes of the hard 2000-byte tool-description cap, the tightest with six.

So the shape moved into the JSON schema, where it reaches the model on every
call rather than only if the model goes looking for it.

THE CONSTRAINT THAT MAKES THIS SAFE, AND IT IS NOT OPTIONAL
-----------------------------------------------------------
🔴 **These models DOCUMENT a shape. They must never NARROW what the connector
accepts.** Any payload that works against `Dict[str, Any]` today has to keep
working, which is why the parameter's RUNTIME type is still `Dict[str, Any]`
and the model is used only to generate the published schema.

That is not a stylistic choice, it was measured. Annotating the parameter with
the BaseModel directly fails in two ways at once:

1. **It rejects payloads that work today.** With `title: Optional[str]`,
   Pydantic refuses `{"title": 123}`; with `position: Optional[int]` it refuses
   `{"position": "abc"}`. Both reach api-v2 unhindered today. A strict model
   turns "the connector cannot build a template because the model guessed" into
   "the connector cannot build a template because WE guessed", which is the same
   customer outcome by a worse route.
2. **It changes what the function receives.** FastMCP would hand the body a
   model instance, and every one of these tools does `payload.pop(...)`,
   `payload["conditions"]`, `payload.get(...)` on a plain dict. Extra keys would
   land in `__pydantic_extra__` rather than in the mapping the body reads.

This is the repo's own standing doctrine, recorded in `CLAUDE.md` rule 4 after
the `prerun` incident: FastMCP validates the signature with Pydantic BEFORE the
function body runs, so **strict guidance, lenient parsing**. Here the guidance
is the schema and the parsing is unchanged.

HOW TO ADD OR CHANGE A SHAPE
----------------------------
Edit the model. `documented_payload()` turns it into a self-contained JSON
schema (no `$ref`, no `$defs`, so no client has to resolve anything) and hangs
it off a `Dict[str, Any]` annotation via `json_schema_extra`.

Two rules when you do:

- **A field is `required` ONLY when api-v2 genuinely rejects the payload
  without it**, or when the tool's own body raises without it. Everything else
  is optional, because every one of these payloads is legitimately partial.
- **Never set `description=` on the returned annotation.** A Field description
  on a shared alias BEATS the function docstring's `Args:` entry, which is the
  exact defect #1252 fixed by stripping those descriptions off four aliases.
  The docstring stays the source of each parameter's prose.
"""

from typing import Annotated, Any, Dict, List, Optional, Type, Union

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "documented_payload",
    "AutomationRuleCreateData",
    "AutomationRuleUpdateData",
    "KickoffFieldCreateData",
    "KickoffFieldUpdateData",
    "StepCreateData",
    "StepFormFieldCreateData",
    "StepFormFieldUpdateData",
    "StepUpdateData",
    "TemplateMappingData",
    "TemplateUpdateData",
]


# ---------------------------------------------------------------------------
# Schema plumbing
# ---------------------------------------------------------------------------

# JSON Schema keywords whose value is a MAP FROM PROPERTY NAME to schema. Their
# keys are caller data, not schema vocabulary, so a walker must recurse into the
# values and leave the names alone.
_NAME_KEYED_KEYWORDS = frozenset({"properties", "patternProperties", "$defs"})


def _inline_defs(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``schema`` with every ``$ref`` replaced by the definition it names.

    A nested model makes Pydantic emit ``{"$ref": "#/$defs/Name"}`` plus a
    sibling ``$defs`` block. That reference is resolved against the ROOT of the
    document it appears in, and these schemas are embedded as a SUB-schema of a
    tool's parameter object, so the pointer would aim at the wrong root. Rather
    than reason about whether a given client hoists ``$defs``, inline everything
    and publish a document that needs no resolution at all.

    Cycles are impossible here by construction (no payload model references
    itself), and a depth guard would hide such a mistake rather than surface it,
    so a self-referential model is left to fail loudly on recursion.
    """
    defs = schema.get("$defs", {}) or {}

    def resolve(node: Any) -> Any:
        if isinstance(node, list):
            return [resolve(item) for item in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = defs.get(ref.split("/")[-1])
            if target is None:
                raise ValueError(f"unresolvable $ref {ref!r} in payload schema")
            merged = dict(resolve(target))
            # A sibling key beside a $ref (a description, say) wins over the
            # definition it points at, which is how JSON Schema 2020-12 reads.
            for key, value in node.items():
                if key != "$ref":
                    merged[key] = resolve(value)
            return merged
        out: Dict[str, Any] = {}
        for key, value in node.items():
            if key == "$defs":
                continue
            # Same rule as _strip_noise: under `properties` the keys are caller
            # data, so a property named `$ref` must not be read as a reference.
            if key in _NAME_KEYED_KEYWORDS and isinstance(value, dict):
                out[key] = {name: resolve(sub) for name, sub in value.items()}
            else:
                out[key] = resolve(value)
        return out

    return resolve(schema)


def _strip_noise(schema: Any) -> Any:
    """Drop the keys Pydantic adds for Python's benefit rather than the model's.

    ``title`` carries the Python class or attribute name, which tells a caller
    nothing it cannot read off the key, and ``"default": null`` is how an
    optional field is spelled in Python, not in JSON Schema, where optionality
    is the absence of the key from ``required``.

    🔴 **A property NAMED `title` is caller data, not the `title` keyword, and a
    flat walk deletes it.** That is not hypothetical: the first cut of this
    function stripped `title` at every depth, which silently removed `title`
    from `add_step_to_template.step_data` and `update_step.step_data` while
    leaving it in `required`, publishing a schema that demanded a key it did not
    define. So descend through `properties` by NAME, never by treating the map
    as another schema node. `test_a_property_named_title_survives_the_stripper`
    pins it.
    """
    if isinstance(schema, list):
        return [_strip_noise(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out: Dict[str, Any] = {}
    for key, value in schema.items():
        if key == "title":
            continue
        if key == "default" and value is None:
            continue
        if key in _NAME_KEYED_KEYWORDS and isinstance(value, dict):
            out[key] = {name: _strip_noise(sub) for name, sub in value.items()}
            continue
        out[key] = _strip_noise(value)
    return out


def payload_schema(model: Type[BaseModel]) -> Dict[str, Any]:
    """The published JSON schema for one payload model: flat, self-contained."""
    raw = model.model_json_schema()
    schema = _strip_noise(_inline_defs(raw))
    # 🔴 The ROOT description has to go, and this is not tidying. Pydantic puts
    # the model's own docstring there, and a description inside
    # `json_schema_extra` is applied LAST, so it would beat the tool function's
    # `Args:` entry for this parameter. That is precisely the defect #1252 fixed
    # by stripping `description=` off four shared aliases: the real prose sat
    # unread in the docstring while the schema published a generic placeholder.
    # Nested descriptions are untouched, because nothing else supplies them.
    schema.pop("description", None)
    # `extra="allow"` is what makes these payloads open, and the schema has to
    # say so or a strict client would refuse a key the connector accepts.
    schema["additionalProperties"] = True
    return schema


def documented_payload(model: Type[BaseModel]):
    """A `Dict[str, Any]` parameter that PUBLISHES ``model``'s shape.

    The runtime type is unchanged, so validation is exactly what it was before
    this module existed: any JSON object is accepted and the tool body receives
    a plain dict. Only the advertised schema changes.
    """
    return Annotated[Dict[str, Any], Field(json_schema_extra=payload_schema(model))]


class _Payload(BaseModel):
    """Base for every payload model: open, and never used as a validator."""

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
#
# Key sets mirror `tools/template_management.py`'s `_STEP_CREATE_KEYS` and
# `_STEP_UPDATE_KEYS`, which are themselves read off api-v2's CreateStepRequest
# and UpdateStepRequest rule sets. The two contracts differ in BOTH directions
# and must not be harmonised: create carries `position` and `tags`, update
# carries `bp_to_launch` and the three `ai_*` keys and REFUSES `position`.
# `tests/regression/test_authoring_payload_schemas.py` pins each model against
# the whitelist its tool actually enforces, so the two cannot drift apart
# silently.

_STEP_TYPES = ["task", "approval", "expiring", "email", "expiring_email"]


class StepDeadline(_Payload):
    """All four keys travel together; api-v2 marks each `required_with:deadline`."""

    value: int = Field(description="How many units")
    unit: str = Field(description="minutes, hours, days, weeks or months")
    option: str = Field(
        description=(
            "Direction. 'from' (the UI shows 'after') or 'prior_to' (shows "
            "'before'). Those two words are labels, never values"
        )
    )
    step: str = Field(
        description=(
            "Anchor: 'start_run' for process launch, else another step's 32-hex id"
        )
    )


class StepStartDate(_Payload):
    """Inert unless `is_soft_start_date` is also sent as False."""

    value: int = Field(description="How many units, 1 or more")
    unit: str = Field(description="minutes, hours, days, weeks or months")


class _StepCommon(_Payload):
    summary: str = Field(default=None, description="HTML instructions for the assignee")
    step_type: str = Field(
        default=None,
        json_schema_extra={"enum": _STEP_TYPES},
        description=(
            "'approval' is what enables the approved and rejected automation "
            "conditions; 'email' is a draft a human sends, 'expiring_email' "
            "sends itself at the deadline"
        ),
    )
    assignees: List[int] = Field(
        default=None,
        description=(
            "Member ids. On an email or expiring_email step these ARE the To line"
        ),
    )
    guests: List[str] = Field(default=None, description="Guest email addresses")
    groups: List[str] = Field(default=None, description="Group ids")
    deadline: StepDeadline = Field(default=None)
    start_date: StepStartDate = Field(default=None)
    webhook: str = Field(default=None, description="Webhook URL fired on completion")
    max_assignable: int = Field(default=None, description="Cap on concurrent assignees")
    allow_guest_owners: bool = Field(
        default=None,
        description="Defaults TRUE on a new step, so send False to hide the guest slot",
    )
    is_soft_start_date: bool = Field(
        default=None,
        description="Defaults TRUE ('start anytime'), which IGNORES start_date",
    )
    skip_start_process: bool = Field(default=None)
    can_complete_only_assignees: bool = Field(default=None)
    everyone_must_complete: bool = Field(default=None)
    prevent_guest_comment: bool = Field(default=None)
    role_changes_every_time: bool = Field(default=None)
    assign_run_starter: bool = Field(default=None)
    top_secret: bool = Field(default=None)
    send_chromeless: bool = Field(
        default=None, description="Send the email without Tallyfy chrome"
    )


class StepCreateData(_StepCommon):
    """`add_step_to_template.step_data`."""

    title: str = Field(description="Step name (the only key api-v2 requires)")
    position: int = Field(
        default=None,
        description="1-based order. Steps append, so this tool issues a follow-up reorder",
    )
    tags: List[str] = Field(default=None, description="Tag ids or names")


class StepUpdateData(_StepCommon):
    """`update_step.step_data`: ONLY the fields to change.

    No `position` on purpose. A body of exactly `{"position": N}` makes
    UpdateStepRequest return early, skipping the title requirement, and still
    reaches StepBuilder::build, which detaches every assignee, group and guest.
    `reorder_step` has its own endpoint. The tool refuses the key outright.
    """

    title: str = Field(default=None, description="Step name. Passing this renames it")
    bp_to_launch: str = Field(
        default=None, description="Template id this step launches"
    )
    ai_assigned: bool = Field(default=None)
    ai_allowed_app_keys: List[str] = Field(default=None)
    ai_on_uncertainty: str = Field(default=None)


# ---------------------------------------------------------------------------
# Form fields (step captures and kickoff/prerun fields)
# ---------------------------------------------------------------------------
#
# `_STEP_FIELD_DATA_KEYS` in `tools/form_fields.py` is the authoritative set for
# a STEP field: both SDK builders copy exactly those keys out of the payload and
# DROP the rest, at HTTP 200, which is #623. The kickoff path forwards its dict
# verbatim into the template's `prerun` list, so it has no whitelist of its own;
# it is documented with the same vocabulary because it is the same field object.

_FIELD_TYPES = [
    "text",
    "textarea",
    "date",
    "dropdown",
    "multiselect",
    "radio",
    "file",
    "table",
    "assignees_form",
]


class FieldOption(_Payload):
    """One dropdown, radio or multiselect choice."""

    text: str = Field(description="The choice as the user sees it ('label' is an alias)")
    id: int = Field(default=None, description="Filled in sequentially when omitted")


class TableColumn(_Payload):
    """One column of a `table` field."""

    label: str = Field(description="Column heading")
    id: int = Field(default=None, description="Filled in sequentially when omitted")


class _CaptureCommon(_Payload):
    label: str = Field(default=None, description="The field's human-readable name")
    guidance: str = Field(
        default=None,
        description=(
            "Help text under the field. This is the key that carries it: "
            "'description' is DISCARDED at HTTP 200"
        ),
    )
    required: bool = Field(
        default=None,
        description="Whether the field is mandatory. There is NO default",
    )
    position: int = Field(default=None, description="Order within the form")
    options: List[FieldOption] = Field(
        default=None, description="dropdown, multiselect and radio only. radio needs 2+"
    )
    columns: List[TableColumn] = Field(default=None, description="table fields only")
    field_validation: Union[List[str], str] = Field(
        default=None, description='Validation rules, e.g. ["email"] or ["numeric"]'
    )
    default_value: Any = Field(default=None)
    default_value_enabled: bool = Field(default=None)
    collect_time: bool = Field(default=None, description="date fields: also ask for a time")
    use_wysiwyg_editor: bool = Field(default=None, description="textarea: rich text")
    prefix: str = Field(default=None, description="Shown before the input")
    suffix: str = Field(default=None, description="Shown after the input")
    settings: Dict[str, Any] = Field(default=None, description="Field-type specific settings")


class StepFormFieldCreateData(_CaptureCommon):
    """`add_form_field_to_step.field_data`.

    `field_type`, `label` and `required` are required: api-v2's
    CaptureRequestValidator carries a rule for each, and this tool refuses a
    payload without `required` rather than letting the SDK default it to True
    and silently create a mandatory field (#630).
    """

    field_type: str = Field(
        json_schema_extra={"enum": _FIELD_TYPES},
        description=(
            "Aliases assignee, assignee_picker, member and member_picker all "
            "resolve to assignees_form"
        ),
    )
    label: str = Field(description="The field's human-readable name")
    required: bool = Field(
        description="Mandatory or not. Pass False explicitly; there is NO default"
    )
    list: Any = Field(default=None, description="Reserved; accepted by the SDK builder")


class StepFormFieldUpdateData(_CaptureCommon):
    """`update_form_field.field_data`: only the properties to change.

    Nothing is required. api-v2 needs `label`, `field_type` and `required` on
    every update, but the tool fetches the current field and fills in whatever
    you leave out. `id` and `alias` are immutable and are refused.
    """

    field_type: str = Field(
        default=None,
        json_schema_extra={"enum": _FIELD_TYPES},
        description=(
            "Changed IN PLACE by api-v2. Never delete and recreate, which "
            "hard-deletes every collected value"
        ),
    )
    list: Any = Field(default=None, description="Reserved; accepted by the SDK builder")


class KickoffFieldCreateData(_CaptureCommon):
    """`add_kickoff_field.field_data`. Filled in BEFORE a process starts."""

    field_type: str = Field(
        json_schema_extra={"enum": _FIELD_TYPES},
        description=(
            "Aliases assignee, assignee_picker, member and member_picker all "
            "resolve to assignees_form"
        ),
    )
    label: str = Field(description="The field's human-readable name")
    required: bool = Field(
        description="Mandatory or not. Pass False explicitly; there is NO default"
    )


class KickoffFieldUpdateData(_CaptureCommon):
    """`update_kickoff_field.field_data`: only the properties to change.

    The tool reads the stored field and merges your keys in. `id` and `alias`
    are immutable.
    """

    field_type: str = Field(
        default=None,
        json_schema_extra={"enum": _FIELD_TYPES},
        description="Can be changed in place; send any options or columns the new type needs",
    )


# ---------------------------------------------------------------------------
# Automation rules
# ---------------------------------------------------------------------------

_ACTION_TYPES = ["visibility", "deadline", "status", "assignment", "webhook"]
_ACTION_VERBS = [
    "show",
    "hide",
    "deadline",
    "reopen",
    "assign",
    "assign_only",
    "unassign",
    "clear_assignees",
    "emit_webhook",
]


class AutomationCondition(_Payload):
    """One `if` clause. Every entry needs a `statement` key, null for step ops."""

    conditionable_id: str = Field(description="The step or field the test reads")
    conditionable_type: str = Field(
        json_schema_extra={"enum": ["step", "field", "kickoff"]},
    )
    operation: str = Field(
        description=(
            "Step ops: completed, reopened, approved, rejected, acknowledged, "
            "expired, not_assigned. Field and kickoff ops: contains, "
            "not_contains, equals, not_equals, equals_any, greater_than, "
            "less_than, is_empty, is_not_empty"
        )
    )
    statement: Any = Field(
        default=None,
        description="The value tested against. Required as a KEY even when null",
    )
    logic: str = Field(
        default=None,
        json_schema_extra={"enum": ["and", "or"]},
        description="AND/OR is PER CONDITION. There is no top-level condition_logic",
    )
    id: str = Field(
        default=None, description="Resend it verbatim to keep a stored condition"
    )


class AutomationActionDeadline(_Payload):
    """An AUTOMATION deadline. Its vocabulary is NOT the step one."""

    value: int = Field(description="How many units")
    unit: str = Field(
        json_schema_extra={
            "enum": ["minutes", "hours", "days", "weeks", "months"]
        },
        description="Plural only here, unlike a step deadline",
    )
    option: str = Field(
        json_schema_extra={"enum": ["before", "from"]},
        description="'prior_to' is the STEP spelling and is rejected here",
    )


class AutomationAssignees(_Payload):
    users: List[int] = Field(default=None, description="Member ids")
    guests: List[str] = Field(default=None, description="Guest email addresses")
    groups: List[str] = Field(default=None, description="Group ids")


class AutomationAction(_Payload):
    """One `then` clause. action_type CONSTRAINS action_verb; they are not independent."""

    action_type: str = Field(json_schema_extra={"enum": _ACTION_TYPES})
    action_verb: str = Field(
        default=None,
        json_schema_extra={"enum": _ACTION_VERBS},
        description=(
            "visibility takes show or hide; deadline takes deadline; status "
            "takes reopen; assignment takes assign, assign_only, unassign or "
            "clear_assignees; webhook takes emit_webhook"
        ),
    )
    target_step_id: str = Field(default=None, description="Every action needs one")
    deadline: AutomationActionDeadline = Field(default=None)
    assignees: AutomationAssignees = Field(
        default=None, description="assignment actions, unless assigning from a field"
    )
    actionable_id: str = Field(
        default=None,
        description="Assign from this field instead of a fixed assignees list",
    )
    actionable_type: str = Field(
        default=None, json_schema_extra={"enum": ["kickoff", "field"]}
    )
    webhook_url: str = Field(default=None, description="emit_webhook needs this")
    alias_name: str = Field(default=None, description="emit_webhook needs this too")
    id: str = Field(default=None, description="Resend it verbatim to keep a stored action")


class AutomationRuleCreateData(_Payload):
    """`create_automation_rule.automation_data`.

    Both lists are required: the tool refuses a payload missing either, naming
    the key, rather than letting api-v2 answer a bare 422.
    """

    conditions: List[AutomationCondition] = Field(description="When the rule fires")
    actions: List[AutomationAction] = Field(
        description="What happens. The key is 'actions', NOT 'then_actions'"
    )
    alias: str = Field(
        default=None,
        description=(
            "A short rule name. api-v2 requires it; this tool derives one from "
            "the actions when you omit it"
        ),
    )


class AutomationRuleUpdateData(_Payload):
    """`update_automation_rule.automation_data`.

    Nothing is required: a list you omit ENTIRELY is refilled by the SDK from a
    fresh read, so omitting one changes nothing. Partial WITHIN a list is the
    destructive case, because api-v2 syncs by id and force-deletes every stored
    entry whose id is absent, at HTTP 200 with no warning.
    """

    conditions: List[AutomationCondition] = Field(
        default=None, description="Send back EVERY condition you want to keep, with its id"
    )
    actions: List[AutomationAction] = Field(
        default=None, description="Send back EVERY action you want to keep, with its id"
    )
    alias: str = Field(default=None, description="A short rule name")


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

class TemplateUpdateData(_Payload):
    """`update_template.template_data`: the metadata fields to change.

    `users` and `groups` REPLACE rather than append, so `[]` clears every
    permission. The tool re-sends the stored sets when you omit them, because
    api-v2 reads an absent key as "detach everyone".
    """

    title: str = Field(default=None)
    summary: str = Field(default=None)
    guidance: str = Field(default=None)
    icon: str = Field(default=None)
    alias: str = Field(default=None)
    webhook: str = Field(default=None)
    is_public: bool = Field(default=None)
    is_featured: bool = Field(default=None)
    is_pinned: bool = Field(default=None)
    auto_naming: bool = Field(default=None)
    folderize_process: bool = Field(default=None)
    allow_launcher_change_name: bool = Field(default=None)
    default_folder: str = Field(default=None, description="Folder id")
    kickoff_title: str = Field(default=None, description="Heading on the kickoff form")
    kickoff_description: str = Field(default=None)
    users: List[int] = Field(
        default=None, description="FULL replacement list of member ids who may access it"
    )
    groups: List[str] = Field(default=None, description="FULL replacement list of group ids")


# ---------------------------------------------------------------------------
# Draft mapping (validation only, no network)
# ---------------------------------------------------------------------------
#
# Nothing here is required, deliberately. This tool's whole job is to REPORT on
# a mapping that may be wrong, so a schema that refused an incomplete draft
# would break the one call it exists to serve. A missing title comes back as an
# entry in `errors`, not as a rejection.

class MappingKickoffField(_Payload):
    alias: str = Field(default=None, description="Referenced by automation conditions")
    label: str = Field(default=None)
    field_type: str = Field(default=None, json_schema_extra={"enum": _FIELD_TYPES})
    required: bool = Field(default=None)


class MappingStep(_Payload):
    temp_id: str = Field(
        default=None, description="Draft-local id that automations point at"
    )
    position: int = Field(default=None)
    title: str = Field(default=None)
    step_type: str = Field(default=None, json_schema_extra={"enum": _STEP_TYPES})
    assignees: List[Any] = Field(default=None)
    deadline: Dict[str, Any] = Field(
        default=None,
        description=(
            "STEP vocabulary: unit minutes to months, singular such as 'day' "
            "also accepted; option from or prior_to"
        ),
    )
    form_fields: List[MappingKickoffField] = Field(default=None)


class MappingAutomation(_Payload):
    automated_alias: str = Field(default=None, description="The rule's name")
    conditions: List[Dict[str, Any]] = Field(
        default=None,
        description="Each carries on, type ('step' or 'field'), operation, statement, logic",
    )
    then_actions: List[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Each carries action_type, action_verb, target, and whichever of "
            "deadline, assignees, webhook_url and alias_name that pair needs. "
            "An ACTION deadline uses unit plural and option before or from"
        ),
    )


class TemplateMappingData(_Payload):
    """`validate_template_mapping.mapping`: the draft you intend to build."""

    title: str = Field(default=None, description="Reported as an error when missing")
    summary: str = Field(default=None)
    kickoff_form: List[MappingKickoffField] = Field(default=None)
    steps: List[MappingStep] = Field(default=None)
    automations: List[MappingAutomation] = Field(default=None)
