"""Whole-template outlines: what the shared trimmer does before it cuts a step (#1659).

**Why this exists.** On 26 Oct 2026 ``assess_template_health`` read a 247-step
template with 69 rules and 29 kickoff fields. The trimmer cut ONLY the step
list, so the result kept every rule and every kickoff field at full size and
the steps fell to one (the container logged ``Compacted steps: 247 -> 1``).
The model was then asked to judge rules that point at 246 steps it never saw.

So when a whole template is over the ceiling, this module shrinks every part of
it before any step is dropped:

* every step becomes an outline: id, position, title, stage, assignee summary,
  deadline, form field labels and a hidden-at-launch flag;
* every rule becomes one line naming the step and field ids it reads and the
  step ids it acts on;
* every kickoff field keeps only id, label, alias, type and required.

Only when the outlines still do not fit are steps cut, and then the cut is a
PAGE boundary (``step_offset``), never a dead end. Each page carries its steps
and each rule whose earliest named step is among them, so a full read sees
every rule once; the first page also carries the template's own fields, the
kickoff fields and any rule that names no step (``TemplateOutline.rules_for``
records the two layouts that were measured and dropped). A rule that names a step
outside the returned page says so in ``_missing_steps``; a rule that names a
step id the template does not have says so in ``_unknown_steps``, so a model
never pages looking for a step that does not exist.

**Measured, so nobody re-derives it.** On the 247-step fixture in
``tests/fixtures/large_template.py`` (titles 23.7 characters on average,
against 23.8 measured over 2,639 steps of staging templates with 100 or more
steps on 2026-10-27), 247 bare ``[position, id, title]`` rows alone encode to
about 16.6 KB, the 69 rules as one-line entries to about 14.3 KB and the 247
full outlines to about 42 KB. The ceiling is 25 KB, so no outline of that
template fits one response; every step is reachable across pages instead
(3 of them), and each page is under the ceiling.

**One binary search.** Paging calls ``_largest_prefix_that_fits`` from
``utils.sdk_serializer``, the search behind every other trim, so the trims
cannot drift apart (rule 16 in the repo CLAUDE.md). The entry point is still
``compact_dict_list_field``: a whole-template read passes ``outline=outline_template``
and this module does the shaping.

**Shared facts.** Hidden-at-launch and "has an assignee" come from
``template_testing.document`` (``parse_template``), the same model
``test_template`` walks, so the outline and the template tester cannot
disagree about which steps start hidden.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Set

from template_testing.document import (
    DEADLINE_ANCHOR_START_RUN,
    NotATemplateDocument,
    TemplateDoc,
    parse_template,
    short_conditionable_type,
)
from utils.sdk_serializer import (
    MAX_RESULT_BYTES,
    _encoded_len,
    _largest_prefix_that_fits,
)

logger = logging.getLogger(__name__)

# The literal every outline result carries, exported so the tool descriptions
# can interpolate it rather than repeat it.
OUTLINE_KEY = "_outline"
MISSING_STEPS_KEY = "_missing_steps"
UNKNOWN_STEPS_KEY = "_unknown_steps"

# Template keys repeated on every page. The template's other own fields ride
# on the first page only.
_PAGE_IDENTITY_KEYS = ("id", "title")

# api-v2's condition and action vocabulary as one word each. conditionable_type
# is Step, Capture or Prerun (AutomatedActionRequest.php:21, short form from
# AutomatedActionTransformer.php:34).
_CONDITION_WORD = {"Step": "step", "Capture": "field", "Prerun": "kickoff"}


def _note(item_label: str, offset_param: str, first: bool) -> str:
    note = (
        "This template was too large to return whole, so it is an OUTLINE. "
        f"Each of the {item_label} is a summary: id, position, title, stage (a "
        "number; stage_ids lists the stage ids in that order), assignees, "
        "deadline, fields (form field labels) and hidden (true when a rule shows "
        "the step, so it starts hidden). A missing key means the step has none. "
        "Each rule is one line naming the step and field ids it reads and the "
        "step ids it acts on. Each rule is in the part that holds the first step "
        "it names; _missing_steps lists its steps in other parts. Never write any of this "
        "back to Tallyfy: read the real values first with "
        "get_template_steps(step_id=..., full_text=True), "
        "analyze_template_automations or get_kickoff_fields."
    )
    if not first:
        note += (
            " This is a later part: the template's own fields, its kickoff "
            "fields and any rule that names no step are in the first part "
            f"({offset_param}=0)."
        )
    return note


def _as_list(value: Any) -> List[Any]:
    """A collection that may arrive Fractal-wrapped as ``{"data": [...]}``."""
    if isinstance(value, dict):
        inner = value.get("data")
        return list(inner) if isinstance(inner, (list, tuple)) else []
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def steps_in_position_order(steps: Sequence[Any]) -> List[Dict[str, Any]]:
    """Steps sorted by position, a step with no position last, ties stable.

    api-v2 does not return steps in position order (#626), so a page cut off
    the raw order drops steps from the middle of the process. Same key as
    ``get_template_steps`` uses.
    """
    usable = [s for s in steps if isinstance(s, dict)]
    return sorted(
        usable,
        key=lambda s: (s.get("position") is None, s.get("position") or 0),
    )


def _parse(template: Dict[str, Any]) -> Optional[TemplateDoc]:
    try:
        return parse_template(template)
    except NotATemplateDocument:
        return None


def _assignee_summary(step: Dict[str, Any]) -> Optional[str]:
    """Who a step is assigned to, counted rather than listed.

    The fields are the ones ``StepDef.has_static_assignee`` reads
    (StepTransformer.php: assignees, groups, guests, assign_run_starter,
    owner_id, ai_assigned; roles from the role include). ``None`` when the step
    has none of them, so the key is left out.
    """
    parts = []
    for key, word in (("assignees", "member"), ("groups", "group"), ("guests", "guest"), ("roles", "role")):
        count = len(_as_list(step.get(key)))
        if count:
            parts.append(f"{count} {word}{'' if count == 1 else 's'}")
    if step.get("assign_run_starter") is True:
        parts.append("run starter")
    if step.get("owner_id"):
        parts.append("owner")
    if step.get("ai_assigned"):
        parts.append("AI")
    return ", ".join(parts) or None


def _deadline_summary(step: Dict[str, Any], position_of: Dict[str, int]) -> Optional[str]:
    """``"2 days after launch"`` or ``"1 hours before step 12"``.

    ``option`` is the direction (``from`` after the anchor, ``prior_to``
    before it) and ``step`` is the anchor, ``start_run`` meaning launch
    (Deadline.php:18). An anchor that is a step in this template is named by
    its position, which is cheaper than its id and is how people name steps;
    an anchor the template does not have keeps its id so the oddity is visible.
    """
    deadline = step.get("deadline")
    if not isinstance(deadline, dict) or deadline.get("value") is None:
        return None
    direction = "before" if deadline.get("option") == "prior_to" else "after"
    anchor = deadline.get("step")
    if not anchor or anchor == DEADLINE_ANCHOR_START_RUN:
        where = "launch"
    elif anchor in position_of:
        where = f"step {position_of[anchor]}"
    else:
        where = f"step {anchor}"
    return f"{deadline.get('value')} {deadline.get('unit') or 'days'} {direction} {where}"


def outline_step(
    step: Dict[str, Any],
    *,
    stage_number: Optional[int],
    position_of: Dict[str, int],
    hidden: bool,
) -> Dict[str, Any]:
    """One step as a short summary. Keys with nothing to say are left out."""
    out: Dict[str, Any] = {"id": step.get("id"), "position": step.get("position")}
    if step.get("title"):
        out["title"] = step["title"]
    if stage_number is not None:
        out["stage"] = stage_number
    who = _assignee_summary(step)
    if who:
        out["assignees"] = who
    due = _deadline_summary(step, position_of)
    if due:
        out["deadline"] = due
    labels = [
        c.get("label") for c in _as_list(step.get("captures"))
        if isinstance(c, dict) and c.get("label")
    ]
    if labels:
        out["fields"] = labels
    if hidden:
        out["hidden"] = True
    return out


def rule_step_ids(rule: Dict[str, Any]) -> List[str]:
    """Every STEP id one rule names, in the order it names them.

    A condition's ``conditionable_id`` is a step id only when its type is
    ``Step``; Capture and Prerun conditions name fields (the #617 guard in
    ``template_testing.references``). Every action's ``target_step_id`` is a
    step id (AutomatedActionRequest.php:34).
    """
    seen: List[str] = []
    for cond in _as_list(rule.get("conditions")):
        if not isinstance(cond, dict):
            continue
        if short_conditionable_type(cond.get("conditionable_type")) == "Step":
            cid = cond.get("conditionable_id")
            if cid and cid not in seen:
                seen.append(cid)
    for act in _as_list(rule.get("then_actions")):
        if not isinstance(act, dict):
            continue
        tid = act.get("target_step_id")
        if tid and tid not in seen:
            seen.append(tid)
    return seen


def rule_line(rule: Dict[str, Any]) -> str:
    """``"if kickoff <id> equals Yes and step <id> completed then show <id>"``."""
    conds = []
    for i, cond in enumerate(_as_list(rule.get("conditions"))):
        if not isinstance(cond, dict):
            continue
        kind = _CONDITION_WORD.get(short_conditionable_type(cond.get("conditionable_type")), "step")
        text = f"{kind} {cond.get('conditionable_id') or '?'} {cond.get('operation') or '?'}"
        statement = cond.get("statement")
        if statement not in (None, ""):
            text += f" {statement}"
        if i:
            text = f"{(cond.get('logic') or 'and').lower()} {text}"
        conds.append(text)
    acts = []
    for act in _as_list(rule.get("then_actions")):
        if not isinstance(act, dict):
            continue
        verb = act.get("action_verb") or act.get("action_type") or "?"
        target = act.get("target_step_id")
        acts.append(f"{verb} {target}" if target else str(verb))
    return (
        f"if {' '.join(conds) if conds else '(no conditions)'} "
        f"then {', '.join(acts) if acts else '(no actions)'}"
    )


def outline_kickoff_field(field: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"id": field.get("id")}
    for key, src in (("label", "label"), ("alias", "alias"), ("type", "field_type")):
        if field.get(src):
            out[key] = field[src]
    if "required" in field:
        out["required"] = bool(field.get("required"))
    return out


class TemplateOutline:
    """One whole template, outlined once and cut into pages on request."""

    def __init__(
        self,
        template: Dict[str, Any],
        *,
        list_key: str,
        item_label: str,
        offset_param: str = "step_offset",
    ):
        self.list_key = list_key
        self.item_label = item_label
        self.offset_param = offset_param
        doc = _parse(template)
        hidden: Set[str] = doc.hidden_at_launch() if doc else set()

        steps = steps_in_position_order(_as_list(template.get(list_key)))
        # A position shared by two steps names neither, so a deadline anchored
        # to one of them keeps its id.
        uses = Counter(s.get("position") for s in steps)
        position_of = {
            s["id"]: s["position"] for s in steps
            if s.get("id") and s.get("position") is not None and uses[s["position"]] == 1
        }
        self.stage_ids: List[str] = []
        for s in steps:
            stage = s.get("stage_id")
            if stage and stage not in self.stage_ids:
                self.stage_ids.append(stage)
        self.steps = [
            outline_step(
                s,
                stage_number=(self.stage_ids.index(s["stage_id"]) + 1) if s.get("stage_id") else None,
                position_of=position_of,
                hidden=s.get("id") in hidden,
            )
            for s in steps
        ]
        self.all_step_ids = {s.get("id") for s in steps if s.get("id")}

        index_of = {s.get("id"): i for i, s in enumerate(steps) if s.get("id")}
        raw_rules = _as_list(template.get("automated_actions"))
        self.rules = []
        for r in raw_rules:
            if not isinstance(r, dict):
                continue
            named = rule_step_ids(r)
            placed = [index_of[i] for i in named if i in index_of]
            self.rules.append(
                (r.get("id"), rule_line(r), named, min(placed) if placed else None)
            )
        self.has_rules = "automated_actions" in template
        self.kickoff = [
            outline_kickoff_field(f) for f in _as_list(template.get("prerun"))
            if isinstance(f, dict)
        ]
        self.has_kickoff = "prerun" in template
        self.first_page_base = {
            k: v for k, v in template.items()
            if k not in (list_key, "automated_actions", "prerun")
        }
        self.later_page_base = {
            k: template[k] for k in _PAGE_IDENTITY_KEYS if k in template
        }

    @property
    def total(self) -> int:
        return len(self.steps)

    def rules_for(self, start: int, count: int) -> List[tuple]:
        """The rules a page carries: each rule once, with its earliest step.

        A rule is placed on the page that holds the earliest step (by position)
        it names, so a model that reads every part sees every rule exactly once,
        and sees it beside at least one of its steps. A rule that names no step
        of this template at all (field conditions and a webhook, say, or only
        step ids that do not exist) goes on the first page. Its other steps are
        listed in ``_missing_steps``.

        Measured on the 247-step fixture before this rule: putting every rule on
        the first page left room for ONE step, and putting a rule on every page
        that holds any of its steps sent 114 rule lines for 69 rules.
        """
        end = start + count
        return [
            rule for rule in self.rules
            if (rule[3] is None and start == 0) or (rule[3] is not None and start <= rule[3] < end)
        ]

    def page(self, start: int, count: int, *, rule_count: Optional[int] = None) -> Dict[str, Any]:
        """The result for steps ``[start, start + count)``, with no paging marker.

        ``rule_count`` keeps only the first N of the page's rule lines; it is
        used only when a page cannot hold one step beside its rules.
        """
        window = self.steps[start:start + count]
        returned = {s.get("id") for s in window}
        first = start == 0
        page: Dict[str, Any] = dict(self.first_page_base if first else self.later_page_base)
        if first and self.has_kickoff:
            page["prerun"] = self.kickoff
        if self.has_rules:
            on_page = self.rules_for(start, count)
            kept = on_page if rule_count is None else on_page[:rule_count]
            rules = []
            for rule_id, line, step_ids, _ in kept:
                entry: Dict[str, Any] = {"id": rule_id, "rule": line}
                missing = [i for i in step_ids if i in self.all_step_ids and i not in returned]
                unknown = [i for i in step_ids if i not in self.all_step_ids]
                if missing:
                    entry[MISSING_STEPS_KEY] = missing
                if unknown:
                    entry[UNKNOWN_STEPS_KEY] = unknown
                rules.append(entry)
            page["automated_actions"] = rules
            if rule_count is not None and rule_count < len(on_page):
                page["_truncated_rules"] = (
                    f"Showing {rule_count} of the {len(on_page)} rules that name "
                    "these steps. The rest are NOT in this response. "
                    "analyze_template_automations returns every rule."
                )
        if self.stage_ids:
            page["stage_ids"] = self.stage_ids
        page[self.list_key] = window
        page[OUTLINE_KEY] = _note(self.item_label, self.offset_param, first)
        return page


def outline_template(
    result: Dict[str, Any],
    *,
    list_key: str = "steps",
    item_label: str = "steps",
    offset: int = 0,
    offset_param: str = "step_offset",
) -> Dict[str, Any]:
    """Outline an oversize whole template and return the page at ``offset``.

    Called by ``compact_dict_list_field`` once it knows the whole result does
    not fit (or a later page was asked for). Every page is measured WITH its
    marker, as every other trim here is, so the answer is under
    ``MAX_RESULT_BYTES``.

    Shapes:

    - every outline fits: the outline with no ``_truncated``, so "no marker"
      still means every step is here;
    - more steps remain: ``_truncated`` names the ``offset_param`` value for the
      next call, the same wording ``get_template_steps`` uses;
    - the last page of a paged read: ``_page`` says it is the last part.

    KNOWN BOUNDARY, the same one every caller of the shared search has: a page
    always holds at least one step, so a first page whose kickoff fields alone
    are over the ceiling still comes back over it. When one step's rules do not
    fit beside it, the rules are cut first (``_truncated_rules``), because
    ``analyze_template_automations`` can return them on their own.
    """
    shaped = TemplateOutline(
        result, list_key=list_key, item_label=item_label, offset_param=offset_param
    )
    total = shaped.total

    def fits(candidate: Dict[str, Any]) -> bool:
        return _encoded_len(candidate) <= MAX_RESULT_BYTES

    if total == 0:
        empty = shaped.page(0, 0)
        if fits(empty) or not shaped.rules:
            return empty
        kept_rules = _largest_prefix_that_fits(
            len(shaped.rules), lambda r: shaped.page(0, 0, rule_count=r)
        )
        return shaped.page(0, 0, rule_count=kept_rules)
    if offset < 0 or offset >= total:
        raise ValueError(f"{offset_param} {offset} is outside 0..{total - 1}")

    def build(n: int, rule_count: Optional[int] = None) -> Dict[str, Any]:
        page = shaped.page(offset, n, rule_count=rule_count)
        first, last = offset + 1, offset + n
        if last < total:
            page["_truncated"] = (
                f"Showing {item_label} {first} to {last} of {total}. The rest are "
                f"NOT in this response. Call again with {offset_param}={last} "
                "for the next part."
            )
        elif offset:
            page["_page"] = (
                f"Showing {item_label} {first} to {last} of {total}. "
                "This is the LAST part."
            )
        return page

    rest = total - offset
    whole = build(rest)
    if fits(whole):
        logger.info("Outlined %s: all %d of %d fit from offset %d", item_label, rest, total, offset)
        return whole

    rule_count: Optional[int] = None
    if not fits(build(1)):
        on_first = len(shaped.rules_for(offset, 1))
        if on_first:
            rule_count = _largest_prefix_that_fits(
                on_first, lambda r: build(1, rule_count=r)
            )
    kept = _largest_prefix_that_fits(rest, lambda n: build(n, rule_count))
    logger.info(
        "Outlined %s: returned %d to %d of %d", item_label, offset + 1, offset + kept, total
    )
    return build(kept, rule_count)
