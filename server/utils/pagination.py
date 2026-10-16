"""
Pagination Utility
Single-page fetcher for SDK list methods.
"""

import math
from typing import Any, Callable, Dict, List, Optional

from fastmcp.exceptions import ToolError

from utils.sdk_serializer import (
    MAX_RESULT_BYTES,
    _encoded_len,
    _largest_prefix_that_fits,
    serialize_dataclass,
)
from constants import DEFAULT_PAGE_SIZE


def fetch_single_page(
    sdk_method: Callable,
    *args: Any,
    page: int = 1,
    per_page: int = DEFAULT_PAGE_SIZE,
    compact_fields: Optional[List[str]] = None,
    page_offset: int = 0,
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Fetch a single named page from a paginated SDK method.

    per_page defaults to DEFAULT_PAGE_SIZE (20) so most pages fit within the
    MAX_RESULT_BYTES (25KB) cap. A page of large records can still go over it.
    Then the result keeps as many rows as fit, and ``_truncated`` plus
    ``meta.next_page_offset`` name the call that returns the rest of the same
    page (``page`` unchanged, ``page_offset`` set). Before #1443 the rows past
    the cap were dropped with no way to reach them (#767).

    Args:
        sdk_method: Bound SDK method to call (e.g. sdk.tasks.get_my_tasks)
        *args: Positional arguments forwarded to sdk_method (e.g. org_id)
        page: 1-based page number to fetch (default 1)
        per_page: Items per page (default DEFAULT_PAGE_SIZE)
        compact_fields: Field names to strip from each serialized record.
        page_offset: Rows of this page to skip, as named by a previous call's
            ``meta.next_page_offset`` (default 0).
        **kwargs: Keyword arguments forwarded to sdk_method.

    Returns:
        Dict with:
            data       - serialized list of records for this page
            meta       - dict with total, returned, truncated, page, total_pages,
                         plus page_offset and next_page_offset when they apply
            _truncated - only when rows of this page are left out; says how
                         to fetch them
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

    if page_offset and page_offset >= len(serialized):
        raise ToolError(
            f"page_offset {page_offset} is past the end of page {page}, which has "
            f"{len(serialized)} rows. Use a page_offset below {len(serialized)}, "
            f"or omit it."
        )
    rows = serialized[page_offset:] if page_offset else serialized

    meta: Dict[str, Any] = {
        "total": api_total,
        "returned": len(rows),
        "truncated": False,
        "page": page,
        "total_pages": total_pages,
    }
    if page_offset:
        meta["page_offset"] = page_offset

    def build(n: int) -> Dict[str, Any]:
        result: Dict[str, Any] = {"data": rows[:n], "meta": dict(meta, returned=n)}
        if n < len(rows):
            next_offset = page_offset + n
            result["meta"].update(truncated=True, next_page_offset=next_offset)
            result["_truncated"] = (
                f"Showing rows {page_offset + 1} to {next_offset} of the "
                f"{len(serialized)} on page {page}. The rest of this page is NOT in "
                f"this response. Call again with page={page} and "
                f"page_offset={next_offset} for them."
            )
        return result

    if not rows or _encoded_len(build(len(rows))) <= MAX_RESULT_BYTES:
        return build(len(rows))
    return build(_largest_prefix_that_fits(len(rows), build))
