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
  "steps": [ {temp_id, position, title, step_type, assignees?, groups?, guests?,
              assign_run_starter?, deadline?, form_fields?} ],
  "automations": [ {automated_alias, conditions:[{on,type,operation,statement,logic}],
                    then_actions:[{action_type,action_verb,target,
                                   deadline?, assignees?, actionable_id?,
                                   actionable_type?, webhook_url?, alias_name?,
                                   comment_content?}]} ]
}

Who does a step (assignees, groups, guests, assign_run_starter) is carried and
NOT checked: nothing below reads those four keys, so a wrong id passes. They are
named in the published schema so a draft can say who does a step under the keys
add_step_to_template takes (tallyfy/work-queue#3654). Leaving them all out does
not leave the built step empty: api-v2 gives it to whoever launches the process,
and a step with no deadline is due 1 day after launch.

A valid mapping also comes back with `build`, the list of what will be built:
each step, who gets it and when it is due at launch with those defaults filled
in, and every rule that changes it later. See describe_build below. The chat
writes its proposal and its confirmation from that list.

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
import re
from typing import Any, Dict

from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations

from metrics import track_tool_execution
from utils.authoring_payloads import TemplateMappingData, documented_payload
from utils.fastmcp_errors import handle_tallyfy_errors
from utils.sdk_serializer import TRUNCATION_MARKER_PREFIX

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
    # tallyfy/mcp#1740, the api-v2 side is tallyfy/api-v2#11737.
    "escalation": ["raise_escalation"],
}
# Derived, never hand-maintained, so the flat sets cannot drift from the pairs.
ACTION_TYPES = set(ACTION_VERBS_BY_TYPE)
ACTION_VERBS = {v for verbs in ACTION_VERBS_BY_TYPE.values() for v in verbs}

# ---------------------------------------------------------------------------
# The escalation action (tallyfy/mcp#1740, tallyfy/api-v2#11737)
# ---------------------------------------------------------------------------
# action_type escalation, action_verb raise_escalation. When the rule fires,
# Tallyfy Bot posts a comment labeled `escalation` on the target step's task.
# The comment at-mentions every recipient, which emails them. It never blocks
# completion and never changes the process status.
#
# The contract, as fixed by the api-v2 work:
# * Recipients sit in `assignees`, the storage the assignment action uses, under
#   `users` (member ids) and `groups` (group ids). At least one is required.
# * Guests are refused.
# * `comment_content` is the text, required, at most 30000 characters, and it
#   may not carry a "+" right before an email address, because the comment path
#   would invite that address to the task as a guest.
# api-v2 answers each of those with a 422. Read at tallyfy/api-v2#11759's head
# 106cef3f (2026-11-06): AutomatedActionRequest keeps only assignees.users and
# assignees.groups for an escalation, and DoableActionValidator holds the
# recipient and "+" rules.
#
# Shared by this validator and create_automation_rule / update_automation_rule
# in tools/automation.py, which imports it, so the two cannot disagree about
# what an escalation needs (the same reason _has_assignees is shared).
ESCALATION_COMMENT_MAX_CHARS = 30000
ESCALATION_RECIPIENTS_EXAMPLE = "{'users': [123], 'groups': ['<group id>']}"

# api-v2's parseAtPlusMentionedEmails() (app/Helpers/email.php), the pattern the
# comment path uses to find an address to invite. PCRE's class [-0-9-a-zA-Z.+-]
# is the same set as the one written here.
_PLUS_MENTION = re.compile(r"\+[-0-9a-zA-Z.+]+@[-0-9a-zA-Z.+_]+\.[a-zA-Z.]{2,5}")


def plus_mentioned_emails(text: str) -> list[str]:
    """The addresses written right after a "+", first occurrence order, "+" dropped.

    Mirrors parseAtPlusMentionedEmails(): every match, de-duplicated, with the
    first "+" of each removed.
    """
    found = dict.fromkeys(_PLUS_MENTION.findall(text))
    return [address.replace("+", "", 1) for address in found]


def _is_hex_object_id(value: Any) -> bool:
    """32 hex characters: a Tallyfy object id, which a group id is."""
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(c in "0123456789abcdefABCDEF" for c in value)
    )


def is_member_id(value: Any) -> bool:
    """True for a value the build tools take as a member id in assignees.users.

    The one predicate behind both callers: create_automation_rule folds each
    entry with int() and refuses a bool or a 32-character object id, and this
    validator refuses exactly the same entries.
    """
    if isinstance(value, bool) or _is_hex_object_id(value):
        return False
    return _survives_int_cast(value)


