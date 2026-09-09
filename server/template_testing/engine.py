"""Orchestration: walk every component, accumulate evidence, run the checks.

Spec section 3 and section 6. The two entry points here are the whole public
surface of the engine, and both are pure functions of a template document.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from template_testing.checks import (
    PLAN_CAVEAT,
    ComponentEvidence,
    ConflictObservation,
    check_always_true_condition,
    check_conflicting_rules,
    check_dangling_reference,
    check_empty_rule,
    check_inert_rule,
    check_inert_start_date,
    check_never_evaluated_automation,
    check_no_completable_path,
    check_orphaned_deadline_anchor,
    check_stale_condition_value,
    check_unassigned_visible_step,
    check_unreachable_step,
)
from template_testing.document import TemplateDoc, parse_template
from template_testing.domains import FieldOutcome, Variable
from template_testing.findings import (
    SEVERITY_ORDER,
    Finding,
    contains_never_wording,
    meets_min_severity,
)
from template_testing.walk import (
    DEFAULT_MAX_ASSIGNMENTS,
    Assignment,
    Component,
    assignment_from_row,
    build_components,
    domain_product,
    enumerate_assignments,
    simulate,
)

# Bumped whenever the engine's OUTPUT changes in a way a conformance fixture
# would notice. The fixtures under fixtures/template-tests/ record it, so a Go
# implementation reading them can refuse a suite it does not understand.
ENGINE_VERSION = "1.0.0"

CONTRADICTORY_PAIRS = (
    frozenset({"show", "hide"}),
    frozenset({"assign", "clear_assignees"}),
    frozenset({"assign", "unassign"}),
    frozenset({"assign_only", "assign"}),
)


class UnknownAnswerLabel(ValueError):
    """A scenario named a field that does not exist, or names two fields.

    Deliberately loud. A silently ignored answer produces a confident report
    about a scenario the user did not ask for (spec 6.2).
    """


def _conflict_pairs(applications) -> List[Tuple[Any, Any]]:
    """Contradictory applications to one target, from two different rules."""
    by_target: Dict[str, List[Any]] = {}
    for app in applications:
        if app.target_step_id:
            by_target.setdefault(app.target_step_id, []).append(app)
    pairs: List[Tuple[Any, Any]] = []
    for apps in by_target.values():
        for i, left in enumerate(apps):
            for right in apps[i + 1 :]:
                if left.automation_id == right.automation_id:
                    continue
                # BOTH must have changed something. Spec check 3's does-not-fire
                # rule names the second ("hide after hide changes nothing, so
                # there is no conflict"), and the first is the same argument run
                # backwards: an action that changed nothing was never overridden,
                # because it never took effect.
                #
                # Measured on the customer's live template 2026-09-08. A rule
                # called "Later steps hidden at launch" hides four steps that are
                # ALREADY hidden at launch, because every one of them is
                # show-targeted (spec 2.4). Requiring only the second produced
                # FOUR ordered_override findings about a rule whose every action
                # is inert. The honest finding there is check 2's `no_effect` on
                # that one rule, which is one line instead of four and is
                # actionable.
                if not (left.changed and right.changed):
                    continue
                verbs = frozenset({left.action_verb, right.action_verb})
                if verbs in CONTRADICTORY_PAIRS:
                    pairs.append((left, right))
                    continue
                if (
                    left.action_type == "deadline"
                    and right.action_type == "deadline"
                    and left.deadline != right.deadline
                ):
                    pairs.append((left, right))
    return pairs


def _order_is_free(result, left, right) -> bool:
    """Could the two applications have happened the other way round?

    The simulation is deterministic, so every conflict it observes is ordered by
    construction. Real order-dependence comes from the STEP ORDERING degree of
    freedom (spec 3.7): if the step that triggered the later rule was ALREADY
    completable when the earlier one fired, a real run could have taken them in
    either order.
    """
    if left.event_index < 0 or right.event_index < 0:
        # A kick-off rule fires in a fixed position order, so the order is forced.
        return False
    trigger_by_event = {event: step for step, event in result.pick_index.items()}
    later_trigger = trigger_by_event.get(right.event_index)
    if later_trigger is None:
        return False
    candidates = result.candidates_at.get(left.event_index, frozenset())
    return later_trigger in candidates and later_trigger != trigger_by_event.get(
        left.event_index
    )


def _accumulate(
    doc: TemplateDoc,
    component: Component,
    max_assignments: int,
    pinned: Optional[Dict[str, FieldOutcome]] = None,
) -> Tuple[ComponentEvidence, List[Tuple[Assignment, Any]]]:
    """Walk one component and fold every assignment's result into one evidence."""
    variables = _apply_pins(component.variables, pinned or {})
    rows, truncated = enumerate_assignments(variables, max_assignments)
    automations = [a for a in doc.automations if a.id in set(component.automation_ids)]

    evidence = ComponentEvidence(
        component=component,
        truncated=truncated,
        assignments_walked=len(rows),
        visibility_targets={
            act.target_step_id
            for auto in automations
            for act in auto.actions
            if act.action_type == "visibility"
            and act.action_verb == "show"
            and act.target_step_id
        },
    )
    anchors = [
        (step.id, step.deadline_anchor)
        for step in doc.steps
        if step.deadline_anchor and step.deadline_anchor in doc.step_ids
    ]

    walks: List[Tuple[Assignment, Any]] = []
    for row in rows:
        assignment = assignment_from_row(variables, row)
        result = simulate(doc, automations, assignment)
        walks.append((assignment, result))

        evidence.shown |= result.shown
        evidence.applicable |= result.applicable
        evidence.effective |= result.effective

        for step_id in result.unassigned_visible:
            evidence.unassigned_witness.setdefault(step_id, assignment)

        for step_id in result.stalled:
            # "The stall is caused only by the walk's canonical position ordering"
            # is an explicit does-not-fire rule (spec check 5), so re-check with
            # the opposite ordering before recording anything. Only run on a
            # stall, which is rare, so the cost is not paid on every assignment.
            alternative = simulate(doc, automations, assignment, reverse_order=True)
            if step_id in alternative.stalled:
                evidence.stalled_witness.setdefault(step_id, assignment)

        for dependent, anchor in anchors:
            if dependent not in result.shown:
                continue
            anchor_is_shown_here = anchor in evidence.visibility_targets
            anchor_hidden_elsewhere = (
                anchor in doc.hidden_at_launch() and not anchor_is_shown_here
            )
            if anchor_hidden_elsewhere:
                # Another component owns this anchor's show rules, so this walk
                # cannot say whether it resolves. Saying nothing beats a witness
                # that describes the simulator rather than the template.
                continue
            key = (dependent, anchor)
            evidence.anchor_eligible[key] = evidence.anchor_eligible.get(key, 0) + 1
            if anchor not in result.completed:
                evidence.anchor_unresolved_count[key] = (
                    evidence.anchor_unresolved_count.get(key, 0) + 1
                )
                evidence.anchor_unresolved.setdefault(key, assignment)

        for left, right in _conflict_pairs(result.applications):
            key = (left.automation_id, right.automation_id, left.target_step_id)
            if key in evidence.conflicts:
                continue
            free = _order_is_free(result, left, right)
            evidence.conflicts[key] = ConflictObservation(
                rule_a=left.automation_id,
                rule_b=right.automation_id,
                target_step_id=left.target_step_id,
                verb_a=left.action_verb,
                verb_b=right.action_verb,
                order_free=free,
                winner=right.automation_id,
                witness=assignment,
            )

    return evidence, walks


