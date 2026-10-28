"""
List tools read through api-v2's table query engine (#1660).

``get_all_templates`` and ``get_organization_runs`` used to read only the
classic list endpoints, ``GET organizations/{org}/checklists`` and
``GET organizations/{org}/runs``. Those return every template's kickoff fields
and automations, or every process's progress breakdown, whether the caller
wants them or not. Measured on the production demo org on 2026-10-27: one page
of 20 templates was 326,192 bytes from the classic endpoint and 9,403 bytes
from ``POST organizations/{org}/checklists/query``.

This module asks the engine for a short list of columns and maps each row back
onto the keys the tool returned before, with the same values, so a caller sees
the same keys with the bulky ones left out. Every key it writes, and the
api-v2 source each value comes from, is listed beside the mapper below.

When the engine is not usable the tool answers from the classic endpoint
instead. ``QueryEngineUnavailable`` carries the reason, and the caller logs it
once as a warning. That happens on:

* a 404, a 422 or any 5xx from the query route (the owner decision on #1660),
  so a column the production catalog lacks never breaks the tool;
* a 200 whose body is not the documented ``{data: [...], meta: {...}}`` shape;
* a count that is not exact. api-v2 answers an estimate or a lower bound when
  counting takes too long, and ``meta.total`` has always been the real count.

Any other error, 401 and 403 included, propagates, because the classic
endpoint would refuse the same caller for the same reason.

What the engine is NOT used for, and why, is in the docstrings of
``templates_query`` and ``runs_query``: a request is sent to the engine only
when it translates into an engine query with the same rows, in the same order,
with the same values. Anything else goes to the classic endpoint without a
warning, because nothing failed.
"""

import logging
import math
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from tallyfy import TallyfyError

from utils.pagination import shape_page
from utils.sdk_serializer import serialize_dataclass

logger = logging.getLogger(__name__)

#: api-v2 ``App\Query\Limits::PAGE_SIZE_MAX``.
ENGINE_PAGE_SIZE_MAX = 100

#: api-v2 ``App\Query\Limits::LIST_VALUES_MAX``: the most values an ``in``
#: condition takes.
ENGINE_LIST_VALUES_MAX = 100

#: The engine pages with a cursor, not a page number, so page N is reached by
#: reading the ids of the rows before it first, 100 at a time. Past this many
#: rows to skip, the classic endpoint's OFFSET is the cheaper read, so the tool
#: uses it. 1,000 rows is page 51 at 20 a page, and at most 10 id-only calls.
ENGINE_MAX_SKIP_ROWS = 1000

#: api-v2 ``ValueParser::DEFAULT_ID_PATTERN``: the only id shape the engine
#: takes for a template, folder or process id.
_ENGINE_ID = re.compile(r"[0-9a-f]{32}")

#: The answers from the query route that mean "use the classic endpoint".
_FALLBACK_STATUSES = frozenset({404, 422})


class QueryEngineUnavailable(Exception):
    """The query route could not answer this call; use the classic endpoint."""


# ---------------------------------------------------------------------------
# Calling the engine
# ---------------------------------------------------------------------------


def _post(sdk: Any, org_id: str, dataset: str, body: Dict[str, Any]) -> Dict[str, Any]:
    endpoint = f"organizations/{org_id}/{dataset}/query"
    try:
        response = sdk._make_request("POST", endpoint, data=body)
    except TallyfyError as exc:
        status = getattr(exc, "status_code", None)
        if status in _FALLBACK_STATUSES or (isinstance(status, int) and status >= 500):
            raise QueryEngineUnavailable(f"POST {dataset}/query answered {status}") from exc
        raise
    if not isinstance(response, dict):
        raise QueryEngineUnavailable(
            f"POST {dataset}/query returned {type(response).__name__}, not an object"
        )
    data = response.get("data")
    meta = response.get("meta")
    if not isinstance(data, list) or not isinstance(meta, dict):
        raise QueryEngineUnavailable(f"POST {dataset}/query returned no data list or no meta")
    for row in data:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise QueryEngineUnavailable(f"POST {dataset}/query returned a row with no id")
    return response


