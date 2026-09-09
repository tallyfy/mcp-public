"""Variable domains, and the condition semantics the walk evaluates.

Spec sections 2.2, 2.3, 3.2, 3.3 and 3.4.

The domain of a field is built from **only the statements its own conditions
name**, plus sentinels. A twenty-option dropdown tested against one value has two
cases, not twenty. That, plus the independence partitioning in ``walk.py``, is
what keeps this bounded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

from template_testing.document import (
    CONDITIONABLE_CAPTURE,
    CONDITIONABLE_PRERUN,
    CONDITIONABLE_STEP,
    STEP_ANY_TIME,
    ConditionDef,
    FieldDef,
    TemplateDoc,
)

# A value that equals no statement, contains no needle, is non-empty and is not
# numeric: "anything else the user might type". This is what makes a domain a
# sound abstraction of an infinite input space rather than a guess (spec 3.3
# rule 7). It is built to be un-collidable and is checked against the real
# statements in ``_other_sentinel``.
_OTHER_BASE = "~~tallyfy-other~~"
# Characters the sentinel may be built from, ordered so the result is stable
# across runs (the engine must be a pure function, so nothing here may consult a
# clock or a random source).
_SENTINEL_ALPHABET = "~^`|@#%&*_=+zqxjkv0987654321"
OTHER_LABEL = "(any other answer)"
EMPTY_LABEL = "(left blank)"

# The time dimension on a `completed` step condition
# (StepCondition::timeClauseFulfilled, StepCondition.php:87-110).
OTHER_TIME = "~~other-time~~"


@dataclass(frozen=True)
class FieldOutcome:
    """One assignment's answer to one field.

    ``selected`` is a tuple so multi-value fields (MultiValueCapture /
    MultiDimensionsCapture, whose ``equals`` is ``in_array`` over the selected
    set) and scalar fields share one representation.
    """

    selected: Tuple[str, ...]
    is_other: bool = False

    @property
    def scalar(self) -> str:
        return self.selected[0] if self.selected else ""

    def describe(self, multi: bool) -> str:
        if self.is_other:
            return OTHER_LABEL
        if not self.selected or (len(self.selected) == 1 and self.selected[0] == ""):
            return EMPTY_LABEL
        return ", ".join(self.selected) if multi else self.scalar


@dataclass(frozen=True)
class StepOutcome:
    """One assignment's answer to "what happened to this step".

    ``ops`` is a frozenset rather than a single operation. Spec 3.4 defines the
    domain as the tested operations plus ``NONE_OF_THESE``, i.e. one operation at
    a time. This engine walks that set AND, when two or more compatible
    operations are tested on one step, the combination in which all of them hold.

    That is a deliberate, recorded SUPERSET of the spec's domain, for one reason:
    checks 1 and 2 are universal claims, and a domain that can never satisfy
    ``completed AND approved`` would report a step reachable only through that
    conjunction as "can never appear". The extra member costs one assignment per
    step variable and can only remove false universals, never add them.
    """

    ops: FrozenSet[str]
    time_clause: Optional[str] = None

    def describe(self) -> str:
        if not self.ops:
            return "(none of the tested outcomes)"
        body = " and ".join(sorted(self.ops))
        if self.time_clause and self.time_clause != OTHER_TIME:
            return f"{body} ({self.time_clause})"
        if self.time_clause == OTHER_TIME:
            return f"{body} (at some other time)"
        return body


# Operations that cannot hold at once, so the "all of them" combination above is
# not generated for them.
_MUTUALLY_EXCLUSIVE = (
    frozenset({"approved", "rejected"}),
    frozenset({"completed", "expired"}),
)


@dataclass(frozen=True)
class Variable:
    """A free input of the machine. ``kind`` is "field" or "step"."""

    kind: str
    ref_id: str
    label: str
    domain: Tuple[Any, ...]
    sampled: bool = False

    @property
    def key(self) -> Tuple[str, str]:
        return (self.kind, self.ref_id)


def _other_sentinel(avoid: Sequence[str]) -> str:
    """A value matching none of ``avoid`` by equality or by substring.

    ⚠️ Built from a character NOTHING to avoid contains, never by extending a
    fixed base. The obvious loop, "append a character until nothing matches",
    does not terminate: a one-character needle that already appears in the base
    is still a substring however much is appended, and ``_OTHER_BASE`` contains
    most of the alphabet. Measured while writing this: a template whose
    ``contains`` needles were ``a b c d`` hung the walk.

    When every candidate character is used, no non-empty value can avoid every
    needle, which is a real property of the input rather than a bug. The longest
    unused-character attempt is returned; the domain is then an approximation and
    the OTHER case may satisfy a ``contains`` it was meant to miss, which can
    only make a universal claim MORE conservative.
    """
    lowered = [a.lower() for a in avoid if a]
    used = set("".join(lowered))
    for char in _SENTINEL_ALPHABET:
        if char in used:
            continue
        candidate = char * 12
        if candidate not in lowered:
            return candidate
    # Every character in the alphabet appears in something we must avoid.
    fallback = _OTHER_BASE
    while fallback.lower() in lowered:
        fallback += _SENTINEL_ALPHABET[0]
    return fallback


def _statement_members(statement: Any) -> List[str]:
    """The values an ``equals_any`` statement names.

    api-v2 sends a list; hand-written rules and some older payloads send a
    comma-separated string. Both are read, because guessing wrong turns a live
    condition into an unsatisfiable one and check 2 then reports a working rule
    as dead.
    """
    if isinstance(statement, (list, tuple, set)):
        return [str(s) for s in statement if s is not None]
    if isinstance(statement, str) and statement:
        return [part.strip() for part in statement.split(",") if part.strip()]
    return []


def _numeric(value: Any) -> Optional[float]:
    """``greater_than``/``less_than`` return FALSE on non-numeric content."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def build_field_domain(fdef: FieldDef, conditions: Sequence[ConditionDef]) -> Variable:
    """Spec 3.3, construction rules 1 to 7 in order."""
    equals_values: List[str] = []
    needles: List[str] = []
    thresholds: List[float] = []
    wants_empty = False

    for c in conditions:
        op = c.operation
        if op in ("equals", "not_equals"):
            if c.statement is not None:
                equals_values.append(str(c.statement))
        elif op == "equals_any":
            equals_values.extend(_statement_members(c.statement))
        elif op in ("contains", "not_contains"):
            needle = str(c.statement) if c.statement is not None else ""
            if needle:
                needles.append(needle)
        elif op in ("greater_than", "less_than"):
            num = _numeric(c.statement)
            if num is not None:
                thresholds.append(num)
        elif op in ("is_empty", "is_not_empty"):
            wants_empty = True

    # Case folding: equals / not_equals / equals_any all strtolower both sides,
    # so two statements differing only in case are ONE domain member. Deduplicate
    # case-insensitively or the walk doubles for no reason (spec 3.3).
    seen_lower = set()
    base_values: List[str] = []
    for v in equals_values:
        low = v.lower()
        if low not in seen_lower:
            seen_lower.add(low)
            base_values.append(v)

    sampled = False
    contains_values: List[str] = []
    distinct_needles = []
    for n in needles:
        if n.lower() not in {d.lower() for d in distinct_needles}:
            distinct_needles.append(n)
    if distinct_needles:
        # One value carrying every needle, plus each singleton. The "contains
        # none" case is the OTHER sentinel added below, so it is not duplicated.
        if len(distinct_needles) > 1:
            contains_values.append(" ".join(distinct_needles))
        contains_values.extend(distinct_needles)
        if len(distinct_needles) > 3:
            sampled = True

    numeric_values: List[str] = []
    if thresholds:
        ordered = sorted(set(thresholds))
        numeric_values.append(_fmt_number(ordered[0] - 1))
        for lo, hi in zip(ordered, ordered[1:]):
            mid = (lo + hi) / 2.0
            if lo < mid < hi:
                numeric_values.append(_fmt_number(mid))
        numeric_values.append(_fmt_number(ordered[-1] + 1))
        # The comparisons are strict, so each boundary is its own case.
        numeric_values.extend(_fmt_number(t) for t in ordered)

    # 🔴 A CLOSED-DOMAIN FIELD CANNOT ANSWER A VALUE IT DOES NOT OFFER, and this
    # is what makes check 1 work on the customer's template.
    #
    # Spec 3.3 builds the domain from the statements the conditions NAME, which is
    # right for an open field. On a radio, dropdown or multiselect it is not: an
    # `equals` against a value that is no longer one of the options is
    # UNSATISFIABLE, which spec check 2 states in as many words. Leaving that value
    # in the domain manufactures a path nobody can take, the rule fires on it, its
    # show action runs, and a step that can never appear is reported as reachable.
    # That is the exact defect this whole engine exists to catch, so it would fail
    # in the one direction that matters.
    #
    # Guarded on `options` being non-empty: an empty list means the options were
    # not fetched, not that every value is impossible (spec check 8).
    if fdef.has_closed_domain and fdef.options:
        offered = {o.lower() for o in fdef.options}
        base_values = [v for v in base_values if v.lower() in offered]

    values: List[str] = []
    for v in base_values + contains_values + numeric_values:
        if v.lower() not in {x.lower() for x in values}:
            values.append(v)
    if wants_empty:
        values.append("")

    other = _other_sentinel(values + distinct_needles)

    domain: List[FieldOutcome] = []
    for v in values:
        domain.append(FieldOutcome(selected=(v,)))
    # Rule 7: always one OTHER sentinel. It doubles as the "non-numeric content"
    # case rule 5 asks for, because it is not parseable as a number.
    domain.append(FieldOutcome(selected=(other,), is_other=True))

    if fdef.is_multi_value and len(values) > 1:
        # MultiValueCapture compares with in_array over the selected set, so the
        # "everything named is selected" case is a distinct, reachable outcome
        # that no singleton covers.
        domain.append(FieldOutcome(selected=tuple(values)))

    return Variable(
        kind="field",
        ref_id=fdef.id,
        label=fdef.label,
        domain=tuple(domain),
        sampled=sampled,
    )


