"""
Template mapping validation: the guardrail half of process-document import.

PURE / DETERMINISTIC: no AI, no network, no disk, no new dependencies. Runs
identically on the DigitalOcean primary and the stateless Cloud Run mirror.

Given a DRAFT Tallyfy template mapping (steps / form fields / automations), e.g.
one the host's Claude agent produced from a customer's flowchart, SOP or process
diagram, this validates it against the code-verified Tallyfy schema (enums +
cross-references) BEFORE the agent builds it with create_template /
add_step_to_template / add_form_field_to_step / create_automation_rule. Catching
a bad step_type, an invalid automation operation, or a dangling target_step
reference here turns a half-built broken template into a single actionable error
list.

Why a deliberate split: AI template generation was removed from this server in
#492. The extraction intelligence belongs in the host (which has Claude vision +
file extractors); this server contributes only the deterministic validation +
the schema contract. See the companion issue for the full flowchart-import design.

Expected mapping shape (the host/agent produces this from the document):
{
  "title": str,
  "summary": str,
  "kickoff_form": [ {alias, label, field_type, required, options?, ...} ],
  "steps": [ {temp_id, position, title, step_type, assignees?, deadline?, form_fields?} ],
  "automations": [ {automated_alias, conditions:[{on,type,operation,statement,logic}],
                    then_actions:[{action_type,action_verb,target,
                                   deadline?, assignees?, webhook_url?, alias_name?}]} ]
}

Two DIFFERENT deadline vocabularies live in this file, and conflating them is a
live bug rather than a tidiness issue (#629):

  step deadline      steps[].deadline
    unit    minutes|minute|hours|hour|days|day|weeks|week|months|month
    option  from | prior_to
    Source: api-v2 app/Step/Deadline.php:43 (unit whitelist) and :20/:22
    (OPTION_FROM='from', OPTION_BEFORE='prior_to'). The request validator
    (CreateStepRequest.php:36-40) enum-checks NEITHER key, so a wrong value is
    accepted with a 201 and then silently does nothing: every consumer
    (StepDeadline.php:66-73, Helpers/tenant.php:173-183) switches on exactly
    those two option values and falls through both arms otherwise.

  automation deadline  automations[].then_actions[].deadline
    unit    minutes|hours|days|weeks|months
    option  before | from
    Source: api-v2 AutomatedActionRequest.php:41-42, which DOES enum-check both.

So 'day' is legal on a step and rejected on an automation, and 'before' is legal
on an automation and inert on a step. Do not merge the two sets.
"""
import logging
from typing import Any, Dict

from fastmcp.tools.tool import ToolResult
from mcp.types import ToolAnnotations
from utils.fastmcp_types import GenericDict
from utils.fastmcp_errors import handle_tallyfy_errors
from metrics import track_tool_execution

logger = logging.getLogger(__name__)

# Code-verified against api-v2 (Checklist / Step / Capture / AutomatedAction /
# Rule / DoableAction models + constants). Keep in sync if the API enums change.
STEP_TYPES = {"task", "approval", "expiring", "email", "expiring_email"}

# NINE field types, and "email" is deliberately absent (#622 item 1). api-v2's
# BaseCapture.php:183-194 does accept it, but the native Tallyfy UI can neither
# create nor render an email field, so both build tools hard-reject it
# (form_fields.py add_form_field_to_step / add_kickoff_field, blocked on purpose
# per #439). Passing a mapping the very next tool call refuses is the exact
# failure this validator exists to prevent, so it aligns DOWN to the builder.
# Do NOT add "email" back just because the API tolerates it.
FIELD_TYPES = {
    "text", "textarea", "radio", "dropdown", "multiselect", "date",
    "file", "table", "assignees_form",
}

# STEP deadlines. Singular forms included: api-v2 app/Step/Deadline.php:43
# whitelists all ten, so rejecting "day" was a false positive (#629 defect 3).
DEADLINE_UNITS = {
    "minutes", "minute", "hours", "hour", "days", "day",
    "weeks", "week", "months", "month",
}
DEADLINE_OPTIONS = {"from", "prior_to"}