def escalation_problems(act: Dict[str, Any]) -> list[str]:
    """What api-v2 would refuse in one escalation action, each naming its key.

    An empty list means the action carries what api-v2 needs. Each entry reads
    as the end of a sentence that starts "an escalation action ...", so both
    callers can prefix it with their own context.
    """
    problems: list[str] = []
    assignees = act.get("assignees")
    if assignees is None:
        assignees = {}
    if not isinstance(assignees, dict):
        problems.append(
            f"takes its recipients in an assignees object such as "
            f"{ESCALATION_RECIPIENTS_EXAMPLE} - got {type(assignees).__name__}"
        )
        assignees = {}

    if not _blank(assignees.get("guests")):
        problems.append(
            "cannot go to guests: its recipients are members and groups only, "
            "so remove assignees.guests and name a member in assignees.users or "
            "a group in assignees.groups"
        )

    # Only users and groups. api-v2 stores nothing else for an escalation, so an
    # id under the assignment action's alias (assignees.assignees) would be
    # dropped there without a word. It is refused here instead, and never
    # counted as a recipient.
    if not _blank(assignees.get("assignees")):
        problems.append(
            "keeps its members in assignees.users, and api-v2 drops "
            "assignees.assignees for an escalation, so move those ids to "
            "assignees.users"
        )
    users = assignees.get("users")
    groups = assignees.get("groups")
    if users is not None and not isinstance(users, list):
        problems.append(
            f"takes assignees.users as a list of member ids - got {type(users).__name__}"
        )
    if groups is not None and not isinstance(groups, list):
        problems.append(
            f"takes assignees.groups as a list of group ids - got {type(groups).__name__}"
        )
    users = users if isinstance(users, list) else []
    groups = groups if isinstance(groups, list) else []
    not_members = [u for u in users if not is_member_id(u)]
    if not_members:
        problems.append(
            f"takes only member ids, whole numbers such as 10013, in assignees.users "
            f"- got {', '.join(repr(u) for u in not_members)}. A group id goes in "
            f"assignees.groups, and an escalation cannot go to a guest"
        )
    has_member = any(is_member_id(u) for u in users)
    has_group = any(isinstance(g, str) and g.strip() for g in groups)
    if not (has_member or has_group):
        problems.append(
            f"needs at least one recipient: a member id in assignees.users or a "
            f"group id in assignees.groups, such as {ESCALATION_RECIPIENTS_EXAMPLE}"
        )

    text = act.get("comment_content")
    if not isinstance(text, str) or not text.strip():
        problems.append(
            f"needs comment_content, the text of the comment it posts - got {text!r}"
        )
    else:
        if len(text) > ESCALATION_COMMENT_MAX_CHARS:
            problems.append(
                f"has comment_content of {len(text)} characters, and api-v2 accepts "
                f"at most {ESCALATION_COMMENT_MAX_CHARS}"
            )
        if TRUNCATION_MARKER_PREFIX in text:
            # analyze_template_automations cuts an escalation's text only when
            # its rule is too large for one reply, and marks the cut. Written
            # back, the cut copy and its marker would be every later email.
            problems.append(
                f"has comment_content holding the marker \"{TRUNCATION_MARKER_PREFIX}\", "
                f"so it is a cut copy of a longer text, not the text itself. "
                f"analyze_template_automations returns an escalation's text whole "
                f"unless its rule is too large for one reply; send the complete text "
                f"again, never the cut copy"
            )
        addresses = plus_mentioned_emails(text)
        if addresses:
            problems.append(
                "cannot have a \"+\" right before an email address in comment_content, "
                "because the comment would invite that address to the task as a "
                "guest, and an escalation goes to members and groups only. Found: +"
                + ", +".join(addresses)
            )
    return problems


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


# ---------------------------------------------------------------------------
# Field-referenced assignees: vocabulary shared with tools/automation.py
# ---------------------------------------------------------------------------
# These live HERE rather than in automation.py because automation.py already
# imports _has_assignees from this module, so this direction keeps the
# dependency one-way and cannot cycle.
#
# They are shared rather than copied because a copy is exactly how mcp#1056's
# fix broke this validator: automation.py learned to fold six aliases onto
# `actionable_id` and this file kept checking the literal key, so a mapping
# using `from_field` was reported as having no assignees while the build tool
# would have built it correctly. server/CLAUDE.md:59 says this validator runs
# BEFORE the build chain, so that error is what the model acts on.

# What a model writes when it means "assign from this field". None is an api-v2
# key; all fold onto actionable_id.
ACTIONABLE_ID_ALIASES = (
    "assignees_from_field",
    "assignees_from",
    "assign_from_field",
    "from_field",
    "field_ref",
    "assignee_field_id",
)

ACTIONABLE_TYPE_MAP = {
    # a form field ON A STEP
    "capture": "Capture",
    "field": "Capture",
    "form_field": "Capture",
    "step_field": "Capture",
    "form": "Capture",
    "input": "Capture",
    # a KICKOFF field
    "prerun": "Prerun",
    "kickoff": "Prerun",
    "kickoff_field": "Prerun",
    "pre_run": "Prerun",
    "launch_field": "Prerun",
    # a STEP. Legal for a DEADLINE action, which anchors off another step's due
    # date; DoableActionValidator's deadline arm carries an explicit
    # `!== 'Step'` escape and its ASSIGNMENT arm carries none, so an assignment
    # pointing at a Step is refused with 422. Measured on staging, not inferred.
    "step": "Step",
    "steps": "Step",
    "task": "Step",
}

# Only these two are legal actionable types for an ASSIGNMENT action.
ASSIGNMENT_ACTIONABLE_TYPES = ("Capture", "Prerun")


def _actionable_id_of(act: Dict[str, Any]) -> Any:
    """The field id this action carries, under the canonical key or any alias."""
    if act.get("actionable_id"):
        return act["actionable_id"]
    for alias in ACTIONABLE_ID_ALIASES:
        if act.get(alias):
            return act[alias]
    return None


def _actionable_type_of(act: Dict[str, Any]) -> Any:
    """The actionable type, resolved through the map.

    An unrecognised value is returned AS WRITTEN rather than as None, so it
    falls into the "not a legal type" branch with a message naming the legal
    ones, instead of the misleading "you forgot actionable_type" branch.
    """
    raw = act.get("actionable_type")
    if not raw:
        return None
    return ACTIONABLE_TYPE_MAP.get(str(raw).strip().lower(), raw)


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


