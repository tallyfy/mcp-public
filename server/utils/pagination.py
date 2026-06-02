"""
Pagination Utility
Single-page fetcher for SDK list methods.
"""

import json
import math
from typing import Any, Callable, Dict, List, Optional
from utils.sdk_serializer import serialize_dataclass
from constants import DEFAULT_PAGE_SIZE, MAX_RESULT_SIZE_CHARS


def fetch_single_page(
    sdk_method: Callable,
    *args: Any,
    page: int = 1,
    per_page: int = DEFAULT_PAGE_SIZE,
    compact_fields: Optional[List[str]] = None,
    max_result_chars: int = MAX_RESULT_SIZE_CHARS,
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Fetch a single named page from a paginated SDK method.

    per_page defaults to DEFAULT_PAGE_SIZE (20) so each page fits comfortably
    within the MAX_RESULT_SIZE_CHARS (25KB) cap. With per_page=100 the API
    returns all items in one page, which our size cap then truncates — leaving
    remaining items unreachable.

    Args:
        sdk_method: Bound SDK method to call (e.g. sdk.tasks.get_my_tasks)
        *args: Positional arguments forwarded to sdk_method (e.g. org_id)
        page: 1-based page number to fetch (default 1)
        per_page: Items per page (default DEFAULT_PAGE_SIZE)
        compact_fields: Field names to strip from each serialized record.
        max_result_chars: Safety cap on serialized JSON size (default MAX_RESULT_SIZE_CHARS).
        **kwargs: Keyword arguments forwarded to sdk_method.

    Returns:
        Dict with:
            data       - serialized list of records for this page
            meta       - dict with total, returned, truncated, page, total_pages
    """
    result = sdk_method(*args, page=page, per_page=per_page, **kwargs)

    page_items = result.data if hasattr(result, "data") else []
    meta = result.meta if hasattr(result, "meta") else None

    api_total = meta.total if meta else len(page_items)
    api_total_pages = getattr(meta, "total_pages", None) if meta else None

    # When the API used its own (smaller) per_page instead of ours, its
    # total_pages is already computed correctly for that page size — trust it.
    # This happens on endpoints like guest tasks that ignore our per_page=20
    # and use a fixed 10. Detecting condition: we got fewer items than we asked
    # for but there are still more items total (i.e. the API paginated, just
    # with a smaller page size than ours).
    if page_items and len(page_items) < per_page and api_total > len(page_items):
        total_pages = (
            api_total_pages
            if api_total_pages
            else max(1, math.ceil(api_total / len(page_items)))
        )
    else:
        # API honored our per_page (or returned everything in one shot).
        # Calculate total_pages locally — the API's own total_pages uses its
        # internal default (often 100), not the per_page=20 we passed, so it
        # would always return 1 for large lists fetched in 20-item chunks.
        total_pages = max(1, math.ceil(api_total / per_page)) if api_total else 1

    serialized = serialize_dataclass(page_items)

    if compact_fields:
        for record in serialized:
            if isinstance(record, dict):
                for field in compact_fields:
                    record.pop(field, None)

    truncated = False
    result_json = json.dumps(serialized, default=str)
    if len(result_json) > max_result_chars and serialized:
        truncated = True
        avg_chars = len(result_json) / len(serialized)
        keep = max(1, int(max_result_chars / avg_chars * 0.9))
        serialized = serialized[:keep]
        result_json = json.dumps(serialized, default=str)
        while serialized and len(result_json) > max_result_chars:
            serialized.pop()
            result_json = json.dumps(serialized, default=str)

    return {
        "data": serialized,
        "meta": {
            "total": api_total,
            "returned": len(serialized),
            "truncated": truncated,
            "page": page,
            "total_pages": total_pages,
        },
    }