# AUTOMATION-action deadlines. Narrower, and a different option vocabulary.
# api-v2 AutomatedActionRequest.php:42 is `in:minutes,hours,days,weeks,months`
# and :41 is `in:before,from`. These are enum-enforced, so a wrong value 422s.
AUTOMATION_DEADLINE_UNITS = {"minutes", "hours", "days", "weeks", "months"}
AUTOMATION_DEADLINE_OPTIONS = {"before", "from"}

STEP_OPS = {
    "completed", "reopened", "approved", "rejected", "acknowledged",
    "expired", "not_assigned",
}
FIELD_OPS = {
    "contains", "not_contains", "equals", "not_equals", "equals_any",
    "greater_than", "less_than", "is_empty", "is_not_empty",
}

# action_type CONSTRAINS action_verb: the two are NOT independent (#629
# defect 1). Mirrors api-v2 DoableActionValidator::acceptedActionVerbs()
# (app/Http/Requests/Checklists/AutomatedActions/DoableActionValidator.php:71-91),
# enforced by the in_array at :22 and surfaced at AutomatedActionRequest.php:113
# as: The action verb only accepts "show" or "hide".
#
# server/tools/automation.py carries the same map as _ACTION_VERBS_BY_TYPE, and
# the build tool enforces it. It cannot be imported: it is a local inside
# register_automation_tools(), not a module-level name. The two are pinned
# against each other behaviourally by
# tests/unit/server/tools/test_template_mapping_validation.py, which drives the
# REAL create_automation_rule with every pair this map accepts.
ACTION_VERBS_BY_TYPE = {
    "visibility": ["show", "hide"],
    "deadline": ["deadline"],
    "status": ["reopen"],
    "assignment": ["assign", "assign_only", "clear_assignees", "unassign"],
    "webhook": ["emit_webhook"],
}
# Derived, never hand-maintained, so the flat sets cannot drift from the pairs.
ACTION_TYPES = set(ACTION_VERBS_BY_TYPE)
ACTION_VERBS = {v for verbs in ACTION_VERBS_BY_TYPE.values() for v in verbs}


def _shorthand_yields_anybody(act: Dict[str, Any]) -> bool:
    """Replicate server/tools/automation.py::_normalize_actions' shorthand fold.

    LLMs write `user_id: 123` or `subject: {...}` instead of an `assignees`
    object, and automation.py folds those into one before the API sees the
    payload. So a mapping using a shorthand is NOT missing its assignees and
    must not be flagged.

    The fidelity matters in both directions, and getting it loosely right is
    how this became a false negative on first writing:

    * The fold only runs when there is NO `assignees` key at all. A present but
      empty `assignees` object BLOCKS it, so a draft carrying both an empty
      object and a shorthand ships empty and 422s.
    * A truthy shorthand is not the same as an extractable one. `subject`
      without a usable `id`, or `user_ids` that is a bare string rather than a
      list, both yield nobody.
    * `user_id` and `user_ids` are read through `int()` by the fold
      (`automation.py:302` and `:305`), so a name rather than an id does not
      merely fail validation later, it raises ValueError from
      `create_automation_rule`. A value that cannot survive that cast yields
      nobody here, and one bad element disqualifies the whole `user_ids` list,
      because the fold's generator raises rather than skipping it.

    Kept honest by TestAssigneeShorthandMatchesTheBuildTool, which drives the
    real build tool and reads the payload it emits.
    """
    if "assignees" in act:
        return False

    users: list = []
    guests: list = []

    subject = act.get("subject")
    if isinstance(subject, dict):
        sid = subject.get("id")
        if sid is not None:
            if isinstance(sid, int) or (isinstance(sid, str) and sid.isdigit()):
                users.append(sid)
            elif isinstance(sid, str) and "@" in sid:
                guests.append(sid)

    uid = act.get("user_id")
    if uid is not None and _survives_int_cast(uid):
        users.append(uid)
    uids = act.get("user_ids")
    if isinstance(uids, list) and uids and all(_survives_int_cast(u) for u in uids):
        users.extend(uids)

    if act.get("email") or act.get("guest_email"):
        guests.append(act.get("email") or act.get("guest_email"))

    return bool(users or guests)


