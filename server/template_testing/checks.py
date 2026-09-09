"""The nine checks, plus the three the code demands.

Spec sections 4 and 5. **The false-positive rules are the specification, not
commentary**, so each one is written down beside the check it constrains and each
has a concrete source.

Every function here is pure: it reads accumulated evidence and returns findings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from template_testing.document import (
    CONDITIONABLE_CAPTURE,
    CONDITIONABLE_PRERUN,
    CONDITIONABLE_STEP,
    DEADLINE_ANCHOR_START_RUN,
    DEFAULT_START_DATE,
    OPTION_FIELD_TYPES,
    AutomationDef,
    TemplateDoc,
)
from template_testing.domains import _statement_members
from template_testing.findings import (
    CONFIDENCE_EXHAUSTIVE,
    CONFIDENCE_SAMPLED,
    Finding,
)
from template_testing.references import orphaned_reference_ids
from template_testing.walk import Assignment, Component

# Spec check 3, extending _CONTRADICTORY_VERBS in automation.py:1130-1134.
CONTRADICTORY_VERBS = (
    frozenset({"show", "hide"}),
    frozenset({"assign", "clear_assignees"}),
    frozenset({"assign", "unassign"}),
    frozenset({"assign_only", "assign"}),
)


@dataclass
class ConflictObservation:
    rule_a: str
    rule_b: str
    target_step_id: str
    verb_a: str
    verb_b: str
    order_free: bool
    winner: str
    witness: Assignment


@dataclass
class ComponentEvidence:
    """Everything one component's walk established, unioned over its assignments."""

    component: Component
    truncated: bool = False
    assignments_walked: int = 0
    shown: Set[str] = field(default_factory=set)
    applicable: Set[str] = field(default_factory=set)
    effective: Set[str] = field(default_factory=set)
    unassigned_witness: Dict[str, Assignment] = field(default_factory=dict)
    stalled_witness: Dict[str, Assignment] = field(default_factory=dict)
    conflicts: Dict[Tuple[str, str, str], ConflictObservation] = field(default_factory=dict)
    # (dependent_step, anchor_step) -> witness on which the anchor did not resolve
    anchor_unresolved: Dict[Tuple[str, str], Assignment] = field(default_factory=dict)
    anchor_eligible: Dict[Tuple[str, str], int] = field(default_factory=dict)
    anchor_unresolved_count: Dict[Tuple[str, str], int] = field(default_factory=dict)
    visibility_targets: Set[str] = field(default_factory=set)


def _confidence(truncated: bool) -> str:
    return CONFIDENCE_SAMPLED if truncated else CONFIDENCE_EXHAUSTIVE


def _lead(text: str) -> str:
    """Upper-case the first character and leave the rest alone.

    Python's own capitalize LOWER-cases everything after the first character, so
    a step titled "Order equipment" comes back as "order equipment" inside a
    sentence. The label is the customer's own wording and must survive.
    """
    return text[:1].upper() + text[1:] if text else text


def _step_label(doc: TemplateDoc, step_id: str) -> str:
    step = doc.step(step_id)
    return f"step {step.position} ({step.title})" if step else f"step {step_id}"


def _rule_label(rule: AutomationDef) -> str:
    return f"'{rule.alias}'" if rule.alias else f"rule {rule.id}"


# ---------------------------------------------------------------------------
# Check 1: unreachable_step
# ---------------------------------------------------------------------------