# ---------------------------------------------------------------------------
# What will be built (tallyfy/work-queue#3654)
# ---------------------------------------------------------------------------
# A valid mapping also comes back with `build`: every step in order, who gets it
# and when it is due when the process launches, with Tallyfy's defaults filled
# in, and every rule that changes it later. The chat writes its proposal and its
# post-build confirmation from this list, so neither can leave a step or a
# default out. On staging (2026-10-06) the model wrote both from memory: a step
# with no role was left off the proposal on three runs of four, and a "within 2
# days" rule came out as 3 days after launch.
#
# Read in the api-v2 checkout at 86687df (origin/master, 2026-10-06), not driven:
#
# * A step created with no deadline is due 1 day after launch. That is the
#   `steps.deadline` column default
#   (database/migrations/up/2019_11_23_182126_add_default_steps_deadlines.sql)
#   and app/Step/DefaultDeadline.php. api-v2 never reads the organization's own
#   default deadline when a step is created.
# * A step created with no member, group or guest goes to whoever launches the
#   process: `assign_run_starter` defaults to true
#   (database/migrations/2023_11_30_112115_add_assign_run_starter_in_steps_table.php),
#   and TaskFactory::buildFromStep (app/Domain/Task/TaskFactory.php:31-37) uses
#   it ONLY when the step names nobody. So a step with a group and
#   assign_run_starter true goes to the group alone.
# * A deadline rule that names nothing to count from counts from the target
#   step's OWN deadline (BaseAction::applyDeadlineActionOnTarget, lines 113-158):
#   "2 days" on a step with the 1 day default lands 3 days after launch. Named
#   a step, it counts from that step's deadline; named a date field, from the
#   date in it, and an empty field changes nothing.
# * `assign_only` replaces whoever is on the step and `assign` adds to them;
#   from a field, an empty pick falls back to the rule's own `assignees`
#   (BaseAction::applyAssignmentActionOnTarget, lines 183-215). `unassign`
#   takes the people it names off the step and leaves everyone else: api-v2
#   0cfd47e9 (2026-10-12, BaseAction lines 212-214 on master 6441c7f7).
#   Production api-v2 (0e89c240, read 2026-10-24) still has the TODO there and
#   ignores the rule until the api-v2 team promotes it (tallyfy/work-queue#3658).
# * Every step a `show` rule targets is hidden when the process launches
#   (ApplyRulesWithPrerunConditions, lines 35-45).
# * Conditions combine left to right on each later condition's `logic`
#   (BaseRule::isApplicable). Every condition after the first must carry 'and'
#   or 'or', or api-v2 refuses the rule, so the validator refuses it too (#3655).
#
# Not modelled: the organization's working days, which can move a deadline
# Tallyfy computes at launch; and another rule having moved a deadline before a
# deadline rule counts from it.

# app/Step/DefaultDeadline.php, the steps.deadline column default.
DEFAULT_STEP_DEADLINE = {"value": 1, "unit": "day", "option": "from", "step": "start_run"}

_PLURAL_UNITS = {
    "minute": "minutes", "hour": "hours", "day": "days", "week": "weeks", "month": "months",
}
_UNIT_ORDER = ("months", "weeks", "days", "hours", "minutes")

_STEP_OP_WORDS = {
    "completed": "is completed",
    "reopened": "is reopened",
    "approved": "is approved",
    "rejected": "is rejected",
    "acknowledged": "is acknowledged",
    "expired": "expires",
    "not_assigned": "has nobody assigned",
}
_FIELD_OP_WORDS = {
    "contains": "contains",
    "not_contains": "does not contain",
    "equals": "equals",
    "not_equals": "does not equal",
    "equals_any": "equals any of",
    "greater_than": "is greater than",
    "less_than": "is less than",
    "is_empty": "is empty",
    "is_not_empty": "is not empty",
}


def _as_list(value: Any) -> list:
    """A step's assignee key as a list. Nothing checks these keys, so a bare
    value is taken as one entry rather than dropped."""
    if isinstance(value, list):
        return [v for v in value if v not in (None, "")]
    if value in (None, "", [], {}):
        return []
    return [value]


def _plural_unit(unit: Any) -> str | None:
    if not isinstance(unit, str):
        return None
    unit = unit.strip().lower()
    if unit in _PLURAL_UNITS:
        return _PLURAL_UNITS[unit]
    return unit if unit in _PLURAL_UNITS.values() else None


def _amount(count: int, unit: str) -> str:
    return f"{count} {unit[:-1] if count == 1 else unit}"


def _joined(parts: list) -> str:
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _offset_text(terms: Dict[str, int], base: str, zero: str) -> str:
    """'3 days after launch', or 'before', from signed amounts per unit."""
    after = [_amount(terms[u], u) for u in _UNIT_ORDER if terms.get(u, 0) > 0]
    before = [_amount(-terms[u], u) for u in _UNIT_ORDER if terms.get(u, 0) < 0]
    if after and before:
        return f"{_joined(after)} after {base}, less {_joined(before)}"
    if after:
        return f"{_joined(after)} after {base}"
    if before:
        return f"{_joined(before)} before {base}"
    return zero


