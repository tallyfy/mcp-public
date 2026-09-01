"""
SDK Response Serializer
Utility functions to serialize SDK dataclass responses to dictionaries,
with automatic compaction to prevent Claude Code CLI's <persisted-output>
truncation on large tool results.

After recursive dataclass serialization, the result also goes through
``utils.response_sanitizer.sanitize_for_user_text`` to translate internal
canonical ``field_type`` codes into friendly display names and drop
internal-only metadata that would otherwise surface to the user (issue
#170). The sanitizer is the LAST pass in the user-visible-text path —
``serialize_dataclass`` and ``sanitize_for_user_text`` are kept as
separate, composable units so the sanitizer can be skipped (via the
``_sanitize=False`` keyword arg) when callers need raw structured data
for tool chaining.
"""

import json
import logging
from typing import Any, Dict, Iterable, List, Optional
from dataclasses import fields, is_dataclass

from utils.response_sanitizer import sanitize_for_user_text

logger = logging.getLogger(__name__)

# Claude Code CLI truncates tool results larger than ~30KB with
# <persisted-output> tags, making the data invisible to the model.
# We target well under that to leave headroom for MCP framing.
MAX_RESULT_BYTES = 25_000

# Per-string cap. DELIBERATELY LEFT AT 500 (issue #982).
#
# Raising it looks like the obvious half of the truncation fix and is not. The
# defect that cost a customer his migrated email bodies was that a cut string
# was INDISTINGUISHABLE from a whole one, not that the cut was at 500. The
# marker below fixes that; the number does not.
#
# Raising it costs delivered items everywhere, because every string competes for
# the same MAX_RESULT_BYTES ceiling. Measured on this branch at 5000: a 50-item
# comment list at 3000 chars each drops from 35 items delivered to 8, and a
# 40-item step list does the same. Fourteen list call sites share this default,
# so a global raise is a 4x cut on thirteen tools that were not the problem.
#
# A cut string is still ADDRESSABLE, because the item carries its id and
# get_template_steps(step_id=..., full_text=True) reads it whole. A dropped item
# is not addressable at all: the caller never learns it exists. That asymmetry
# is the argument #767 makes about pagination, and it is why the escape hatch is
# the right lever here and the default is not.
MAX_STRING_LENGTH = 500

# The literal every truncation marker opens with, exported so a tool DESCRIPTION
# can interpolate it instead of repeating it. A description that tells a model to
# look for "[truncated:" while this module emits something else is worse than no
# advice at all, and nothing would have gone red on the rename.
TRUNCATION_MARKER_PREFIX = "[truncated:"


def _truncation_marker(kept: int, total: int) -> str:
    """The suffix appended to a string this module had to cut.

    It stays a plain ``str`` on purpose: no consumer, test, or
    ``sanitize_for_user_text`` pass has to learn a new type, and the value can
    still be rendered straight into a tool result.

    It says the total length because "some of this is missing" is not actionable
    on its own, and it says do-not-write-it-back because that is the failure this
    marker exists to stop. The previous marker was ``"..."``, which is
    indistinguishable from an ellipsis the author typed, so a model round-tripping
    a description had no way to know it was overwriting the rest of it.
    """
    return (
        f" {TRUNCATION_MARKER_PREFIX} this is the first {kept} of {total} characters, "
        "NOT the full value. Do not write it back, you would overwrite the rest.]"
    )


def _is_empty(value: Any) -> bool:
    """Return True for values that carry no useful information."""
    if value is None:
        return True
    if isinstance(value, (str, list, dict)) and len(value) == 0:
        return True
    return False


