"""
Search Tools
Tools for searching tasks, processes, templates, and snippets
"""

import logging
from typing import Annotated, Any, Dict, List, Optional

from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
from pydantic import Field
from tallyfy import TallyfySDK
from mcp.types import ToolAnnotations
from utils.fastmcp_errors import handle_tallyfy_errors
from utils.auth_context import get_authenticated_credentials, TALLYFY_API_BASE_URL
from utils.fastmcp_types import SearchQuery, PageNumber, PageSize, OptionalBool
from utils.pagination import fetch_single_page
from utils.sdk_serializer import serialize_dataclass, compact_search_all_buckets
from metrics import track_tool_execution

logger = logging.getLogger(__name__)

# Valid types for search_all
VALID_SEARCH_TYPES = {"blueprint", "process", "task", "snippet", "capture", "step"}
DEFAULT_SEARCH_TYPES = ["blueprint", "process", "task", "snippet"]

# Issue #157: Tallyfy's search endpoint /api/organizations/{org_id}/search?on=task
# does NOT return run_id for any task — neither one-off nor process-bound. Without
# run_id, an LLM caller has no way to identify which process a task belongs to,
# forcing extra search_for_processes + get_tasks_for_process round-trips.
#
# Workaround: for each process task (is_oneoff_task=False) lacking run_id, call
# sdk.tasks.get_standalone_task(task_id) — that endpoint returns run_id even for
# tasks that live inside a process. Bound the fan-out at this many fill-ins per
# page to keep latency predictable; LLM is signaled via a meta flag when capped.
RUN_ID_FILLIN_CAP = 25


def _fill_in_run_ids(
    data: List[Dict[str, Any]],
    sdk: Any,
    org_id: str,
) -> bool:
    """
    Patch run_id onto process-task results that lack it.

    Mutates ``data`` in place. Returns True if the fill-in had to be capped
    (i.e. more than RUN_ID_FILLIN_CAP process tasks needed enrichment).

    Skips:
      - one-off tasks (is_oneoff_task=True) — they have no parent run by definition
      - results that already carry a non-empty run_id (future-proof against API
        eventually returning the field natively)
      - non-dict entries (defensive)

    Failures from individual SDK calls are logged and swallowed so that one
    bad task does not abort the whole page.
    """
    if not isinstance(data, list):
        return False

    needs_fillin: List[Dict[str, Any]] = [
        item
        for item in data
        if isinstance(item, dict)
        and item.get("is_oneoff_task") is False
        and not item.get("run_id")
        and item.get("id")
    ]

    if not needs_fillin:
        return False

    capped = len(needs_fillin) > RUN_ID_FILLIN_CAP
    targets = needs_fillin[:RUN_ID_FILLIN_CAP]

    for item in targets:
        task_id = item["id"]
        try:
            task = sdk.tasks.get_standalone_task(org_id, task_id)
            run_id = getattr(task, "run_id", None) if task is not None else None
            if run_id:
                item["run_id"] = run_id
        except Exception as exc:
            # One bad task should not poison the whole page. Log and move on.
            logger.warning(
                "search_for_tasks: run_id fill-in failed for task %s: %s",
                task_id,
                exc,
            )

    return capped

SearchTypesList = Annotated[Optional[List[str]], Field(
    description=(
        "Which entity types to search. "
        "Valid values: blueprint, process, task, snippet, capture, step. "
        "Defaults to all four main types if omitted."
    ),
    examples=[["blueprint", "process", "task", "snippet"]],
)]


