"""
Automation History tools (tallyfy/mcp#1437)

``explain_step_visibility`` answers "why is this step shown, or hidden, in my
running process?" from what Tallyfy RECORDED, not from a reading of the
template as it is today.

Where each fact comes from, all measured on staging 2026-09-29:

* **The rules.** ``GET runs/{run}?with=checklist`` returns ``$run->checklist``,
  the template VERSION row the process launched from (``Run::checklist()`` is
  ``belongsTo(Checklist)->withTrashed()``), with its ``automated_actions``. A
  plain ``GET checklists/{id}`` resolves the id as a timeline and answers with
  the CURRENT version, so it would describe rules this process never ran. On a
  staging run launched before a rule was added, the run include carried one
  automation and the template read carried two.
* **The checks.** api-v2's ``AutomationExecutionActivity`` writes one activity
  feed row per rule evaluation: ``type`` ``execution`` and ``verb`` ``executed``
  or ``execution_fail``. ``audit_state`` holds the automation id, its alias,
  each condition with the value it expects and ``isMet``, and the actions it
  took keyed by task id. The feed endpoint cannot filter on those verbs today
  (``GetActivitiesRequest`` closes ``verb`` and ``type`` to other values;
  tallyfy/api-v2#10997 adds them), so the run's feed is paged and filtered here.
  Its default sort is newest first, so a page cap drops the OLDEST checks and
  keeps the latest one per rule, which is the one this tool reports.
* **The steps.** ``GET runs/{run}/tasks`` includes hidden tasks
  (``status: auto-skipped``) and gives each task's ``step_id``, which is the id
  space automation targets use. All 32 targets on the staging copy of the
  purchase request matched a task.

Two things the record does NOT hold, and the tool says so rather than guessing:

* the value a condition actually read, only the value it expected and whether
  it was met. For a kick-off field the answer as it reads NOW is reported
  beside it, labelled as such;
* the automatic hide at launch. ``ApplyRulesWithPrerunConditions`` hides every
  step a show action targets when the process is created, on Pro plans and
  trials, and writes no row for it. So a hidden step with a show rule is
  explained from the launched version's show rules.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations
from tallyfy import TallyfySDK, TallyfyError

from metrics import track_tool_execution
from utils.auth_context import TALLYFY_API_BASE_URL, get_authenticated_credentials
from utils.fastmcp_errors import handle_tallyfy_errors
from utils.fastmcp_types import GenericDict, OptionalTaskId, ProcessId
from utils.response_sanitizer import sanitize_for_user_text
from utils.sdk_serializer import compact_dict_list_field

logger = logging.getLogger(__name__)

# Measured on staging 2026-09-29: a per_page=100 read of a 122-row run feed
# answered 100 rows and total_pages 2.
PAGE_SIZE = 100

# Bounds on how much one call reads: 2,000 feed rows and 1,000 tasks. The feed
# is newest first, so hitting its cap loses the oldest checks and keeps the
# latest per rule. The result says when either cap was hit.
MAX_FEED_PAGES = 20
MAX_TASK_PAGES = 10

HIDDEN_STATUS = "auto-skipped"

NO_EVALUATION = "no evaluation recorded"
EVALUATIONS_RECORDED = "evaluations recorded"

# How the record names a condition's subject, in the words a person uses.
_SUBJECT_KINDS = {
    "task": "step",
    "step": "step",
    "prerun": "kick-off field",
    "capture": "step form field",
    "field": "step form field",
}

# Field types whose answer is picked from a list, so an expected value that no
# option can satisfy can never match.
_CHOICE_FIELD_TYPES = frozenset({"dropdown", "radio", "multiselect"})

# Operations that compare the WHOLE answer against the statement, so the
# statement has to be a whole option. Raw names as the template stores them,
# and the spellings api-v2's ProcessRuleLog writes into the record.
_EQUALITY_OPERATIONS = frozenset({
    "equals", "not_equals", "equals_any",
    "is", "is not", "is any of",
})

# Operations api-v2 matches by SUBSTRING: SimpleValueCapture::contains and
# MultiValueCapture::contains call stripos, so "IT equipment" matches the option
# "There is IT equipment and/or software ...". A statement fails here only when
# it is inside no option at all (tallyfy/mcp#1493). ProcessRuleLog writes
# not_contains as "does not contain". is_empty and is_not_empty carry no
# statement, and the numeric operations compare a number, so neither set has them.
_SUBSTRING_OPERATIONS = frozenset({"contains", "not_contains", "does not contain"})

# A step condition's statement is a time clause ("any_time", "on-time",
# "early_24", "late_24"), not a value to show next to the step name.
_TIME_CLAUSES = frozenset({"any_time", "on-time", "on_time", "early_24", "late_24"})

LIMITS = [
    "The record keeps each condition's expected value and whether it was met, "
    "not the value that was read at the time.",
    "Hiding a step at launch because a show rule targets it writes no record.",
]


# ---------------------------------------------------------------------------
# Small readers
# ---------------------------------------------------------------------------

def _first(value: Any) -> Any:
    """Activity feed columns arrive as one-element lists (``verb: ["executed"]``)."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _iso(timestamp: Any) -> Optional[str]:
    """Feed rows say ``2026-09-29 21:00:21`` in UTC; runs say ``...T21:00:16Z``."""
    if not isinstance(timestamp, str) or not timestamp:
        return None
    text = timestamp.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
    return text


