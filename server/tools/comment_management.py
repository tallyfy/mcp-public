"""
Comment Management Tools
Tools for reading and writing task comments/threads
"""

import logging
import re
from typing import List, Optional

from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
from tallyfy import TallyfySDK
from mcp.types import ToolAnnotations
from utils.fastmcp_errors import handle_tallyfy_errors
from utils.auth_context import get_authenticated_credentials, TALLYFY_API_BASE_URL
from utils.fastmcp_types import (
    ProcessId,
    TaskId,
    CommentId,
    GenericList,
    GenericDict,
    OptionalString,
    UserIdList,
)
from utils.sdk_serializer import serialize_dataclass, compact_result
from metrics import track_tool_execution

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# @mention parsing (W-bug6 / issue #173)
# ---------------------------------------------------------------------------
#
# Comment bodies may include @mentions in several formats. The MCP layer
# parses them out, resolves them to user_ids via the org-users helper, and
# forwards the resulting integer IDs to the SDK as `sent_to=` so the Tallyfy
# API fires the @mention notification. Without this step, mentions written
# in plain English (e.g. ``@Zurly Nike``) or as canonical Froala markup
# (``<span data-id="…">``) are stored as inert text and trigger no notification.
#
# Recognised formats:
#   @<numeric_id>           e.g. @20059                      -> by id
#   @<email>                e.g. @zurly@example.com          -> by email
#   @"Display Name"         e.g. @"Zurly Nike"               -> by full name
#   @<username>             e.g. @zurly                      -> by username
#   <span … data-id="…">    Froala mention HTML (pre-formatted markup)
#
# The parser is lenient: malformed tokens (``@@invalid``) are silently left
# in the comment body unchanged. No mentions found means no org-users API
# call is made (the lookup is opt-in based on the body content).

# HTML mention span (Froala tribute) — extract data-id attribute.
_HTML_MENTION_RE = re.compile(
    r'<span[^>]*\bdata-id\s*=\s*["\'](\d+)["\'][^>]*>',
    re.IGNORECASE,
)

# @"Display Name" — quoted multi-word identifier (run first so quoted spaces
# do not break subsequent token parsing).
_QUOTED_NAME_RE = re.compile(r'@"([^"\n]+)"')

# @<email> — must come before @<username> because the email pattern subsumes
# the username pattern. Negative-lookbehind avoids matching the second @ in
# pre-tokens like ``user@example.com`` or already-extracted @[uid] markup.
_EMAIL_RE = re.compile(
    r'(?<![\w@])@([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b'
)

# @<numeric_id> — 4 to 10 digits (long enough to discourage matching small
# unrelated counts, short enough to stay within plausible user_id space).
_USER_ID_RE = re.compile(r'(?<![\w@])@(\d{4,10})\b')

# @<username> — alphanumeric token with allowed punctuation `._-`.
_USERNAME_RE = re.compile(r'(?<![\w@])@([A-Za-z][\w._\-]*)\b')


def _has_mention_token(content: str) -> bool:
    """Quick check: does the body contain any @-token worth resolving?"""
    if not content:
        return False
    if "@" not in content and "data-id" not in content.lower():
        return False
    return True