def _serialize_inner(obj: Any, max_string_length: Optional[int] = MAX_STRING_LENGTH) -> Any:
    """Recursive serialize without the user-text sanitisation pass.

    Internal helper used by ``serialize_dataclass`` so the sanitiser
    only runs once, at the top of the recursion (instead of on every
    nested level).

    ``max_string_length`` is threaded through every level so a caller can ask
    for one whole object uncut (``None``) without the cap reappearing on a
    nested field. ``None`` means no cap at all.
    """
    if obj is None:
        return None

    if is_dataclass(obj) and not isinstance(obj, type):
        result = {}
        for field in fields(obj):
            value = getattr(obj, field.name)
            serialized = _serialize_inner(value, max_string_length)
            if not _is_empty(serialized):
                result[field.name] = serialized
        return result

    elif isinstance(obj, list):
        return [_serialize_inner(item, max_string_length) for item in obj]

    elif isinstance(obj, dict):
        compacted = {}
        for key, value in obj.items():
            serialized = _serialize_inner(value, max_string_length)
            if not _is_empty(serialized):
                compacted[key] = serialized
        return compacted

    elif (
        isinstance(obj, str)
        and max_string_length is not None
        and len(obj) > max_string_length
    ):
        return obj[:max_string_length] + _truncation_marker(max_string_length, len(obj))

    else:
        # Primitive type (str, int, float, bool, etc.)
        return obj


def serialize_dataclass(
    obj: Any,
    *,
    _sanitize: bool = True,
    max_string_length: Optional[int] = MAX_STRING_LENGTH,
) -> Any:
    """
    Recursively serialize a dataclass object to a dictionary.
    Strips null/empty fields to reduce payload size, then applies the
    user-visible-text sanitiser (issue #170) which translates internal
    ``field_type`` codes to display names and drops internal-only
    metadata keys (e.g. ``stored_as``, ``field_type_internal``).

    Args:
        obj: Object to serialize (dataclass, list, dict, or primitive)
        _sanitize: If False, skip the user-visible-text sanitisation
            pass and return the raw serialised structure. Default True.
            Used internally by other helpers to avoid double-passes.
        max_string_length: Per-string cap. ``None`` returns every string
            whole, which is only safe for ONE object and only when the
            caller then bounds the result (see ``window_longest_text``):
            an uncapped serialisation can exceed MAX_RESULT_BYTES, and
            the client cuts anything over that silently.

    Returns:
        Serialized representation (dict, list, or primitive). When
        ``_sanitize=True`` (default), the structure is safe to drop into
        ``ToolResult.content`` for the user-visible text path.
    """
    serialized = _serialize_inner(obj, max_string_length)
    if _sanitize:
        return sanitize_for_user_text(serialized)
    return serialized


def unwrap_fractal(response: Any, includes: Iterable[str] = ()) -> Dict[str, Any]:
    """Unwrap a RAW api-v2 body and its named Fractal includes, keeping every key.

    This is the shape half of the "read the raw body, not the SDK dataclass"
    pattern. It does no serialising: the caller decides which serialiser to
    apply, because a task strips noise fields a template does not.

    **Why raw bodies are read at all.** Every list model in ``tallyfy.models``
    is a fixed allowlist. ``Model.from_dict`` copies the attributes it declares
    and drops the rest of the body on the floor, with no error and no log line.
    Measured 2026-09-01 against the pinned SDK: ``Task`` declares 43 fields and
    none of them is ``summary``, ``original_summary`` or ``top_secret``;
    ``Template`` declares 43 and none of them is ``tags``; ``GuestDetails``
    reads ``disabled_on``, a key the guest route has never emitted (it exists
    on the organization-members pivot, a different resource), while the key that
    actually carries a guest's live state is ``disabled_at``. So a field can be
    requested on the wire, returned by api-v2, and still never reach the caller.
    That is `tallyfy/sdk#33`, and until it ships this is the way round it.

    Two envelope shapes are handled, both seen in real responses:

    * the outer Fractal envelope, ``{"data": {...}}``, unwrapped to the object;
    * a named include, which arrives Fractal-wrapped a SECOND time as
      ``{"tags": {"data": [...]}}``, unwrapped to a plain list so callers do
      not each have to know that api-v2 double-wraps an include.

    An include that is absent is left absent rather than materialised as ``[]``,
    because "not requested" and "requested and empty" are different answers and
    a caller verifying a write needs to tell them apart.

    Anything that is not a dict yields ``{}``, matching the established
    sentinel: ``ToolResult`` requires non-None content and an empty payload is
    how this server says "nothing came back".
    """
    raw = response.get("data", response) if isinstance(response, dict) else response
    if not isinstance(raw, dict):
        return {}

    raw = dict(raw)
    for key in includes:
        wrapped = raw.get(key)
        if isinstance(wrapped, dict) and "data" in wrapped:
            raw[key] = wrapped.get("data") or []

    return raw