def _signed_terms(deadline: Any, from_word: str) -> Dict[str, int] | None:
    """{unit: signed value} for one deadline object, or None when it cannot be
    counted (no whole-number value, or a unit or option nothing knows)."""
    if not isinstance(deadline, dict):
        return None
    value = deadline.get("value")
    unit = _plural_unit(deadline.get("unit"))
    if isinstance(value, bool) or not isinstance(value, int) or unit is None:
        return None
    option = deadline.get("option")
    if option == from_word:
        return {unit: value}
    if option in ("prior_to", "before"):
        return {unit: -value}
    return None


def _added(first: Dict[str, int], second: Dict[str, int]) -> Dict[str, int]:
    total = dict(first)
    for unit, value in second.items():
        total[unit] = total.get(unit, 0) + value
    return total


class _BuildReader:
    """Reads a mapping that passed validation into the list of what gets built."""

    def __init__(self, mapping: Dict[str, Any]):
        self.steps = [s for s in (mapping.get("steps") or []) if isinstance(s, dict)]
        self.ids = [s.get("temp_id") or f"step_{i + 1}" for i, s in enumerate(self.steps)]
        self.by_id = dict(zip(self.ids, self.steps))
        self.fields: Dict[str, str] = {}
        for f in mapping.get("kickoff_form") or []:
            if isinstance(f, dict) and f.get("alias"):
                self.fields[f["alias"]] = f"kickoff field '{f.get('label') or f['alias']}'"
        for sid, s in self.by_id.items():
            for f in s.get("form_fields") or []:
                if isinstance(f, dict) and f.get("alias"):
                    self.fields[f["alias"]] = (
                        f"field '{f.get('label') or f['alias']}' on {self._step_name(sid)}"
                    )
        self.automations = [a for a in (mapping.get("automations") or []) if isinstance(a, dict)]

    def _step_name(self, sid: Any) -> str:
        step = self.by_id.get(sid)
        return f"step '{step.get('title') or sid}'" if step else f"step '{sid}'"

    def _field_name(self, alias: Any) -> str:
        # A condition on a field the mapping lacks is only a warning, so a list
        # or an object can reach this lookup in a valid mapping.
        return self.fields[alias] if _known(alias, self.fields) else f"field '{alias}'"

    # -- who gets the step at launch -------------------------------------

    def goes_to(self, step: Dict[str, Any]) -> Dict[str, Any]:
        members = _as_list(step.get("assignees"))
        groups = _as_list(step.get("groups"))
        guests = _as_list(step.get("guests"))
        starter = step.get("assign_run_starter")
        named = _joined(
            [f"member {m}" for m in members]
            + [f"group {g}" for g in groups]
            + [f"guest {g}" for g in guests]
        )
        if named:
            says = f"{named} only"
            if starter:
                says += (
                    ", not whoever launches the process: assign_run_starter "
                    "only counts on a step that names nobody"
                )
            launcher, default, short = False, False, named
        elif starter is False:
            says = short = "nobody, because the step names nobody and assign_run_starter is false"
            launcher, default = False, False
        elif starter:
            says = "whoever launches the process, set by assign_run_starter"
            launcher, default, short = True, False, "whoever launches the process"
        else:
            says = (
                "whoever launches the process, Tallyfy's default because the "
                "step names nobody"
            )
            launcher, default, short = True, True, "whoever launches the process"
        return {
            "members": members,
            "groups": groups,
            "guests": guests,
            "launcher": launcher,
            "default": default,
            "says": says,
            "_short": short,
        }

    # -- when the step is due at launch ----------------------------------

    def due(self, sid: Any) -> Dict[str, Any]:
        """{base, terms, default, says}. base is 'launch' or another step's id;
        terms is None when the deadline cannot be counted."""
        step = self.by_id.get(sid) or {}
        deadline = step.get("deadline")
        default = not deadline
        if default:
            deadline = DEFAULT_STEP_DEADLINE
        terms = _signed_terms(deadline, "from")
        anchor = deadline.get("step") if isinstance(deadline, dict) else None
        base = "launch" if anchor in (None, "", "start_run") else anchor
        if terms is None:
            says = (
                f"not countable: the deadline {deadline!r} has no whole-number "
                f"value, so check it before you build"
            )
        elif base == "launch":
            says = _offset_text(terms, "launch", "at launch")
        elif base == sid:
            says = "not countable: its deadline counts from itself"
            terms = None
        else:
            says = _offset_text(
                terms, f"{self._step_name(base)} is due", f"when {self._step_name(base)} is due"
            )
        if default:
            says += ", Tallyfy's default because the step sets no deadline"
        return {"base": base, "terms": terms, "default": default, "says": says}

    def _counted(self, due: Dict[str, Any], extra: Dict[str, int]) -> str | None:
        """Where a deadline lands: `extra` on top of `due`, in due's base."""
        if due["terms"] is None:
            return None
        total = _added(due["terms"], extra)
        if due["base"] == "launch":
            return _offset_text(total, "launch", "at launch")
        name = self._step_name(due["base"])
        return _offset_text(total, f"{name} is due", f"when {name} is due")

    # -- the rules that change it later ----------------------------------

    def _condition(self, cond: Dict[str, Any]) -> str:
        on = cond.get("on")
        op = cond.get("operation")
        if cond.get("type") == "step":
            return f"{self._step_name(on)} {_STEP_OP_WORDS.get(op, op)}"
        words = _FIELD_OP_WORDS.get(op, op)
        if op in ("is_empty", "is_not_empty"):
            return f"{self._field_name(on)} {words}"
        return f"{self._field_name(on)} {words} '{cond.get('statement')}'"

    def when(self, automation: Dict[str, Any]) -> str:
        conditions = [c for c in (automation.get("conditions") or []) if isinstance(c, dict)]
        if not conditions:
            return "no condition is given"
        text = self._condition(conditions[0])
        for cond in conditions[1:]:
            # A valid mapping carries 'and' or 'or' here (#3655).
            logic = str(cond.get("logic") or "").strip().lower()
            text += f" {logic} {self._condition(cond)}"
        return text

    def _people(self, act: Dict[str, Any]) -> str:
        """Who an assignment names, from its field reference and its bucket."""
        bucket = act.get("assignees") if isinstance(act.get("assignees"), dict) else {}
        static = _joined(
            [f"member {u}" for u in _as_list(bucket.get("users") or bucket.get("assignees"))]
            + [f"group {g}" for g in _as_list(bucket.get("groups"))]
            + [f"guest {g}" for g in _as_list(bucket.get("guests"))]
        )
        field = _actionable_id_of(act)
        if field:
            picked = f"the person picked in {self._field_name(field)}"
            return f"{picked}, or {static} if it is empty" if static else picked
        return static or "the people the rule names"

    def rule(self, automation: Dict[str, Any], act: Dict[str, Any], target: Any,
             who: Dict[str, Any]) -> Dict[str, Any]:
        at, av = act.get("action_type"), act.get("action_verb")
        when = self.when(automation)
        entry: Dict[str, Any] = {
            "rule": automation.get("automated_alias"),
            "action": at,
            "verb": av,
            "when": when,
        }
        until = f" Until then it stays with {who['_short']}."
        if at == "visibility" and av == "show":
            says = f"Hidden when the process launches. Shown when {when}."
        elif at == "visibility":
            says = f"Hidden when {when}."
        elif at == "assignment" and av == "assign_only":
            says = f"When {when}, {self._people(act)} replaces whoever is on the step."
            if _actionable_id_of(act) and not _has_assignees(act):
                says += " If that field is empty then, nobody is left on the step."
            says += until
        elif at == "assignment" and av == "assign":
            says = f"When {when}, {self._people(act)} is added to whoever is on the step." + until
        elif at == "assignment" and av == "clear_assignees":
            says = f"When {when}, everyone is taken off the step." + until
        elif at == "assignment" and av == "unassign":
            says = (f"When {when}, {self._people(act)} is taken off the step and "
                    f"everyone else on it stays.") + until
        elif at == "assignment":
            says = f"When {when}, nothing changes: Tallyfy does not act on '{av}' today."
        elif at == "deadline":
            entry["lands"], says = self._deadline_rule(act, target, when)
        elif at == "status":
            says = f"Reopened when {when}."
        elif at == "escalation":
            says = (
                f"When {when}, Tallyfy posts a comment labeled escalation on this "
                f"step's task that mentions {self._people(act)}, which emails them. "
                f"It does not block the task or change the process status, and "
                f"it posts nothing once the task is completed."
            )
        else:
            says = f"Sends a webhook to {act.get('webhook_url')} when {when}."
        entry["says"] = says
        return entry

    def _deadline_rule(self, act: Dict[str, Any], target: Any, when: str) -> tuple:
        """(lands, says) for a deadline rule, counted the way api-v2 counts it."""
        rule_terms = _signed_terms(act.get("deadline"), "from") or {}
        amount = _offset_text(rule_terms, "", "at").strip()
        anchor_id = _actionable_id_of(act)
        anchor_type = _actionable_type_of(act)
        if anchor_id and anchor_type in ("Capture", "Prerun"):
            date = f"the date in {self._field_name(anchor_id)}"
            lands = _offset_text(rule_terms, date, date)
            says = (
                f"When {when}, the deadline moves to {lands}. If that field is "
                f"empty then, the deadline does not change."
            )
            return lands, says
        if anchor_id or anchor_type:
            if anchor_type != "Step" or anchor_id not in self.by_id:
                lands = f"{amount} '{anchor_id}'"
                says = (
                    f"When {when}, the deadline moves to {lands}, which cannot "
                    f"be counted: '{anchor_id}' is not a step or a field in "
                    f"this mapping."
                )
                return lands, says
            own, counted_from = self.due(anchor_id), f"{self._step_name(anchor_id)}'s deadline"
        else:
            own, counted_from = self.due(target), "its own deadline"
        lands = self._counted(own, rule_terms)
        if lands is None:
            lands = f"{amount} {counted_from}"
            says = f"When {when}, the deadline moves to {lands}, which cannot be counted from here."
            return lands, says
        says = (
            f"When {when}, the deadline moves to {lands}: {amount} {counted_from}, "
            f"which is {own['says']}."
        )
        return lands, says

    def build(self) -> list:
        listed = []
        for number, (sid, step) in enumerate(zip(self.ids, self.steps), start=1):
            who = self.goes_to(step)
            due = self.due(sid)
            rules, hidden = [], False
            for automation in self.automations:
                for act in automation.get("then_actions") or []:
                    if not isinstance(act, dict) or act.get("target") != sid:
                        continue
                    if act.get("action_type") == "visibility" and act.get("action_verb") == "show":
                        hidden = True
                    rules.append(self.rule(automation, act, sid, who))
            who.pop("_short")
            listed.append({
                "step": number,
                "temp_id": sid,
                "title": step.get("title"),
                "goes_to": who,
                "due": {"default": due["default"], "says": due["says"]},
                "hidden_at_launch": hidden,
                "rules": rules,
            })
        return listed