def _has_assignees(act: Dict[str, Any]) -> bool:
    """True when the action supplies somebody api-v2 will actually accept.

    Mirrors DoableActionValidator::doAssigneesHaveErrorMessage() (:49-58), which
    is `BaseAssignees::newFromArray((array) $assignees)->isEmpty()`. Two details
    of that factory decide cases a looser reading gets wrong, both at
    app/Domain/Owners/BaseAssignees.php:14-24:

    * It reads only the STRING keys `users` (or `assignees` as its alias),
      `guests` and `groups`. A bare list carries none of them, so PHP's array
      cast yields positional keys, every bucket defaults to `[]`, and the action
      is empty however many ids the list held.
    * The users bucket is `array_filter($users, 'is_numeric')`, so a list of
      names rather than ids filters to nothing.

    `guests` and `groups` are taken at face value: AutomatedActionRequest.php:47
    and :48 already validate them with ValidGuestEmail and the org_group rule,
    which reject a bad value with a message naming the field.
    """
    if "assignees" in act:
        assignees = act["assignees"]
        if not isinstance(assignees, dict):
            return False
        users = assignees.get("users") or assignees.get("assignees") or []
        if isinstance(users, list) and any(_is_numeric_id(u) for u in users):
            return True
        return bool(assignees.get("guests") or assignees.get("groups"))
    return _shorthand_yields_anybody(act)


def _survives_int_cast(value: Any) -> bool:
    """True when `int(value)` succeeds, which is what the shorthand fold calls.

    Deliberately NOT `_is_numeric_id`. That one mirrors PHP `is_numeric`, which
    api-v2 applies to an already-built `assignees.users` bucket, and it accepts
    "1.5" because `float("1.5")` parses. `automation.py` reaches those same
    values through `int()`, and `int("1.5")` raises. Two consumers, two
    predicates; collapsing them into one would be wrong in whichever direction
    it was collapsed.
    """
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True