def compact_dict_list_field(
    result: Dict[str, Any],
    list_key: str,
    *,
    item_label: str = "items",
) -> Dict[str, Any]:
    """
    Trim ``result[list_key]`` until the whole dict encodes under MAX_RESULT_BYTES.

    Returns ``result`` unchanged when it already fits, when ``list_key`` is
    missing or is not a list, or when the list is empty (nothing to trim, so a
    ``_truncated`` marker would be a false statement).

    Otherwise a binary search finds the longest prefix that fits WITH the
    marker included in every trial, and the result gains
    ``_truncated: "Showing N of M <item_label>"``. Every other key is preserved
    verbatim, which is what lets a caller keep a true total (e.g. the fan-out's
    ``task_count``) alongside a trimmed list.

    This is the ONE implementation. It exists because the same binary search was
    written a third time inline in ``tools/task_management.py``; see rule 16 in
    the repo CLAUDE.md, where four copies of an option normalizer drifted apart
    and the durable fix was a shared helper rather than four correct copies.
    """
    if not isinstance(result, dict):
        return result

    items = result.get(list_key)
    if not isinstance(items, list) or not items:
        return result

    if len(json.dumps(result, separators=(",", ":"), default=str)) <= MAX_RESULT_BYTES:
        return result

    total_count = len(items)
    lo, hi = 1, total_count
    while lo < hi:
        mid = (lo + hi + 1) // 2
        trial = {
            **result,
            list_key: items[:mid],
            "_truncated": f"Showing {mid} of {total_count} {item_label}",
        }
        if len(json.dumps(trial, separators=(",", ":"), default=str)) <= MAX_RESULT_BYTES:
            lo = mid
        else:
            hi = mid - 1

    logger.info("Compacted %s: %d -> %d %s", list_key, total_count, lo, item_label)
    return {
        **result,
        list_key: items[:lo],
        "_truncated": f"Showing {lo} of {total_count} {item_label}",
    }


