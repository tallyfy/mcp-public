"""
Template mapping validation — the guardrail half of process-document import.

PURE / DETERMINISTIC: no AI, no network, no disk, no new dependencies. Runs
identically on the DigitalOcean primary and the stateless Cloud Run mirror.

Given a DRAFT Tallyfy template mapping (steps / form fields / automations) — e.g.
one the host's Claude agent produced from a customer's flowchart, SOP or process
diagram — this validates it against the code-verified Tallyfy schema (enums +
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
                    then_actions:[{action_type,action_verb,target,custom_data?}]} ]
}
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
FIELD_TYPES = {
    "text", "textarea", "radio", "dropdown", "multiselect", "date",
    "email", "file", "table", "assignees_form",
}
DEADLINE_UNITS = {"minutes", "hours", "days", "weeks", "months"}
DEADLINE_OPTIONS = {"from", "prior_to"}
STEP_OPS = {
    "completed", "reopened", "approved", "rejected", "acknowledged",
    "expired", "not_assigned",
}
FIELD_OPS = {
    "contains", "not_contains", "equals", "not_equals", "equals_any",
    "greater_than", "less_than", "is_empty", "is_not_empty",
}
ACTION_TYPES = {"visibility", "deadline", "assignment", "status", "webhook"}
ACTION_VERBS = {
    "show", "hide", "deadline", "assign", "assign_only",
    "clear_assignees", "unassign", "reopen", "emit_webhook",
}


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
                errors.append(f"step {sid}: deadline.unit '{dl.get('unit')}' is invalid")
            if dl.get("option") not in DEADLINE_OPTIONS:
                errors.append(f"step {sid}: deadline.option '{dl.get('option')}' is invalid")
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
            if act.get("action_type") not in ACTION_TYPES:
                errors.append(f"automation '{alias}': action_type '{act.get('action_type')}' is invalid")
            if act.get("action_verb") not in ACTION_VERBS:
                errors.append(f"automation '{alias}': action_verb '{act.get('action_verb')}' is invalid")
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

This tool does NO AI and NO network calls — it is a pure schema/cross-reference
validator. Pass the full mapping object you intend to build.

mapping shape:
{"title","summary","kickoff_form":[{alias,label,field_type,required,...}],
 "steps":[{temp_id,position,title,step_type,assignees,deadline,form_fields}],
 "automations":[{automated_alias,conditions:[{on,type:'step'|'field',operation,statement,logic}],
                 then_actions:[{action_type,action_verb,target,custom_data}]}]}

Returns: {valid: bool, errors: [str], warnings: [str], summary: {steps, kickoff_fields, automations}}.""",
        tags={"templates", "import", "validation", "process-document", "read-only"},
        annotations=ToolAnnotations(
            title="Validate a draft template mapping",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
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