def _is_numeric_id(value: Any) -> bool:
    """PHP `is_numeric` for the values a JSON payload can carry."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value)
        except ValueError:
            return False
        return True
    return False


def validate_mapping(mapping: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a draft template mapping. Returns {valid, errors, warnings, summary}."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(mapping, dict):
        return {"valid": False, "errors": ["mapping must be an object"], "warnings": [], "summary": {}}

    if not mapping.get("title"):
        errors.append("template title is missing")

    step_ids: set[str] = set()
    field_aliases: set[str] = set()

    def _check_field(f: Dict[str, Any], where: str) -> None:
        ft = f.get("field_type")
        if ft not in FIELD_TYPES:
            errors.append(f"{where}: field_type '{ft}' is not one of {sorted(FIELD_TYPES)}")
        if not f.get("label"):
            warnings.append(f"{where}: form field has no label")
        if f.get("alias"):
            field_aliases.add(f["alias"])

    for f in mapping.get("kickoff_form", []) or []:
        _check_field(f, "kickoff_form")

    steps = mapping.get("steps", []) or []
    for i, s in enumerate(steps):
        sid = s.get("temp_id") or f"step_{i + 1}"
        step_ids.add(sid)
        if s.get("step_type") not in STEP_TYPES:
            errors.append(f"step {sid}: step_type '{s.get('step_type')}' is not one of {sorted(STEP_TYPES)}")
        if not s.get("title"):
            errors.append(f"step {sid}: missing title")
        dl = s.get("deadline")
        if dl:
            if dl.get("unit") not in DEADLINE_UNITS:
                errors.append(
                    f"step {sid}: deadline.unit '{dl.get('unit')}' is invalid - "
                    f"a STEP deadline accepts {sorted(DEADLINE_UNITS)}"
                )
            if dl.get("option") not in DEADLINE_OPTIONS:
                errors.append(
                    f"step {sid}: deadline.option '{dl.get('option')}' is invalid - "
                    f"a STEP deadline accepts {sorted(DEADLINE_OPTIONS)} "
                    f"('prior_to' counts backwards, 'from' counts forwards). "
                    f"'before' is the AUTOMATION spelling and does nothing here"
                )
        for f in s.get("form_fields", []) or []:
            _check_field(f, f"step {sid}")

    # deadline.step cross-refs (resolved after all step ids are known)
    for s in steps:
        dl = s.get("deadline")
        if dl and dl.get("step") not in (None, "start_run") and dl.get("step") not in step_ids:
            errors.append(f"step {s.get('temp_id')}: deadline.step '{dl.get('step')}' does not resolve to a step")

    automations = mapping.get("automations", []) or []
    for a in automations:
        alias = a.get("automated_alias", "?")
        for c in a.get("conditions", []) or []:
            ctype = c.get("type")
            op = c.get("operation")
            if ctype == "step":
                if op not in STEP_OPS:
                    errors.append(f"automation '{alias}': step operation '{op}' is invalid")
                if c.get("on") not in step_ids:
                    errors.append(f"automation '{alias}': condition.on step '{c.get('on')}' does not resolve")
            elif ctype == "field":
                if op not in FIELD_OPS:
                    errors.append(f"automation '{alias}': field operation '{op}' is invalid")
                if c.get("on") not in field_aliases:
                    warnings.append(f"automation '{alias}': condition.on field '{c.get('on')}' is not a defined field alias")
            else:
                errors.append(f"automation '{alias}': condition.type '{ctype}' must be 'step' or 'field'")
        for act in a.get("then_actions", []) or []:
            at = act.get("action_type")
            av = act.get("action_verb")
            accepted = ACTION_VERBS_BY_TYPE.get(at)

            if accepted is None:
                errors.append(
                    f"automation '{alias}': action_type '{at}' is invalid - "
                    f"must be one of {sorted(ACTION_TYPES)}"
                )
                # The type is unknown, so the PAIR cannot be judged. Fall back to
                # the flat verb vocabulary so a doubly-wrong action reports both
                # halves instead of sending the agent back for a second round.
                if av not in ACTION_VERBS:
                    errors.append(
                        f"automation '{alias}': action_verb '{av}' is invalid - "
                        f"must be one of {sorted(ACTION_VERBS)}"
                    )
            elif av not in accepted:
                message = (
                    f"automation '{alias}': action_verb '{av}' is invalid for "
                    f"action_type '{at}' - '{at}' accepts {accepted}"
                )
                # If the verb is legal but under a different type, name that type:
                # 'reopen' belongs to 'status' and never to 'visibility'.
                owner = next(
                    (t for t, verbs in ACTION_VERBS_BY_TYPE.items() if av in verbs),
                    None,
                )
                if owner:
                    message += f"; '{av}' belongs to action_type '{owner}'"
                errors.append(message)

            # Per-type required keys api-v2 enforces and this validator did not
            # look for at all (#629 defect 2).
            if at == "webhook":
                # AutomatedActionRequest.php:51-52: webhook_url is required_if
                # action_type=webhook, and alias_name is required_with webhook_url.
                # DoableActionValidator.php:60-69 repeats the first one.
                if not act.get("webhook_url"):
                    errors.append(
                        f"automation '{alias}': webhook action needs a non-empty "
                        f"webhook_url (api-v2: Please provide a webhook URL for "
                        f"the webhook action.)"
                    )
                elif not act.get("alias_name"):
                    errors.append(
                        f"automation '{alias}': webhook action needs alias_name "
                        f"alongside webhook_url"
                    )

            if at == "assignment" and av != "clear_assignees":
                # DoableActionValidator.php:49-58 skips this when the action reads
                # its people off a form field, which is actionable_id/_type on the
                # payload (AutomatedActionRequest.php:35-36, :103-104).
                if not act.get("actionable_id") and not _has_assignees(act):
                    errors.append(
                        f"automation '{alias}': assignment action '{av}' needs a "
                        f"non-empty assignees object such as "
                        f"{{'users': [123], 'guests': [], 'groups': []}} (api-v2: "
                        f"Please fill a list of assignees for the assignment action.)"
                    )

            if at == "deadline":
                # AutomatedActionRequest.php:39-42. Note the AUTOMATION sets here,
                # not the wider step-deadline ones - see the module docstring.
                adl = act.get("deadline")
                if not isinstance(adl, dict):
                    errors.append(
                        f"automation '{alias}': deadline action needs a deadline "
                        f"object {{value, unit, option}}"
                    )
                else:
                    if not isinstance(adl.get("value"), int) or isinstance(
                        adl.get("value"), bool
                    ):
                        errors.append(
                            f"automation '{alias}': deadline.value must be an "
                            f"integer - got {adl.get('value')!r}"
                        )
                    if adl.get("unit") not in AUTOMATION_DEADLINE_UNITS:
                        errors.append(
                            f"automation '{alias}': deadline.unit "
                            f"'{adl.get('unit')}' is invalid - an AUTOMATION "
                            f"deadline accepts {sorted(AUTOMATION_DEADLINE_UNITS)} "
                            f"(no singular forms, unlike a step deadline)"
                        )
                    if adl.get("option") not in AUTOMATION_DEADLINE_OPTIONS:
                        errors.append(
                            f"automation '{alias}': deadline.option "
                            f"'{adl.get('option')}' is invalid - an AUTOMATION "
                            f"deadline accepts "
                            f"{sorted(AUTOMATION_DEADLINE_OPTIONS)} "
                            f"('prior_to' is the STEP spelling and is rejected here)"
                        )

            if act.get("target") not in step_ids:
                errors.append(f"automation '{alias}': action target '{act.get('target')}' does not resolve to a step")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "steps": len(steps),
            "kickoff_fields": len(mapping.get("kickoff_form", []) or []),
            "automations": len(automations),
        },
    }


def register_template_mapping_validation_tools(mcp):
    """Register the process-document mapping validation tool with the MCP server."""

    @mcp.tool(
        name="validate_template_mapping",
        description="""Validate a DRAFT Tallyfy template mapping before building it.