def window_longest_text(
    obj: Dict[str, Any],
    *,
    offset: int = 0,
    max_bytes: int = MAX_RESULT_BYTES,
) -> Dict[str, Any]:
    """Return ``obj`` with its single longest string windowed to fit ``max_bytes``.

    This exists because "return the whole value" is not achievable in one
    response and pretending otherwise is the defect this module is being fixed
    for. An uncapped serialisation of one step with a 60,000-character summary
    encodes to about 60KB. That is 2.4x MAX_RESULT_BYTES and well past the ~30KB
    at which the CLI cuts a tool result, so the caller receives a truncated
    payload with nothing saying so. ``compact_result`` cannot help: with a single
    item its binary search bottoms out at 1 and it labels the result
    ``"Showing 1 of 1 items"``, which reads as complete.

    So the whole value is delivered ACROSS CALLS instead of pretending to fit in
    one. The window that was actually returned is named in the text itself,
    along with the exact offset to ask for next, so a caller can reassemble the
    value and can tell when it is done.

    Returns ``obj`` unchanged when it already fits and ``offset`` is 0.
    """
    if not isinstance(obj, dict):
        return obj

    def encoded_len(candidate: Any) -> int:
        """Measured with DEFAULT separators on purpose: that is the widest of the
        two encodings in use here, so a window that fits under it also fits under
        the compact one. Measuring the compact form instead put a result 25 bytes
        over the ceiling, because ", " and ": " cost two bytes per key more.
        """
        return len(json.dumps(candidate, default=str))

    if offset == 0 and encoded_len(obj) <= max_bytes:
        return obj

    key = max(
        (k for k, v in obj.items() if isinstance(v, str)),
        key=lambda k: len(obj[k]),
        default=None,
    )
    if key is None:
        # Nothing string-shaped to window. Say so rather than returning an
        # oversize payload that reads as complete.
        return {**obj, "_truncated": (
            f"This result is {encoded_len(obj)} bytes, over the {max_bytes}-byte "
            "limit, and has no single long text field to split. It WILL be cut "
            "before you see all of it."
        )}

    whole = obj[key]
    total = len(whole)
    if offset >= total:
        raise ValueError(
            f"text_offset {offset} is past the end of '{key}', which is {total} "
            f"characters. The last valid offset is {total - 1}."
        )

    remaining = whole[offset:]

    # Binary search the largest window that still fits WITH its marker, since the
    # marker's own length depends on the numbers inside it and JSON escaping can
    # make a character cost more than a byte.
    def build(keep: int, base: Dict[str, Any]) -> Dict[str, Any]:
        end = offset + keep
        if offset == 0 and end >= total:
            return {**base, key: whole}
        marker = (
            f" {TRUNCATION_MARKER_PREFIX} this is characters {offset} to {end} "
            f"of {total} of '{key}', NOT the full value. Do not write it back. "
        )
        marker += (
            f"Call the same tool again with text_offset={end} for the next part.]"
            if end < total else "This is the LAST part.]"
        )
        return {**base, key: remaining[:keep] + marker}

    def largest_window_that_fits(base: Dict[str, Any]) -> Dict[str, Any]:
        """Binary search the biggest window that still fits WITH its marker.

        The marker's own length depends on the numbers inside it and JSON
        escaping can make a character cost more than a byte, so every trial is
        measured rather than computed.
        """
        lo, hi = 1, len(remaining)
        if encoded_len(build(hi, base)) <= max_bytes:
            return build(hi, base)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if encoded_len(build(mid, base)) <= max_bytes:
                lo = mid
            else:
                hi = mid - 1
        return build(lo, base)

    windowed = largest_window_that_fits(obj)
    if encoded_len(windowed) <= max_bytes:
        logger.info(
            "Windowed '%s': offset %d, kept %d of %d chars",
            key, offset, len(windowed[key]), total,
        )
        return windowed

    # STILL over with the window at its smallest, so the weight is NOT in the
    # field being windowed and no window size can fix it. Windowing one string
    # only bounds a payload whose other values are small; a step carrying a long
    # summary AND eight long form-field defaults is over the ceiling before the
    # summary is considered at all. Returning the smallest window unchecked here
    # handed back a 73,915-byte result against a 25,000-byte ceiling, which the
    # client cuts with nothing saying so -- the exact silence this module is
    # being fixed for, one level up from the string case.
    #
    # So drop the heaviest siblings, largest first, until it fits, and NAME every
    # one that was dropped. A named absence can be asked for again; a silent cut
    # cannot.
    reduced = dict(obj)
    withheld: List[str] = []
    heavies = sorted(
        ((k, encoded_len(v)) for k, v in obj.items() if k != key),
        key=lambda kv: kv[1],
        reverse=True,
    )
    for name, size in heavies:
        reduced.pop(name, None)
        withheld.append(f"'{name}' ({size} bytes)")
        candidate = largest_window_that_fits(
            {**reduced, "_withheld": (
                "Removed to fit the "
                f"{max_bytes}-byte response limit and NOT returned: "
                + ", ".join(withheld)
                + ". Ask for these on their own; they were not empty."
            )}
        )
        if encoded_len(candidate) <= max_bytes:
            logger.info(
                "Windowed '%s' and withheld %s to fit %d bytes",
                key, ", ".join(withheld), max_bytes,
            )
            return candidate

    # Nothing left to withhold. Say so rather than returning an oversize payload
    # that reads as complete.
    return {**windowed, "_truncated": (
        f"This result is {encoded_len(windowed)} bytes even with '{key}' cut to "
        f"its shortest and every other field removed, over the {max_bytes}-byte "
        "limit. It WILL be cut before you see all of it."
    )}