def _components_or_baseline(doc: TemplateDoc) -> List[Component]:
    """Always give the walk at least one component to run.

    ⚠️ A template with no live automations produces ZERO components, and a walk
    that never runs collects no evidence, so checks 4 and 5 could not fire on the
    simplest template in the estate: a plain step list with nobody assigned. That
    is the shape most likely to have the defect, and it failed toward "nothing
    found". One empty component walks the steps with no rules, which is exactly
    what such a template does at runtime.
    """
    components = build_components(doc)
    if components:
        return components
    return [Component(index=0, automation_ids=(), variables=())]


def _apply_pins(
    variables: Sequence[Variable], pinned: Dict[str, FieldOutcome]
) -> List[Variable]:
    out: List[Variable] = []
    for var in variables:
        if var.kind == "field" and var.ref_id in pinned:
            out.append(
                Variable(
                    kind=var.kind,
                    ref_id=var.ref_id,
                    label=var.label,
                    domain=(pinned[var.ref_id],),
                    sampled=var.sampled,
                )
            )
        else:
            out.append(var)
    return out


def _enforce_honesty(findings: List[Finding]) -> List[Finding]:
    """A sampled finding may never make a universal claim.

    Enforced mechanically rather than left to whoever writes the next detail
    string. The check wording is what a customer reads, and "can never appear"
    from a walk that tested a fraction of the paths is the one failure that would
    cost this tool its credibility (spec 3.6).
    """
    for finding in findings:
        if finding.confidence != "exhaustive":
            if contains_never_wording(finding.title) or contains_never_wording(finding.detail):
                raise AssertionError(
                    f"sampled finding '{finding.check}' makes a universal claim: "
                    f"{finding.title!r}"
                )
    return findings