def check_unreachable_step(
    doc: TemplateDoc,
    evidence: List[ComponentEvidence],
    shown_anywhere: Set[str],
    pinned: bool = False,
) -> List[Finding]:
    """A step no path can reach (spec check 1). This is the customer's incident.

    ``pinned`` says the caller narrowed the answer domains before walking, which
    ``run_scenario`` does. **The walk is then exhaustive over a SUBSET, and saying
    "on any answer anybody could give" about a subset is false.** Measured on
    tallyfy/mcp#1274: pinning a two-option dropdown to the value that does not fire
    the show rule reported "can never appear" at high severity with exhaustive
    confidence, for a step the other answer shows. The unpinned run correctly
    reported nothing.

    ``_enforce_honesty`` cannot catch it, because the confidence genuinely IS
    exhaustive for the pinned walk. The claim is wrong about SCOPE, not about
    certainty, so the wording is what has to change.

    Does NOT fire when the step is not visibility/show-targeted at all. Such a
    step starts VISIBLE and can never be unreachable, and firing here would be the
    single most damaging false positive available, because it would flag most
    steps in most templates.
    """
    findings: List[Finding] = []
    # A show action can point at a step that has been deleted. Check 6 reports that
    # dangling reference; telling the reader a step that does not exist "can never
    # appear" is a second, phantom finding about nothing, so only real steps count.
    hidden = doc.hidden_at_launch() & doc.step_ids
    for step_id in sorted(hidden, key=lambda s: (doc.step(s).position if doc.step(s) else 0)):
        if step_id in shown_anywhere:
            continue
        affecting = [e for e in evidence if step_id in e.visibility_targets]
        truncated = any(e.truncated for e in affecting) or not affecting
        show_rules = [
            auto.id
            for auto in doc.automations
            if not auto.archived_at
            and any(
                a.action_type == "visibility"
                and a.action_verb == "show"
                and a.target_step_id == step_id
                for a in auto.actions
            )
        ]
        label = _step_label(doc, step_id)
        walked = sum(e.assignments_walked for e in affecting)
        if truncated:
            title = f"Nobody reached {label} in the paths tested"
            detail = (
                f"{_lead(label)} is hidden when a process starts, because an "
                f"automation targets it with a show action. It did not appear in any "
                f"of the {walked} paths tested. This template was too large to walk "
                f"exhaustively, so this is what the sample found rather than a "
                f"statement about every path."
            )
        elif pinned:
            title = f"{_lead(label)} does not appear on these answers"
            detail = (
                f"{_lead(label)} is hidden when a process starts, because an "
                f"automation targets it with a show action. Nothing shows it on the "
                f"answers you pinned. Other answers may well show it: this run only "
                f"looked at the ones you gave. Run the plain template test to find out "
                f"whether any answer can reach it."
            )
        else:
            title = f"{_lead(label)} can never appear"
            detail = (
                f"{_lead(label)} is hidden when a process starts, because an "
                f"automation targets it with a show action. No rule in this template "
                f"ever satisfies that show, on any answer anybody could give. On "
                f"current settings nobody will ever see this step."
            )
        findings.append(
            Finding(
                check="unreachable_step",
                # A pinned run cannot say a step is unreachable, only that these
                # answers did not reach it, so it must not carry the severity of
                # the incident this check was built for.
                severity="medium" if (pinned and not truncated) else "high",
                confidence=_confidence(truncated),
                title=title,
                detail=detail,
                step_ids=[step_id],
                rule_ids=show_rules,
                witness=None,
                recommended_action="review",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Check 2: inert_rule
# ---------------------------------------------------------------------------


def check_inert_rule(
    doc: TemplateDoc, evidence: List[ComponentEvidence]
) -> List[Finding]:
    """A rule that can never change anything (spec check 2).

    Two sub-kinds, reported separately because the remedies differ. A rule with
    zero conditions or zero actions is NOT reported here: AutomatedRule::forBlueprint
    short-circuits it before evaluation, so it is structural, and calling it
    ``never_applicable`` would imply the walk proved something it never ran.
    """
    findings: List[Finding] = []
    by_rule = {rid: e for e in evidence for rid in e.component.automation_ids}
    for rule in doc.automations:
        if rule.archived_at or rule.is_empty_rule:
            continue
        ev = by_rule.get(rule.id)
        if ev is None:
            continue
        label = _rule_label(rule)
        if rule.id not in ev.applicable:
            if ev.truncated:
                title = f"Rule {label} did not fire in the paths tested"
                detail = (
                    f"Rule {label} did not become applicable in any of the "
                    f"{ev.assignments_walked} paths tested. This part of the template "
                    f"was too large to walk exhaustively, so this is what the sample "
                    f"found rather than a statement about every path."
                )
            else:
                title = f"Rule {label} can never fire"
                detail = (
                    f"Rule {label}'s conditions are never all satisfied together, on "
                    f"any answer anybody could give. It has no effect on this template."
                )
            findings.append(
                Finding(
                    check="inert_rule",
                    severity="medium",
                    confidence=_confidence(ev.truncated),
                    title=title,
                    detail=detail,
                    step_ids=sorted({a.target_step_id for a in rule.actions if a.target_step_id}),
                    rule_ids=[rule.id],
                    witness=None,
                    recommended_action="review",
                )
            )
            continue

        if rule.id in ev.effective:
            continue
        # An emit_webhook has an external side effect this engine cannot see, so a
        # rule carrying one is never `no_effect`.
        if any(a.is_webhook for a in rule.actions):
            continue
        if ev.truncated:
            title = f"Rule {label} changed nothing in the paths tested"
            detail = (
                f"Rule {label} fired, and on every path tested each of its actions "
                f"was already true, so nothing changed. This part of the template was "
                f"too large to walk exhaustively."
            )
        else:
            title = f"Rule {label} fires but changes nothing"
            detail = (
                f"Rule {label} does become applicable, and every time it does, each of "
                f"its actions is already true, so applying it changes nothing. Showing "
                f"a step that is already visible, or hiding one that is already hidden, "
                f"does nothing at all."
            )
        findings.append(
            Finding(
                check="inert_rule",
                severity="low",
                confidence=_confidence(ev.truncated),
                title=title,
                detail=detail,
                step_ids=sorted({a.target_step_id for a in rule.actions if a.target_step_id}),
                rule_ids=[rule.id],
                witness=None,
                recommended_action="review",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Check 3: conflicting_rules
# ---------------------------------------------------------------------------


def check_conflicting_rules(
    doc: TemplateDoc, evidence: List[ComponentEvidence]
) -> List[Finding]:
    """Two rules fighting over the same step (spec check 3).

    ``ordered_override`` is the lower-severity half: the two are deterministically
    ordered by the event sequence, so one always wins. Silently picking a winner
    and calling it a conflict is wrong; silently picking a winner and saying
    nothing is worse.
    """
    findings: List[Finding] = []
    for ev in evidence:
        for obs in ev.conflicts.values():
            label = _step_label(doc, obs.target_step_id)
            if obs.order_free:
                findings.append(
                    Finding(
                        check="conflicting_rules",
                        severity="high",
                        confidence=_confidence(ev.truncated),
                        title=f"Two rules fight over {label}",
                        detail=(
                            f"On one set of answers both rules apply to {label}, one "
                            f"with '{obs.verb_a}' and one with '{obs.verb_b}'. Which one "
                            f"wins depends on the order the earlier steps happen to be "
                            f"completed in, so the same answers can produce different "
                            f"outcomes on different runs."
                        ),
                        step_ids=[obs.target_step_id],
                        rule_ids=[obs.rule_a, obs.rule_b],
                        witness=obs.witness.describe(doc),
                        recommended_action="review",
                    )
                )
            else:
                findings.append(
                    Finding(
                        check="ordered_override",
                        severity="medium",
                        confidence=_confidence(ev.truncated),
                        title=f"One rule always overrides another on {label}",
                        detail=(
                            f"On one set of answers both rules apply to {label}, one "
                            f"with '{obs.verb_a}' and one with '{obs.verb_b}'. The order "
                            f"is fixed, so rule {obs.winner} always wins and the other "
                            f"never has any visible effect here."
                        ),
                        step_ids=[obs.target_step_id],
                        rule_ids=[obs.rule_a, obs.rule_b],
                        witness=obs.witness.describe(doc),
                        recommended_action="review",
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# Check 4: unassigned_visible_step
# ---------------------------------------------------------------------------


def check_unassigned_visible_step(
    doc: TemplateDoc, evidence: List[ComponentEvidence]
) -> List[Finding]:
    """A visible step with nobody assigned (spec check 4).

    An ``email`` or ``expiring_email`` step has no separate recipient field: its
    "To" line IS the assignee list. So an unassigned email step is a real finding
    and the message says "this email has no recipient", not "nobody is assigned".
    Same check, different words.
    """
    findings: List[Finding] = []
    seen: Dict[str, Tuple[ComponentEvidence, Assignment]] = {}
    for ev in evidence:
        for step_id, witness in ev.unassigned_witness.items():
            seen.setdefault(step_id, (ev, witness))
    for step_id in sorted(seen, key=lambda s: (doc.step(s).position if doc.step(s) else 0)):
        ev, witness = seen[step_id]
        step = doc.step(step_id)
        label = _step_label(doc, step_id)
        if step is not None and step.is_email_step:
            title = f"The email on {label} has no recipient"
            detail = (
                f"{_lead(label)} is an email step, and an email step's "
                f"assignee list IS its 'To' line. Nobody is on it, so this email has "
                f"nowhere to go."
            )
        else:
            title = f"Nobody is assigned to {label}"
            detail = (
                f"{_lead(label)} becomes visible on this path with no assignee, "
                f"no group, no guest, no role, no owner, and no rule that assigns it. "
                f"Somebody has to notice it themselves for it to get done."
            )
        findings.append(
            Finding(
                check="unassigned_visible_step",
                severity="high",
                confidence=_confidence(ev.truncated),
                title=title,
                detail=detail,
                step_ids=[step_id],
                rule_ids=[],
                witness=witness.describe(doc),
                recommended_action="review",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Check 5: no_completable_path
# ---------------------------------------------------------------------------


def check_no_completable_path(
    doc: TemplateDoc, evidence: List[ComponentEvidence]
) -> List[Finding]:
    """A path where nothing can be done (spec check 5)."""
    findings: List[Finding] = []
    seen: Dict[str, Tuple[ComponentEvidence, Assignment]] = {}
    for ev in evidence:
        for step_id, witness in ev.stalled_witness.items():
            seen.setdefault(step_id, (ev, witness))
    for step_id in sorted(seen, key=lambda s: (doc.step(s).position if doc.step(s) else 0)):
        ev, witness = seen[step_id]
        label = _step_label(doc, step_id)
        findings.append(
            Finding(
                check="no_completable_path",
                severity="high",
                confidence=_confidence(ev.truncated),
                title=f"The process gets stuck at {label}",
                detail=(
                    f"On this path {label} is visible with nobody assigned, and the "
                    f"step is set so that only assignees may complete it. Nobody can "
                    f"finish it, so the process cannot end. Completing the other "
                    f"steps in the reverse order does not unblock it either."
                ),
                step_ids=[step_id],
                rule_ids=[],
                witness=witness.describe(doc),
                recommended_action="review",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Check 6: dangling_reference
# ---------------------------------------------------------------------------


def check_dangling_reference(doc: TemplateDoc, raw: Dict[str, Any]) -> List[Finding]:
    """A rule pointing at something deleted (spec check 6). Structural.

    The known completeness limits are PRINTED with the finding rather than buried:
    a soft-deleted then-action appears in no template read at all
    (``Step::doableActionsTarget()`` is ``->withTrashed()``), and
    ``Step::hasDeadlineDependents()`` is tenant-wide with no ``checklist_id``
    scope, so an anchor from another template is invisible here.
    """
    findings: List[Finding] = []
    valid_step_ids = doc.step_ids
    condition_id_sets = {
        CONDITIONABLE_STEP: valid_step_ids,
        # Both sets are ALWAYS authoritative: an empty capture list contributes
        # zero ids whether or not the serializer strips the key, so passing None
        # here would skip the check on every template with no form fields.
        CONDITIONABLE_CAPTURE: doc.capture_ids,
        CONDITIONABLE_PRERUN: doc.prerun_ids,
    }
    raw_rules = raw.get("automated_actions") or []
    raw_by_id = {str(r.get("id")): r for r in raw_rules if isinstance(r, dict)}

    for rule in doc.automations:
        if rule.archived_at:
            continue
        raw_rule = raw_by_id.get(rule.id, {})
        orphaned = orphaned_reference_ids(
            raw_rule.get("conditions") or raw_rule.get("automated_action_conditions") or [],
            raw_rule.get("then_actions")
            or raw_rule.get("actions")
            or raw_rule.get("automated_action_actions")
            or [],
            condition_id_sets,
            valid_step_ids,
        )
        if not orphaned:
            continue
        findings.append(
            Finding(
                check="dangling_reference",
                severity="high",
                confidence=CONFIDENCE_EXHAUSTIVE,
                title=f"Rule {_rule_label(rule)} points at something that is gone",
                detail=(
                    f"Rule {_rule_label(rule)} names "
                    f"{', '.join(sorted(set(orphaned)))}, which no longer exists in this "
                    f"template. Review the rule rather than deleting it: a rule can mix "
                    f"valid and dangling references, so deleting it destroys working "
                    f"logic. Two things this cannot see, so check them by hand if the "
                    f"rule still looks wrong: an action that was soft deleted appears in "
                    f"no template read at all, and a deadline anchored from a DIFFERENT "
                    f"template is out of scope here."
                ),
                step_ids=[],
                rule_ids=[rule.id],
                witness=None,
                recommended_action="review",
            )
        )

    for step in doc.steps:
        anchor = step.deadline_anchor
        if anchor and anchor not in valid_step_ids:
            findings.append(
                Finding(
                    check="dangling_reference",
                    severity="high",
                    confidence=CONFIDENCE_EXHAUSTIVE,
                    title=f"The deadline on {_step_label(doc, step.id)} counts from a step that is gone",
                    detail=(
                        f"{_lead(_step_label(doc, step.id))} has a deadline "
                        f"counted from step {anchor}, which is not in this template. The "
                        f"due date cannot be worked out."
                    ),
                    step_ids=[step.id],
                    rule_ids=[],
                    witness=None,
                    recommended_action="review",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Check 7: orphaned_deadline_anchor
# ---------------------------------------------------------------------------


def check_orphaned_deadline_anchor(
    doc: TemplateDoc, evidence: List[ComponentEvidence], pinned: bool = False
) -> List[Finding]:
    """A deadline anchored to a step hidden on that path (spec check 7).

    ``pinned`` has the same meaning and the same consequence as in
    ``check_unreachable_step``: the walk is exhaustive over a SUBSET of answers, so
    the ``always`` upgrade and its "never happens on any path" wording would be a
    universal claim drawn from a partial view. Withheld on a pinned run.

    ``start_run`` is never an anchor to a step. Both sides of the comparison come
    from the transformed payload, because StepTransformer.php:32 re-emits the
    anchor through ``timelineID()``.
    """
    findings: List[Finding] = []
    totals: Dict[Tuple[str, str], List[int]] = {}
    witnesses: Dict[Tuple[str, str], Tuple[ComponentEvidence, Assignment]] = {}
    for ev in evidence:
        for pair, eligible in ev.anchor_eligible.items():
            bucket = totals.setdefault(pair, [0, 0])
            bucket[0] += eligible
            bucket[1] += ev.anchor_unresolved_count.get(pair, 0)
            if pair in ev.anchor_unresolved:
                witnesses.setdefault(pair, (ev, ev.anchor_unresolved[pair]))

    for pair in sorted(witnesses, key=lambda p: (doc.step(p[0]).position if doc.step(p[0]) else 0)):
        dependent, anchor = pair
        ev, witness = witnesses[pair]
        eligible, unresolved = totals.get(pair, (0, 0))
        # `not pinned` for the reason in the docstring: a pinned walk sees a
        # subset of answers, so it cannot license "on any path".
        always = (
            eligible > 0 and unresolved == eligible and not ev.truncated and not pinned
        )
        severity = "high" if always else "medium"
        b_label = _step_label(doc, dependent)
        a_label = _step_label(doc, anchor)
        if always:
            detail = (
                f"The deadline on {b_label} is counted from {a_label}, and {a_label} "
                f"never happens on any path where {b_label} is visible. The due date "
                f"can never be worked out."
            )
        else:
            detail = (
                f"The deadline on {b_label} is counted from {a_label}. On some paths "
                f"{b_label} is visible while {a_label} is hidden or never completes, so "
                f"on those runs the due date cannot be worked out."
            )
        findings.append(
            Finding(
                check="orphaned_deadline_anchor",
                severity=severity,
                confidence=_confidence(ev.truncated),
                title=f"The deadline on {b_label} counts from a step that may not happen",
                detail=detail,
                step_ids=[dependent, anchor],
                rule_ids=[],
                witness=witness.describe(doc),
                recommended_action="review",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Check 8: stale_condition_value
# ---------------------------------------------------------------------------


def check_stale_condition_value(doc: TemplateDoc) -> List[Finding]:
    """A condition testing an answer the field can no longer give (spec check 8).

    Four does-not-fire rules, and each one inverts the meaning if ignored:
    an open-domain field (text, textarea, email, file) can hold any ``equals``
    value; ``contains`` is a substring test so an exact-match miss proves nothing;
    ``not_equals`` against a missing value is ALWAYS TRUE rather than
    unsatisfiable, which is the opposite finding; and an empty ``options`` list
    means the options were not fetched, not that a value is impossible.
    """
    findings: List[Finding] = []
    for rule in doc.automations:
        if rule.archived_at:
            continue
        for cond in rule.conditions:
            if cond.conditionable_type not in (CONDITIONABLE_CAPTURE, CONDITIONABLE_PRERUN):
                continue
            fdef = doc.field(cond.conditionable_id)
            if fdef is None:
                continue

            if cond.operation in ("greater_than", "less_than"):
                if fdef.field_type in OPTION_FIELD_TYPES or fdef.field_type in (
                    "date",
                    "file",
                    "assignees_form",
                ):
                    findings.append(
                        Finding(
                            check="stale_condition_value",
                            severity="medium",
                            confidence=CONFIDENCE_EXHAUSTIVE,
                            title=f"A rule compares '{fdef.label}' as a number",
                            detail=(
                                f"Rule {_rule_label(rule)} asks whether '{fdef.label}' is "
                                f"{cond.operation.replace('_', ' ')} "
                                f"{cond.statement!r}, but that field is a "
                                f"'{fdef.field_type}' and cannot hold a number, so the "
                                f"comparison is always false."
                            ),
                            step_ids=[fdef.owner_step_id] if fdef.owner_step_id else [],
                            rule_ids=[rule.id],
                            witness=None,
                            recommended_action="review",
                        )
                    )
                continue

            if cond.operation not in ("equals", "equals_any"):
                continue
            if not fdef.has_closed_domain:
                continue
            if not fdef.options:
                continue
            wanted = (
                [str(cond.statement)]
                if cond.operation == "equals" and cond.statement is not None
                else _statement_members(cond.statement)
            )
            available = {o.lower() for o in fdef.options}
            missing = [w for w in wanted if w.lower() not in available]
            if not missing or len(missing) < len(wanted):
                continue
            findings.append(
                Finding(
                    check="stale_condition_value",
                    severity="medium",
                    confidence=CONFIDENCE_EXHAUSTIVE,
                    title=f"A rule waits for an answer '{fdef.label}' no longer offers",
                    detail=(
                        f"Rule {_rule_label(rule)} waits for '{fdef.label}' to be "
                        f"{', '.join(repr(m) for m in missing)}. That is not one of the "
                        f"choices on the field, which offers "
                        f"{', '.join(repr(o) for o in fdef.options)}. Nobody can give the "
                        f"answer this rule is waiting for."
                    ),
                    step_ids=[fdef.owner_step_id] if fdef.owner_step_id else [],
                    rule_ids=[rule.id],
                    witness=None,
                    recommended_action="review",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# always_true_condition: the mirror of check 8, owned by check 2
# ---------------------------------------------------------------------------


def check_always_true_condition(doc: TemplateDoc) -> List[Finding]:
    """A ``not_equals`` against a value the field cannot give, so it is always true.

    OWNERSHIP. The spec assigns this finding to check 2, not to check 8:
    "An unmatched statement makes those always true, not unsatisfiable. That is a
    different finding and belongs in check 2 as ``always_true_condition``.
    Reporting it here inverts the meaning." (tallyfy/mcp#997, check 8, does-not-fire).
    Before this existed each check pointed at the other and nothing reported it
    (tallyfy/work-queue#2364 defect 3).

    It lives beside check 8 in this file because the two share one question, does
    this statement appear in the field's option list, and splitting that across two
    modules would let the two answers drift. Check 2's own ``check_inert_rule`` is
    path-derived and takes ``evidence``; this is structural and takes only the
    document, which is why it is a separate function rather than a branch there.

    ⚠️ ``not_contains`` is deliberately NOT covered, even though the spec names it
    alongside ``not_equals`` in that sentence. Its other does-not-fire rule says a
    substring test "can match a longer option, so an exact-match miss proves
    nothing". That cuts both ways: a statement absent from the option list may
    still be a substring of one, so ``not_contains`` is not provably always true
    and reporting it would be the false positive the spec calls the most damaging
    available.
    """
    findings: List[Finding] = []
    for rule in doc.automations:
        if rule.archived_at:
            continue
        for cond in rule.conditions:
            if cond.conditionable_type not in (CONDITIONABLE_CAPTURE, CONDITIONABLE_PRERUN):
                continue
            if cond.operation != "not_equals":
                continue
            if cond.statement is None:
                continue
            fdef = doc.field(cond.conditionable_id)
            if fdef is None:
                continue
            if not fdef.has_closed_domain:
                continue
            if not fdef.options:
                continue
            if str(cond.statement).lower() in {o.lower() for o in fdef.options}:
                continue
            findings.append(
                Finding(
                    check="always_true_condition",
                    severity="medium",
                    confidence=CONFIDENCE_EXHAUSTIVE,
                    title=f"A rule's test on '{fdef.label}' is always true",
                    detail=(
                        f"Rule {_rule_label(rule)} fires when '{fdef.label}' is not "
                        f"{cond.statement!r}. That is not one of the choices on the "
                        f"field, which offers "
                        f"{', '.join(repr(o) for o in fdef.options)}, so no answer can "
                        f"ever equal it and this test is true whatever the person "
                        f"picks. The rule is not gated by this condition at all."
                    ),
                    step_ids=[fdef.owner_step_id] if fdef.owner_step_id else [],
                    rule_ids=[rule.id],
                    witness=None,
                    recommended_action="review",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Check 9: inert_start_date
# ---------------------------------------------------------------------------


def check_inert_start_date(doc: TemplateDoc) -> List[Finding]:
    """A start date set but ignored because the step is on "Start anytime".

    🔴 The reporting rule is PART of the check. One migration sets ``start_date``
    and ``is_soft_start_date`` together, so this fires on nearly every step of
    nearly every template. Reporting it per step would bury the other eight checks
    in noise and make the whole tool feel broken. So: ONE finding per template,
    ``info`` when the value is the untouched default and ``medium`` when somebody
    deliberately changed it, which is the case worth surfacing and is the
    customer's exact case.
    """
    default_steps: List[str] = []
    changed_steps: List[str] = []
    for step in doc.steps:
        if not step.is_soft_start_date:
            continue
        if not isinstance(step.start_date, dict):
            continue
        try:
            value = int(step.start_date.get("value", 0))
        except (TypeError, ValueError):
            continue
        if value < 1:
            continue
        unit = step.start_date.get("unit")
        if value == DEFAULT_START_DATE["value"] and unit == DEFAULT_START_DATE["unit"]:
            default_steps.append(step.id)
        else:
            changed_steps.append(step.id)

    if not default_steps and not changed_steps:
        return []

    if changed_steps:
        titles = ", ".join(_step_label(doc, s) for s in changed_steps)
        return [
            Finding(
                check="inert_start_date",
                severity="medium",
                confidence=CONFIDENCE_EXHAUSTIVE,
                title="Start dates are set but are being ignored",
                detail=(
                    f"Somebody set a start date on {titles}, and those steps are also "
                    f"set to 'Start anytime', which switches the start date off. The "
                    f"timer does nothing. If the wait is intended, turn 'Start anytime' "
                    f"off on those steps; if it is not, clear the start date so it does "
                    f"not mislead the next person who reads the template."
                    + (
                        f" A further {len(default_steps)} step(s) still carry Tallyfy's "
                        f"own default of 2 hours, which is normal and needs nothing done."
                        if default_steps
                        else ""
                    )
                ),
                step_ids=changed_steps + default_steps,
                rule_ids=[],
                witness=None,
                recommended_action="review",
            )
        ]

    return [
        Finding(
            check="inert_start_date",
            severity="info",
            confidence=CONFIDENCE_EXHAUSTIVE,
            title="Start dates show 2 hours but do nothing",
            detail=(
                f"{len(default_steps)} step(s) show a start date of 2 hours while also "
                f"being set to 'Start anytime', which switches the start date off. This "
                f"is Tallyfy's own default on every new step and nothing is wrong. It is "
                f"reported once so nobody spends time wondering why a two hour wait never "
                f"happens."
            ),
            step_ids=default_steps,
            rule_ids=[],
            witness=None,
            recommended_action="none",
        )
    ]


# ---------------------------------------------------------------------------
# Section 5: the three the code demands
# ---------------------------------------------------------------------------


def check_empty_rule(doc: TemplateDoc) -> List[Finding]:
    findings: List[Finding] = []
    for rule in doc.automations:
        if rule.archived_at or not rule.is_empty_rule:
            continue
        missing = "conditions" if not rule.conditions else "actions"
        findings.append(
            Finding(
                check="empty_rule",
                severity="low",
                confidence=CONFIDENCE_EXHAUSTIVE,
                title=f"Rule {_rule_label(rule)} is incomplete",
                detail=(
                    f"Rule {_rule_label(rule)} has no {missing}, so Tallyfy skips it "
                    f"before it is ever evaluated. It does nothing at all."
                ),
                step_ids=[],
                rule_ids=[rule.id],
                witness=None,
                recommended_action="review",
            )
        )
    return findings


def check_never_evaluated_automation(doc: TemplateDoc) -> List[Finding]:
    """Neither selector in spec 2.6 ever picks this rule up.

    Distinct from ``never_applicable``: the conditions may be perfectly
    satisfiable and are simply never asked.
    """
    findings: List[Finding] = []
    owning_step = {
        cid: step.id for step in doc.steps for cid in step.capture_ids
    }
    for rule in doc.automations:
        if rule.archived_at or rule.is_empty_rule:
            continue
        if rule.has_prerun_condition:
            continue
        reachable = False
        for cond in rule.conditions:
            if cond.conditionable_type == CONDITIONABLE_STEP:
                if cond.conditionable_id in doc.step_ids:
                    reachable = True
                    break
            elif cond.conditionable_type == CONDITIONABLE_CAPTURE:
                if cond.conditionable_id in owning_step:
                    reachable = True
                    break
        if reachable:
            continue
        findings.append(
            Finding(
                check="never_evaluated_automation",
                severity="medium",
                confidence=CONFIDENCE_EXHAUSTIVE,
                title=f"Nothing ever asks rule {_rule_label(rule)}",
                detail=(
                    f"Rule {_rule_label(rule)} has no kick-off condition, and none of "
                    f"its conditions names a step in this template or a form field on "
                    f"one. Tallyfy only checks a rule when a process starts, when the "
                    f"kick-off form is completed, or when a step the rule mentions is "
                    f"completed or reopened, so this rule is never checked at all."
                ),
                step_ids=[],
                rule_ids=[rule.id],
                witness=None,
                recommended_action="review",
            )
        )
    return findings


PLAN_CAVEAT = (
    "Automations only run on a Pro plan or during a trial. On any other plan every "
    "automation in this template is inactive and no step is hidden when a process "
    "starts, so none of the findings above about rules would apply. This tool reads "
    "the template and cannot see which plan the organization is on."
)
