"""Health facts for ``assess_template_health``, computed in code over the WHOLE template (#1659).

**Why in code.** A large template cannot reach the model whole: the result
ceiling is 25 KB and a 247-step template is several hundred. Before #1659 the
tool sent one step and every rule and asked the model to judge them. These
checks need no judgement, only counting and matching ids, so they run here on
the full template, before the 500-character string cap and before any outline
or page, and the model gets the answers rather than a sample to guess from.

Every check is defined from api-v2's own behaviour, cited at the check:

* **No assignee**: ``StepDef.has_static_assignee`` from
  ``template_testing.document``, the same predicate ``test_template`` uses
  (members, groups, guests, roles, run starter, owner, AI). Rules that assign
  a step at run time are not counted, so this is "nobody set on the step".
* **No summary**: the summary is absent or holds only markup and whitespace.
* **No deadline**: ``deadline`` is absent or has no ``value``. ``Step::deadline``
  decodes a null column to ``[]`` (app/Models/Step.php:386), which the
  serialiser drops.
* **Unknown ids**: ``template_testing.references.orphaned_reference_ids``, the
  shared check with the #617 guard (a condition id is a step id only when its
  type is ``Step``).
* **No actions / no conditions**: ``AutomatedRule::forBlueprint`` returns an
  empty rule when either list is empty (AutomatedRule.php:17-19), so neither
  can ever do anything.
* **Hidden and nothing can show it**: Tallyfy has no hidden flag on a step. A
  step starts hidden when ANY rule's action shows it
  (ApplyRulesWithPrerunConditions.php:36-44). So "hidden that no rule shows"
  means: hidden at launch, and every rule that shows it has no conditions, so
  none of them can ever fire.
* **Required kickoff field nothing uses**: no rule condition or action names its
  id, and no step title, step summary or the process name pattern carries it as
  ``{{alias}}`` (the variable syntax ``VariableReplacement::getReplacedValue``
  replaces) or by id.

Lists are capped at ``LIST_CAP`` entries so the facts stay small next to the
outline; every list sits beside its true ``count``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Set

from template_testing.document import (
    NotATemplateDocument,
    parse_template,
)
from template_testing.references import orphaned_reference_ids
from utils.template_outline import steps_in_position_order

LIST_CAP = 10

_TAG = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"(?:\s|&nbsp;|&#160;)+")


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, dict):
        inner = value.get("data")
        return list(inner) if isinstance(inner, (list, tuple)) else []
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _text(value: Any) -> str:
    """Visible text of a possibly-HTML string, whitespace collapsed."""
    if not isinstance(value, str):
        return ""
    return _SPACE.sub(" ", _TAG.sub(" ", value)).strip()


def _capped(items: List[Any]) -> List[Any]:
    return items[:LIST_CAP]


def _step_ref(step: Dict[str, Any]) -> Dict[str, Any]:
    return {"position": step.get("position"), "id": step.get("id")}


def _step_list(steps: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    refs = [_step_ref(s) for s in steps]
    return {"count": len(refs), "steps": _capped(refs)}


def _rule_list(rule_ids: List[Any]) -> Dict[str, Any]:
    return {"count": len(rule_ids), "rules": _capped(rule_ids)}


def _uses_field(field: Dict[str, Any], texts: List[str]) -> bool:
    fid = field.get("id")
    alias = field.get("alias")
    pattern = re.compile(r"\{\{\s*" + re.escape(alias) + r"\s*\}\}") if alias else None
    for text in texts:
        if fid and fid in text:
            return True
        if pattern is not None and pattern.search(text):
            return True
    return False


def compute_health_facts(template: Dict[str, Any]) -> Dict[str, Any]:
    """The checkable facts about one raw template body.

    ``template`` is the unwrapped body from ``_read_template_body``: raw, not
    serialised, so no string has been cut yet. Includes may still be
    Fractal-wrapped; both shapes are read.
    """
    steps = steps_in_position_order(_as_list(template.get("steps")))
    rules = [r for r in _as_list(template.get("automated_actions")) if isinstance(r, dict)]
    kickoff = [f for f in _as_list(template.get("prerun")) if isinstance(f, dict)]

    try:
        doc = parse_template(template)
    except NotATemplateDocument:
        doc = None

    step_ids: Set[str] = {s["id"] for s in steps if s.get("id")}
    capture_ids: Set[str] = {
        c["id"]
        for s in steps
        for c in _as_list(s.get("captures"))
        if isinstance(c, dict) and c.get("id")
    }
    kickoff_ids: Set[str] = {f["id"] for f in kickoff if f.get("id")}
    stages = {s.get("stage_id") for s in steps if s.get("stage_id")}

    # --- steps -----------------------------------------------------------
    by_id = {s.get("id"): s for s in steps}
    no_assignee = [
        by_id[d.id] for d in (doc.steps if doc else [])
        if d.id in by_id and not d.has_static_assignee()
    ]
    no_summary = [s for s in steps if not _text(s.get("summary"))]
    no_deadline = [
        s for s in steps
        if not isinstance(s.get("deadline"), dict) or s["deadline"].get("value") is None
    ]

    titles: Dict[str, List[Dict[str, Any]]] = {}
    for s in steps:
        key = _text(s.get("title")).casefold()
        if key:
            titles.setdefault(key, []).append(s)
    duplicates = [
        {"title": group[0].get("title"), "steps": [_step_ref(s) for s in group]}
        for group in titles.values() if len(group) > 1
    ]

    # --- rules -----------------------------------------------------------
    unknown: List[Dict[str, Any]] = []
    no_actions: List[Any] = []
    no_conditions: List[Any] = []
    condition_id_sets = {"Step": step_ids, "Capture": capture_ids, "Prerun": kickoff_ids}
    for rule in rules:
        conditions = [c for c in _as_list(rule.get("conditions")) if isinstance(c, dict)]
        actions = [a for a in _as_list(rule.get("then_actions")) if isinstance(a, dict)]
        orphans = orphaned_reference_ids(conditions, actions, condition_id_sets, step_ids)
        if orphans:
            unknown.append({"rule": rule.get("id"), "unknown_ids": orphans})
        if not actions:
            no_actions.append(rule.get("id"))
        if not conditions:
            no_conditions.append(rule.get("id"))

    # --- hidden at launch, and nothing can show it -------------------------
    stuck: List[Dict[str, Any]] = []
    if doc is not None:
        hidden = doc.hidden_at_launch()
        for step_id in hidden:
            shown_by = [
                auto for auto in doc.automations
                if any(
                    a.action_type == "visibility" and a.action_verb == "show"
                    and a.target_step_id == step_id
                    for a in auto.actions
                )
            ]
            if shown_by and all(not auto.conditions for auto in shown_by) and step_id in by_id:
                stuck.append(by_id[step_id])
        stuck = steps_in_position_order(stuck)

    # --- required kickoff fields that nothing uses -------------------------
    texts = [str(template.get("default_process_name_format") or "")]
    for s in steps:
        texts.append(str(s.get("title") or ""))
        texts.append(str(s.get("summary") or ""))
    rule_ids_named: Set[str] = set()
    for rule in rules:
        for c in _as_list(rule.get("conditions")):
            if isinstance(c, dict) and c.get("conditionable_id"):
                rule_ids_named.add(c["conditionable_id"])
        for a in _as_list(rule.get("then_actions")):
            if isinstance(a, dict) and a.get("actionable_id"):
                rule_ids_named.add(a["actionable_id"])
    unused = [
        {"id": f.get("id"), "label": f.get("label") or f.get("alias")}
        for f in sorted(kickoff, key=lambda f: f.get("position") or 0)
        if f.get("required") and f.get("id") not in rule_ids_named
        and not _uses_field(f, texts)
    ]

    return {
        "_scope": (
            f"Computed in code from all {len(steps)} steps, {len(rules)} rules and "
            f"{len(kickoff)} kickoff fields, before any outline or page, so these "
            f"facts cover the whole template. Each list shows at most {LIST_CAP}; "
            "count is the true total."
        ),
        "counts": {
            "steps": len(steps),
            "stages": len(stages),
            "kickoff_fields": len(kickoff),
            "step_fields": len(capture_ids),
            "rules": len(rules),
        },
        "steps_without_assignee": _step_list(no_assignee),
        "steps_without_summary": _step_list(no_summary),
        "steps_without_deadline": _step_list(no_deadline),
        "rules_with_unknown_ids": {"count": len(unknown), "rules": _capped(unknown)},
        "rules_without_actions": _rule_list(no_actions),
        "rules_without_conditions": _rule_list(no_conditions),
        "duplicate_step_titles": {"count": len(duplicates), "groups": _capped(duplicates)},
        "hidden_steps_nothing_can_show": _step_list(stuck),
        "required_kickoff_fields_unused": {"count": len(unused), "fields": _capped(unused)},
    }