def register_search_tools(mcp):
    """Register all search tools with the MCP server"""

    @mcp.tool(
        name="search_for_tasks",
        description="""Search for tasks in the organization by keyword.

MANDATORY: You MUST provide a 'query' parameter. Calling with empty/missing query WILL FAIL.

CORRECT usage examples:
- search_for_tasks(query="urgent") - search for urgent tasks
- search_for_tasks(query="review document") - search for document review tasks
- search_for_tasks(query="approval") - search for approval-related tasks

WRONG usage (will fail):
- search_for_tasks() - NO! Missing required query parameter
- search_for_tasks(query="") - NO! Empty query not allowed

Extract search terms from user's request. If user says "find my tasks about reports", use query="reports".

PAGINATION: Returns 20 results per page. Use page=2, page=3, etc. for more. meta.total_pages shows total page count.""",
        tags={"search", "discovery", "tasks", "read-only"},
        annotations=ToolAnnotations(
            title="Search for tasks",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("search_for_tasks")
    @handle_tallyfy_errors("search for tasks")
    def search_for_tasks(
        query: SearchQuery,
        page: PageNumber = 1,
        per_page: PageSize = 20,
        tags: OptionalBool = False,
    ) -> ToolResult:
        """
        Search for tasks in the organization.

        Args:
            query: Search query string (REQUIRED - must not be empty)
            page: Page number (default: 1)
            per_page: Results per page (default: 20, max: 100)
            tags: Include tags in results (default: False)

        Returns:
            Dict with 'data' (list of tasks) and 'meta' (total, returned,
            truncated, page, total_pages). For process tasks
            (is_oneoff_task=False), run_id is filled in via a follow-up
            get_standalone_task call (see issue #157) so callers can resolve
            the parent process without extra round-trips. Capped at
            RUN_ID_FILLIN_CAP fill-ins per page; meta._run_id_fillin_capped
            signals when the cap was hit.
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            page_payload = fetch_single_page(
                sdk.searches.search_tasks,
                org_id, query,
                page=page, per_page=per_page, tags=tags,
            )
            # Issue #157: search API omits run_id; back-fill from
            # get_standalone_task for process tasks so the LLM can identify
            # the parent process without extra search_for_processes calls.
            capped = _fill_in_run_ids(page_payload.get("data", []), sdk, org_id)
            if capped:
                meta = page_payload.setdefault("meta", {})
                if isinstance(meta, dict):
                    meta["_run_id_fillin_capped"] = True
            return ToolResult(
                content=page_payload,
                structured_content=None
            )

    @mcp.tool(
        name="search_for_processes",
        description="""Search for workflow processes (runs) in the organization by keyword.

MANDATORY: You MUST provide a 'query' parameter. Calling with empty/missing query WILL FAIL.

CORRECT usage examples:
- search_for_processes(query="onboarding") - search for onboarding processes
- search_for_processes(query="John Doe") - search for processes related to John Doe
- search_for_processes(query="Q4 report") - search for Q4 report processes

WRONG usage (will fail):
- search_for_processes() - NO! Missing required query parameter
- search_for_processes(query="") - NO! Empty query not allowed

Extract search terms from user's request. If user says "find the hiring process for Sarah", use query="hiring Sarah".

PAGINATION: Returns 20 results per page. Use page=2, page=3, etc. for more. meta.total_pages shows total page count.""",
        tags={"search", "discovery", "processes", "workflow", "read-only"},
        annotations=ToolAnnotations(
            title="Search for processes",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("search_for_processes")
    @handle_tallyfy_errors("search for processes")
    def search_for_processes(
        query: SearchQuery,
        page: PageNumber = 1,
        per_page: PageSize = 20,
        tags: OptionalBool = False,
    ) -> ToolResult:
        """
        Search for processes in the organization.

        Args:
            query: Search query string (REQUIRED - must not be empty)
            page: Page number (default: 1)
            per_page: Results per page (default: 20, max: 100)
            tags: Include tags in results (default: False)

        Returns:
            Dict with 'data' (list of matching processes) and 'meta' (total, returned, truncated, page, total_pages)
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            return ToolResult(
                content=fetch_single_page(
                    sdk.searches.search_processes,
                    org_id, query,
                    page=page, per_page=per_page, tags=tags,
                ),
                structured_content=None,
            )

    @mcp.tool(
        name="search_for_templates",
        description="""Search for workflow templates (blueprints) in the organization by keyword.

MANDATORY: You MUST provide a 'query' parameter. Calling with empty/missing query WILL FAIL.

CORRECT usage examples:
- search_for_templates(query="onboarding") - search for onboarding templates
- search_for_templates(query="HR") - search for HR-related templates
- search_for_templates(query="approval workflow") - search for approval templates

WRONG usage (will fail):
- search_for_templates() - NO! Missing required query parameter
- search_for_templates(query="") - NO! Empty query not allowed

Extract search terms from user's request. If user says "show me HR templates", use query="HR".
If user wants ALL templates without filtering, use get_all_templates() instead.

PAGINATION: Returns 20 results per page. Use page=2, page=3, etc. for more. meta.total_pages shows total page count.""",
        tags={"search", "discovery", "templates", "blueprints", "read-only"},
        annotations=ToolAnnotations(
            title="Search for templates",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("search_for_templates")
    @handle_tallyfy_errors("search for templates")
    def search_for_templates(
        query: SearchQuery,
        page: PageNumber = 1,
        per_page: PageSize = 20,
        tags: OptionalBool = False,
    ) -> ToolResult:
        """
        Search for templates in the organization.

        Args:
            query: Search query string (REQUIRED - must not be empty)
            page: Page number (default: 1)
            per_page: Results per page (default: 20, max: 100)
            tags: Include tags in results (default: False)

        Returns:
            Dict with 'data' (list of templates) and 'meta' (total, returned, truncated, page, total_pages)
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            return ToolResult(
                content=fetch_single_page(
                    sdk.searches.search_templates,
                    org_id, query,
                    page=page, per_page=per_page, tags=tags,
                ),
                structured_content=None
            )

    @mcp.tool(
        name="search_for_snippets",
        description="""Search for text snippets (stored text templates) in the organization by keyword.

MANDATORY: You MUST provide a 'query' parameter. Calling with empty/missing query WILL FAIL.

CORRECT usage examples:
- search_for_snippets(query="welcome email") - search for welcome email snippets
- search_for_snippets(query="disclaimer") - search for disclaimer snippets

WRONG usage (will fail):
- search_for_snippets() - NO! Missing required query parameter
- search_for_snippets(query="") - NO! Empty query not allowed

Extract search terms from user's request. If user says "find the thank you snippet", use query="thank you".

PAGINATION: Returns 20 results per page. Use page=2, page=3, etc. for more. meta.total_pages shows total page count.""",
        tags={"search", "discovery", "snippets", "read-only"},
        annotations=ToolAnnotations(
            title="Search for snippets",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("search_for_snippets")
    @handle_tallyfy_errors("search for snippets")
    def search_for_snippets(
        query: SearchQuery,
        page: PageNumber = 1,
        per_page: PageSize = 20,
    ) -> ToolResult:
        """
        Search for text snippets in the organization.

        Args:
            query: Search query string (REQUIRED - must not be empty)
            page: Page number (default: 1)
            per_page: Results per page (default: 20, max: 100)

        Returns:
            Dict with 'data' (list of snippets) and 'meta' (total, returned, truncated, page, total_pages)
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            return ToolResult(
                content=fetch_single_page(
                    sdk.searches.search_snippets,
                    org_id, query,
                    page=page, per_page=per_page,
                ),
                structured_content=None
            )

    @mcp.tool(
        name="search_all",
        meta={
            "openai/toolInvocation/invoking": "Searching Tallyfy...",
            "openai/toolInvocation/invoked": "Search complete",
        },
        description="""Search across multiple entity types in one request. Returns results grouped by type.

MANDATORY: You MUST provide a 'query' parameter. Calling with empty/missing query WILL FAIL.

Searches blueprints, processes, tasks, and snippets by default. Use the 'types' parameter to narrow down.
Valid types: blueprint, process, task, snippet, capture, step.

PAGINATION: Returns 20 items per page per entity type by default. Use page=2, page=3 for more results, or per_page to change page size (max 100).

CORRECT usage examples:
- search_all(query="onboarding") - search everything for "onboarding"
- search_all(query="Q4", types=["process", "task"]) - search only processes and tasks
- search_all(query="report", per_page=10) - search with fewer results per type
- search_all(query="onboarding", page=2) - second page of results

WRONG usage (will fail):
- search_all() - NO! Missing required query parameter
- search_all(query="") - NO! Empty query not allowed""",
        tags={"search", "discovery", "universal", "read-only"},
        annotations=ToolAnnotations(
            title="Search across all entity types",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("search_all")
    @handle_tallyfy_errors("search all")
    def search_all(
        query: SearchQuery,
        types: SearchTypesList = None,
        page: PageNumber = 1,
        per_page: PageSize = 20,
        tags: OptionalBool = False,
    ) -> ToolResult:
        """
        Search across multiple entity types in one API request.

        Args:
            query: Search query string (REQUIRED - must not be empty)
            types: Entity types to search (default: blueprint, process, task, snippet).
                   Valid values: blueprint, process, task, snippet, capture, step.
            page: Page number (default: 1)
            per_page: Results per page per type (default: 20, max: 100)
            tags: Include tags in results (default: False)

        Returns:
            Dict keyed by type, each with 'data' list and 'meta' (total, returned, page, total_pages)
        """
        search_types = types if types else DEFAULT_SEARCH_TYPES
        invalid = set(search_types) - VALID_SEARCH_TYPES
        if invalid:
            raise ToolError(
                f"Invalid search types: {', '.join(sorted(invalid))}. "
                f"Valid types: {', '.join(sorted(VALID_SEARCH_TYPES))}"
            )

        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            results = sdk.searches.search_all(
                org_id, query,
                page=page, per_page=per_page, tags=tags, types=search_types
            )
            output = {}
            for type_name, result_list in results.items():
                data = serialize_dataclass(
                    result_list.data if hasattr(result_list, "data") else []
                )
                meta = (
                    result_list.meta
                    if hasattr(result_list, "meta") and result_list.meta
                    else None
                )
                api_total = meta.total if meta and hasattr(meta, "total") else len(data)
                total_pages = meta.total_pages if meta and hasattr(meta, "total_pages") else 1
                output[type_name] = {
                    "data": data,
                    "meta": {
                        "total": api_total,
                        "returned": len(data),
                        "page": page,
                        "total_pages": total_pages,
                    },
                }
            # Cap aggregate size: each populated bucket gets an equal share
            # of MAX_RESULT_BYTES, with a `_truncated` marker per trimmed
            # bucket so the LLM knows results were capped (issue #230).
            output = compact_search_all_buckets(output)
            return ToolResult(
                content=output,
                structured_content=None
            )