def _readable_time(iso_timestamp: Optional[str]) -> str:
    if not iso_timestamp:
        return "an unrecorded time"
    return iso_timestamp.replace("T", " ").replace("Z", " UTC")


def _clean_name(name: Any) -> Any:
    """Process names built from a name format carry U+FEFF between the parts."""
    if not isinstance(name, str):
        return name
    return " ".join(name.replace("\ufeff", " ").split())


def _unwrap(value: Any) -> Any:
    """A Fractal include arrives as ``{"data": ...}``."""
    if isinstance(value, dict) and "data" in value and len(value) <= 2:
        return value.get("data")
    return value


def _answer_text(value: Any) -> Any:
    """A kick-off answer as a person would read it, or None when it is not simple."""
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, list):
        texts = [
            item.get("text") if isinstance(item, dict) else item
            for item in value
        ]
        texts = [t for t in texts if isinstance(t, (str, int, float)) and t != ""]
        return texts or None
    if isinstance(value, dict):
        text = value.get("text")
        return text if isinstance(text, (str, int, float)) else None
    if isinstance(value, (str, int, float, bool)):
        text = str(value)
        return text if len(text) <= 200 else text[:200] + "..."
    return None


# ---------------------------------------------------------------------------
# API reads
# ---------------------------------------------------------------------------

def read_run_with_version(sdk, org_id: str, process_id: str) -> Dict[str, Any]:
    """The process, with the template version it launched from as ``checklist``.

    ``checklist.steps`` rather than ``checklist``: the nested include carries each
    step's title and its form fields, so a rule on a step form field can be named
    and its expected value checked against that field's options. Measured on
    staging 2026-09-29 for a 33-step template: 0.5 s against 0.35 s.
    """
    endpoint = f"organizations/{org_id}/runs/{process_id}"
    try:
        response = sdk._make_request("GET", endpoint, params={"with": "checklist.steps"})
    except TallyfyError as exc:
        if getattr(exc, "status_code", None) == 404:
            raise ToolError(
                f"No process with id {process_id} was found in this organization. "
                "A process id is the 32-character id of a running process, not a "
                "template id or a task id."
            )
        raise
    run = response.get("data") if isinstance(response, dict) else None
    if not isinstance(run, dict) or not run.get("id"):
        raise ToolError(f"No process with id {process_id} was found in this organization.")
    version = _unwrap(run.get("checklist"))
    if isinstance(version, dict):
        steps = _unwrap(version.get("steps"))
        version["steps"] = steps if isinstance(steps, list) else []
        run["checklist"] = version
    else:
        run["checklist"] = None
    return run


def _read_pages(sdk, endpoint: str, params: Dict[str, Any], max_pages: int) -> Tuple[List[dict], bool, Optional[int]]:
    """Every row of a paginated read, whether the read was complete, and the total."""
    rows: List[dict] = []
    total: Optional[int] = None
    page = 1
    while True:
        response = sdk._make_request(
            "GET", endpoint, params={**params, "per_page": PAGE_SIZE, "page": page}
        )
        data = response.get("data") if isinstance(response, dict) else None
        if isinstance(data, list):
            rows.extend(r for r in data if isinstance(r, dict))
        pagination = ((response or {}).get("meta") or {}).get("pagination") or {}
        total = pagination.get("total", total)
        total_pages = pagination.get("total_pages") or 1
        if page >= total_pages:
            return rows, True, total
        if page >= max_pages:
            return rows, False, total
        page += 1