def describe_build(mapping: Dict[str, Any]) -> list:
    """Every step of a VALID mapping as it will be built and launched."""
    return _BuildReader(mapping).build()


# ---------------------------------------------------------------------------
# Whole deadlines and real field references (tallyfy/work-queue#3655)
# ---------------------------------------------------------------------------
# Three shapes used to pass here and then fail the build call. Read in the
# api-v2 checkout at 504a0fd6 (2026-10-23), not driven:
#
# * A step deadline. CreateStepRequest.php:37-41 is `deadline` sometimes,
#   required, array, then value `required_with:deadline|integer|min:0` and
#   unit, option and step each `required_with:deadline`. add_step_to_template
#   forwards an incomplete deadline on create, so a missing key is a 422 halfway
#   through the build. Laravel's `integer` also takes the string "3"; a whole
#   number is asked for here, as the action deadline check below already does,
#   because the build list can count only a number.
# * A field an action reads. AutomatedActionRequest.php:144-150 looks up the
#   actionable by id under its type and answers "The selected field is not
#   valid." when nothing is found, so a kickoff field named as a step field
#   fails too. DoableActionValidator.php:25-44 then needs an assignment field to
#   be assignees_form, or text whose field_validation holds 'email', and a
#   deadline field to be date; a deadline may name a Step instead.
# * Condition logic. AutomatedActionRequest.php:103-107 refuses every
#   condition after the first whose logic is not 'and' or 'or', and
#   create_automation_rule raises on any other logic value it is given.