def _exact_count(meta: Dict[str, Any], dataset: str) -> int:
    count = meta.get("count")
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or meta.get("countIsEstimate") is not False
        or meta.get("countIsAtLeast") is not False
    ):
        raise QueryEngineUnavailable(
            f"POST {dataset}/query gave no exact count (count={count!r}, "
            f"countIsEstimate={meta.get('countIsEstimate')!r}, "
            f"countIsAtLeast={meta.get('countIsAtLeast')!r})"
        )
    return count


def query_page(
    sdk: Any,
    org_id: str,
    dataset: str,
    *,
    columns: List[str],
    sort: List[Dict[str, str]],
    page: int,
    per_page: int,
    filter_tree: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Read page ``page`` of ``per_page`` rows and the exact row count.

    The engine pages with a keyset cursor, so for page N the ids of the
    (N - 1) * per_page rows before it are read first, with no other column, at
    most 100 a call. The first call of every walk asks for ``count: exact``.
    The cursor is valid for the same sort and filter only (api-v2
    ``TasksQueryController`` swagger, ``cursor``), and both stay fixed here.

    Returns the rows exactly as the engine sent them, and the count.
    """
    base: Dict[str, Any] = {"v": 1, "sort": sort}
    if filter_tree:
        base["filter"] = filter_tree

    total: Optional[int] = None
    cursor: Optional[str] = None
    skip = (page - 1) * per_page

    def call(cols: List[str], size: int) -> Dict[str, Any]:
        nonlocal total, cursor
        body = dict(base, columns=cols, page_size=size)
        if cursor:
            body["cursor"] = cursor
        if total is None:
            body["count"] = "exact"
        response = _post(sdk, org_id, dataset, body)
        meta = response["meta"]
        if total is None:
            total = _exact_count(meta, dataset)
        next_cursor = meta.get("nextCursor")
        cursor = next_cursor if meta.get("hasMore") and isinstance(next_cursor, str) else None
        return response

    while skip > 0:
        size = min(skip, ENGINE_PAGE_SIZE_MAX)
        response = call([], size)
        read = len(response["data"])
        if read > size or (read == 0 and cursor):
            # Either would put the next page at the wrong row.
            raise QueryEngineUnavailable(
                f"POST {dataset}/query returned {read} rows for a page of {size}"
            )
        skip -= read
        if cursor is None:
            # The rows ran out before this page starts: it is past the end,
            # which the classic endpoint answers with an empty page too.
            assert total is not None
            return [], total

    response = call(columns, per_page)
    assert total is not None
    if len(response["data"]) > per_page:
        raise QueryEngineUnavailable(
            f"POST {dataset}/query returned {len(response['data'])} rows for a page of {per_page}"
        )
    return list(response["data"]), total


def engine_or_classic(
    tool_name: str,
    engine: Optional[Callable[[], Dict[str, Any]]],
    classic: Callable[[], Dict[str, Any]],
) -> Dict[str, Any]:
    """Answer from the engine when ``engine`` is given and works, else classic.

    ``engine`` is None when the request does not translate exactly, and then
    the classic endpoint answers with nothing logged. When the engine was tried
    and could not answer, one warning names the tool and the reason.
    """
    if engine is not None:
        try:
            return engine()
        except QueryEngineUnavailable as exc:
            logger.warning(
                "%s: the table query engine could not answer (%s); "
                "answered from the classic list endpoint instead",
                tool_name,
                exc,
            )
    return classic()


def engine_listing(
    sdk: Any,
    org_id: str,
    dataset: str,
    *,
    columns: List[str],
    sort: List[Dict[str, str]],
    filter_tree: Optional[Dict[str, Any]],
    row_mapper: Callable[[Dict[str, Any]], Dict[str, Any]],
    page: int,
    per_page: int,
    page_offset: int,
    compact_fields: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """One page of a listing tool's result, read through the engine.

    Same shape as ``fetch_single_page``: the mapped rows go through the same
    ``serialize_dataclass`` (empty values dropped, long strings cut, the
    user-text sanitiser) and the same ``shape_page``.
    """
    rows, total = query_page(
        sdk,
        org_id,
        dataset,
        columns=columns,
        sort=sort,
        page=page,
        per_page=per_page,
        filter_tree=filter_tree,
    )
    total_pages = max(1, math.ceil(total / per_page)) if total else 1
    return shape_page(
        serialize_dataclass([row_mapper(row) for row in rows]),
        total=total,
        total_pages=total_pages,
        page=page,
        page_offset=page_offset,
        compact_fields=compact_fields,
    )


# ---------------------------------------------------------------------------
# Turning a request into an engine query, or deciding it cannot be one
# ---------------------------------------------------------------------------


def _ids(value: Optional[str]) -> Optional[List[str]]:
    """Split a comma list the classic endpoint would split, or None if any part
    is not an id the engine takes (api-v2 ``BaseRepository::filter`` splits
    every filter value on commas before the scope sees it)."""
    if value is None:
        return []
    parts = str(value).split(",")
    if not parts or len(parts) > ENGINE_LIST_VALUES_MAX:
        return None
    if not all(_ENGINE_ID.fullmatch(part) for part in parts):
        return None
    return parts


def _id_condition(column: str, ids: List[str]) -> Dict[str, Any]:
    if len(ids) == 1:
        return {"column": column, "operator": "eq", "value": ids[0]}
    return {"column": column, "operator": "in", "value": ids}


def _and(conditions: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"logic": "and", "filters": conditions}


def _engine_page(page: Optional[int], per_page: int) -> Optional[int]:
    """The page number when the engine can reach it, else None.

    An explicit null page is not guessed at: the classic endpoint answers it
    as it always has.
    """
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        return None
    if (page - 1) * per_page > ENGINE_MAX_SKIP_ROWS:
        return None
    return page


# ---------------------------------------------------------------------------
# Templates: get_all_templates
# ---------------------------------------------------------------------------

#: The columns a get_all_templates row is built from. No form-field column,
#: because api-v2#10699 (the form-field backfill) is not on production.
TEMPLATE_COLUMNS = [
    "title",
    "type",
    "status",
    "folder",
    "starred",
    "is_pinned",
    "explanation_video",
    "steps_count",
    "processes_count",
    "created_by",
    "created_at",
    "last_updated",
    "last_updated_by",
]

#: GET checklists orders by ``is_pinned DESC`` (ChecklistsRepository::isPinned)
#: then by ``checklists.last_updated DESC`` (ChecklistsControllerNew sets
#: defaultSortBy 'updated', and Checklist::scopeUpdated flips ASC to DESC).
#: The engine's ``last_updated`` is the same ``last_updated("checklists")``
#: function (ChecklistsDataset::lastUpdated), and this is also the engine's own
#: default sort. The engine adds the id as a last tie-breaker; the classic
#: query has none, so rows that tie on both keys come back in no defined
#: order there.
TEMPLATE_SORT = [
    {"column": "is_pinned", "dir": "desc"},
    {"column": "last_updated", "dir": "desc"},
]


def templates_query(folder: Optional[str], page: Optional[int], per_page: int) -> Optional[Dict[str, Any]]:
    """The engine query for a get_all_templates call, or None to use classic.

    The rows are the same: ChecklistVisibility calls
    ChecklistsRepository::withPermissions() itself and keeps the same tenant,
    soft-delete and system-template scopes as GET checklists. ``folder`` is the
    only filter the tool sends; Checklist::scopeFolder keeps 32-character ids
    and matches ``folder_id IN (...)``, which is the engine's ``folder`` column
    (``checklists.folder_id``). A folder value with anything but engine ids in
    it stays on the classic endpoint, so its own rules apply.
    """
    engine_page = _engine_page(page, per_page)
    ids = _ids(folder)
    if engine_page is None or ids is None:
        return None
    return {
        "page": engine_page,
        "filter_tree": _id_condition("folder", ids) if ids else None,
        "sort": TEMPLATE_SORT,
    }


def _member_id(value: Any) -> Any:
    """``{id: "12", label: ...}`` -> 12, the integer the classic row carries."""
    if not isinstance(value, dict):
        return None
    raw = value.get("id")
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return raw


def template_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """One engine row as the old get_all_templates row.

    Each key is the one the classic row put there, through the SDK's
    ``Template`` (and, for keys that model does not type, its ``extra``):

    ========================  ==============================================
    key                       engine column (ChecklistsDataset::rows)
    ========================  ==============================================
    id                        id, the timeline id GET checklists returns
    title                     title
    starred                   starred, the viewer's own star
    explanation_video         explanation_video
    created_by                created_by.id as an integer
    started_processes         processes_count ("GET /checklists
                              started_processes")
    created_at, last_updated  created_at, last_updated, both as
                              ``Y-m-d\\TH:i:s\\Z``
    type                      type
    last_updated_by           last_updated_by.id as an integer
    is_pinned                 is_pinned
    extra.steps_count         steps_count ("GET /checklists steps_count")
    extra.status              status
    extra.folder_id           folder.id
    ========================  ==============================================

    Left out, because the engine does not return them: summary (the engine
    has a plain-text preview, not the HTML), prerun, automated_actions, alias,
    icon, owner_id, kickoff_title, kickoff_description and the template's
    settings flags. get_template and get_kickoff_fields return those.
    """
    folder = row.get("folder")
    return {
        "id": row["id"],
        "title": row.get("title"),
        "starred": row.get("starred"),
        "explanation_video": row.get("explanation_video"),
        "created_by": _member_id(row.get("created_by")),
        "started_processes": row.get("processes_count"),
        "created_at": row.get("created_at"),
        "last_updated": row.get("last_updated"),
        "type": row.get("type"),
        "last_updated_by": _member_id(row.get("last_updated_by")),
        "is_pinned": row.get("is_pinned"),
        "extra": {
            "steps_count": row.get("steps_count"),
            "status": row.get("status"),
            "folder_id": folder.get("id") if isinstance(folder, dict) else None,
        },
    }


# ---------------------------------------------------------------------------
# Processes: get_organization_runs
# ---------------------------------------------------------------------------

#: The columns a get_organization_runs row is built from. No form-field column.
RUN_COLUMNS = [
    "name",
    "template",
    "status",
    "type",
    "progress_complete",
    "progress_total",
    "started_by",
    "owner",
    "starred",
    "started_at",
    "last_updated",
    "due",
]

#: The run statuses whose engine filter is GET runs' own: active and complete
#: are the stored status (Run::scopeActive, Run::scopeComplete) and the
#: engine's ``status`` matches the stored status for them; problem is
#: Run::scopeProblem() itself, which RunsDataset::legacyProblem() calls.
#: delayed, improvement and starred are other scopes and stay on classic.
_ENGINE_RUN_STATUSES = frozenset({"active", "complete", "problem"})


def runs_query(
    *,
    with_data: Optional[str],
    form_fields_values: Optional[bool],
    owners: Optional[str],
    task_status: Optional[str],
    groups: Optional[str],
    status: Optional[str],
    folder: Optional[str],
    checklist_id: Optional[str],
    starred: Optional[bool],
    run_type: Optional[str],
    tag: Optional[str],
    sort: Optional[str],
    archived: Optional[str],
    page: Optional[int],
    per_page: int,
) -> Optional[Dict[str, Any]]:
    """The engine query for a get_organization_runs call, or None to use classic.

    Translated, because the engine column was built to match GET runs:

    * ``checklist_id``: Run::scopeChecklist matches ``blueprint_timeline IN``,
      the engine's ``template`` column.
    * ``folder``: Run::scopeFolder reads folder_objects; the engine's
      ``folder`` reads them the same way ("folder in [A, B] is GET
      runs?folder=A,B", RunsDataset notes).
    * ``status`` active, complete or problem: see ``_ENGINE_RUN_STATUSES``.
    * ``archived="only"``: the engine's ``archived eq true`` reads GET
      runs?archived=only.
    * ``sort`` "-created_at" or "created_at": the engine has no created_at
      column, so this sorts on ``started_at``, which Run::boot sets in the
      same ``creating`` hook. Measured on production on 2026-10-27 over all
      153,616 processes: the two differ by at most 1.0 second, so the order
      can differ only between processes started within a second of each
      other. GET runs adds no tie-breaker there, so that order was not
      defined before either.

    Kept on classic: ``with_data`` and ``form_fields_values`` (the caller
    asked for the heavy data), ``owners`` (GET runs matches the owner OR any
    task assignee, which no engine column does), ``task_status``, ``groups``,
    ``tag``, ``run_type`` (Run::scopeType receives the comma-split ARRAY, so
    its exact SQL was not proven equal), ``starred`` (Run::scopeStarred
    ignores the value, so ``starred=false`` lists the STARRED processes there
    and must keep doing so), ``archived="true"`` (live and archived together,
    which the engine cannot read), any other ``sort``, and any status not
    named above.
    """
    engine_page = _engine_page(page, per_page)
    if engine_page is None:
        return None
    if any(v is not None for v in (with_data, form_fields_values, owners, task_status,
                                   groups, starred, run_type, tag)):
        return None
    if sort == "-created_at":
        direction = "desc"
    elif sort == "created_at":
        direction = "asc"
    else:
        return None
    if archived not in (None, "only"):
        return None
    if status is not None and status not in _ENGINE_RUN_STATUSES:
        return None
    template_ids = _ids(checklist_id)
    folder_ids = _ids(folder)
    if template_ids is None or folder_ids is None:
        return None

    conditions: List[Dict[str, Any]] = []
    if archived == "only":
        conditions.append({"column": "archived", "operator": "eq", "value": True})
    if status is not None:
        conditions.append({"column": "status", "operator": "eq", "value": status})
    if template_ids:
        conditions.append(_id_condition("template", template_ids))
    if folder_ids:
        conditions.append(_id_condition("folder", folder_ids))
    return {
        "page": engine_page,
        "filter_tree": _and(conditions),
        "sort": [{"column": "started_at", "dir": direction}],
    }


def _percent(complete: Any, total: Any) -> Any:
    """Run::getProgressAttribute: 100 * complete / total, or 0 with no tasks.

    PHP's json_encode writes a whole float as an integer (50, not 50.0), and
    the classic row carried what it wrote, so a whole result is an int here.
    """
    if not isinstance(complete, int) or not isinstance(total, int):
        return None
    if total <= 0:
        return 0
    value = 100 * (complete / total)
    return int(value) if value.is_integer() else value


def run_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """One engine row as the old get_organization_runs row.

    Each key is the one RunTransformer wrote and the SDK's ``Run`` carried
    (``type`` rides in ``extra`` because ``Run`` does not type it):

    ==================  ===================================================
    key                 engine column (RunsDataset::rows)
    ==================  ===================================================
    id                  id
    checklist_id        template.id (runs.blueprint_timeline)
    checklist_title     template.label, the title of the template version
                        the process was launched from
    name                name
    status              status, the stored runs.status
    progress            progress_complete and progress_total ("GET /runs
                        progress.complete and progress.total"), with
                        percent worked out as Run::getProgressAttribute does
    started_by          started_by.id as an integer (runs.user_id)
    starred             starred, the viewer's own star
    due_date            due ("which GET /runs returns as due_date")
    owner_id            owner.id as an integer
    started_at          started_at
    last_updated        last_updated (runs.updated_at)
    extra.type          type
    ==================  ===================================================

    Left out, because the engine does not return them: increment_id,
    created_at, completed_at, archived_at, summary, whole_progress,
    progress.approved and progress.rejected, the prerun_* keys, late_tasks,
    due_soon, due_date_passed, max_task_deadline and collaborators.
    get_process returns those.
    """
    template = row.get("template")
    complete = row.get("progress_complete")
    total = row.get("progress_total")
    return {
        "id": row["id"],
        "checklist_id": template.get("id") if isinstance(template, dict) else None,
        "checklist_title": template.get("label") if isinstance(template, dict) else None,
        "name": row.get("name"),
        "status": row.get("status"),
        "progress": {
            "complete": complete,
            "total": total,
            "percent": _percent(complete, total),
        },
        "started_by": _member_id(row.get("started_by")),
        "starred": row.get("starred"),
        "due_date": row.get("due"),
        "owner_id": _member_id(row.get("owner")),
        "started_at": row.get("started_at"),
        "last_updated": row.get("last_updated"),
        "extra": {"type": row.get("type")},
    }