def read_run_tasks(sdk, org_id: str, process_id: str) -> Tuple[List[dict], bool]:
    """Every task in the run, hidden ones included, and whether all were read."""
    rows, complete, _total = _read_pages(
        sdk, f"organizations/{org_id}/runs/{process_id}/tasks", {}, MAX_TASK_PAGES
    )
    return rows, complete


def read_automation_checks(sdk, org_id: str, process_id: str) -> Tuple[List[dict], bool, Optional[int]]:
    """The run's activity feed, filtered to rule evaluations.

    Returns the evaluation rows, whether the WHOLE feed was read, and how many
    feed rows exist in total.
    """
    rows, complete, total = _read_pages(
        sdk,
        f"organizations/{org_id}/activity-feeds",
        {"entity_type": "run", "entity_id": process_id},
        MAX_FEED_PAGES,
    )
    checks = [
        r for r in rows
        if _first(r.get("type")) == "execution"
        and isinstance(r.get("audit_state"), dict)
        and r["audit_state"].get("automation_id")
        and r.get("auditable_id") in (None, process_id)
    ]
    return checks, complete, total


# ---------------------------------------------------------------------------
# Building the answer
# ---------------------------------------------------------------------------

def latest_checks(checks: List[dict]) -> Dict[str, Dict[str, Any]]:
    """The newest evaluation of each automation, and how often each was checked."""
    ordered = sorted(
        checks,
        key=lambda r: (_iso(r.get("created_at")) or "", r.get("id") or 0),
    )
    latest: Dict[str, Dict[str, Any]] = {}
    for row in ordered:
        automation_id = row["audit_state"]["automation_id"]
        entry = latest.setdefault(automation_id, {"row": row, "count": 0})
        entry["row"] = row
        entry["count"] += 1
    return latest