STEP_DEADLINE_KEYS = ("value", "unit", "option", "step")
CONDITION_LOGIC = ("and", "or")


def _known(value: Any, names: Any) -> bool:
    """`value in names`, False for a value that cannot be looked up (a list or
    an object where an id belongs) instead of a TypeError."""
    try:
        return value in names
    except TypeError:
        return False


def _blank(value: Any) -> bool:
    """Laravel `required`: null, an empty string or an empty array fails it."""
    return value is None or (isinstance(value, str) and not value.strip()) or value in ([], {})


def _step_deadline_problems(sid: Any, deadline: Any) -> list[str]:
    """What api-v2 would refuse in one step's deadline, each naming its key."""
    if not isinstance(deadline, dict):
        return [
            f"step {sid}: deadline must be an object with value, unit, option "
            f"and step - got {type(deadline).__name__}"
        ]
    problems = []
    missing = [key for key in STEP_DEADLINE_KEYS if _blank(deadline.get(key))]
    if missing:
        problems.append(
            f"step {sid}: deadline is missing {', '.join(missing)} - api-v2 needs "
            f"value, unit, option and step together (step 'start_run' counts "
            f"from launch, or give another step's temp_id)"
        )
    value = deadline.get("value")
    if "value" not in missing:
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(
                f"step {sid}: deadline.value must be a whole number such as 2 - "
                f"got {value!r}"
            )
        elif value < 0:
            problems.append(f"step {sid}: deadline.value must be 0 or more - got {value}")
    if "unit" not in missing and deadline.get("unit") not in DEADLINE_UNITS:
        problems.append(
            f"step {sid}: deadline.unit '{deadline.get('unit')}' is invalid - "
            f"a STEP deadline accepts {sorted(DEADLINE_UNITS)}"
        )
    if "option" not in missing and deadline.get("option") not in DEADLINE_OPTIONS:
        problems.append(
            f"step {sid}: deadline.option '{deadline.get('option')}' is invalid - "
            f"a STEP deadline accepts {sorted(DEADLINE_OPTIONS)} "
            f"('prior_to' counts backwards, 'from' counts forwards). "
            f"'before' is the AUTOMATION spelling and does nothing here"
        )
    return problems


def _condition_logic_problem(alias: Any, index: int, cond: Dict[str, Any]) -> str | None:
    """The logic api-v2 and create_automation_rule would refuse, or None."""
    logic = cond.get("logic")
    if logic is None:
        if index == 0:
            return None
        return (
            f"automation '{alias}': condition {index + 1} needs logic 'and' or "
            f"'or' to join it to the ones before it; api-v2 refuses a later "
            f"condition without it"
        )
    if str(logic).strip().lower() not in CONDITION_LOGIC:
        return (
            f"automation '{alias}': condition {index + 1} logic {logic!r} must be "
            f"'and' or 'or'"
        )
    return None


def _validates_email(field: Dict[str, Any]) -> bool:
    rules = field.get("field_validation")
    if isinstance(rules, str):
        rules = [rules]
    return isinstance(rules, list) and "email" in rules


# What each action type accepts in the field it reads, and api-v2's words when
# the field is the wrong type (DoableActionValidator.php:32 and :41).
_FIELD_ACCEPTED_BY = {
    "assignment": (
        lambda f: f.get("field_type") == "assignees_form"
        or (f.get("field_type") == "text" and _validates_email(f)),
        "an assignment rule",
        'an assignees_form field, or a text field whose field_validation holds '
        '"email" (api-v2: The selected form field should be "assignees list" '
        'type or text email only.)',
    ),
    "deadline": (
        lambda f: f.get("field_type") == "date",
        "a deadline rule",
        'a date field (api-v2: The selected form field should be "date" type.)',
    ),
}