def compact_result(result: Any) -> Any:
    """
    Ensure a serialized tool result stays under the CLI size limit.

    If the JSON-encoded result exceeds MAX_RESULT_BYTES, progressively
    truncate: first trim list items, then strip more fields.
    """
    encoded = json.dumps(result, separators=(",", ":"), default=str)
    if len(encoded) <= MAX_RESULT_BYTES:
        return result

    # For dict results with a "data" list, trim the list
    if isinstance(result, dict) and "data" in result and isinstance(result["data"], list):
        return compact_dict_list_field(result, "data")

    # For plain lists, trim and surface a truncation marker so the LLM
    # knows the list was capped (issue #222). The marker has to fit
    # alongside the data — the binary search includes it in every trial.
    if isinstance(result, list):
        total_count = len(result)
        lo, hi = 1, total_count
        while lo < hi:
            mid = (lo + hi + 1) // 2
            trial = {
                "data": result[:mid],
                "_truncated": f"Showing {mid} of {total_count} items",
            }
            if len(json.dumps(trial, separators=(",", ":"), default=str)) <= MAX_RESULT_BYTES:
                lo = mid
            else:
                hi = mid - 1
        logger.info(f"Compacted list result: {total_count} → {lo} items")
        return {
            "data": result[:lo],
            "_truncated": f"Showing {lo} of {total_count} items",
        }

    # Last resort for non-list large results (single object)
    logger.warning(f"Result too large ({len(encoded)} bytes) and not a list — cannot auto-compact")
    return result


def compact_search_all_buckets(
    buckets: Dict[str, Dict[str, Any]],
    max_total_bytes: int = MAX_RESULT_BYTES,
) -> Dict[str, Dict[str, Any]]:
    """
    Compact a search_all-style ``{type: {data: [...], meta: {...}}}`` dict so
    the aggregate JSON encoding fits under ``max_total_bytes``.

    Each non-empty bucket gets an equal share of the byte budget; buckets that
    fit are passed through. Trimmed buckets gain a ``_truncated`` marker so the
    LLM knows the list was capped (issue #230).
    """
    encoded = json.dumps(buckets, separators=(",", ":"), default=str)
    if len(encoded) <= max_total_bytes:
        return buckets

    populated = [
        name for name, bucket in buckets.items()
        if isinstance(bucket, dict)
        and isinstance(bucket.get("data"), list)
        and bucket["data"]
    ]
    n = max(1, len(populated))
    budget_per_bucket = max_total_bytes // n

    out: Dict[str, Dict[str, Any]] = {}
    for type_name, bucket in buckets.items():
        if not isinstance(bucket, dict):
            out[type_name] = bucket
            continue
        data = bucket.get("data")
        if not isinstance(data, list) or not data:
            out[type_name] = bucket
            continue

        total = len(data)
        lo, hi = 1, total
        while lo < hi:
            mid = (lo + hi + 1) // 2
            trial = {**bucket, "data": data[:mid]}
            if mid < total:
                trial["_truncated"] = f"Showing {mid} of {total} items"
            size = len(json.dumps(trial, separators=(",", ":"), default=str))
            if size <= budget_per_bucket:
                lo = mid
            else:
                hi = mid - 1

        kept = lo
        new_bucket = {**bucket, "data": data[:kept]}
        if kept < total:
            new_bucket["_truncated"] = f"Showing {kept} of {total} items"
            logger.info(f"Compacted search_all bucket '{type_name}': {total} → {kept} items")
        out[type_name] = new_bucket

    return out


def serialize_paginated_response(response_obj: Any) -> Dict[str, Any]:
    """
    Serialize a paginated SDK response (UsersList, TasksList, RunsList, etc.)
    to a dictionary with 'data', 'meta', and 'count' keys.
    Applies compaction to stay under the CLI size limit.

    Args:
        response_obj: Paginated response object with .data and .meta attributes,
                     or a plain list for backward compatibility with tests

    Returns:
        Dictionary with 'data' (list of items), 'meta' (pagination info), and 'count' (total count)
    """
    if response_obj is None:
        return {"data": [], "count": 0}

    # Handle plain lists (for test backward compatibility)
    if isinstance(response_obj, list):
        result = {
            "data": serialize_dataclass(response_obj),
            "count": len(response_obj)
        }
        return compact_result(result)

    # Handle structured SDK response objects with .data attribute
    count = response_obj.count if hasattr(response_obj, 'count') else len(response_obj.data) if hasattr(response_obj, 'data') else 0

    result = {
        "data": serialize_dataclass(response_obj.data),
        "count": count
    }
    meta = serialize_dataclass(response_obj.meta) if hasattr(response_obj, 'meta') else None
    if not _is_empty(meta):
        result["meta"] = meta
    return compact_result(result)


