"""Normalise one raw template document into the domain model the walk uses.

Every fact encoded here was read from api-v2 source and is cited in
tallyfy/mcp#997 section 2. The citations are kept in the code because the whole
value of this engine is that it agrees with production; a divergence here makes
the tool wrong in a way nobody can debug.

The input is the payload of
``GET /organizations/{org}/checklists/{id}?with=steps,automated_actions,prerun``.

⚠️ It must be the RAW payload, not ``serialize_dataclass(sdk_template)``. On the
pinned SDK (``tallyfy==1.3.12``) ``Step.from_dict`` maps a fixed key list and
drops everything else, so ``assign_run_starter``, ``owner_id`` and ``ai_assigned``
never survive it. Feeding this engine an SDK-serialised step therefore reports
every run-starter-assigned step as unassigned, which spec 2.9 names as the
largest single false-positive source in check 4. ``assert_is_template_document``
below is the guard; the tool fetches raw.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

# ---------------------------------------------------------------------------
# Vocabulary, mirrored from api-v2. See spec 2.1 and 2.2.
# ---------------------------------------------------------------------------

# conditionable_type is stored as an FQCN (`Tallyfy\API\V1\Models\Step`,
# AutomatedAction.php:97) and transformed to the short name on the way out
# (AutomatedActionTransformer.php:34, `Str::afterLast(..., '\\')`). The runtime
# engine switches on the FQCN (BaseRule.php:107-117). Accept both spellings and
# never assume one (spec 2.1).
CONDITIONABLE_STEP = "Step"
CONDITIONABLE_CAPTURE = "Capture"
CONDITIONABLE_PRERUN = "Prerun"
CONDITIONABLE_TYPES = (CONDITIONABLE_STEP, CONDITIONABLE_CAPTURE, CONDITIONABLE_PRERUN)

# StepCondition::isMet(), StepCondition.php:57-77.
STEP_OPERATIONS = frozenset(
    {"completed", "reopened", "approved", "rejected", "acknowledged", "expired", "not_assigned"}
)

# StepCondition::timeClauseFulfilled(), StepCondition.php:87-110.
# A null / empty / "any_time" statement is SATISFIABLE. It used to fall through
# to false, which made every API-created step condition permanently dead
# (mcp#571, api-v2#9636). That is repaired. An engine that still treats a null
# statement as unsatisfiable reports working automations as inert, and spec 2.2
# names this the most tempting wrong assumption in the file.
STEP_TIME_CLAUSES = frozenset({"on-time", "early_24", "late_24"})
STEP_ANY_TIME = frozenset({None, "", "any_time"})

# SimpleValueCapture / MultiValueCapture, app/Checklist/Checklist/Rules/CaptureTypes/.
FIELD_OPERATIONS = frozenset(
    {
        "contains",
        "not_contains",
        "equals",
        "not_equals",
        "equals_any",
        "greater_than",
        "less_than",
        "is_empty",
        "is_not_empty",
    }
)

# DoableActionValidator::acceptedActionVerbs(), mirrored at automation.py:171-195.
# There is no `show` action_type and no `assign` action_type: "show step 4" is
# action_type=visibility, action_verb=show (spec 2.1).
ACTION_VERBS_BY_TYPE = {
    "visibility": frozenset({"show", "hide"}),
    "deadline": frozenset({"deadline"}),
    "status": frozenset({"reopen"}),
    "assignment": frozenset({"assign", "assign_only", "clear_assignees", "unassign"}),
    "webhook": frozenset({"emit_webhook"}),
}

# Deadline::START_RUN, app/Step/Deadline.php:18. Not a step id (spec 2.8).
DEADLINE_ANCHOR_START_RUN = "start_run"

# The database default set by one migration
# (2022_02_24_125353_change_default_value_of_start_date_in_steps_table.php:17-18):
# every step is born "2 hours" AND "Start anytime" at once, so the timer is inert
# by construction. Check 9 exists because of this (spec 2.8, check 9).
DEFAULT_START_DATE = {"unit": "hours", "value": 2}

# Option-bearing field types, the only ones whose `equals` domain is closed
# (check 8).
OPTION_FIELD_TYPES = frozenset({"radio", "dropdown", "multiselect"})
# Fields whose selection is a set rather than a scalar: MultiValueCapture /
# MultiDimensionsCapture compare with in_array, not a scalar comparison (spec 3.3).
MULTI_VALUE_FIELD_TYPES = frozenset({"multiselect", "table", "assignees_form"})
# An unassigned step of these types has no recipient rather than no assignee.
# Same check, different words (check 4).
EMAIL_STEP_TYPES = frozenset({"email", "expiring_email"})


def short_conditionable_type(value: Any) -> str:
    """Return the short class name for a conditionable_type in either spelling.

    ``Tallyfy\\API\\V1\\Models\\Step`` and ``Step`` both answer ``Step``.
    """
    if not isinstance(value, str) or not value:
        return CONDITIONABLE_STEP
    return value.rsplit("\\", 1)[-1].strip() or CONDITIONABLE_STEP


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldDef:
    """A kick-off field (Prerun) or a step form field (Capture)."""

    id: str
    label: str
    field_type: str
    kind: str  # "prerun" | "capture"
    options: tuple = ()
    owner_step_id: Optional[str] = None  # None for kick-off fields

    @property
    def is_multi_value(self) -> bool:
        return self.field_type in MULTI_VALUE_FIELD_TYPES

    @property
    def has_closed_domain(self) -> bool:
        return self.field_type in OPTION_FIELD_TYPES


@dataclass
class StepDef:
    id: str
    title: str
    position: int
    step_type: str = ""
    deadline: Optional[Dict[str, Any]] = None
    start_date: Optional[Dict[str, Any]] = None
    is_soft_start_date: bool = False
    assignees: tuple = ()
    groups: tuple = ()
    guests: tuple = ()
    roles: tuple = ()
    assign_run_starter: bool = False
    owner_id: Optional[Any] = None
    ai_assigned: Optional[Any] = None
    can_complete_only_assignees: bool = False
    allow_guest_owners: bool = False
    capture_ids: tuple = ()

    @property
    def is_email_step(self) -> bool:
        return (self.step_type or "").lower() in EMAIL_STEP_TYPES

    def has_static_assignee(self) -> bool:
        """The check-4 predicate, minus the path-derived assignment actions.

        Spec check 4: "no possible assignee" requires ALL of assignees, groups,
        guests, roles empty, ``assign_run_starter`` not true, ``owner_id`` unset
        and ``ai_assigned`` unset.

        ``allow_guest_owners`` is deliberately NOT consulted. It defaults TRUE in
        the database and renders a Guest slot in the UI, but a slot is not an
        assignee; treating it as one would suppress the finding on nearly every
        template, and treating its absence as evidence would be equally wrong.
        """
        return bool(
            self.assignees
            or self.groups
            or self.guests
            or self.roles
            or self.assign_run_starter is True
            or _is_set(self.owner_id)
            or _is_set(self.ai_assigned)
        )

    @property
    def deadline_anchor(self) -> Optional[str]:
        """The step id this step's deadline counts from, or None.

        ``start_run`` is the sentinel for "from the launch of the process" and is
        not a step id (spec 2.8), so it answers None.
        """
        if not isinstance(self.deadline, dict):
            return None
        anchor = self.deadline.get("step")
        if not anchor or anchor == DEADLINE_ANCHOR_START_RUN:
            return None
        return str(anchor)


@dataclass(frozen=True)
class ConditionDef:
    id: str
    conditionable_id: str
    conditionable_type: str  # already shortened
    operation: str
    statement: Any
    logic: str
    position: int
    column_contains_name: Optional[str] = None


@dataclass(frozen=True)
class ActionDef:
    id: str
    action_type: str
    action_verb: str
    target_step_id: str
    position: int
    deadline: Optional[tuple] = None  # (value, unit, option), hashable
    has_assignees: bool = False

    @property
    def is_webhook(self) -> bool:
        return self.action_type == "webhook" or self.action_verb == "emit_webhook"


@dataclass
class AutomationDef:
    id: str
    alias: str
    position: int
    conditions: List[ConditionDef] = field(default_factory=list)
    actions: List[ActionDef] = field(default_factory=list)
    archived_at: Optional[str] = None

    @property
    def is_empty_rule(self) -> bool:
        """AutomatedRule::forBlueprint short-circuits these (AutomatedRule.php:17-19).

        Structural, never path-derived: reporting an empty rule as
        ``never_applicable`` would imply the walk proved something it never ran
        (spec check 2).
        """
        return not self.conditions or not self.actions

    @property
    def has_prerun_condition(self) -> bool:
        """AutomatedAction::hasPrerunRules(), AutomatedAction.php:166-171."""
        return any(c.conditionable_type == CONDITIONABLE_PRERUN for c in self.conditions)


@dataclass
class TemplateDoc:
    id: str
    title: str
    steps: List[StepDef] = field(default_factory=list)
    automations: List[AutomationDef] = field(default_factory=list)
    fields: Dict[str, FieldDef] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._steps_by_id = {s.id: s for s in self.steps}

    @property
    def step_ids(self) -> Set[str]:
        return set(self._steps_by_id)

    def step(self, step_id: str) -> Optional[StepDef]:
        return self._steps_by_id.get(step_id)

    def field(self, field_id: str) -> Optional[FieldDef]:
        return self.fields.get(field_id)

    @property
    def capture_ids(self) -> Set[str]:
        return {f.id for f in self.fields.values() if f.kind == "capture"}

    @property
    def prerun_ids(self) -> Set[str]:
        return {f.id for f in self.fields.values() if f.kind == "prerun"}

    def hidden_at_launch(self) -> Set[str]:
        """Default visibility is DERIVED. There is no ``hidden`` column (spec 2.4).

        A step is hidden when a process starts if and only if some action targets
        it with visibility/show, per ApplyRulesWithPrerunConditions.php:36-44.
        Every other step starts visible, and firing check 1 on a step that is not
        show-targeted would be the single most damaging false positive available,
        because it would flag most steps in most templates.
        """
        return {
            a.target_step_id
            for auto in self.automations
            for a in auto.actions
            if a.action_type == "visibility" and a.action_verb == "show" and a.target_step_id
        }


class NotATemplateDocument(ValueError):
    """Raised when the input is not a template read.

    Check 9 depends on ``is_soft_start_date``, which ``_TASK_NOISE_FIELDS`` strips
    from TASK reads (sdk_serializer.py, "internal scheduling flag") while
    StepTransformer.php:50 keeps it on the TEMPLATE read path. Fed task data the
    flag reads as absent and check 9 would report every step as having a live
    timer, which is exactly how a reader concludes a task starts in two hours when
    it starts whenever (spec 2.8, check 9). So assert the input rather than
    guessing.
    """


def _is_set(value: Any) -> bool:
    """True when an optional scalar carries a real value.

    ``serialize_dataclass._is_empty`` drops None and empty str/list/dict, so an
    absent key means empty rather than "not fetched" (spec 2.10). ``0`` and
    ``False`` are NOT absent, but neither is a usable owner id, so both answer
    False here.
    """
    if value is None or value is False:
        return False
    if isinstance(value, (str, list, dict, tuple)):
        return len(value) > 0
    if isinstance(value, int):
        return value != 0
    return True


def _as_tuple(value: Any) -> tuple:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        return tuple(value)
    return (value,)


def _collection(value: Any) -> tuple:
    """Read a collection that may or may not be wrapped in a Fractal envelope.

    🔴 THE THREE INCLUDES ON ONE TEMPLATE READ DO NOT AGREE WITH EACH OTHER.
    Measured on a live customer template 2026-09-08: ``steps`` arrives as
    ``{"data": [...]}`` while ``automated_actions`` and ``prerun`` arrive as
    bare lists, in the SAME response.

    Note the direction this failed in, because it is the rarer and louder one. A
    bare ``len()`` on the wrapped dict answers **1**, so a five step template read
    as one step, every action target pointed at a step that was "missing", and the
    engine reported **11 high findings** on a template that has far fewer: seven
    dangling references and four unreachable steps, all fabricated by the read.

    An alarming false positive is not the safe direction here. It is a report
    that tells a customer their template is broken when the tool is.
    """
    if isinstance(value, dict):
        inner = value.get("data")
        if isinstance(inner, (list, tuple)):
            return tuple(inner)
        return ()
    return _as_tuple(value)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _option_labels(raw: Any) -> tuple:
    """Pull the comparable text out of a capture's ``options``.

    api-v2 emits option objects; older payloads and hand-written fixtures use bare
    strings. Both are accepted because check 8's whole job is comparing a
    statement against this list, and mis-reading the shape would make it fire on
    every option field.
    """
    out = []
    for opt in _collection(raw):
        if isinstance(opt, dict):
            for key in ("text", "label", "value", "title", "name"):
                if opt.get(key):
                    out.append(str(opt[key]))
                    break
        elif opt is not None:
            out.append(str(opt))
    return tuple(out)


def assert_is_template_document(doc: Any) -> None:
    """Refuse anything that is not a template read, loudly.

    Note the direction this guards: a task payload silently answers "no start
    date gate" for every step, and the resulting report is confident and wrong.
    """
    if not isinstance(doc, dict):
        raise NotATemplateDocument("template document must be a dict")
    if "steps" not in doc and "automated_actions" not in doc:
        raise NotATemplateDocument(
            "not a template document: expected 'steps' and/or 'automated_actions'. "
            "Fetch GET /organizations/{org}/checklists/{id}"
            "?with=steps,automated_actions,prerun."
        )
    if "task_id" in doc or "run_id" in doc:
        raise NotATemplateDocument(
            "this looks like TASK data, not a template. is_soft_start_date is "
            "stripped from task reads, so check 9 would report every step as "
            "having a live timer."
        )


def _unwrap(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Accept both the bare document and the ``{"data": {...}}`` API envelope."""
    if (
        isinstance(doc.get("data"), dict)
        and "steps" not in doc
        and "automated_actions" not in doc
    ):
        return doc["data"]
    return doc