def _field_reference_problem(
    alias: Any, field_id: Any, kind: str, action_type: str,
    kickoff_fields: Dict[str, Dict[str, Any]], step_fields: Dict[str, Dict[str, Any]],
) -> str | None:
    """Why an action's field reference would fail the build, or None.

    `kind` is the resolved actionable type, Prerun (a kickoff field) or Capture
    (a field on a step), and the id must be that field's alias in the mapping.
    """
    own, other = (kickoff_fields, step_fields) if kind == "Prerun" else (step_fields, kickoff_fields)
    field = own[field_id] if _known(field_id, own) else None
    if field is None:
        if _known(field_id, other):
            right = "field" if kind == "Prerun" else "kickoff"
            return (
                f"automation '{alias}': '{field_id}' is a "
                f"{'field on a step' if kind == 'Prerun' else 'kickoff field'}, so "
                f"actionable_type must be \"{right}\" (api-v2: The selected field "
                f"is not valid.)"
            )
        return (
            f"automation '{alias}': actionable_id '{field_id}' names no "
            f"{'kickoff field' if kind == 'Prerun' else 'field on a step'} in this "
            f"mapping - use the alias of a field in kickoff_form or in a step's "
            f"form_fields (api-v2: The selected field is not valid.)"
        )
    accepts, rule, wanted = _FIELD_ACCEPTED_BY[action_type]
    if not accepts(field):
        return (
            f"automation '{alias}': '{field_id}' is a field of type "
            f"{field.get('field_type')}, and {rule} needs {wanted}"
        )
    return None


def validate_mapping(mapping: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a draft template mapping.

    Returns {valid, errors, warnings, summary, build}; `build` is filled only
    when the mapping is valid (see describe_build)."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(mapping, dict):
        return {
            "valid": False, "errors": ["mapping must be an object"], "warnings": [],
            "summary": {}, "build": [],
        }

    if not mapping.get("title"):
        errors.append("template title is missing")

    step_ids: set[str] = set()
    field_aliases: set[str] = set()
    # alias -> field, kept apart because an action names one kind or the other.
    kickoff_fields: Dict[str, Dict[str, Any]] = {}
    step_fields: Dict[str, Dict[str, Any]] = {}

    def _check_field(f: Dict[str, Any], where: str, kind: Dict[str, Dict[str, Any]]) -> None:
        ft = f.get("field_type")
        if ft not in FIELD_TYPES:
            errors.append(f"{where}: field_type '{ft}' is not one of {sorted(FIELD_TYPES)}")
        if not f.get("label"):
            warnings.append(f"{where}: form field has no label")
        if f.get("alias"):
            field_aliases.add(f["alias"])
            kind[f["alias"]] = f

    for f in mapping.get("kickoff_form", []) or []:
        _check_field(f, "kickoff_form", kickoff_fields)

    steps = mapping.get("steps", []) or []
    for i, s in enumerate(steps):
        sid = s.get("temp_id") or f"step_{i + 1}"
        step_ids.add(sid)
        if s.get("step_type") not in STEP_TYPES:
            errors.append(f"step {sid}: step_type '{s.get('step_type')}' is not one of {sorted(STEP_TYPES)}")
        if not s.get("title"):
            errors.append(f"step {sid}: missing title")
        # None is a step with no deadline, which gets Tallyfy's default. Anything
        # else is sent as written, so it must be whole (#3655).
        if s.get("deadline") is not None:
            errors.extend(_step_deadline_problems(sid, s["deadline"]))
        for f in s.get("form_fields", []) or []:
            _check_field(f, f"step {sid}", step_fields)

    # deadline.step cross-refs (resolved after all step ids are known)
    for s in steps:
        dl = s.get("deadline")
        if (
            isinstance(dl, dict) and not _blank(dl.get("step"))
            and dl.get("step") != "start_run" and not _known(dl.get("step"), step_ids)
        ):
            errors.append(f"step {s.get('temp_id')}: deadline.step '{dl.get('step')}' does not resolve to a step")

    automations = mapping.get("automations", []) or []
    for a in automations:
        alias = a.get("automated_alias", "?")
        for index, c in enumerate(a.get("conditions", []) or []):
            logic_problem = _condition_logic_problem(alias, index, c)
            if logic_problem:
                errors.append(logic_problem)
            ctype = c.get("type")
            op = c.get("operation")
            if ctype == "step":
                if op not in STEP_OPS:
                    errors.append(f"automation '{alias}': step operation '{op}' is invalid")
                if not _known(c.get("on"), step_ids):
                    errors.append(f"automation '{alias}': condition.on step '{c.get('on')}' does not resolve")
            elif ctype == "field":
                if op not in FIELD_OPS:
                    errors.append(f"automation '{alias}': field operation '{op}' is invalid")
                if not _known(c.get("on"), field_aliases):
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

            if at == "escalation":
                # Recipients (members and groups, no guests) and the comment
                # text. Shared with the build tool, see escalation_problems.
                errors.extend(
                    f"automation '{alias}': escalation action {problem}"
                    for problem in escalation_problems(act)
                )

            if at == "assignment" and av != "clear_assignees":
                # DoableActionValidator.php:49-58 skips this when the action reads
                # its people off a form field, which is actionable_id/_type on the
                # payload (AutomatedActionRequest.php:35-36, :103-104).
                # The id is read through the SAME alias fold create_automation_rule
                # applies, or a mapping written with a shorthand is reported as
                # having no assignees while the build tool would accept it.
                a_id = _actionable_id_of(act)
                a_type = _actionable_type_of(act)
                if a_id and not a_type:
                    errors.append(
                        f"automation '{alias}': a field reference needs "
                        f"actionable_type alongside actionable_id - \"kickoff\" "
                        f"for a kickoff field, \"field\" for a form field on a step"
                    )
                elif a_type and not a_id:
                    errors.append(
                        f"automation '{alias}': a field reference needs "
                        f"actionable_id alongside actionable_type"
                    )
                elif a_id and a_type not in ASSIGNMENT_ACTIONABLE_TYPES:
                    errors.append(
                        f"automation '{alias}': an assignment action cannot take "
                        f"its assignees from a step; actionable_type must be "
                        f"\"kickoff\" or \"field\" and must name an assignee-picker "
                        f"(assignees_form) field, or a text field validated as an "
                        f"email (api-v2: The selected form field should be "
                        f"\"assignees list\" type or text email only.)"
                    )
                elif a_id:
                    # The field must be in this mapping, under the kind the
                    # type names, and of a type that holds people (#3655).
                    problem = _field_reference_problem(
                        alias, a_id, a_type, "assignment", kickoff_fields, step_fields,
                    )
                    if problem:
                        errors.append(problem)
                elif not a_id and not _has_assignees(act):
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
                # What the rule counts from: nothing (the step's own deadline),
                # another step, or a date field. Both keys or neither (#3655).
                d_id, d_type = _actionable_id_of(act), _actionable_type_of(act)
                if bool(d_id) != bool(d_type):
                    errors.append(
                        f"automation '{alias}': a deadline rule that counts from "
                        f"a step or a date field needs both actionable_id and "
                        f"actionable_type"
                    )
                elif d_type == "Step":
                    if not _known(d_id, step_ids):
                        errors.append(
                            f"automation '{alias}': actionable_id '{d_id}' names no "
                            f"step in this mapping - use a step's temp_id"
                        )
                elif d_type in ("Capture", "Prerun"):
                    problem = _field_reference_problem(
                        alias, d_id, d_type, "deadline", kickoff_fields, step_fields,
                    )
                    if problem:
                        errors.append(problem)
                elif d_type:
                    errors.append(
                        f"automation '{alias}': actionable_type '{d_type}' is "
                        f"invalid - a deadline rule counts from \"step\", "
                        f"\"kickoff\" or \"field\""
                    )

            if not _known(act.get("target"), step_ids):
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
        # Only a mapping that passed is read, so nothing is ever proposed
        # from a draft that cannot be built.
        "build": describe_build(mapping) if not errors else [],
    }