def _fmt_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def build_step_domain(step_id: str, title: str, conditions: Sequence[ConditionDef]) -> Variable:
    """Spec 3.4, with the documented superset noted on ``StepOutcome``."""
    tested_ops: List[str] = []
    time_clauses: List[str] = []
    for c in conditions:
        op = c.operation
        if op and op not in tested_ops:
            tested_ops.append(op)
        if op == "completed":
            stmt = c.statement if c.statement not in ("",) else None
            if stmt not in STEP_ANY_TIME and stmt is not None:
                clause = str(stmt)
                if clause not in time_clauses:
                    time_clauses.append(clause)

    outcomes: List[StepOutcome] = []
    time_dimension: List[Optional[str]] = [None]
    if time_clauses:
        time_dimension = list(time_clauses) + [OTHER_TIME]

    def _add(ops: FrozenSet[str]) -> None:
        if "completed" in ops and time_dimension != [None]:
            for tc in time_dimension:
                candidate = StepOutcome(ops=ops, time_clause=tc)
                if candidate not in outcomes:
                    outcomes.append(candidate)
        else:
            candidate = StepOutcome(ops=ops, time_clause=None)
            if candidate not in outcomes:
                outcomes.append(candidate)

    for op in tested_ops:
        _add(frozenset({op}))

    if len(tested_ops) >= 2:
        combined = frozenset(tested_ops)
        if not any(pair <= combined for pair in _MUTUALLY_EXCLUSIVE):
            _add(combined)

    # NONE_OF_THESE: the step happened, and none of the tested outcomes hold.
    outcomes.append(StepOutcome(ops=frozenset()))

    return Variable(
        kind="step",
        ref_id=step_id,
        label=title,
        domain=tuple(outcomes),
    )


