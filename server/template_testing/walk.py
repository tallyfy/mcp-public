"""Independence partitioning, the assignment cap, and one bounded simulation.

Spec sections 3.5, 3.6 and 3.7.

The whole combinatorial control lives here. Components are walked SEPARATELY and
their findings unioned, so total cost is the SUM over components rather than the
product: a template with forty unrelated rules costs about as much as forty
templates with one rule each.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from template_testing.document import AutomationDef, StepDef, TemplateDoc
from template_testing.domains import (
    FieldOutcome,
    StepOutcome,
    Variable,
    build_variables,
    condition_ref,
    fold_conditions,
)

DEFAULT_MAX_ASSIGNMENTS = 50_000

# A reopen action can put a completed step back in play. Automations fire at most
# once per run (spec 2.5), so the walk terminates on its own; this ceiling is a
# belt-and-braces guard so a malformed template cannot spin.
_MAX_PROGRESSION_STEPS = 500


@dataclass
class Application:
    """One action applied during one simulation."""

    automation_id: str
    action_id: str
    action_type: str
    action_verb: str
    target_step_id: str
    changed: bool
    event_index: int
    deadline: Optional[tuple] = None


@dataclass
class SimulationResult:
    shown: Set[str] = field(default_factory=set)
    applicable: Set[str] = field(default_factory=set)
    effective: Set[str] = field(default_factory=set)
    applications: List[Application] = field(default_factory=list)
    unassigned_visible: Set[str] = field(default_factory=set)
    stalled: Set[str] = field(default_factory=set)
    visible_at_end: Set[str] = field(default_factory=set)
    completed: Set[str] = field(default_factory=set)
    never_visible: Set[str] = field(default_factory=set)
    # step_id -> event index at which it was picked; kickoff is -1.
    pick_index: Dict[str, int] = field(default_factory=dict)
    # event index -> the steps that were simultaneously completable then.
    candidates_at: Dict[int, FrozenSet[str]] = field(default_factory=dict)
    # automation id -> the event index at which it fired.
    fired_at: Dict[str, int] = field(default_factory=dict)


@dataclass
class Component:
    index: int
    automation_ids: Tuple[str, ...]
    variables: Tuple[Variable, ...]
    truncated: bool = False
    assignments_walked: int = 0


@dataclass
class Assignment:
    """One complete set of answers. ``field_values`` is keyed by field id."""

    field_values: Dict[str, FieldOutcome]
    step_outcomes: Dict[str, StepOutcome]

    def describe(self, doc: TemplateDoc) -> Dict[str, str]:
        """The witness, in field labels and answers a human would recognise."""
        out: Dict[str, str] = {}
        for field_id, value in self.field_values.items():
            fdef = doc.field(field_id)
            if fdef is None:
                continue
            out[fdef.label] = value.describe(fdef.is_multi_value)
        for step_id, outcome in self.step_outcomes.items():
            step = doc.step(step_id)
            if step is None:
                continue
            out[f"Step {step.position}: {step.title}"] = outcome.describe()
        return out


# ---------------------------------------------------------------------------
# 3.5 Independence partitioning
# ---------------------------------------------------------------------------


def _conditions_by_ref(
    automations: Sequence[AutomationDef],
) -> Dict[Tuple[str, str], List]:
    out: Dict[Tuple[str, str], List] = {}
    for auto in automations:
        for cond in auto.conditions:
            ref = condition_ref(cond)
            if ref is not None:
                out.setdefault(ref, []).append(cond)
    return out


def _automation_refs(auto: AutomationDef) -> Set[Tuple[str, str]]:
    return {r for r in (condition_ref(c) for c in auto.conditions) if r is not None}


def build_components(doc: TemplateDoc) -> List[Component]:
    """Connected components over the automations (spec 3.5).

    Two automations are adjacent when they share a condition variable, when one's
    action target is a step the other names in a condition, or when one's action
    target owns a form field the other names in a condition (a Capture condition
    fires on its owning step's completion, so ownership is a real edge, spec 2.6).

    ⚠️ One edge here is NOT in the spec and is deliberate: two automations that
    apply a VISIBILITY action to the SAME target step are also adjacent. Without
    it a step's ``show`` rules can land in different components, and since a
    component walk evaluates only its own automations, one component would report
    the step as never shown while another shows it. Check 1 is a universal claim,
    so that would be a false "can never appear" - the exact failure spec 3.6 says
    would cost this tool its credibility. The edge only ever merges components, so
    it cannot create a finding that the spec's edge set would not also allow.
    """
    live = [a for a in doc.automations if not a.archived_at]
    index = {a.id: i for i, a in enumerate(live)}
    parent = list(range(len(live)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    refs = {a.id: _automation_refs(a) for a in live}
    targets = {a.id: {act.target_step_id for act in a.actions if act.target_step_id} for a in live}
    vis_targets = {
        a.id: {
            act.target_step_id
            for act in a.actions
            if act.target_step_id and act.action_type == "visibility"
        }
        for a in live
    }

    for i, left in enumerate(live):
        for right in live[i + 1 :]:
            adjacent = bool(refs[left.id] & refs[right.id])
            if not adjacent:
                for tgt in targets[left.id]:
                    if ("step", tgt) in refs[right.id]:
                        adjacent = True
                        break
                    owner_fields = doc.step(tgt).capture_ids if doc.step(tgt) else ()
                    if any(("field", cid) in refs[right.id] for cid in owner_fields):
                        adjacent = True
                        break
            if not adjacent:
                for tgt in targets[right.id]:
                    if ("step", tgt) in refs[left.id]:
                        adjacent = True
                        break
                    owner_fields = doc.step(tgt).capture_ids if doc.step(tgt) else ()
                    if any(("field", cid) in refs[left.id] for cid in owner_fields):
                        adjacent = True
                        break
            if not adjacent and (vis_targets[left.id] & vis_targets[right.id]):
                adjacent = True
            if adjacent:
                union(index[left.id], index[right.id])

    grouped: Dict[int, List[AutomationDef]] = {}
    for auto in live:
        grouped.setdefault(find(index[auto.id]), []).append(auto)

    components: List[Component] = []
    for i, (_, members) in enumerate(sorted(grouped.items())):
        members = sorted(members, key=lambda a: a.position)
        variables = build_variables(doc, _conditions_by_ref(members))
        components.append(
            Component(
                index=i,
                automation_ids=tuple(a.id for a in members),
                variables=tuple(variables[k] for k in sorted(variables, key=lambda k: (k[0], k[1]))),
            )
        )
    return components


# ---------------------------------------------------------------------------
# 3.6 The cap, and the honesty requirement
# ---------------------------------------------------------------------------


def domain_product(variables: Sequence[Variable]) -> int:
    total = 1
    for var in variables:
        total *= max(1, len(var.domain))
    return total


def enumerate_assignments(
    variables: Sequence[Variable], max_assignments: int
) -> Tuple[List[Tuple[Any, ...]], bool]:
    """Exhaustive product when it fits, a 2-wise covering array when it does not.

    Returns ``(rows, truncated)``. A truncated component may never emit a "never"
    claim; that rule is enforced where findings are built, not here.
    """
    if not variables:
        return [()], False
    if domain_product(variables) <= max_assignments:
        return list(itertools.product(*[v.domain for v in variables])), False
    return _pairwise_covering_array(variables, max_assignments), True


def _pairwise_covering_array(
    variables: Sequence[Variable], max_assignments: int
) -> List[Tuple[Any, ...]]:
    """A deterministic greedy 2-wise covering array.

    Greedy rather than optimal on purpose: the engine must be a pure function of
    its input, so nothing here may consult a clock or a random source. Ties break
    on domain index, which makes two runs over the same template byte-identical.
    """
    n = len(variables)
    domains = [list(v.domain) for v in variables]
    if n == 1:
        return [(value,) for value in domains[0][:max_assignments]]

    uncovered: Set[Tuple[int, int, int, int]] = set()
    for i in range(n):
        for j in range(i + 1, n):
            for a in range(len(domains[i])):
                for b in range(len(domains[j])):
                    uncovered.add((i, j, a, b))

    rows: List[Tuple[Any, ...]] = []
    while uncovered and len(rows) < max_assignments:
        picked: List[Optional[int]] = [None] * n
        # Seed on the pair that is still missing and lowest in canonical order.
        i, j, a, b = min(uncovered)
        picked[i], picked[j] = a, b
        for k in range(n):
            if picked[k] is not None:
                continue
            best_index, best_gain = 0, -1
            for cand in range(len(domains[k])):
                gain = 0
                for other in range(n):
                    if other == k or picked[other] is None:
                        continue
                    lo, hi = (other, k) if other < k else (k, other)
                    va, vb = (picked[other], cand) if other < k else (cand, picked[other])
                    if (lo, hi, va, vb) in uncovered:
                        gain += 1
                if gain > best_gain:
                    best_index, best_gain = cand, gain
            picked[k] = best_index
        for x in range(n):
            for y in range(x + 1, n):
                uncovered.discard((x, y, picked[x], picked[y]))
        rows.append(tuple(domains[k][picked[k]] for k in range(n)))
    return rows


def assignment_from_row(
    variables: Sequence[Variable], row: Sequence[Any]
) -> Assignment:
    field_values: Dict[str, FieldOutcome] = {}
    step_outcomes: Dict[str, StepOutcome] = {}
    for var, value in zip(variables, row):
        if var.kind == "field":
            field_values[var.ref_id] = value
        else:
            step_outcomes[var.ref_id] = value
    return Assignment(field_values=field_values, step_outcomes=step_outcomes)


# ---------------------------------------------------------------------------
# 3.7 Simulating one assignment
# ---------------------------------------------------------------------------


def _dependent_automations(
    doc: TemplateDoc, automations: Sequence[AutomationDef], step: StepDef
) -> List[AutomationDef]:
    """AutomatedAction::whereActionRulesDependentOnStep, AutomatedAction.php:173-192.

    On ``task.completed`` and ``task.reopen`` only automations whose conditions
    reference that step, or a form field BELONGING to that step, are evaluated.

    ⚠️ A form field's value changing does not trigger anything. A Capture
    condition is evaluated when the step that OWNS that field is completed or
    reopened (spec 2.6). This is not obvious from the data model and it changes
    which paths exist.
    """
    owned = set(step.capture_ids)
    out = []
    for auto in automations:
        for cond in auto.conditions:
            ref = condition_ref(cond)
            if ref == ("step", step.id) or (ref and ref[0] == "field" and ref[1] in owned):
                out.append(auto)
                break
    return sorted(out, key=lambda a: a.position)


def _completable(step: StepDef, has_assignee: bool) -> bool:
    """Nobody can complete a step with no assignee when only assignees may.

    An unassigned step whose ``can_complete_only_assignees`` is false is untidy
    (check 4) and not stuck (check 5): any member may complete it.
    """
    return has_assignee or not step.can_complete_only_assignees


def simulate(
    doc: TemplateDoc,
    automations: Sequence[AutomationDef],
    assignment: Assignment,
    reverse_order: bool = False,
) -> SimulationResult:
    """One bounded event simulation. Mirrors api-v2; see spec 3.7 step by step."""
    result = SimulationResult()
    steps = list(doc.steps)
    by_id = {s.id: s for s in steps}

    # 1. Pre-pass. Hidden at launch is DERIVED from the whole template's show
    #    actions, not from the component being walked, because that is what the
    #    observer does at run.created (spec 2.4).
    hidden = set(doc.hidden_at_launch())
    visible = {s.id for s in steps if s.id not in hidden}
    completed: Dict[str, StepOutcome] = {}
    has_assignee = {s.id: s.has_static_assignee() for s in steps}
    executed: Set[str] = set()
    result.shown = {s.id for s in steps if s.id in visible}

    def apply_actions(auto: AutomationDef, event_index: int) -> None:
        changed_any = False
        for action in sorted(auto.actions, key=lambda a: a.position):
            target = action.target_step_id
            step = by_id.get(target)
            changed = False
            if step is None:
                # A dangling target. Check 6 reports it; nothing changes here.
                pass
            elif action.action_type == "visibility":
                # BaseAction::applyVisibilityActionOnTarget, BaseAction.php:80-99:
                # show on an already-visible target returns FALSE, hide on an
                # already-hidden target returns FALSE, and ANY visibility action
                # on a COMPLETED target returns false. A returned false means no
                # state change and no onSuccess callback.
                if target in completed:
                    changed = False
                elif action.action_verb == "show" and target not in visible:
                    visible.add(target)
                    result.shown.add(target)
                    changed = True
                elif action.action_verb == "hide" and target in visible:
                    visible.discard(target)
                    changed = True
            elif action.action_type == "assignment":
                if action.action_verb in ("assign", "assign_only"):
                    if not has_assignee[target]:
                        has_assignee[target] = True
                        changed = True
                    elif action.action_verb == "assign_only":
                        changed = True
                elif action.action_verb in ("clear_assignees", "unassign"):
                    if has_assignee[target]:
                        has_assignee[target] = False
                        changed = True
            elif action.action_type == "deadline":
                changed = True
            elif action.action_type == "status" and action.action_verb == "reopen":
                if target in completed:
                    completed.pop(target, None)
                    visible.add(target)
                    changed = True
            elif action.is_webhook:
                # An emit_webhook has an external side effect this engine cannot
                # see, so it is never `no_effect` (spec check 2).
                changed = True
            result.applications.append(
                Application(
                    automation_id=auto.id,
                    action_id=action.id,
                    action_type=action.action_type,
                    action_verb=action.action_verb,
                    target_step_id=target,
                    changed=changed,
                    event_index=event_index,
                    deadline=action.deadline,
                )
            )
            changed_any = changed_any or changed
        executed.add(auto.id)
        result.fired_at.setdefault(auto.id, event_index)
        if changed_any:
            result.effective.add(auto.id)

    def evaluate(auto: AutomationDef, event_index: int) -> None:
        # Automations fire at most once per run: both observers skip one that has
        # already executed (ApplyRulesWithPrerunConditions.php:50-52,
        # ApplyRules.php:42-45). That is what keeps a single path cheap.
        if auto.id in executed:
            return
        step_view: Dict[str, Optional[StepOutcome]] = {
            sid: completed.get(sid) for sid in by_id
        }
        if not fold_conditions(auto.conditions, assignment.field_values, step_view):
            return
        result.applicable.add(auto.id)
        apply_actions(auto, event_index)

    # 2. Kick-off. Only automations with at least one Prerun condition, once, in
    #    position order (spec 2.6 selector 1).
    for auto in sorted(automations, key=lambda a: a.position):
        if auto.has_prerun_condition:
            evaluate(auto, -1)

    # 3. Progression.
    event_index = 0
    for _ in range(_MAX_PROGRESSION_STEPS):
        candidates = [
            s
            for s in steps
            if s.id in visible and s.id not in completed and _completable(s, has_assignee[s.id])
        ]
        if not candidates:
            break
        candidates.sort(key=lambda s: s.position, reverse=reverse_order)
        result.candidates_at[event_index] = frozenset(c.id for c in candidates)
        picked = candidates[0]
        if not has_assignee[picked.id]:
            result.unassigned_visible.add(picked.id)
        completed[picked.id] = assignment.step_outcomes.get(
            picked.id, StepOutcome(ops=frozenset())
        )
        result.pick_index[picked.id] = event_index
        for auto in _dependent_automations(doc, automations, picked):
            evaluate(auto, event_index)
        event_index += 1

    # 4. Termination.
    for step in steps:
        if step.id in visible and step.id not in completed:
            result.visible_at_end.add(step.id)
            if not has_assignee[step.id]:
                result.unassigned_visible.add(step.id)
            if not _completable(step, has_assignee[step.id]):
                result.stalled.add(step.id)
    result.completed = set(completed)
    result.never_visible = {s.id for s in steps if s.id not in result.shown}
    return result