USE THIS after you have turned a flowchart / SOP / process diagram into a draft
template structure, and BEFORE you call create_template + add_step_to_template +
add_form_field_to_step + create_automation_rule. It checks every step_type,
form field_type, deadline, and automation (condition operations + action
types/verbs) against Tallyfy's allowed values, and confirms every automation
target / condition reference resolves to a step/field defined in the same
mapping. Returns a precise error list so you fix the mapping in one pass instead
of building a half-broken template.

This tool does NO AI and NO network calls, it is a pure schema/cross-reference
validator. Pass the full mapping object you intend to build.

mapping shape:
{"title","summary","kickoff_form":[{alias,label,field_type,required,...}],
 "steps":[{temp_id,position,title,step_type,assignees,deadline,form_fields}],
 "automations":[{automated_alias,conditions:[{on,type:'step'|'field',operation,statement,logic}],
                 then_actions:[{action_type,action_verb,target,
                                deadline,assignees,webhook_url,alias_name}]}]}

action_type CONSTRAINS action_verb, they are NOT independent:
  visibility -> show|hide  deadline -> deadline  status -> reopen
  webhook -> emit_webhook  assignment -> assign|assign_only|unassign|clear_assignees
webhook also needs webhook_url AND alias_name; assignment other than
clear_assignees needs a non-empty assignees {users,guests,groups}.

The two deadline vocabularies DIFFER and are checked separately:
  steps[].deadline         unit minutes|hours|days|weeks|months, singular such as
                           'day' also accepted; option from|prior_to
  then_actions[].deadline  unit plural only; option before|from

Returns: {valid: bool, errors: [str], warnings: [str], summary: {steps, kickoff_fields, automations}}.""",
        tags={"templates", "import", "validation", "process-document", "read-only"},
        annotations=ToolAnnotations(
            title="Validate a draft template mapping",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            # False, and stated explicitly rather than left to default: this
            # tool makes no network calls at all, so its domain really is
            # closed. Omitting a hint is NOT the same as declaring it False.
            # OpenAI's submission portal HARD-BLOCKS the MCP step on any tool
            # that does not declare all three of readOnlyHint, destructiveHint
            # and openWorldHint: "validate_template_mapping did not include an
            # annotation for openWorldHint. Add the annotation in the MCP
            # server metadata, then rescan before submitting." This was the
            # only one of 109 tools missing any annotation, and on its own it
            # blocked the entire ChatGPT directory submission. See #568.
            openWorldHint=False,
        ),
        output_schema=None,
    )
    @track_tool_execution("validate_template_mapping")
    @handle_tallyfy_errors("validate template mapping")
    def validate_template_mapping(mapping: GenericDict) -> ToolResult:
        """
        Validate a draft Tallyfy template mapping (no AI, no network).

        Args:
            mapping: The draft template structure (title, kickoff_form, steps,
                automations) to validate before building it via the
                create_template / add_step / add_form_field / create_automation
                tool-chain.

        Returns:
            Dict with 'valid' (bool), 'errors' (list of blocking problems),
            'warnings' (non-blocking), and 'summary' (counts).
        """
        result = validate_mapping(mapping)
        return ToolResult(content=result, structured_content=None)