def _extract_mentioned_user_ids(content: str, sdk, org_id: str) -> List[int]:
    """Parse @mentions from a comment body and resolve them to user IDs.

    Returns a deduplicated list of integer user IDs in insertion order. A
    failure to resolve any individual token is silent — the comment is
    posted with whatever IDs we did manage to resolve, and unresolved tokens
    remain in the body as plain text (matching native Tallyfy behaviour).
    """
    if not _has_mention_token(content):
        return []

    resolved: List[int] = []

    def _add(uid: Optional[int]) -> None:
        if uid is not None and uid not in resolved:
            resolved.append(uid)

    # 1. HTML mention spans — `data-id` is a verbatim user_id, no lookup needed.
    for m in _HTML_MENTION_RE.finditer(content):
        try:
            _add(int(m.group(1)))
        except ValueError:
            continue

    # 2. @<numeric_id> — verbatim user_id, no lookup needed.
    for m in _USER_ID_RE.finditer(content):
        try:
            _add(int(m.group(1)))
        except ValueError:
            continue

    # The remaining patterns (email, quoted name, username) need the org
    # users index to translate text -> integer ID. Build it lazily — we only
    # pay the API cost when there is at least one such token in the body.
    needs_directory = bool(
        _EMAIL_RE.search(content)
        or _QUOTED_NAME_RE.search(content)
        or _USERNAME_RE.search(content)
    )
    if not needs_directory:
        return resolved

    try:
        users_list = sdk.users.get_organization_users_list(org_id)
        users = getattr(users_list, "data", None) or []
    except Exception:
        logger.warning(
            "Failed to fetch org users list for @mention resolution on org %s",
            org_id,
            exc_info=True,
        )
        return resolved

    by_email: dict = {}
    by_username: dict = {}
    by_full_name: dict = {}
    for u in users:
        uid = getattr(u, "id", None)
        if uid is None:
            continue
        email = (getattr(u, "email", "") or "").lower()
        username = (getattr(u, "username", "") or "").lower()
        first = (getattr(u, "first_name", "") or "").strip()
        last = (getattr(u, "last_name", "") or "").strip()
        if email:
            by_email.setdefault(email, uid)
        if username:
            by_username.setdefault(username, uid)
        full = f"{first} {last}".strip().lower()
        if full:
            by_full_name.setdefault(full, uid)

    # 3. @<email>
    for m in _EMAIL_RE.finditer(content):
        token = m.group(1).lower()
        _add(by_email.get(token))

    # 4. @"Display Name"
    for m in _QUOTED_NAME_RE.finditer(content):
        name = m.group(1).strip().lower()
        _add(by_full_name.get(name))

    # 5. @<username> — last because email/quoted-name patterns may already
    # have consumed parts of the body, but our regex doesn't mutate `content`,
    # it just finds matches. The duplicate guard in `_add` keeps things tidy.
    for m in _USERNAME_RE.finditer(content):
        token = m.group(1).lower()
        # Skip purely numeric usernames (already handled by _USER_ID_RE)
        if token.isdigit():
            continue
        _add(by_username.get(token))

    return resolved