def _summary(
    doc: TemplateDoc,
    findings: List[Finding],
    coverage: Dict[str, Any],
    always_mention: Optional[List[Finding]] = None,
) -> str:
    """One paragraph a non-engineer reads first.

    ``always_mention`` carries findings that exist but sit BELOW the caller's
    ``min_severity``, and today that is exactly one check: ``inert_start_date``.

    Why it is here rather than solved with severity, since both alternatives were
    considered and rejected. Check 9 is deliberately ``info`` when the start date
    is Tallyfy's untouched two-hour default, because it fires on nearly every step
    of nearly every template and the reporting rule is part of the check. Raising
    that to ``medium`` would undo the noise control the spec spells out. Exempting
    one check from a filter the caller supplied would make ``min_severity`` mean
    something different for one check, which is worse than a sentence.

    A sentence in the summary costs nothing, is always present, and is the
    honest answer to the question the customer actually asked: the two-hour timer
    they can see is doing nothing.
    """
    high = [f for f in findings if f.severity == "high"]
    counts = {s: sum(1 for f in findings if f.severity == s) for s in SEVERITY_ORDER}
    title = doc.title or "This template"
    extra = " ".join(f.title + "." for f in (always_mention or []))

    if not findings and extra:
        base = (
            f"{title} has nothing serious wrong with it. One thing worth knowing: "
            f"{extra} "
        )
        if not coverage["exhaustive"]:
            base += (
                f"Part of it was too large to walk exhaustively, so "
                f"{coverage['assignments_walked']} paths were tested rather than all "
                f"of them. "
            )
        return base + PLAN_CAVEAT

    if not findings:
        if coverage["exhaustive"]:
            body = (
                f"{title} was walked all the way through, every path, and nothing was "
                f"found. Every step can be reached, every rule can fire, and nobody "
                f"gets stuck."
            )
        else:
            body = (
                f"{title} is too large to walk exhaustively. Nothing was found in the "
                f"{coverage['assignments_walked']} paths that were tested. That is not "
                f"the same as saying there is nothing to find."
            )
        return body + " " + PLAN_CAVEAT

    lead = f"{title} has {len(findings)} thing(s) worth looking at"
    if high:
        lead += f", {len(high)} of them serious"
    lead += ". "
    if high:
        lead += " ".join(f.title + "." for f in high[:3]) + " "
    lead += (
        "Counts by severity: "
        + ", ".join(f"{counts[s]} {s}" for s in reversed(SEVERITY_ORDER) if counts[s])
        + ". "
    )
    if extra:
        lead += extra + " "
    if not coverage["exhaustive"]:
        lead += (
            f"Part of this template was too large to walk exhaustively, so "
            f"{coverage['assignments_walked']} paths were tested rather than all of "
            f"them. Anything not listed here was not proved absent, only not found. "
        )
    return lead + PLAN_CAVEAT


