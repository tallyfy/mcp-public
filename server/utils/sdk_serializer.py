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
from typing import Any, Dict, List
from dataclasses import fields, is_dataclass

from utils.response_sanitizer import sanitize_for_user_text

logger = logging.getLogger(__name__)

# Claude Code CLI truncates tool results larger than ~30KB with
# <persisted-output> tags, making the data invisible to the model.
# We target well under that to leave headroom for MCP framing.
MAX_RESULT_BYTES = 25_000
MAX_STRING_LENGTH = 500


def _is_empty(value: Any) -> bool:
    """Return True for values that carry no useful information."""
    if value is None:
        return True
    if isinstance(value, (str, list, dict)) and len(value) == 0:
        return True
    return False


def _serialize_inner(obj: Any) -> Any:
    """Recursive serialize without the user-text sanitisation pass.

    Internal helper used by ``serialize_dataclass`` so the sanitiser
    only runs once, at the top of the recursion (instead of on every
    nested level).
    """
    if obj is None:
        return None

    if is_dataclass(obj) and not isinstance(obj, type):
        result = {}
        for field in fields(obj):
            value = getattr(obj, field.name)
            serialized = _serialize_inner(value)
            if not _is_empty(serialized):
                result[field.name] = serialized
        return result

    elif isinstance(obj, list):
        return [_serialize_inner(item) for item in obj]

    elif isinstance(obj, dict):
        compacted = {}
        for key, value in obj.items():
            serialized = _serialize_inner(value)
            if not _is_empty(serialized):
                compacted[key] = serialized
        return compacted

    elif isinstance(obj, str) and len(obj) > MAX_STRING_LENGTH:
        return obj[:MAX_STRING_LENGTH] + "..."

    else:
        # Primitive type (str, int, float, bool, etc.)
        return obj


def serialize_dataclass(obj: Any, *, _sanitize: bool = True) -> Any:
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

    Returns:
        Serialized representation (dict, list, or primitive). When
        ``_sanitize=True`` (default), the structure is safe to drop into
        ``ToolResult.content`` for the user-visible text path.
    """
    serialized = _serialize_inner(obj)
    if _sanitize:
        return sanitize_for_user_text(serialized)
    return serialized


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
        data = result["data"]
        total_count = len(data)
        # Binary search for the max items that fit
        lo, hi = 1, total_count
        while lo < hi:
            mid = (lo + hi + 1) // 2
            trial = {**result, "data": data[:mid], "_truncated": f"Showing {mid} of {total_count} items"}
            if len(json.dumps(trial, separators=(",", ":"), default=str)) <= MAX_RESULT_BYTES:
                lo = mid
            else:
                hi = mid - 1
        result = {**result, "data": data[:lo], "_truncated": f"Showing {lo} of {total_count} items"}
        logger.info(f"Compacted list result: {total_count} → {lo} items")
        return result

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
_TASK_NOISE_FIELDS = frozenset({
    "allow_guest_owners",           # always false on one-off tasks
    "is_completable",               # always true for visible tasks
    "status_label",                 # duplicate of status
    "has_deadline_dependent_child_tasks",  # internal dependency flag
    "can_complete_only_assignees",  # internal flag
    "is_soft_start_date",           # internal scheduling flag
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