def register_comment_management_tools(mcp):
    """Register all comment/thread management tools with the MCP server"""

    @mcp.tool(
        name="get_task_comments",
        description="""Get all comments (threads) on a task.

REQUIRED: 'task_id' (32-char hex).
Optional: 'run_id' (32-char hex process ID) — provide it if you have it to avoid an extra lookup.

If run_id is omitted, it is resolved automatically from the task. Never call this without task_id.""",
        tags=["tasks", "comments", "threads", "read-only", "collaboration"],
        annotations=ToolAnnotations(
            title="Get task comments",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_task_comments")
    @handle_tallyfy_errors("get task comments")
    def get_task_comments(task_id: TaskId, run_id: OptionalString = None) -> GenericList:
        """
        Get all comments on a specific task.

        Args:
            task_id: Task ID to retrieve comments for (REQUIRED - 32-character hex string)
            run_id: Process (run) ID the task belongs to — optional. If omitted, resolved
                automatically via a task lookup (costs one extra API call).

        Returns:
            List of comment objects with content, author, and timestamps
        """
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            resolved_run_id = run_id
            if not resolved_run_id:
                task = sdk.get_standalone_task(org_id, task_id)
                resolved_run_id = getattr(task, 'run_id', None) or getattr(task, 'checklist_id', None)
                if not resolved_run_id:
                    raise ToolError(
                        "Could not resolve run_id for this task automatically. "
                        "Please provide run_id explicitly."
                    )
            comments = sdk.threads.get_task_comments(org_id, resolved_run_id, task_id)
            return ToolResult(
                content=compact_result([serialize_dataclass(c) for c in comments]) if comments else [],
                structured_content=None
            )

    @mcp.tool(
        name="add_task_comment",
        description="""Add a comment to a task.

REQUIRED: 'task_id' (32-char hex) and 'content' (comment text).

Optional:
- 'run_id': process/run ID — pass it if you have it; you'll need it to call 'get_task_comments' later
- 'label': "comment" (default) | "problem" | "resolve" | "improvement" | "advice"
- 'state': "open" (default) | "hide-for-guests" | "collapsed"
- 'sent_to': list of user IDs (integers) to @mention and notify. Look up user IDs first via get_organization_users or get_organization_users_list, then pass them here. Example: sent_to=[20059, 20033]

LABEL='resolve' SEMANTICS (issue #172):
- If the task HAS an open problem flag (an existing thread with label='problem' that has not been resolved), this tool automatically clears that flag by routing through the resolve endpoint, matching the native Tallyfy app's behaviour. Pass 'run_id' when available to avoid an extra API lookup.
- If the task has NO open problem flag, label='resolve' is cosmetic only — the comment is stored with the resolve label but no problem flag is cleared (because there is nothing to clear). The function logs a WARNING in that case so the cosmetic outcome is visible in logs.
- To explicitly clear a specific known problem thread without posting a separate comment, call resolve_task_issues(task_id, thread_id) directly — that path is unambiguous and returns the resolution outcome.

CORRECT usage:
  add_task_comment(task_id="abc123...", content="Everything is on track")
  add_task_comment(task_id="abc123...", run_id="def456...", content="Blocked on approval", label="problem")
  add_task_comment(task_id="abc123...", run_id="def456...", content="Issue resolved", label="resolve")
  add_task_comment(task_id="abc123...", content="Please review", sent_to=[20059])

Never call this without both required parameters.""",
        tags=["tasks", "comments", "threads", "write", "collaboration"],
        annotations=ToolAnnotations(
            title="Add task comment",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("add_task_comment")
    @handle_tallyfy_errors("add task comment")
    def add_task_comment(
        task_id: TaskId,
        content: str,
        run_id: OptionalString = None,
        label: Optional[str] = None,
        state: Optional[str] = None,
        sent_to: UserIdList = None,
    ) -> GenericDict:
        """
        Add a comment to a task.

        Args:
            task_id: Task ID to comment on (REQUIRED - 32-character hex string)
            content: Comment text (REQUIRED)
            run_id: Process/run ID — not used by this endpoint but accepted so callers retain
                it in context for a subsequent get_task_comments call (optional)
            label: Comment type — "comment" | "problem" | "resolve" | "improvement" | "advice" (default: "comment")
            state: Visibility — "open" | "hide-for-guests" | "collapsed" (default: "open")
            sent_to: List of user IDs (integers) to @mention and notify (optional)

        Returns:
            Created comment object
        """
        if not content or not content.strip():
            raise ToolError("content cannot be empty")

        # Inject @[uid] markup for explicit sent_to so the rendered comment
        # shows the mention. The actual notification is fired by the
        # `sent_to` body parameter forwarded to the SDK below.
        effective_content = content.strip()
        if sent_to:
            mention_str = " ".join(f"@[{uid}]" for uid in sent_to)
            effective_content = f"{mention_str} {effective_content}"

        # run_id is intentionally not forwarded — the write endpoint only needs task_id.
        # It is accepted here so callers (e.g. Claude) keep it in context for get_task_comments.
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # W-bug6 / issue #173: parse @mentions out of the body and merge
            # them with explicit sent_to before forwarding to the SDK. The
            # Tallyfy API's `sent_to` parameter is what fires the
            # notification — plain-text @-tokens alone do not.
            body_mention_ids = _extract_mentioned_user_ids(
                effective_content, sdk, org_id
            )
            combined_sent_to = None
            if sent_to or body_mention_ids:
                merged = []
                for uid in (sent_to or []):
                    if uid not in merged:
                        merged.append(uid)
                for uid in body_mention_ids:
                    if uid not in merged:
                        merged.append(uid)
                combined_sent_to = merged or None

            # When resolving, find open problem threads and resolve them using the
            # user's content directly. This avoids double-posting: the resolve
            # endpoint itself stores the comment, so we skip add_task_comment.
            # The first problem thread gets the user's text; any additional threads
            # get the default "Resolved" text.
            resolved_count = 0
            if label == "resolve":
                try:
                    resolved_run_id = run_id
                    if not resolved_run_id:
                        task = sdk.get_standalone_task(org_id, task_id)
                        resolved_run_id = (
                            getattr(task, "run_id", None)
                            or getattr(task, "checklist_id", None)
                        )
                    if resolved_run_id:
                        comments = sdk.threads.get_task_comments(
                            org_id, resolved_run_id, task_id
                        )
                        problem_threads = [
                            c for c in (comments or [])
                            if (
                                getattr(c, "label", None) == "problem"
                                and not getattr(c, "resolve_id", None)
                            )
                        ]
                        for i, c in enumerate(problem_threads):
                            resolve_content = effective_content if i == 0 else "Resolved"
                            try:
                                sdk.threads.resolve_task_issues(
                                    org_id, task_id, c.id,
                                    content=resolve_content,
                                    state=state,
                                )
                                resolved_count += 1
                            except Exception:
                                logger.warning(
                                    "Failed to auto-resolve problem thread %s on task %s",
                                    c.id,
                                    task_id,
                                )
                        if resolved_count:
                            logger.info(
                                "Auto-resolved %d problem thread(s) on task %s",
                                resolved_count,
                                task_id,
                            )
                except Exception:
                    logger.warning(
                        "Failed to auto-resolve problems on task %s after resolve comment",
                        task_id,
                        exc_info=True,
                    )

            # Fall back to add_task_comment when label != "resolve", or when no
            # problem threads were found to resolve (resolve label is then
            # cosmetic — see #172).
            if label == "resolve" and resolved_count == 0:
                # Path B at the API level: posting a comment with
                # label='resolve' on a task with no open problem flag does
                # NOT clear anything (it is just stored as a styled comment).
                # Surface this clearly in logs so the operator/developer
                # can see why nothing was unblocked. Call
                # resolve_task_issues(task_id, thread_id) for an explicit
                # clear when the resolve action is required.
                logger.warning(
                    "add_task_comment(label='resolve') on task %s found no "
                    "open problem threads; the comment will be stored as "
                    "cosmetic only — the task's problem flag is not cleared "
                    "because there is nothing to clear. Call "
                    "resolve_task_issues(task_id, thread_id) directly if you "
                    "need to clear a specific known problem thread.",
                    task_id,
                )

            if label != "resolve" or resolved_count == 0:
                _kwargs = {"state": state, "label": label}
                if combined_sent_to is not None:
                    _kwargs["sent_to"] = combined_sent_to
                result = sdk.threads.add_task_comment(
                    org_id, task_id, effective_content,
                    **_kwargs,
                )
                serialized = serialize_dataclass(result) if result else {}
            else:
                serialized = {"resolved_problems": resolved_count}

            return ToolResult(
                content=serialized,
                structured_content=None
            )

    @mcp.tool(
        name="update_task_comment",
        description="Update an existing comment on a task. REQUIRED: 'task_id' (32-char hex), 'comment_id', and 'content' (new text). Optional: 'run_id' (pass if available), 'label', 'state', 'sent_to' (list of user IDs to @mention and notify). Never call this without all three required parameters.",
        tags=["tasks", "comments", "threads", "write", "collaboration"],
        annotations=ToolAnnotations(
            title="Update task comment",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("update_task_comment")
    @handle_tallyfy_errors("update task comment")
    def update_task_comment(
        task_id: TaskId,
        comment_id: CommentId,
        content: str,
        run_id: OptionalString = None,
        label: Optional[str] = None,
        state: Optional[str] = None,
        sent_to: UserIdList = None,
    ) -> GenericDict:
        """
        Update an existing comment on a task.

        Args:
            task_id: Task ID (REQUIRED - 32-character hex string)
            comment_id: Comment ID to update (REQUIRED)
            content: New comment text (REQUIRED)
            run_id: Process/run ID — not used by this endpoint, accepted for context continuity (optional)
            label: Comment type — "comment" | "problem" | "resolve" | "improvement" | "advice" (optional)
            state: Visibility — "open" | "hide-for-guests" | "collapsed" (optional)
            sent_to: List of user IDs (integers) to @mention and notify (optional)

        Returns:
            Updated comment object
        """
        if not content or not content.strip():
            raise ToolError("content cannot be empty")

        effective_content = content.strip()
        if sent_to:
            mention_str = " ".join(f"@[{uid}]" for uid in sent_to)
            effective_content = f"{mention_str} {effective_content}"

        # run_id intentionally not forwarded — write endpoint only needs task_id.
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.threads.update_task_comment(
                org_id, task_id, comment_id, effective_content,
                state=state,
                label=label,
            )
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="delete_task_comment",
        description="Delete a comment from a task. REQUIRED: 'task_id' (32-char hex) and 'comment_id'. Optional: 'run_id' (pass if available). This action cannot be undone. Never call this without both required parameters.",
        tags=["tasks", "comments", "threads", "write", "collaboration"],
        annotations=ToolAnnotations(
            title="Delete task comment",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("delete_task_comment")
    @handle_tallyfy_errors("delete task comment")
    def delete_task_comment(task_id: TaskId, comment_id: CommentId, run_id: OptionalString = None) -> GenericDict:
        """
        Delete a comment from a task.

        Args:
            task_id: Task ID (REQUIRED - 32-character hex string)
            comment_id: Comment ID to delete (REQUIRED)
            run_id: Process/run ID — not used by this endpoint, accepted for context continuity (optional)

        Returns:
            Result of the deletion operation
        """
        # run_id intentionally not forwarded — write endpoint only needs task_id.
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.threads.delete_task_comment(org_id, task_id, comment_id)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="report_task_issue",
        description="Report an issue on a task. REQUIRED: 'task_id' (32-char hex) and 'content' (issue description). Optional: 'run_id' (pass if available). Creates a comment with 'problem' label. Never call this without both required parameters.",
        tags=["tasks", "comments", "issues", "write", "collaboration"],
        annotations=ToolAnnotations(
            title="Report task issue",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("report_task_issue")
    @handle_tallyfy_errors("report task issue")
    def report_task_issue(task_id: TaskId, content: str, run_id: OptionalString = None) -> GenericDict:
        """
        Report an issue on a task.

        Args:
            task_id: Task ID (REQUIRED - 32-character hex string)
            content: Issue description (REQUIRED)
            run_id: Process/run ID — not used by this endpoint, accepted for context continuity (optional)

        Returns:
            Created issue comment object
        """
        if not content or not content.strip():
            raise ToolError("content cannot be empty")

        # run_id intentionally not forwarded — write endpoint only needs task_id.
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.threads.report_task_issue(org_id, task_id, content.strip())
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )

    @mcp.tool(
        name="resolve_task_issues",
        description="Resolve a specific issue on a task. REQUIRED: 'task_id' (32-char hex) and 'thread_id' (the issue comment ID returned by report_task_issue). Optional: 'run_id' (pass if available). Never call this without both required parameters.",
        tags=["tasks", "comments", "issues", "write", "collaboration"],
        annotations=ToolAnnotations(
            title="Resolve task issues",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("resolve_task_issues")
    @handle_tallyfy_errors("resolve task issues")
    def resolve_task_issues(task_id: TaskId, thread_id: CommentId, run_id: OptionalString = None) -> GenericDict:
        """
        Resolve an open issue on a task.

        Args:
            task_id: Task ID (REQUIRED - 32-character hex string)
            thread_id: Issue comment ID to resolve (REQUIRED - returned by report_task_issue)
            run_id: Process/run ID — not used by this endpoint, accepted for context continuity (optional)

        Returns:
            Result of the resolution operation
        """
        # run_id intentionally not forwarded — write endpoint only needs task_id.
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            result = sdk.threads.resolve_task_issues(org_id, task_id, thread_id)
            return ToolResult(
                content=serialize_dataclass(result) if result else {},
                structured_content=None
            )