def build_variables(
    doc: TemplateDoc, conditions_by_ref: Dict[Tuple[str, str], List[ConditionDef]]
) -> Dict[Tuple[str, str], Variable]:
    """One variable per reference actually named by a condition (spec 3.2)."""
    variables: Dict[Tuple[str, str], Variable] = {}
    for (kind, ref_id), conds in conditions_by_ref.items():
        if kind == "field":
            fdef = doc.field(ref_id)
            if fdef is None:
                # A dangling Capture/Prerun reference. Check 6 reports it; it
                # contributes no variable because there is no field to answer.
                continue
            variables[(kind, ref_id)] = build_field_domain(fdef, conds)
        else:
            step = doc.step(ref_id)
            if step is None:
                continue
            variables[(kind, ref_id)] = build_step_domain(ref_id, step.title, conds)
    return variables


def condition_ref(condition: ConditionDef) -> Optional[Tuple[str, str]]:
    """The variable a condition reads, or None when it names nothing usable."""
    if not condition.conditionable_id:
        return None
    if condition.conditionable_type == CONDITIONABLE_STEP:
        return ("step", condition.conditionable_id)
    if condition.conditionable_type in (CONDITIONABLE_CAPTURE, CONDITIONABLE_PRERUN):
        return ("field", condition.conditionable_id)
    return None


# ---------------------------------------------------------------------------
# Evaluation. Mirrors app/Checklist/Checklist/Rules/ exactly.
# ---------------------------------------------------------------------------