class _Version:
    """The launched template version, indexed for lookups."""

    def __init__(self, run: Dict[str, Any], tasks: List[dict]):
        self.document = run.get("checklist") or {}
        automations = self.document.get("automated_actions") or []
        self.automations = [a for a in automations if isinstance(a, dict) and a.get("id")]
        self.by_id = {a["id"]: a for a in self.automations}
        prerun = _unwrap(self.document.get("prerun")) or []
        self.fields = {f["id"]: f for f in prerun if isinstance(f, dict) and f.get("id")}
        answers = run.get("prerun")
        self.answers = answers if isinstance(answers, dict) else {}
        steps = [s for s in self.document.get("steps") or [] if isinstance(s, dict)]
        self.step_titles = {s["id"]: s.get("title") for s in steps if s.get("id")}
        self.captures = {
            c["id"]: c
            for s in steps
            for c in (_unwrap(s.get("captures")) or [])
            if isinstance(c, dict) and c.get("id")
        }
        self.tasks = tasks
        self.task_by_step = {}
        for task in tasks:
            step_id = task.get("step_id")
            if step_id and step_id not in self.task_by_step:
                self.task_by_step[step_id] = task
        self.task_by_id = {t.get("id"): t for t in tasks if t.get("id")}

    def step_title(self, step_id: str) -> Optional[str]:
        task = self.task_by_step.get(step_id)
        if task and task.get("title"):
            return task.get("title")
        return self.step_titles.get(step_id)

    def field(self, kind: str, field_id: Any) -> Optional[Dict[str, Any]]:
        if kind == "prerun":
            return self.fields.get(field_id)
        if kind in ("capture", "field"):
            return self.captures.get(field_id)
        return None

    def targets(self, automation: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for action in automation.get("then_actions") or []:
            if not isinstance(action, dict):
                continue
            target = action.get("target_step_id")
            entry: Dict[str, Any] = {
                "action": action.get("action_verb") or action.get("action_type"),
            }
            # The task id, not the step id, because it is what a follow-up call
            # for one step takes. A step with no task in this process keeps its
            # step id so the target is still identified.
            task = self.task_by_step.get(target)
            if task and task.get("id"):
                entry["task_id"] = task["id"]
            elif target:
                entry["step_id"] = target
            title = self.step_title(target) if target else None
            if title:
                entry["step_title"] = title
            out.append(entry)
        return out

    def verbs_on_step(self, automation: Dict[str, Any], step_id: str) -> List[str]:
        return [
            a.get("action_verb") or a.get("action_type")
            for a in automation.get("then_actions") or []
            if isinstance(a, dict) and a.get("target_step_id") == step_id
        ]

    def field_for(self, automation_id: str, kind: str, statement: Any, label: Any) -> Optional[Dict[str, Any]]:
        """The field a logged condition was about, when the version names exactly one.

        The record names a field by its LABEL, so it is matched back to the
        version's condition by kind, expected value and label together. Anything
        less than one clear match yields None and nothing is added.
        """
        automation = self.by_id.get(automation_id) or {}
        candidates = []
        for condition in automation.get("conditions") or []:
            if not isinstance(condition, dict):
                continue
            if (condition.get("conditionable_type") or "").lower() != kind:
                continue
            if condition.get("statement") != statement:
                continue
            field = self.field(kind, condition.get("conditionable_id"))
            if field and (label is None or field.get("label") == label):
                candidates.append(field)
        unique = {f["id"]: f for f in candidates}
        return next(iter(unique.values())) if len(unique) == 1 else None


def _option_texts(field: Dict[str, Any]) -> List[str]:
    return [
        str(o.get("text")) for o in field.get("options") or []
        if isinstance(o, dict) and o.get("text") not in (None, "")
    ]


def _is_substring_operation(operation: Any) -> bool:
    return str(operation or "").lower() in _SUBSTRING_OPERATIONS


def _missing_options(field: Dict[str, Any], operation: Any, statement: Any) -> List[str]:
    """Expected values no option of a choice field can satisfy.

    An equality operation needs the value to be a whole option. contains and
    not_contains need it inside at least one option, as api-v2's stripos does.
    Both compare ignoring case and the spaces around the value.
    """
    if (field.get("field_type") or "") not in _CHOICE_FIELD_TYPES:
        return []
    op = str(operation or "").lower()
    if op not in _EQUALITY_OPERATIONS and op not in _SUBSTRING_OPERATIONS:
        return []
    options = {t.strip().lower() for t in _option_texts(field)}
    if not options:
        return []
    substring = op in _SUBSTRING_OPERATIONS

    def offered(text: str) -> bool:
        if substring:
            return any(text in option for option in options)
        return text in options

    expected = statement if isinstance(statement, list) else [statement]
    return [
        str(e) for e in expected
        if isinstance(e, (str, int, float)) and str(e).strip()
        and not offered(str(e).strip().lower())
    ]


def _describe_logged_condition(rule: Dict[str, Any], automation_id: str, version: _Version) -> Dict[str, Any]:
    kind = str(rule.get("type") or "").lower()
    subject = rule.get("task_title") if kind in ("task", "step") else rule.get("field_label")
    condition: Dict[str, Any] = {
        "subject": subject,
        "subject_type": _SUBJECT_KINDS.get(kind, kind or None),
        "operation": rule.get("operation"),
    }
    statement = rule.get("statement")
    if statement is not None and not (kind in ("task", "step") and statement in _TIME_CLAUSES):
        condition["expected"] = statement
    elif kind in ("task", "step") and statement in _TIME_CLAUSES and statement != "any_time":
        condition["timing"] = statement
    if rule.get("logic"):
        condition["logic"] = rule.get("logic")
    met = rule.get("isMet")
    condition["met"] = met if isinstance(met, bool) else None

    if kind in ("capture", "field") and rule.get("task_title"):
        condition["on_step"] = rule.get("task_title")
    if kind in ("prerun", "capture", "field"):
        field = version.field_for(automation_id, "field" if kind == "field" else kind,
                                  statement, rule.get("field_label"))
        if field:
            if kind == "prerun":
                answer = _answer_text(version.answers.get(field["id"]))
                if answer is not None:
                    condition["current_answer"] = answer
            if _missing_options(field, rule.get("operation"), statement):
                condition["expected_value_is_not_an_option"] = True
                condition["field_options"] = _option_texts(field)[:15]
    return condition


def _describe_template_condition(condition: Dict[str, Any], version: _Version) -> Dict[str, Any]:
    """A condition read from the launched version, for a rule with no recorded check."""
    kind = str(condition.get("conditionable_type") or "").lower()
    target = condition.get("conditionable_id")
    field = version.field(kind, target)
    if kind == "step":
        subject = version.step_title(target)
    else:
        subject = (field or {}).get("label")
    described: Dict[str, Any] = {
        "subject_type": _SUBJECT_KINDS.get(kind, kind or None),
        "operation": condition.get("operation"),
    }
    if subject:
        described = {"subject": subject, **described}
    statement = condition.get("statement")
    if statement is not None and not (kind == "step" and statement in _TIME_CLAUSES):
        described["expected"] = statement
    if condition.get("logic"):
        described["logic"] = condition.get("logic")
    if field and _missing_options(field, condition.get("operation"), statement):
        described["expected_value_is_not_an_option"] = True
        described["field_options"] = _option_texts(field)[:15]
    return described


def _actions_taken(row: Dict[str, Any], version: _Version) -> List[Dict[str, Any]]:
    actions = row["audit_state"].get("actions")
    if not isinstance(actions, dict):
        return []
    out = []
    for task_id, taken in actions.items():
        if not isinstance(taken, dict):
            continue
        entry: Dict[str, Any] = {"task_id": task_id, "actions": taken.get("actions") or []}
        title = taken.get("title") or (version.task_by_id.get(task_id) or {}).get("title")
        if title:
            entry["task_title"] = title
        out.append(entry)
    return out


def _first_logic_dropped(conditions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """``logic`` joins a condition to the one BEFORE it, so the first has none.

    api-v2 stores a value on every condition and the engine ignores the
    first one's, so showing it would describe a join that does not exist.
    """
    if conditions:
        conditions[0].pop("logic", None)
    return conditions


def _latest_check(entry: Optional[Dict[str, Any]], automation_id: str, version: _Version) -> Optional[Dict[str, Any]]:
    if not entry:
        return None
    row = entry["row"]
    verb = _first(row.get("verb"))
    return {
        "checked_at": _iso(row.get("created_at")),
        "outcome": "executed" if verb == "executed" else "did not execute",
        "conditions": _first_logic_dropped([
            _describe_logged_condition(rule, automation_id, version)
            for rule in row["audit_state"].get("rules") or []
            if isinstance(rule, dict)
        ]),
        "actions_taken": _actions_taken(row, version),
        "times_checked": entry["count"],
    }


def _rule_entry(automation: Dict[str, Any], entry: Optional[Dict[str, Any]], version: _Version) -> Dict[str, Any]:
    automation_id = automation["id"]
    name = automation.get("automated_alias")
    if not name and entry:
        name = entry["row"]["audit_state"].get("alias")
    result: Dict[str, Any] = {
        "automation_id": automation_id,
        "name": name,
        "targets": version.targets(automation),
    }
    check = _latest_check(entry, automation_id, version)
    if check is None:
        result["latest_check"] = NO_EVALUATION
        conditions = sorted(
            (c for c in automation.get("conditions") or [] if isinstance(c, dict)),
            key=lambda c: c.get("position") or 0,
        )
        result["conditions_in_template"] = _first_logic_dropped([
            _describe_template_condition(c, version) for c in conditions
        ])
    else:
        result["latest_check"] = check
    return result


def _quoted(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(f'"{v}"' for v in value)
    return f'"{value}"'


def _condition_phrase(condition: Dict[str, Any]) -> str:
    subject = condition.get("subject") or f"a {condition.get('subject_type') or 'condition'}"
    phrase = f"{_quoted(subject)} {condition.get('operation') or ''}".rstrip()
    if "expected" in condition:
        phrase += f" {_quoted(condition['expected'])}"
    return phrase


def _sentence(text: str) -> str:
    """End a sentence once, even when it closes on a quoted full stop."""
    return text if text.rstrip('"').endswith((".", "?", "!")) else text + "."


def _unmet_sentences(rule: Dict[str, Any], when: str) -> List[str]:
    check = rule["latest_check"]
    unmet = [c for c in check["conditions"] if c.get("met") is False]
    if not unmet:
        return [f"{_quoted(rule['name'])} did not fire at its latest check ({when})."]
    noun = "this condition was" if len(unmet) == 1 else "these conditions were"
    out = [_sentence(
        f"{_quoted(rule['name'])} has not fired. At its latest check ({when}) {noun} "
        "not met: " + "; ".join(_condition_phrase(c) for c in unmet)
    )]
    for condition in unmet:
        if condition.get("expected_value_is_not_an_option"):
            relation = (
                "containing" if _is_substring_operation(condition.get("operation")) else "with"
            )
            out.append(_sentence(
                f"The field {_quoted(condition.get('subject'))} has no option {relation} "
                f"the text {_quoted(condition.get('expected'))}"
            ))
        if "current_answer" in condition:
            out.append(_sentence(
                f"Its answer now reads {_quoted(condition['current_answer'])}"
            ))
    return out


def _task_reason(task: Dict[str, Any], hidden: bool, show_rules: List[Dict[str, Any]],
                 hide_rules: List[Dict[str, Any]], any_check: bool) -> Optional[str]:
    """One plain paragraph built only from the version's rules and the record."""
    title = _quoted(task.get("title") or "This step")

    def fired(rule):
        check = rule["latest_check"]
        return isinstance(check, dict) and check["outcome"] == "executed"

    def when(rule):
        return _readable_time(rule["latest_check"]["checked_at"])

    def names(rules):
        return " and ".join(_quoted(r["name"]) for r in rules)

    parts: List[str] = []
    if hidden:
        if show_rules:
            one = len(show_rules) == 1
            parts.append(
                f"{title} is hidden. Tallyfy hid it when the process launched, because "
                f"the show {'rule' if one else 'rules'} {names(show_rules)} "
                f"{'targets' if one else 'target'} it, and a step stays hidden until one "
                "of its show rules fires."
            )
            for rule in show_rules:
                if not isinstance(rule["latest_check"], dict):
                    parts.append(f"No check of {_quoted(rule['name'])} is recorded.")
                elif fired(rule):
                    parts.append(f"{_quoted(rule['name'])} fired at {when(rule)}.")
                else:
                    parts.extend(_unmet_sentences(rule, when(rule)))
            for rule in hide_rules:
                if fired(rule):
                    parts.append(f"The hide rule {_quoted(rule['name'])} fired at {when(rule)}.")
        else:
            fired_hides = [r for r in hide_rules if fired(r)]
            if fired_hides:
                parts.append(f"{title} is hidden.")
                parts.extend(
                    f"The hide rule {_quoted(r['name'])} fired at {when(r)}." for r in fired_hides
                )
            elif any_check:
                parts.append(f"{title} is hidden, and no recorded rule check hid it.")
    else:
        fired_shows = [r for r in show_rules if fired(r)]
        if fired_shows:
            parts.append(
                f"{title} is showing because "
                + " and ".join(
                    f"the show rule {_quoted(r['name'])} fired at {when(r)}" for r in fired_shows
                )
                + "."
            )
        elif show_rules:
            parts.append(
                f"{title} is showing, and no show rule that targets it has a recorded firing."
            )
        elif hide_rules:
            unfired = [r for r in hide_rules if not fired(r)]
            if unfired:
                parts.append(
                    f"{title} is showing. No show rule targets it, and the hide "
                    f"{'rule' if len(unfired) == 1 else 'rules'} {names(unfired)} "
                    f"{'has' if len(unfired) == 1 else 'have'} not fired."
                )
    return " ".join(parts) if parts else None


def _compact_for_whole_process(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Shrink the per-rule entries for the whole-process answer.

    A kick-off answer is a fact about the process, so it is lifted out of the
    conditions and reported once per field instead of once per rule that reads
    it; the option lists stay behind the flag. Measured on the staging copy of
    the purchase request: 32 checked rules came to 27 KB with both inline,
    which the 25 KB cap cut to 27 of 32.
    """
    answers: Dict[str, Any] = {}
    for entry in entries:
        check = entry["latest_check"]
        if not isinstance(check, dict):
            continue
        for condition in check["conditions"]:
            answer = condition.pop("current_answer", None)
            if answer is not None and condition.get("subject"):
                answers.setdefault(condition["subject"], answer)
            condition.pop("field_options", None)
        if not check.get("actions_taken"):
            check.pop("actions_taken", None)
    return answers


def build_explanation(run: Dict[str, Any], tasks: List[dict], checks: List[dict],
                      *, feed_complete: bool = True, feed_total: Optional[int] = None,
                      tasks_complete: bool = True,
                      task_id: Optional[str] = None) -> Dict[str, Any]:
    """Assemble the answer. Pure, so the unit tests drive it with recorded bodies."""
    version = _Version(run, tasks)
    latest = latest_checks(checks)
    document = version.document

    result: Dict[str, Any] = {
        "process": {
            "id": run.get("id"),
            "name": _clean_name(run.get("name")),
            "status": run.get("status"),
            "launched_at": run.get("started_at") or run.get("created_at"),
        },
        "launched_from": {
            "template_id": document.get("id") or run.get("checklist_id"),
            "title": document.get("title") or run.get("checklist_title"),
            "version_last_edited": document.get("last_updated"),
            "note": (
                "Every rule below comes from this template version, the one the process "
                "launched from. The template may have changed since."
            ),
        },
        "limits": list(LIMITS),
    }
    if document.get("archived_at"):
        result["launched_from"]["version_retired_at"] = document.get("archived_at")
        result["launched_from"]["note"] = (
            "Every rule below comes from the template version this process launched "
            "from. That version has since been replaced by a newer one or archived, so "
            "the template as it reads today can have different rules."
        )
    if document.get("archived_at") and not document.get("steps"):
        result["limits"].append(
            "Tallyfy no longer returns the steps of a retired template version, so a "
            "condition on a step form field of this version is shown without its label."
        )
    if not document:
        result["limits"].append(
            "The template version this process launched from could not be read, so no "
            "rule is described."
        )
    if not feed_complete:
        result["limits"].append(
            f"Only the newest {PAGE_SIZE * MAX_FEED_PAGES} of {feed_total} activity entries "
            "were read. The latest check of each rule is in that window; older checks are not."
        )
    if not tasks_complete:
        result["limits"].append(
            f"Only the first {PAGE_SIZE * MAX_TASK_PAGES} tasks of this process were read."
        )
    if version.automations and not checks:
        result["limits"].append(
            "No rule check is recorded for this process at all. Tallyfy checks rules, "
            "and hides show-rule steps at launch, only on Pro plans and trials."
        )

    if task_id:
        task = version.task_by_id.get(task_id)
        if task is None:
            raise ToolError(
                f"Task {task_id} is not a task in process {run.get('id')}. "
                "get_tasks_for_process lists the tasks in a process."
            )
        step_id = task.get("step_id")
        hidden = task.get("status") == HIDDEN_STATUS
        targeting = [a for a in version.automations if version.verbs_on_step(a, step_id)]
        rules = []
        for automation in targeting:
            entry = _rule_entry(automation, latest.get(automation["id"]), version)
            entry.pop("targets", None)
            entry["acts_on_this_step"] = version.verbs_on_step(automation, step_id)
            check = entry["latest_check"]
            if isinstance(check, dict):
                check["actions_taken_on_this_step"] = [
                    verb
                    for taken in check.pop("actions_taken")
                    if taken.get("task_id") == task_id
                    for verb in taken.get("actions") or []
                ]
            rules.append(entry)
        show_rules = [r for r in rules if "show" in r["acts_on_this_step"]]
        hide_rules = [r for r in rules if "hide" in r["acts_on_this_step"]]
        any_check = any(isinstance(r["latest_check"], dict) for r in rules)

        result["task"] = {
            "id": task.get("id"),
            "title": task.get("title"),
            "step_id": step_id,
            "status": task.get("status"),
            "hidden": hidden,
        }
        result["evaluation"] = EVALUATIONS_RECORDED if any_check else NO_EVALUATION
        result["rules_targeting_this_step"] = rules
        if show_rules:
            result["hidden_at_launch"] = {
                "applies": True,
                "because": (
                    "A show rule targets this step. On Pro plans and trials Tallyfy hides "
                    "every such step when the process launches and shows it only when one "
                    "of its show rules fires. That hide is not recorded."
                ),
                "show_rules": [r["name"] for r in show_rules],
            }
        result["reason"] = (
            _task_reason(task, hidden, show_rules, hide_rules, any_check) if rules else None
        )
        return sanitize_for_user_text(result)

    checked = [a for a in version.automations if a["id"] in latest]
    unchecked = [a for a in version.automations if a["id"] not in latest]
    entries = [_rule_entry(a, latest[a["id"]], version) for a in checked]
    # Newest check first, the order the feed itself uses, so that when the 25 KB
    # cap trims the list it is the oldest checks that go.
    entries.sort(
        key=lambda e: (e["latest_check"]["checked_at"] or "", e["name"] or ""), reverse=True
    )
    answers_now = _compact_for_whole_process(entries)
    result["summary"] = {
        "rules_in_launched_version": len(version.automations),
        "rules_checked": len(checked),
        "rules_that_fired_at_latest_check": sum(
            1 for e in entries if e["latest_check"]["outcome"] == "executed"
        ),
        "hidden_steps": sum(1 for t in tasks if t.get("status") == HIDDEN_STATUS),
        "steps": len(tasks),
    }
    result["evaluation"] = EVALUATIONS_RECORDED if entries else NO_EVALUATION
    result["rules_never_checked"] = [a.get("automated_alias") for a in unchecked]
    if answers_now:
        result["kickoff_answers_now"] = answers_now
    result["rule_checks"] = entries
    return compact_dict_list_field(
        sanitize_for_user_text(result), "rule_checks", item_label="rule checks"
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_automation_history_tools(mcp):
    """Register the tools that read what Tallyfy recorded about a process's rules."""

    @mcp.tool(
        name="explain_step_visibility",
        description=(
            "Explains why a step in a running process is shown or hidden, from the "
            "record Tallyfy keeps each time it checks an automation rule for that "
            "process. REQUIRED: 'process_id' (32-character hex). OPTIONAL: 'task_id', "
            "one task in that process, which narrows the answer to that step.\n\n"
            "The rules come from the template version the process launched from, "
            "which can differ from the template as it is today; 'launched_from' names "
            "that version. For each rule it returns the latest recorded check: when it "
            "ran, whether it executed, each condition with the value the rule expects "
            "and whether it was met, and the actions it took.\n\n"
            "With 'task_id' the result also holds the task's status, every rule whose "
            "actions target that step, and 'reason', one paragraph built only from "
            "those rules and records. A step that a show rule targets is hidden when "
            "the process launches and appears only when one of its show rules fires; "
            "that hide leaves no record, so 'hidden_at_launch' states it from the "
            "template version. When no rule targets the step, 'evaluation' reads "
            "'no evaluation recorded' and 'reason' is null.\n\n"
            "The record holds the expected value and met or not, never the value "
            "actually read. For a kick-off field, 'current_answer' is the answer as it "
            "reads now, which may have changed since the check, and "
            "'expected_value_is_not_an_option' marks a rule that expects text the "
            "field does not offer. Without 'task_id' the result lists every checked "
            "rule in time order plus 'rules_never_checked'. Read-only: it changes "
            "nothing."
        ),
        tags=["automation", "history", "visibility", "process", "read-only"],
        annotations=ToolAnnotations(
            title="Explain why a step is shown or hidden",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None,
    )
    @track_tool_execution("explain_step_visibility")
    @handle_tallyfy_errors("explain step visibility")
    def explain_step_visibility(
        process_id: ProcessId,
        task_id: OptionalTaskId = None,
    ) -> GenericDict:
        """
        Explain why a step in a running process is shown or hidden.

        Args:
            process_id: The running process (run) to explain, 32-character hex.
            task_id: Optional task in that process, to narrow the answer to one step.

        Returns:
            launched_from, the latest check of each rule, and for a task its status,
            the rules targeting its step, hidden_at_launch, evaluation and reason.
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            run = read_run_with_version(sdk, org_id, process_id)
            tasks, tasks_complete = read_run_tasks(sdk, org_id, process_id)
            checks, complete, total = read_automation_checks(sdk, org_id, process_id)
        result = build_explanation(
            run,
            tasks,
            checks,
            feed_complete=complete,
            feed_total=total,
            tasks_complete=tasks_complete,
            task_id=task_id or None,
        )
        return ToolResult(content=result, structured_content=None)