def register_template_mapping_validation_tools(mcp):
    """Register the process-document mapping validation tool with the MCP server."""

    @mcp.tool(
        name="validate_template_mapping",
        description="""Validate a DRAFT Tallyfy template mapping before building it.

USE THIS after turning a flowchart, SOP or diagram into a draft template, and
BEFORE create_template + add_step_to_template + add_form_field_to_step +
create_automation_rule. It checks each step_type, field_type, deadline and
automation against Tallyfy's allowed values, and that every step or field an
automation names is in the mapping. No AI, no network. Pass the full mapping.

mapping shape:
{"title","summary","kickoff_form":[{alias,label,field_type,required,...}],
 "steps":[{temp_id,position,title,step_type,assignees,groups,guests,assign_run_starter,deadline,form_fields}],
 "automations":[{automated_alias,conditions:[{on,type:'step'|'field',operation,statement,logic}],
  then_actions:[{action_type,action_verb,target,deadline,assignees,
  webhook_url,alias_name,comment_content}]}]}

action_type CONSTRAINS action_verb:
  visibility -> show|hide  deadline -> deadline  status -> reopen
  webhook -> emit_webhook  assignment -> assign|assign_only|unassign|clear_assignees
  escalation -> raise_escalation
webhook also needs webhook_url AND alias_name; assignment other than
clear_assignees needs assignees {users,guests,groups} or an assignees_form
field (actionable_id = its alias, actionable_type kickoff|field); escalation
needs assignees {users,groups}, no guests, and comment_content. Each
condition after the first needs logic and|or.

The two deadline vocabularies DIFFER and are checked separately:
  steps[].deadline: value, unit, option and step all required; unit
    minutes|hours|days|weeks|months, singular such as 'day' also accepted;
    option from|prior_to
  then_actions[].deadline: unit plural only; option before|from

Returns {valid, errors, warnings, summary, build}. build, only when valid: each
step in order as it will be built, who gets it and when it is due at launch with
Tallyfy's defaults filled in, and each rule that changes it later. Write the
proposal and the confirmation from build.""",
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
    def validate_template_mapping(
        mapping: documented_payload(TemplateMappingData),
    ) -> ToolResult:
        """
        Validate a draft Tallyfy template mapping (no AI, no network).

        Args:
            mapping: The draft template structure (title, kickoff_form, steps,
                automations) to validate before building it via the
                create_template / add_step / add_form_field / create_automation
                tool-chain.

        Returns:
            Dict with 'valid' (bool), 'errors' (list of blocking problems),
            'warnings' (non-blocking), 'summary' (counts) and 'build' (each
            step as it will be built and launched; empty unless valid).
        """
        result = validate_mapping(mapping)
        return ToolResult(content=result, structured_content=None)