# Task fields that are always present but carry no actionable information for Claude.
# These are internal platform flags, duplicates, or fields that are only meaningful
# when non-default (and are already implied by other fields when they are non-default).
#
# `is_soft_start_date` was in this set and is deliberately NOT any more. It is not an
# internal flag: it is the user-facing "Start anytime" / "Start on-time" toggle
# (client-v2 features/steps/.../step-deadline.component.html).
#
# What made stripping it misleading, read from api-v2 TaskTransformer.php: a task emits
# `started_at` (line 61), a concrete resolved timestamp, ALONGSIDE `is_soft_start_date`
# (line 90). `started_at` is NOT in this set, so it survived while its gate did not, and
# `is_soft_start_date` is exactly what says whether that timestamp BINDS ("Start
# on-time", not workable before it) or is advisory ("Start anytime").
#
# It compounds a default. A step is born with start_date {"unit":"hours","value":2} AND
# is_soft_start_date true, both set by one api-v2 migration
# (2022_02_24_125353_change_default_value_of_start_date_in_steps_table.php), so the two
# hours is inert by construction and every task inherits a started_at about two hours
# out. Shown that timestamp and not the flag, a reader concludes the task starts in two
# hours when in fact it starts whenever.
#
# NOTE the field is `started_at` on a task, not `start_date`. Template STEPS carry
# `start_date` and do not pass through this set at all: get_template_steps serialises
# them with `serialize_dataclass`, which never consults _TASK_NOISE_FIELDS.
_TASK_NOISE_FIELDS = frozenset({
    "allow_guest_owners",           # always false on one-off tasks
    "is_completable",               # always true for visible tasks
    "status_label",                 # duplicate of status
    "has_deadline_dependent_child_tasks",  # internal dependency flag
    "can_complete_only_assignees",  # internal flag
    "everyone_must_complete",       # only relevant for template steps
})


def serialize_task(task: Any) -> Dict[str, Any]:
    """
    Serialize a Task dataclass to a dictionary, stripping noise fields that
    carry no actionable information for Claude.

    Args:
        task: Task dataclass object

    Returns:
        Compacted dictionary with noise fields removed
    """
    result = serialize_dataclass(task)
    if isinstance(result, dict):
        for field in _TASK_NOISE_FIELDS:
            result.pop(field, None)
    return result


def serialize_search_response(response_obj: Any) -> Dict[str, Any]:
    """
    Serialize a search SDK response (SearchResultsList) to a dictionary.
    Applies compaction to stay under the CLI size limit.

    Args:
        response_obj: SearchResultsList object with .data, .meta, and .search_type,
                     or a plain list for backward compatibility with tests

    Returns:
        Dictionary with search results, metadata, and count
    """
    if response_obj is None:
        return {"data": [], "count": 0}

    # Handle plain lists (for test backward compatibility)
    if isinstance(response_obj, list):
        result = {
            "data": serialize_dataclass(response_obj),
            "count": len(response_obj)
        }
        return compact_result(result)

    # Handle structured SDK response objects
    count = response_obj.count if hasattr(response_obj, 'count') else len(response_obj.data) if hasattr(response_obj, 'data') else 0

    result = {
        "data": serialize_dataclass(response_obj.data),
        "count": count
    }
    meta = serialize_dataclass(response_obj.meta) if hasattr(response_obj, 'meta') else None
    if not _is_empty(meta):
        result["meta"] = meta
    search_type = getattr(response_obj, 'search_type', None)
    if search_type:
        result["search_type"] = search_type
    return compact_result(result)