def evaluate_field_condition(condition: ConditionDef, outcome: FieldOutcome) -> bool:
    """SimpleValueCapture / MultiValueCapture semantics, spec 2.2.

    ``CaptureCondition::isMet()`` is ``method_exists($capture, $operation)`` then
    a dynamic call, so an operation the capture type does not implement returns
    FALSE silently. An unknown operation therefore answers False here too.
    """
    op = condition.operation
    values = [v for v in outcome.selected]
    lowered = [v.lower() for v in values]
    scalar = outcome.scalar

    if op == "equals":
        return str(condition.statement or "").lower() in lowered
    if op == "not_equals":
        return str(condition.statement or "").lower() not in lowered
    if op == "equals_any":
        members = {m.lower() for m in _statement_members(condition.statement)}
        return any(v in members for v in lowered)
    if op == "contains":
        needle = str(condition.statement or "").lower()
        # stripos with an empty needle matches; api-v2 does the same.
        return any(needle in v for v in lowered)
    if op == "not_contains":
        needle = str(condition.statement or "").lower()
        return not any(needle in v for v in lowered)
    if op == "greater_than":
        left, right = _numeric(scalar), _numeric(condition.statement)
        return left is not None and right is not None and left > right
    if op == "less_than":
        left, right = _numeric(scalar), _numeric(condition.statement)
        return left is not None and right is not None and left < right
    if op == "is_empty":
        return scalar.strip() == "" and len(values) <= 1
    if op == "is_not_empty":
        return not (scalar.strip() == "" and len(values) <= 1)
    return False


def evaluate_step_condition(condition: ConditionDef, outcome: Optional[StepOutcome]) -> bool:
    """StepCondition::isMet() plus timeClauseFulfilled(), spec 2.2.

    ``outcome`` is None when the step has not been reached on this path, in which
    case nothing has happened to it and every operation is false.
    """
    if outcome is None:
        return False
    op = condition.operation
    if op not in outcome.ops:
        return False
    if op != "completed":
        return True

    # For `completed` only, statement is a TIME CLAUSE, not a value. A null,
    # empty or "any_time" statement is SATISFIABLE (mcp#571, api-v2#9636).
    stmt = condition.statement
    if stmt in STEP_ANY_TIME:
        return True
    return outcome.time_clause == str(stmt)


def evaluate_condition(
    condition: ConditionDef,
    field_values: Dict[str, FieldOutcome],
    step_outcomes: Dict[str, Optional[StepOutcome]],
) -> bool:
    ref = condition_ref(condition)
    if ref is None:
        return False
    kind, ref_id = ref
    if kind == "step":
        return evaluate_step_condition(condition, step_outcomes.get(ref_id))
    value = field_values.get(ref_id)
    if value is None:
        # A condition on a field this walk holds no answer for. False rather than
        # a guess: a fabricated answer would make an existential finding claim a
        # path that was never tested.
        return False
    return evaluate_field_condition(condition, value)


def fold_conditions(
    conditions: Sequence[ConditionDef],
    field_values: Dict[str, FieldOutcome],
    step_outcomes: Dict[str, Optional[StepOutcome]],
) -> bool:
    """BaseRule::isApplicable(), BaseRule.php:45-62. Spec 2.3.

    Three properties, all load-bearing and none of them what anyone assumes:

    1. It is a STRICT LEFT FOLD in ``position`` order. ``A or B and C`` evaluates
       as ``(A or B) and C``, not ``A or (B and C)``. An engine that parses the
       conditions into a boolean AST with normal precedence disagrees with
       production on exactly the templates that are hardest to reason about by
       hand.
    2. The FIRST condition's own ``logic`` is never read. ``shift()`` takes it and
       the loop reads the operator off the nth condition, so setting the first
       condition to ``or`` does nothing.
    3. An unrecognised operator is a NO-OP, so the accumulator carries forward
       unchanged and that condition is evaluated and then discarded (mcp#680).
    """
    ordered = sorted(conditions, key=lambda c: c.position)
    if not ordered:
        return False
    result = evaluate_condition(ordered[0], field_values, step_outcomes)
    for cond in ordered[1:]:
        is_met = evaluate_condition(cond, field_values, step_outcomes)
        if cond.logic == "and":
            result = result and is_met
        elif cond.logic == "or":
            result = result or is_met
        # else: no-op, accumulator carries forward unchanged.
    return result
