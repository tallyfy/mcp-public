"""
Automation Tools
Tools for managing template automation rules and analysis
"""

from fastmcp.tools.tool import ToolResult
from tallyfy import TallyfySDK
from mcp.types import ToolAnnotations
from fastmcp.exceptions import ToolError
from utils.fastmcp_errors import handle_tallyfy_errors
from utils.auth_context import get_authenticated_credentials, TALLYFY_API_BASE_URL
import json
from utils.fastmcp_types import (
    TemplateId,
    StepId,
    AutomationId,
    GenericDict,
    GenericList,
)
from utils.sdk_serializer import serialize_dataclass
from metrics import track_tool_execution


def register_automation_tools(mcp):
    """Register all automation tools with the MCP server"""

    # Map user-friendly conditionable_type values to the API's PascalCase enum.
    # API validates: 'required|in:Capture,Prerun,Step' (case-sensitive PascalCase).
    # LLMs often guess shorthand names, causing 422 errors (MCP-3X).
    _CONDITIONABLE_TYPE_MAP = {
        # Step variants — API expects "Step"
        "step": "Step",
        "steps": "Step",
        "bluprint_step": "Step",
        "bluprint\\step": "Step",
        "checklist_step": "Step",
        "task": "Step",
        "task_step": "Step",
        # Form field / capture variants — API expects "Capture"
        "field": "Capture",
        "capture": "Capture",
        "form_field": "Capture",
        "bluprint\\capture": "Capture",
        "form": "Capture",
        "input": "Capture",
        # Kickoff / prerun variants — API expects "Prerun"
        "prerun": "Prerun",
        "kickoff": "Prerun",
        "kickoff_field": "Prerun",
        "pre_run": "Prerun",
    }

    # Map common LLM-guessed operation values to API-expected values.
    # API validates step operations: completed, reopened, approved, rejected,
    # acknowledged, expired, not_assigned.
    # API validates field operations: contains, not_contains, equals, not_equals,
    # equals_any, greater_than, less_than, is_empty, is_not_empty.
    _OPERATION_MAP = {
        "is": "completed",
        "complete": "completed",
        "is_complete": "completed",
        "is_completed": "completed",
        "done": "completed",
        "finished": "completed",
        "reopen": "reopened",
        "is_reopened": "reopened",
        "approve": "approved",
        "is_approved": "approved",
        "reject": "rejected",
        "is_rejected": "rejected",
        "acknowledge": "acknowledged",
        "is_acknowledged": "acknowledged",
        "expire": "expired",
        "is_expired": "expired",
        "unassigned": "not_assigned",
        "is_not_assigned": "not_assigned",
        "equal": "equals",
        "is_equal": "equals",
        "not_equal": "not_equals",
        "does_not_equal": "not_equals",
        "contain": "contains",
        "does_not_contain": "not_contains",
        "empty": "is_empty",
        "not_empty": "is_not_empty",
    }

    # Valid statement values for step operations (operation alone defines trigger).
    _STEP_OPERATIONS = {
        "completed", "reopened", "approved", "rejected",
        "acknowledged", "expired", "not_assigned",
    }

    # Map LLM-guessed action_type to API-expected values.
    # API validates: 'required|in:visibility,deadline,assignment,status,webhook'
    _ACTION_TYPE_MAP = {
        "assign": "assignment",
        "assignee": "assignment",
        "assign_user": "assignment",
        "add_assignee": "assignment",
        "reassign": "assignment",
        "show": "visibility",
        "hide": "visibility",
        "show_hide": "visibility",
        "reopen_step": "status",
        "set_deadline": "deadline",
        "emit_webhook": "webhook",
        "send_webhook": "webhook",
    }

    # Shorthands an LLM reaches for that Tallyfy has no action for. These were
    # briefly mapped to "status", whose only verb is `reopen` (DoableAction.php
    # $actions_verbs has no complete verb), so _INFERABLE_VERBS below turned a
    # request to COMPLETE a step into one that REOPENED it. Removing the mapping
    # stopped the wrong action but left a bare 422 that names no cause, which is
    # how the kickoff contract bug stayed undiagnosed for seven months. Say why.
    _UNSUPPORTED_ACTION_TYPES = {
        "complete": (
            "Tallyfy automations cannot complete a step. The only status action "
            "is 'reopen' (action_type='status'). To make a step finish on its own, "
            "look at the step's own settings rather than an automation rule."
        ),
        "complete_step": (
            "Tallyfy automations cannot complete a step. The only status action "
            "is 'reopen' (action_type='status'). To make a step finish on its own, "
            "look at the step's own settings rather than an automation rule."
        ),
    }

    # Map LLM-guessed action_verb to API-expected values.
    # API validates: 'required|in:show,hide,deadline,assign,assign_only,
    #   clear_assignees,unassign,reopen,emit_webhook'
    _ACTION_VERB_MAP = {
        "add_assignee": "assign",
        "add_assignees": "assign",
        "reassign": "assign",
        "remove_assignee": "unassign",
        "remove_assignees": "unassign",
        "clear": "clear_assignees",
        "webhook": "emit_webhook",
        "send_webhook": "emit_webhook",
        "set_deadline": "deadline",
    }

    # action_type constrains action_verb — the two are NOT independent. Mirrors
    # DoableActionValidator::acceptedActionVerbs()
    # (app/Http/Requests/Checklists/AutomatedActions/DoableActionValidator.php:71-85).
    # A pair outside this map is rejected by the API with
    # "The action verb only accepts ...", so catch it locally with a better message.
    _ACTION_VERBS_BY_TYPE = {
        "visibility": ["show", "hide"],
        "deadline": ["deadline"],
        "status": ["reopen"],
        "assignment": ["assign", "assign_only", "clear_assignees", "unassign"],
        "webhook": ["emit_webhook"],
    }

    # Types with exactly one legal verb — safe to infer when the caller omits it.
    _INFERABLE_VERBS = {
        t: v[0] for t, v in _ACTION_VERBS_BY_TYPE.items() if len(v) == 1
    }

    # 'then_actions.*.deadline.unit'   => in:minutes,hours,days,weeks,months
    # 'then_actions.*.deadline.option' => in:before,from
    # 'then_actions.*.deadline.value'  => integer
    # Each is required_with the deadline object, so a partial deadline 422s.
    _DEADLINE_UNITS = ["minutes", "hours", "days", "weeks", "months"]
    _DEADLINE_OPTIONS = ["before", "from"]

    # 'conditions.*.logic' => in:and,or  (per-condition, NOT a top-level field)
    _CONDITION_LOGIC_VALUES = ["and", "or"]

    def _validate_action_pair(act: dict) -> None:
        """Reject an action_type/action_verb pair the API would refuse."""
        at = act.get("action_type")
        av = act.get("action_verb")

        accepted = _ACTION_VERBS_BY_TYPE.get(at)
        if accepted is None:
            raise ToolError(
                f"action_type must be one of "
                f"{', '.join(sorted(_ACTION_VERBS_BY_TYPE))} - got {at!r}."
            )

        if av in accepted:
            return

        message = (
            f"action_verb {av!r} is not valid for action_type {at!r}. "
            f"{at!r} accepts: {', '.join(accepted)}."
        )
        # If the verb is legal but under a different type, name that type —
        # e.g. reopen belongs to 'status', never to 'visibility'.
        owner = next(
            (t for t, verbs in _ACTION_VERBS_BY_TYPE.items() if av in verbs), None
        )
        if owner:
            message += f" The verb {av!r} belongs to action_type '{owner}'."
        raise ToolError(message)

    def _validate_deadline(act: dict) -> None:
        """A deadline action needs a COMPLETE {value, option, unit} object."""
        if act.get("action_type") != "deadline":
            return
        dl = act.get("deadline")
        if not isinstance(dl, dict):
            raise ToolError(
                'A deadline action requires a complete deadline object: '
                '{"value": <int>, "unit": "minutes|hours|days|weeks|months", '
                '"option": "before|from"}. All three keys are required together.'
            )
        missing = [k for k in ("value", "unit", "option") if dl.get(k) in (None, "")]
        if missing:
            raise ToolError(
                f"deadline is missing required key(s): {', '.join(missing)}. "
                'All of value, unit and option must be sent together, e.g. '
                '{"value": 3, "unit": "days", "option": "from"}.'
            )
        if not isinstance(dl["value"], int) or isinstance(dl["value"], bool):
            raise ToolError(f"deadline.value must be an integer - got {dl['value']!r}.")
        if dl["unit"] not in _DEADLINE_UNITS:
            raise ToolError(
                f"deadline.unit must be one of {', '.join(_DEADLINE_UNITS)} "
                f"- got {dl['unit']!r}."
            )
        if dl["option"] not in _DEADLINE_OPTIONS:
            raise ToolError(
                f"deadline.option must be one of {', '.join(_DEADLINE_OPTIONS)} "
                f"- got {dl['option']!r}. 'before' counts backwards from the "
                f"anchor, 'from' counts forwards."
            )

    def _normalize_actions(actions: list) -> list:
        """Resolve shorthand action_type/action_verb and fix assignee payload."""
        for act in actions:
            # Normalize action_type
            at = act.get("action_type", "")
            if at:
                if at.lower() in _UNSUPPORTED_ACTION_TYPES:
                    raise ToolError(_UNSUPPORTED_ACTION_TYPES[at.lower()])
                resolved = _ACTION_TYPE_MAP.get(at.lower())
                if resolved:
                    act["action_type"] = resolved

            # Normalize action_verb
            av = act.get("action_verb", "")
            if av:
                resolved = _ACTION_VERB_MAP.get(av.lower())
                if resolved:
                    act["action_verb"] = resolved
            elif act.get("action_type") in _INFERABLE_VERBS:
                # deadline/status/webhook each accept exactly one verb, so an
                # omitted verb is unambiguous rather than an error.
                act["action_verb"] = _INFERABLE_VERBS[act["action_type"]]

            # Fix LLM-guessed assignee payloads → API "assignees" format.
            # LLMs often send: subject:{type:"member",id:123} or user_id:123
            # API expects: assignees:{users:[123],guests:[],groups:[]}
            if act.get("action_type") == "assignment" and "assignees" not in act:
                users = []
                guests = []
                # Extract from "subject" (common LLM pattern)
                subject = act.pop("subject", None)
                if isinstance(subject, dict):
                    sid = subject.get("id")
                    if sid is not None:
                        if isinstance(sid, int) or (isinstance(sid, str) and sid.isdigit()):
                            users.append(int(sid))
                        elif isinstance(sid, str) and "@" in sid:
                            guests.append(sid)
                # Extract from flat "user_id" / "user_ids"
                uid = act.pop("user_id", None)
                if uid is not None:
                    users.append(int(uid))
                uids = act.pop("user_ids", None)
                if isinstance(uids, list):
                    users.extend(int(u) for u in uids)
                # Extract from flat "email" / "guest_email"
                email = act.pop("email", None) or act.pop("guest_email", None)
                if email:
                    guests.append(email)
                if users or guests:
                    act["assignees"] = {"users": users, "guests": guests}

            # Only after normalisation can the pair be judged.
            _validate_action_pair(act)
            _validate_deadline(act)

        return actions

    def _normalize_conditions(conditions: list) -> list:
        """Resolve shorthand conditionable_type and operation values."""
        for cond in conditions:
            # Normalize conditionable_type
            ct = cond.get("conditionable_type", "")
            if ct:
                resolved = _CONDITIONABLE_TYPE_MAP.get(ct.lower())
                if resolved:
                    cond["conditionable_type"] = resolved

            # Normalize operation
            op = cond.get("operation", "")
            if op:
                resolved_op = _OPERATION_MAP.get(op.lower())
                if resolved_op:
                    cond["operation"] = resolved_op

            # For step operations, statement must be a scalar or null.
            # LLMs often send "complete"/"any_time" — clear to null if nonsensical.
            final_op = cond.get("operation", "")
            if final_op in _STEP_OPERATIONS:
                stmt = cond.get("statement")
                if isinstance(stmt, str) and stmt.lower() in (
                    "complete", "any_time", "any", "true", "yes", "done",
                ):
                    cond["statement"] = None

            # 'conditions.*.statement' => 'present' in AutomatedActionRequest.
            # Laravel's `present` requires the KEY to exist (null is fine), so a
            # condition that simply omits it is a 422. Step conditions legitimately
            # carry no statement — the operation alone is the trigger — which is
            # exactly the case an LLM tends to omit.
            cond.setdefault("statement", None)

            # Per-condition AND/OR. There is NO top-level condition_logic field in
            # api-v2; the boolean join lives on each condition as `logic`
            # (AutomatedActionRequest: 'conditions.*.logic' => 'in:and,or',
            # persisted to Rule.logic).
            logic = cond.get("logic")
            if logic is not None:
                normalized_logic = str(logic).strip().lower()
                if normalized_logic not in _CONDITION_LOGIC_VALUES:
                    raise ToolError(
                        f"condition 'logic' must be 'and' or 'or' - got {logic!r}."
                    )
                cond["logic"] = normalized_logic

        return conditions

    def _apply_condition_logic(automation_data: dict) -> None:
        """Translate the non-existent top-level condition_logic to per-condition logic.

        `condition_logic` is not a field api-v2 has ever had — the SDK forwards it
        verbatim and AutomatedActionsController strips it via onlyValidatedFields(),
        so any AND/OR intent expressed that way was silently dropped and every rule
        fell back to the API default. Fold it onto each condition, where the API
        actually reads it, and drop the dead key so it is not sent.
        """
        if "condition_logic" not in automation_data:
            return

        logic = automation_data.pop("condition_logic")
        if logic is None:
            return

        normalized = str(logic).strip().lower()
        if normalized not in _CONDITION_LOGIC_VALUES:
            raise ToolError(
                f"condition_logic must be 'and' or 'or' - got {logic!r}."
            )
        for cond in automation_data.get("conditions") or []:
            # An explicit per-condition logic always wins.
            cond.setdefault("logic", normalized)

    def _default_alias(automation_data: dict) -> str:
        """Build an automated_alias when the caller omits one.

        api-v2 requires it (AutomatedActionRequest.php:15,
        'automated_alias' => 'required|string|max:300'), and the SDK create path
        emits it ONLY when `alias` is present (it maps alias to automated_alias and
        never reads automated_alias itself), so a create with no alias returns 422.
        Derive a short, readable label from the actions rather than pushing an
        undocumented required field back onto the caller.
        """
        actions = automation_data.get("actions") or []
        verbs = []
        for a in actions:
            if isinstance(a, dict):
                v = a.get("action_verb") or a.get("action_type")
                if v and str(v) not in verbs:
                    verbs.append(str(v))
        label = ("Auto: " + ", ".join(verbs)) if verbs else "Automation rule"
        return label[:300]

    def _ensure_automated_alias(automation_data: dict) -> None:
        """Guarantee automation_data carries an `alias` the SDK will forward.

        Accepts either `alias` or the api-v2 field name `automated_alias`; when both
        are absent, generates one. Preserves the caller's value, capped at the
        api-v2 limit of 300 characters. Without this, a create that follows the
        documented conditions+actions contract fails with a 422 on the missing
        automated_alias (tallyfy/mcp#617).
        """
        alias = automation_data.get("alias") or automation_data.get("automated_alias")
        if not alias:
            alias = _default_alias(automation_data)
        automation_data["alias"] = str(alias)[:300]
        # The SDK create only reads `alias`; drop the api-v2 field name so the
        # payload carries a single, canonical key.
        automation_data.pop("automated_alias", None)

    @mcp.tool(
        name="create_automation_rule",
        description="""Create conditional automation (if-then rules) for workflow templates.

REQUIRED: 'template_id' (32-char hex) + 'automation_data' (dict with `conditions`+`actions`).

COMPATIBILITY - action_type CONSTRAINS action_verb, they are NOT independent:
  visibility -> show | hide    deadline -> deadline    status -> reopen
  webhook -> emit_webhook      assignment -> assign | assign_only | unassign | clear_assignees
`reopen` NEVER pairs with `visibility`. Single-verb types may omit action_verb.

conditionable_type: step | field | kickoff (auto-resolved)
Step ops: completed, reopened, approved, rejected, acknowledged, expired, not_assigned
Field/kickoff ops: contains, not_contains, equals, not_equals, equals_any,
greater_than, less_than, is_empty, is_not_empty

EVERY condition needs a `statement` key (null for step ops). AND/OR goes on EACH
condition as `logic`:"and"|"or". No top-level condition_logic exists.

EXAMPLE (ids are 32-char hex, no hyphens) - SHOW a step when a kickoff
field = "Yes" (to hide it: action_verb "hide"):
{"alias":"Show legal","conditions":[{"conditionable_id":"<ko_field_id>","conditionable_type":"kickoff","operation":"equals","statement":"Yes","logic":"and"}],"actions":[{"action_type":"visibility","action_verb":"show","target_step_id":"<step_id>"}]}

Same envelope for other actions, swapping the "actions" entry:
  deadline: {..,"action_type":"deadline","deadline":{"value":3,"unit":"days","option":"from"}}
  assign:   {..,"action_type":"assignment","action_verb":"assign","assignees":{"users":[12345]}}

Every action needs `target_step_id`. deadline needs ALL of value/unit/option
(unit minutes|hours|days|weeks|months, option before|from). webhook needs webhook_url+alias_name.
Use "actions" (NOT "then_actions"). Tallyfy requires "alias" (a short rule name, NOT "automated_alias"); if you omit it this tool fills one in.""",
        tags=["automation", "rules", "conditional", "write"],
        annotations=ToolAnnotations(
            title="Create automation rule",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("create_automation_rule")
    @handle_tallyfy_errors("create automation rule")
    def create_automation_rule(template_id: TemplateId, automation_data: GenericDict) -> GenericDict:
        """
        Create conditional automation (if-then rules).

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            automation_data: Dictionary containing automation rule data with conditions and actions (REQUIRED)

        Returns:
            Created AutomatedAction object data
        """
        if "conditions" not in automation_data:
            raise ToolError(
                "automation_data must contain a 'conditions' key defining when the rule triggers. "
                "Example: {\"conditions\": [{\"conditionable_id\": \"a1b2c3d4e5f6789012345678901234ef\", "
                "\"conditionable_type\": \"step\", \"operation\": \"completed\", \"statement\": null, "
                "\"logic\": \"and\"}], \"actions\": [...]}"
            )
        if "actions" not in automation_data:
            raise ToolError(
                "automation_data must contain an 'actions' key (NOT 'then_actions') defining what happens when conditions are met. "
                "Example: {\"conditions\": [...], \"actions\": [{\"action_type\": \"visibility\", "
                "\"action_verb\": \"show\", \"target_step_id\": \"a1b2c3d4e5f6789012345678901234ef\"}]}"
            )

        # Resolve shorthand values to API-expected format (MCP-3X)
        _apply_condition_logic(automation_data)
        _normalize_conditions(automation_data["conditions"])
        if "actions" in automation_data:
            _normalize_actions(automation_data["actions"])
        # api-v2 requires automated_alias; supply one when the caller omits it so
        # the documented conditions+actions contract no longer 422s (mcp#617).
        _ensure_automated_alias(automation_data)

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.create_automation_rule(org_id, template_id, automation_data)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="update_automation_rule",
        description="""Modify automation conditions and actions.

REQUIRED (all three): 'template_id' (32-char hex), 'automation_id' (32-char hex), 'automation_data' (dict).

Use "actions" (NOT "then_actions"). Use "alias" (NOT "automated_alias"). Use "step"/"field"/"kickoff" for conditionable_type (auto-resolved).

VALID ENUM VALUES:
  STEP operations:    completed, reopened, approved, rejected, acknowledged, expired, not_assigned
  FIELD/KICKOFF ops:  contains, not_contains, equals, not_equals, equals_any, greater_than, less_than, is_empty, is_not_empty

COMPATIBILITY MATRIX - action_type constrains action_verb
  visibility -> show | hide    deadline -> deadline    status -> reopen
  webhook -> emit_webhook      assignment -> assign | assign_only | unassign | clear_assignees
e.g. `reopen` is only valid with action_type "status", never "visibility".
conditionable_type step | field | kickoff pairs with any action_verb.

CONDITIONS: every entry needs a `statement` key (null for step ops). AND/OR is set
PER CONDITION as `logic`:"and"|"or"; there is no top-level condition_logic field.

ACTION-SPECIFIC REQUIRED FIELDS (all actions need `target_step_id`):
  - assignment    → `assignees: {users:[...], guests:[...], groups:[...]}`
  - deadline      → COMPLETE `deadline: {value:int, unit:minutes|hours|days|weeks|months, option:before|from}`
  - emit_webhook  → `webhook_url` AND `alias_name`

INCOMPATIBLE COMBOS (produce 422 errors):
  - an action_verb outside its action_type's list (see matrix above)
  - assignment without `assignees`; a partial `deadline`
  - webhook without `webhook_url` + `alias_name`
  - operation="completed" with conditionable_type="field" (use "equals")
  - a condition with no `statement` key

CORRECT: update_automation_rule(template_id="abc...", automation_id="def...", automation_data={"conditions":[...], "actions":[...]})
WRONG:   update_automation_rule(automation_id="def...", automation_data={...})  ← MISSING template_id

Never call this without all three parameters.""",
        tags=["automation", "rules", "update", "write", "configuration"],
        annotations=ToolAnnotations(
            title="Update automation rule",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("update_automation_rule")
    @handle_tallyfy_errors("update automation rule")
    def update_automation_rule(
        template_id: TemplateId,
        automation_id: AutomationId,
        automation_data: GenericDict
    ) -> GenericDict:
        """
        Modify automation conditions and actions.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            automation_id: Automation rule ID to update (REQUIRED - 32-character hex string)
            automation_data: Dictionary containing updated automation rule data (REQUIRED)
                - Can include partial updates to conditions, actions, or both
                - Structure: {conditions: [...], then_actions: [...], name: str, etc.}

        Returns:
            Updated AutomatedAction object data
        """
        # Resolve shorthand values to API-expected format (MCP-3X)
        _apply_condition_logic(automation_data)
        if "conditions" in automation_data:
            _normalize_conditions(automation_data["conditions"])
        if "actions" in automation_data:
            _normalize_actions(automation_data["actions"])

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.update_automation_rule(org_id, template_id, automation_id, automation_data)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="delete_automation_rule",
        description="Remove an automation rule from a workflow template. REQUIRED: 'template_id' (32-char hex) and 'automation_id' (32-char hex). Never call this without both parameters.",
        tags=["automation", "rules", "delete", "write", "admin"],
        annotations=ToolAnnotations(
            title="Delete automation rule",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("delete_automation_rule")
    @handle_tallyfy_errors("delete automation rule")
    def delete_automation_rule(template_id: TemplateId, automation_id: AutomationId) -> bool:
        """
        Remove an automation rule.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            automation_id: Automation rule ID (REQUIRED - 32-character hex string)

        Returns:
            True if deletion was successful
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.delete_automation_rule(org_id, template_id, automation_id)
            return ToolResult(content=result, structured_content=None)

    def _fingerprint_conditions(conditions):
        """Create a hashable fingerprint from a list of conditions for grouping."""
        normalized = []
        for c in conditions:
            normalized.append((
                c.get("conditionable_id", ""),
                c.get("conditionable_type", ""),
                c.get("operation", ""),
                json.dumps(c.get("statement"), sort_keys=True),
            ))
        return tuple(sorted(normalized))

    def _fingerprint_action(action):
        """Create a hashable fingerprint from a single action."""
        return (
            action.get("action_type", ""),
            action.get("action_verb", ""),
            action.get("target_step_id", ""),
            json.dumps(action.get("assignees"), sort_keys=True) if action.get("assignees") else "",
            json.dumps(action.get("deadline"), sort_keys=True) if action.get("deadline") else "",
            action.get("webhook_url", ""),
        )

    def _fingerprint_actions(actions):
        """Create a hashable fingerprint from a list of actions."""
        return tuple(sorted(_fingerprint_action(a) for a in actions))

    def _detect_redundant_groups(automations):
        """Detect redundant automation rule groups from a list of serialized rules.

        Returns a list of redundancy group dicts with type, description, and rule IDs.
        """
        if len(automations) < 2:
            return []

        # Group rules by their condition fingerprint
        groups = {}
        for rule in automations:
            conditions = rule.get("conditions") or rule.get("automated_action_conditions") or []
            fp = _fingerprint_conditions(conditions)
            groups.setdefault(fp, []).append(rule)

        redundant_groups = []
        for fp, rules in groups.items():
            if len(rules) < 2:
                continue

            # Sub-group by action fingerprint to find exact duplicates
            actions_map = {}
            for rule in rules:
                actions = rule.get("then_actions") or rule.get("actions") or rule.get("automated_action_actions") or []
                afp = _fingerprint_actions(actions)
                actions_map.setdefault(afp, []).append(rule)

            # Exact duplicates: same conditions AND same actions
            for afp, dup_rules in actions_map.items():
                if len(dup_rules) > 1:
                    redundant_groups.append({
                        'type': 'exact_duplicate',
                        'description': f'{len(dup_rules)} rules with identical conditions and actions',
                        'rule_ids': [r.get('id') for r in dup_rules],
                        'rules': [{'id': r.get('id'), 'alias': r.get('alias', '')} for r in dup_rules],
                    })

            # Same trigger, different actions — candidate for merging
            if len(actions_map) > 1:
                redundant_groups.append({
                    'type': 'same_trigger',
                    'description': f'{len(rules)} rules share the same trigger conditions - actions could be merged into one rule',
                    'rule_ids': [r.get('id') for r in rules],
                    'rules': [{'id': r.get('id'), 'alias': r.get('alias', '')} for r in rules],
                })

        return redundant_groups

    @mcp.tool(
        name="analyze_template_automations",
        description="""Retrieve and analyze all automation rules for a template.

Returns the complete automation rules (conditions, actions, targets), a step lookup map,
and pre-computed redundant_groups identifying duplicates and merge candidates.

Use this data to:
- Review redundant_groups for exact duplicates (same trigger AND actions) and same-trigger rules (mergeable actions)
- Find conflicting rules (same conditions but contradictory actions, e.g. show vs hide the same step)
- Spot orphaned rules: a then-action `target_step_id` not in step_lookup, or a condition whose `conditionable_type` is "Step" and whose `conditionable_id` is not in step_lookup. A `conditionable_type` of "Capture" (form field) or "Prerun" (kickoff field) is a valid trigger whose id is NOT a step id, so its absence from step_lookup is normal, NOT an orphan.
- Assess automation complexity and suggest simplification

IMPORTANT: Two rules with the same structure (e.g. both have 1 condition and 1 action) are NOT redundant
if they reference different steps. Always compare conditionable_id AND target_step_id values.

To consolidate: review redundant_groups, then use update_automation_rule to merge actions and
delete_automation_rule to remove duplicates. The LLM decides which changes to apply.

REQUIRED: 'template_id' (32-character hex string). Never call this without the template_id parameter.""",
        tags=["automation", "analysis", "optimization", "read-only"],
        annotations=ToolAnnotations(
            title="Analyze template automations",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("analyze_template_automations")
    @handle_tallyfy_errors("analyze template automations")
    def analyze_template_automations(template_id: TemplateId) -> GenericDict:
        """
        Retrieve and analyze all automation rules for a template.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)

        Returns:
            Dictionary with automations (raw rules), step_lookup (id-to-title map),
            redundant_groups (pre-computed duplicates/merge candidates), and template metadata
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            template = sdk.templates.get_template(org_id, template_id=template_id)
            if not template:
                raise ToolError("Template not found")

            steps = [serialize_dataclass(s) for s in template.steps] if template.steps else []
            automations = [serialize_dataclass(a) for a in template.automated_actions] if template.automated_actions else []
            step_lookup = {s['id']: s.get('title', 'Unknown') for s in steps if 'id' in s}
            redundant_groups = _detect_redundant_groups(automations)

            return ToolResult(
                content={
                    'template_id': template_id,
                    'template_title': template.title,
                    'automations': automations,
                    'step_lookup': step_lookup,
                    'redundant_groups': redundant_groups,
                    'total_automations': len(automations),
                    'total_steps': len(steps),
                },
                structured_content=None
            )

    # Contradictory action verb pairs for conflict detection
    _CONTRADICTORY_VERBS = {
        frozenset({"show", "hide"}),
        frozenset({"assign", "clear_assignees"}),
        frozenset({"assign", "unassign"}),
    }

    def _build_suggestions(automations, step_lookup, valid_capture_ids=None, valid_prerun_ids=None):
        """Build prioritized consolidation suggestions from automation rules.

        Detects: exact duplicates, same-trigger merge candidates, conflicting rules,
        and orphaned rules referencing entities not in the template.
        Returns a list of suggestion dicts with priority, type, and recommended action.

        valid_capture_ids / valid_prerun_ids are the template's form-field and
        kickoff-field ids. They are validated separately from step ids because a
        condition's conditionable_id is a step id ONLY when conditionable_type is
        "Step" (see the orphan block).
        """
        suggestions = []

        if len(automations) < 1:
            return suggestions

        # --- Orphaned rules (reference entities not in the template) ---
        # A condition's conditionable_id is NOT always a step id. api-v2 allows
        # conditionable_type in {Step, Capture, Prerun}
        # (AutomatedActionRequest.php:21), and AutomatedActionTransformer.php:34-35
        # emits conditionable_id for all three: Step, Capture (a step form field) and
        # Prerun (a kickoff field). Only Step ids live in step_lookup, so the old
        # code (which tested every conditionable_id against step ids) flagged EVERY
        # form-field- and kickoff-triggered rule as orphaned and recommended deleting
        # working automations (tallyfy/mcp#617). Validate each conditionable_id
        # against the id set for its OWN type; when a type's ids cannot be
        # enumerated, skip it (never a false orphan).
        #
        # A then_action's target_step_id is always a step id
        # (AutomatedActionRequest.php:34, exists:steps,timeline_id), so it is the one
        # reference that reliably dangles. The old code read only
        # actions/automated_action_actions, but api-v2 emits `then_actions`
        # (AutomatedActionTransformer.php:18), so that check never ran and real
        # orphaned targets were never caught. Read then_actions first, matching the
        # duplicate/conflict detectors in this same module.
        valid_step_ids = set(step_lookup.keys())
        condition_id_sets = {
            "Step": valid_step_ids,
            "Capture": set(valid_capture_ids) if valid_capture_ids is not None else None,
            "Prerun": set(valid_prerun_ids) if valid_prerun_ids is not None else None,
        }
        for rule in automations:
            conditions = rule.get("conditions") or rule.get("automated_action_conditions") or []
            actions = rule.get("then_actions") or rule.get("actions") or rule.get("automated_action_actions") or []
            orphaned_ids = []
            for c in conditions:
                cid = c.get("conditionable_id", "")
                if not cid:
                    continue
                # api-v2 defaults an omitted conditionable_type to Step nowhere, but
                # every real condition carries one; fall back to Step defensively.
                ctype = c.get("conditionable_type") or "Step"
                valid_ids = condition_id_sets.get(ctype)
                # valid_ids is None => this type's ids are unavailable; do not flag,
                # so a valid form-field/kickoff rule is never reported as an orphan.
                if valid_ids is not None and cid not in valid_ids:
                    orphaned_ids.append(cid)
            for a in actions:
                tid = a.get("target_step_id", "")
                if tid and tid not in valid_step_ids:
                    orphaned_ids.append(tid)
            if orphaned_ids:
                suggestions.append({
                    'type': 'orphaned_rule',
                    'priority': 'high',
                    'rule_ids': [rule.get('id')],
                    'rules': [{'id': rule.get('id'), 'alias': rule.get('alias', '')}],
                    'orphaned_step_ids': orphaned_ids,
                    'description': f'Rule references {len(orphaned_ids)} entity(ies) not in the template',
                    # Non-destructive: a rule can mix valid and dangling references,
                    # so deleting the whole rule risks destroying working logic. A
                    # human decides (mcp#617).
                    'recommended_action': 'review',
                })

        if len(automations) < 2:
            return suggestions

        # --- Group by condition fingerprint ---
        groups = {}
        for rule in automations:
            conditions = rule.get("conditions") or rule.get("automated_action_conditions") or []
            fp = _fingerprint_conditions(conditions)
            groups.setdefault(fp, []).append(rule)

        for fp, rules in groups.items():
            if len(rules) < 2:
                continue

            actions_map = {}
            for rule in rules:
                actions = rule.get("then_actions") or rule.get("actions") or rule.get("automated_action_actions") or []
                afp = _fingerprint_actions(actions)
                actions_map.setdefault(afp, []).append(rule)

            # --- Exact duplicates ---
            for afp, dup_rules in actions_map.items():
                if len(dup_rules) > 1:
                    suggestions.append({
                        'type': 'exact_duplicate',
                        'priority': 'high',
                        'rule_ids': [r.get('id') for r in dup_rules],
                        'rules': [{'id': r.get('id'), 'alias': r.get('alias', '')} for r in dup_rules],
                        'description': f'{len(dup_rules)} rules with identical conditions and actions - keep one, delete the rest',
                        'recommended_action': 'delete_duplicates',
                    })

            # --- Conflicting rules (same trigger, contradictory actions on same target) ---
            if len(actions_map) > 1:
                all_actions = []
                for rule in rules:
                    actions = rule.get("then_actions") or rule.get("actions") or rule.get("automated_action_actions") or []
                    for a in actions:
                        all_actions.append((rule.get('id'), a))

                for i, (rid1, a1) in enumerate(all_actions):
                    for rid2, a2 in all_actions[i + 1:]:
                        if rid1 == rid2:
                            continue
                        same_target = (
                            a1.get("target_step_id") and
                            a1.get("target_step_id") == a2.get("target_step_id")
                        )
                        verb_pair = frozenset({a1.get("action_verb", ""), a2.get("action_verb", "")})
                        if same_target and verb_pair in _CONTRADICTORY_VERBS:
                            target_name = step_lookup.get(a1["target_step_id"], a1["target_step_id"])
                            suggestions.append({
                                'type': 'conflict',
                                'priority': 'high',
                                'rule_ids': [rid1, rid2],
                                'rules': [
                                    {'id': rid1, 'alias': next((r.get('alias', '') for r in rules if r.get('id') == rid1), '')},
                                    {'id': rid2, 'alias': next((r.get('alias', '') for r in rules if r.get('id') == rid2), '')},
                                ],
                                'description': f'Contradictory actions ({a1.get("action_verb")} vs {a2.get("action_verb")}) on step "{target_name}" with the same trigger',
                                'recommended_action': 'review_and_resolve',
                            })

                # --- Same trigger, different actions (merge candidate) ---
                suggestions.append({
                    'type': 'same_trigger_merge',
                    'priority': 'medium',
                    'rule_ids': [r.get('id') for r in rules],
                    'rules': [{'id': r.get('id'), 'alias': r.get('alias', '')} for r in rules],
                    'description': f'{len(rules)} rules share the same trigger - actions can be merged into one rule',
                    'recommended_action': 'merge_actions',
                })

        # Sort by priority: high first
        priority_order = {'high': 0, 'medium': 1, 'low': 2}
        suggestions.sort(key=lambda s: priority_order.get(s['priority'], 9))

        return suggestions

    @mcp.tool(
        name="suggest_automation_consolidation",
        description="""Read-only: prioritized consolidation suggestions for a template's automation rules.

`suggestion_type` (lowercase, in `type` field):
- `orphaned_rule` (high): a condition or a then-action references a step, form field (Capture), or kickoff field (Prerun) that is not in the template. Includes `orphaned_step_ids`. Recommended: review; do NOT auto-delete, a rule may still hold valid actions.
- `exact_duplicate` (high): IDENTICAL conditions AND actions across rules. Recommended: keep one, delete the rest.
- `conflict` (high): same trigger, contradictory `action_verb`s on the same target (show/hide, assign/clear_assignees, assign/unassign). Recommended: review.
- `same_trigger_merge` (medium): same trigger, non-conflicting actions across rules. Recommended: merge via `update_automation_rule`, delete the rest.

RETURN: {template_id, template_title, suggestions: [{type, priority:"high"|"medium"|"low", rule_ids, rules:[{id,alias}], description, recommended_action:"review"|"delete_duplicates"|"review_and_resolve"|"merge_actions"}], summary: {total_automations, total_suggestions, high_priority, medium_priority}}

EXAMPLE: suggest_automation_consolidation(template_id="58c03f...") returns {template_title:"New hire onboarding", suggestions:[{type:"orphaned_rule",priority:"high",rule_ids:["3f8a1c0d9e2b4a6c8d0e1f2a3b4c5d6e"],orphaned_step_ids:["7a1b2c3d4e5f60718293a4b5c6d7e8f9"],recommended_action:"review"},{type:"exact_duplicate",priority:"high",rule_ids:["b1c2d3e4f5061728394a5b6c7d8e9f00","c2d3e4f5061728394a5b6c7d8e9f0011"],recommended_action:"delete_duplicates"}], summary:{total_automations:12,total_suggestions:5,high_priority:2}}. Remediation: "delete_duplicates" then `delete_automation_rule`; "merge_actions" then `update_automation_rule`; "review"/"review_and_resolve" then ask a human before deleting.

REQUIRED: 'template_id' (32-character hex). Never call without it.""",
        tags=["automation", "analysis", "optimization", "suggestions", "read-only"],
        annotations=ToolAnnotations(
            title="Suggest automation consolidation",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("suggest_automation_consolidation")
    @handle_tallyfy_errors("suggest automation consolidation")
    def suggest_automation_consolidation(template_id: TemplateId) -> GenericDict:
        """
        Generate prioritized consolidation suggestions for automation rules.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)

        Returns:
            Dictionary with prioritized suggestions, each containing type, priority,
            affected rule IDs, description, and recommended action
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            template = sdk.templates.get_template(org_id, template_id=template_id)
            if not template:
                raise ToolError("Template not found")

            steps = [serialize_dataclass(s) for s in template.steps] if template.steps else []
            automations = [serialize_dataclass(a) for a in template.automated_actions] if template.automated_actions else []
            step_lookup = {s['id']: s.get('title', 'Unknown') for s in steps if 'id' in s}

            # Form-field (Capture) ids live inside each step's `captures`; kickoff
            # (Prerun) ids live on the template. Both are legitimate condition
            # triggers and are NOT step ids, so they must be validated against their
            # OWN id sets rather than step_lookup (mcp#617). get_template already
            # requests with=steps,automated_actions,prerun, so both are present.
            # Capture ids come from the serialized steps' nested `captures`. An
            # empty result means the template genuinely has zero form fields (an
            # empty capture list contributes zero ids whether or not the serializer
            # strips it), so the set is ALWAYS authoritative and is passed as-is,
            # never None: a Capture-triggered rule pointing at a field that no longer
            # exists is then correctly flagged. get_template always requests
            # with=steps,automated_actions,prerun, so captures are enumerable. This
            # refines the empty-set Autofix, which conflated "zero captures" with
            # "unavailable" via an any('captures' in s) probe -- serialize_dataclass
            # drops the empty `captures` key, so that probe read every captureless
            # template as unavailable and skipped the check (mcp#617).
            valid_capture_ids = {
                c['id']
                for s in steps
                for c in (s.get('captures') or [])
                if isinstance(c, dict) and c.get('id')
            }
            prerun_fields = getattr(template, 'prerun', None)
            if isinstance(prerun_fields, list):
                prerun = [serialize_dataclass(p) for p in prerun_fields]
                valid_prerun_ids = {p['id'] for p in prerun if isinstance(p, dict) and p.get('id')}
            else:
                valid_prerun_ids = set()

            suggestions = _build_suggestions(
                automations, step_lookup, valid_capture_ids, valid_prerun_ids
            )

            return ToolResult(
                content={
                    'template_id': template_id,
                    'template_title': template.title,
                    'suggestions': suggestions,
                    'summary': {
                        'total_automations': len(automations),
                        'total_suggestions': len(suggestions),
                        'high_priority': sum(1 for s in suggestions if s['priority'] == 'high'),
                        'medium_priority': sum(1 for s in suggestions if s['priority'] == 'medium'),
                    },
                },
                structured_content=None
            )

    @mcp.tool(
        name="get_step_visibility_conditions",
        description="Analyze when and how a step becomes visible based on all automations. REQUIRED: 'template_id' (32-char hex) and 'step_id' (32-char hex). Never call this without both parameters.",
        tags=["automation", "analysis", "conditional", "visibility", "read-only"],
        annotations=ToolAnnotations(
            title="Get step visibility conditions",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_step_visibility_conditions")
    @handle_tallyfy_errors("analyze step visibility conditions")
    def get_step_visibility_conditions(template_id: TemplateId, step_id: StepId) -> GenericDict:
        """
        Analyze when/how a step becomes visible based on all automations.

        Args:
            template_id: Template ID (REQUIRED - 32-character hex string)
            step_id: Step ID to analyze (REQUIRED - 32-character hex string)

        Returns:
            Dictionary containing step visibility analysis with rules and logic
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.templates.get_step_visibility_conditions(org_id, template_id, step_id)
            return ToolResult(content=serialize_dataclass(result) if result else {}, structured_content=None)