def test_template_document(
    raw: Dict[str, Any],
    max_assignments: int = DEFAULT_MAX_ASSIGNMENTS,
    min_severity: str = "medium",
) -> Dict[str, Any]:
    """Walk every path through one template document and report problems.

    Pure. Takes the payload of
    ``GET /organizations/{org}/checklists/{id}?with=steps,automated_actions,prerun``
    and returns the shape spec 6.1 defines.
    """
    doc = parse_template(raw)
    components = _components_or_baseline(doc)

    evidence: List[ComponentEvidence] = []
    for component in components:
        component_evidence, _ = _accumulate(doc, component, max_assignments)
        component.truncated = component_evidence.truncated
        component.assignments_walked = component_evidence.assignments_walked
        evidence.append(component_evidence)

    shown_anywhere: Set[str] = set()
    for ev in evidence:
        shown_anywhere |= ev.shown

    findings: List[Finding] = []
    findings += check_unreachable_step(doc, evidence, shown_anywhere)
    findings += check_inert_rule(doc, evidence)
    findings += check_conflicting_rules(doc, evidence)
    findings += check_unassigned_visible_step(doc, evidence)
    findings += check_no_completable_path(doc, evidence)
    findings += check_dangling_reference(doc, _unwrapped(raw))
    findings += check_orphaned_deadline_anchor(doc, evidence)
    findings += check_stale_condition_value(doc)
    findings += check_always_true_condition(doc)
    findings += check_inert_start_date(doc)
    findings += check_empty_rule(doc)
    findings += check_never_evaluated_automation(doc)
    _enforce_honesty(findings)

    coverage = {
        "components": len(components),
        "assignments_walked": sum(e.assignments_walked for e in evidence),
        "exhaustive": all(not e.truncated for e in evidence),
        "truncated_components": [e.component.index for e in evidence if e.truncated],
        "max_assignments": max_assignments,
    }

    visible = [f for f in findings if meets_min_severity(f.severity, min_severity)]
    visible.sort(key=lambda f: (-SEVERITY_ORDER.index(f.severity), f.check))
    # Findings the caller's min_severity hides that the summary must still name.
    # `inert_start_date` is the one check the spec deliberately demotes for noise
    # reasons, which makes it the one most likely to vanish from view entirely,
    # and it is the incident this whole feature came from.
    hidden_but_worth_saying = [
        f
        for f in findings
        if f.check == "inert_start_date" and f not in visible
    ]

    return {
        "engine_version": ENGINE_VERSION,
        "template_id": doc.id,
        "template_title": doc.title,
        "summary": _summary(doc, visible, coverage, hidden_but_worth_saying),
        "coverage": coverage,
        "findings": [f.to_dict() for f in visible],
        "counts": {s: sum(1 for f in findings if f.severity == s) for s in SEVERITY_ORDER},
    }


def _unwrapped(raw: Dict[str, Any]) -> Dict[str, Any]:
    if (
        isinstance(raw, dict)
        and isinstance(raw.get("data"), dict)
        and "steps" not in raw
        and "automated_actions" not in raw
    ):
        return raw["data"]
    return raw


def _resolve_answers(
    doc: TemplateDoc, answers: Dict[str, Any]
) -> Dict[str, FieldOutcome]:
    """Map ``{field label or id: value}`` onto field ids.

    Resolving by LABEL is deliberate: nobody types a 32-character hex id into a
    question. An unknown or ambiguous label FAILS with the list of available
    labels rather than being guessed at or quietly dropped.
    """
    by_label: Dict[str, List[str]] = {}
    for fdef in doc.fields.values():
        by_label.setdefault(fdef.label.strip().lower(), []).append(fdef.id)

    resolved: Dict[str, FieldOutcome] = {}
    for key, value in (answers or {}).items():
        key_text = str(key).strip()
        if key_text in doc.fields:
            field_id = key_text
        else:
            matches = by_label.get(key_text.lower(), [])
            if len(matches) > 1:
                raise UnknownAnswerLabel(
                    f"'{key}' matches {len(matches)} fields in this template. Use the "
                    f"field id instead. Available: "
                    f"{', '.join(sorted(f.label for f in doc.fields.values()))}"
                )
            if not matches:
                raise UnknownAnswerLabel(
                    f"'{key}' is not a field on this template. Available: "
                    f"{', '.join(sorted(f.label for f in doc.fields.values())) or '(none)'}"
                )
            field_id = matches[0]
        if isinstance(value, (list, tuple, set)):
            resolved[field_id] = FieldOutcome(selected=tuple(str(v) for v in value))
        else:
            resolved[field_id] = FieldOutcome(selected=(str(value),))
    return resolved