def parse_template(doc: Dict[str, Any]) -> TemplateDoc:
    """Normalise a raw template payload. Pure; raises NotATemplateDocument.

    The envelope is unwrapped BEFORE the guard runs, so a caller may hand over
    either the bare document or the ``{"data": {...}}`` the API actually returns.
    Guarding first would refuse the shape that comes off the wire.
    """
    if not isinstance(doc, dict):
        raise NotATemplateDocument("template document must be a dict")
    doc = _unwrap(doc)
    assert_is_template_document(doc)

    steps: List[StepDef] = []
    fields: Dict[str, FieldDef] = {}

    for raw in _collection(doc.get("steps")):
        if not isinstance(raw, dict):
            continue
        step_id = str(raw.get("id") or "")
        if not step_id:
            continue
        capture_ids = []
        for cap in _collection(raw.get("captures")):
            if not isinstance(cap, dict):
                continue
            cap_id = str(cap.get("id") or "")
            if not cap_id:
                continue
            capture_ids.append(cap_id)
            fields[cap_id] = FieldDef(
                id=cap_id,
                label=str(cap.get("label") or cap.get("title") or cap_id),
                field_type=str(cap.get("field_type") or cap.get("type") or "text"),
                kind="capture",
                options=_option_labels(cap.get("options")),
                owner_step_id=step_id,
            )
        steps.append(
            StepDef(
                id=step_id,
                title=str(raw.get("title") or raw.get("alias") or step_id),
                position=_as_int(raw.get("position"), len(steps) + 1),
                step_type=str(raw.get("step_type") or raw.get("task_type") or ""),
                deadline=raw.get("deadline") if isinstance(raw.get("deadline"), dict) else None,
                start_date=(
                    raw.get("start_date") if isinstance(raw.get("start_date"), dict) else None
                ),
                is_soft_start_date=bool(raw.get("is_soft_start_date")),
                assignees=_as_tuple(raw.get("assignees")),
                groups=_as_tuple(raw.get("groups")),
                guests=_as_tuple(raw.get("guests")),
                roles=_as_tuple(raw.get("roles")),
                assign_run_starter=bool(raw.get("assign_run_starter")),
                owner_id=raw.get("owner_id"),
                ai_assigned=raw.get("ai_assigned"),
                can_complete_only_assignees=bool(raw.get("can_complete_only_assignees")),
                allow_guest_owners=bool(raw.get("allow_guest_owners")),
                capture_ids=tuple(capture_ids),
            )
        )

    for raw in _collection(doc.get("prerun")):
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get("id") or "")
        if not pid:
            continue
        fields[pid] = FieldDef(
            id=pid,
            label=str(raw.get("label") or raw.get("title") or pid),
            field_type=str(raw.get("field_type") or raw.get("type") or "text"),
            kind="prerun",
            options=_option_labels(raw.get("options")),
            owner_step_id=None,
        )

    automations: List[AutomationDef] = []
    for idx, raw in enumerate(_collection(doc.get("automated_actions"))):
        if not isinstance(raw, dict):
            continue
        # api-v2 emits `then_actions` (AutomatedActionTransformer.php:18); the
        # other two spellings are what older readers and the create/update path
        # use. Read then_actions first, matching the detectors in automation.py.
        raw_actions = (
            raw.get("then_actions")
            or raw.get("actions")
            or raw.get("automated_action_actions")
            or []
        )
        raw_conditions = (
            raw.get("conditions") or raw.get("automated_action_conditions") or []
        )
        conditions = []
        for cidx, c in enumerate(_collection(raw_conditions)):
            if not isinstance(c, dict):
                continue
            conditions.append(
                ConditionDef(
                    id=str(c.get("id") or f"c{cidx}"),
                    conditionable_id=str(c.get("conditionable_id") or ""),
                    conditionable_type=short_conditionable_type(c.get("conditionable_type")),
                    operation=str(c.get("operation") or ""),
                    statement=c.get("statement"),
                    logic=str(c.get("logic") or "and").lower(),
                    position=_as_int(c.get("position"), cidx + 1),
                    column_contains_name=c.get("column_contains_name"),
                )
            )
        actions = []
        for aidx, a in enumerate(_collection(raw_actions)):
            if not isinstance(a, dict):
                continue
            dl = a.get("deadline")
            actions.append(
                ActionDef(
                    id=str(a.get("id") or f"a{aidx}"),
                    action_type=str(a.get("action_type") or ""),
                    action_verb=str(a.get("action_verb") or ""),
                    target_step_id=str(a.get("target_step_id") or ""),
                    position=_as_int(a.get("position"), aidx + 1),
                    deadline=(
                        (dl.get("value"), dl.get("unit"), dl.get("option"))
                        if isinstance(dl, dict)
                        else None
                    ),
                    has_assignees=bool(a.get("assignees")),
                )
            )
        automations.append(
            AutomationDef(
                id=str(raw.get("id") or f"rule{idx}"),
                alias=str(raw.get("automated_alias") or raw.get("alias") or ""),
                position=_as_int(raw.get("position"), idx + 1),
                conditions=sorted(conditions, key=lambda c: c.position),
                actions=sorted(actions, key=lambda a: a.position),
                archived_at=raw.get("archived_at") or raw.get("deleted_at"),
            )
        )

    return TemplateDoc(
        id=str(doc.get("id") or ""),
        title=str(doc.get("title") or doc.get("name") or ""),
        steps=sorted(steps, key=lambda s: s.position),
        automations=sorted(automations, key=lambda a: a.position),
        fields=fields,
    )