def run_scenario(
    raw: Dict[str, Any],
    answers: Dict[str, Any],
    max_assignments: int = DEFAULT_MAX_ASSIGNMENTS,
) -> Dict[str, Any]:
    """"Show me what happens if the nominee declines" (spec 6.2).

    ``answers`` may be PARTIAL, and the partial case is the interesting one. Free
    variables are still enumerated and every conclusion is labelled ``certain``
    when it holds on every completion of the unspecified fields, or
    ``depends_on`` naming the fields that decide it.
    """
    doc = parse_template(raw)
    pinned = _resolve_answers(doc, answers)
    components = _components_or_baseline(doc)

    total_walks = 0
    rules_fired: Set[str] = set()
    evidence: List[ComponentEvidence] = []
    # Per component, because visibility is decided by the component that OWNS a
    # step's show rules and by nothing else. Counting sightings across every
    # component and comparing against the grand total is wrong: a step shown on
    # all of its own component's paths reads as "seen once in three" as soon as an
    # unrelated component contributes two more walks, and a settled answer is then
    # reported as "it depends".
    per_component: List[Dict[str, Any]] = []

    for component in components:
        component_evidence, walks = _accumulate(doc, component, max_assignments, pinned)
        evidence.append(component_evidence)
        rules_fired |= component_evidence.applicable
        free_here = set()
        for var in component.variables:
            if var.kind == "field" and var.ref_id not in pinned:
                fdef = doc.field(var.ref_id)
                if fdef is not None:
                    free_here.add(fdef.label)
        counts: Dict[str, int] = {s.id: 0 for s in doc.steps}
        for _, result in walks:
            total_walks += 1
            for step_id in counts:
                if step_id in result.shown:
                    counts[step_id] += 1
        per_component.append(
            {
                "walks": len(walks),
                "counts": counts,
                "free": free_here,
                "shows": component_evidence.visibility_targets,
            }
        )

    hidden = doc.hidden_at_launch()
    conclusions: List[Dict[str, Any]] = []
    for step in doc.steps:
        if step.id not in hidden:
            visible, always, certainty, depends = True, True, "certain", []
        else:
            owning = [c for c in per_component if step.id in c["shows"]]
            seen = sum(c["counts"][step.id] for c in owning)
            walked = sum(c["walks"] for c in owning)
            if not owning or seen == 0:
                visible, always, certainty, depends = False, False, "certain", []
            elif seen == walked:
                visible, always, certainty, depends = True, True, "certain", []
            else:
                depends = sorted({label for c in owning for label in c["free"]})
                visible, always, certainty = True, False, "depends_on"
        conclusions.append(
            {
                "step_id": step.id,
                "position": step.position,
                "title": step.title,
                "visible": visible,
                "always_visible": always,
                "certainty": certainty,
                "depends_on": depends,
                "assigned": step.has_static_assignee(),
                "deadline_anchor": step.deadline_anchor
                or ("start_run" if isinstance(step.deadline, dict) else None),
            }
        )

    free_labels: Set[str] = {label for c in per_component for label in c["free"]}
    shown_anywhere: Set[str] = set()
    for ev in evidence:
        shown_anywhere |= ev.shown

    findings: List[Finding] = []
    # A pinned walk is exhaustive over a SUBSET of answers, so no check may claim
    # "on any answer anybody could give" from it (tallyfy/mcp#1274). The findings
    # still appear; they are worded for what this run actually looked at.
    # `pinned` here is the resolved answers dict from _resolve_answers above, so an
    # empty one means the caller narrowed nothing and the universal wording is earned.
    any_pins = bool(pinned)
    findings += check_unreachable_step(doc, evidence, shown_anywhere, pinned=any_pins)
    findings += check_conflicting_rules(doc, evidence)
    findings += check_unassigned_visible_step(doc, evidence)
    findings += check_no_completable_path(doc, evidence)
    findings += check_orphaned_deadline_anchor(doc, evidence, pinned=any_pins)
    _enforce_honesty(findings)

    return {
        "engine_version": ENGINE_VERSION,
        "template_id": doc.id,
        "template_title": doc.title,
        "answers_applied": {
            doc.field(fid).label: list(outcome.selected)
            for fid, outcome in pinned.items()
            if doc.field(fid)
        },
        "free_fields": sorted(free_labels),
        "coverage": {
            "components": len(components),
            "assignments_walked": total_walks,
            "exhaustive": all(not e.truncated for e in evidence),
            "truncated_components": [e.component.index for e in evidence if e.truncated],
            "max_assignments": max_assignments,
        },
        "steps": conclusions,
        "rules_that_fire": sorted(rules_fired),
        "findings": [f.to_dict() for f in findings],
    }
