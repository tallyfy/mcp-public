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
# THE ONLY THING THAT FIRES A MENTION NOTIFICATION IS ``@[<user_id>]`` MARKUP
# INSIDE THE COMMENT BODY.
#
# api-v2 parses mentions out of the stored content — ``atmentioned_users()``
# (app/Helpers/email.php:500) feeds ``atmentioned_users_ids()``
# (app/Helpers/email.php:458-460), which is literally
# ``preg_match_all('/@\[(\d+)]/', $content, $matches)``. The resulting users
# are what ``App\Messaging\Comment::postOn()`` (app/Messaging/Comment.php:70)
# dispatches ``users.at_mentioned`` for.
#
# There is NO request-side ``sent_to`` parameter. PostCommentRequest
# (app/Http/Requests/Tasks/PostCommentRequest.php) validates only
# content/state/label, and a repo-wide grep for ``sent_to`` finds it in just
# two @SWG doc annotations plus ThreadTransformer, where it is a RESPONSE
# field derived from ``$thread->participants``. A ``sent_to`` key in the
# request body is therefore read by nothing and notifies nobody.
#
# So the MCP layer resolves every mention shorthand to a user_id and then
# REWRITES THE BODY so each one is ``@[<user_id>]``. Rendering comes free:
# api-v2 turns ``@[id]`` back into ``<strong>Full Name</strong>`` on read.
#
# Recognised input formats (all normalised to ``@[<user_id>]``):
#   @[<numeric_id>]         e.g. @[20059]                    -> already canonical
#   @<numeric_id>           e.g. @20059                      -> by id
#   @<email>                e.g. @zurly@example.com          -> by email
#   @"Display Name"         e.g. @"Zurly Nike"               -> by full name
#   @<username>             e.g. @zurly                      -> by username
#   <span … data-id="…">    Froala mention HTML (id read, span left intact)
#
# The parser is lenient: malformed tokens (``@@invalid``) and tokens that
# resolve to nobody are silently left in the comment body unchanged. No
# mentions found means no org-users API call is made (the lookup is opt-in
# based on the body content).

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

# @[<user_id>] — the canonical markup api-v2 actually reads. None of the
# rewrite patterns above can match inside it (each requires the character
# after `@` to be a digit, letter or quote — never `[`), so already-canonical
# mentions survive the rewrite untouched.
_MARKUP_MENTION_RE = re.compile(r'@\[(\d+)]')

# A whole Froala mention element, opening tag through closing tag. Its inner
# text is a rendered display name like ``@Zurly Nike`` — running the plain-text
# rewrites over it would corrupt the label (``@[20059] Nike``) for no gain,
# since the id is already carried by data-id. Masked out during rewriting.
_MENTION_SPAN_ELEMENT_RE = re.compile(
    r'<span[^>]*\bdata-id\s*=\s*["\']\d+["\'][^>]*>.*?</span>',
    re.IGNORECASE | re.DOTALL,
)

# Placeholder for a masked span. Deliberately contains no '@' so none of the
# mention patterns can match it while it stands in for the real markup.
_MASK_TEMPLATE = "\x00mention-span-{}\x00"


def _mask_mention_spans(content: str) -> tuple:
    """Replace Froala mention elements with inert placeholders.

    Returns ``(masked_content, spans)``; pass both to :func:`_unmask_mention_spans`.
    """
    spans: List[str] = []

    def _stash(m):
        spans.append(m.group(0))
        return _MASK_TEMPLATE.format(len(spans) - 1)

    return _MENTION_SPAN_ELEMENT_RE.sub(_stash, content), spans


def _unmask_mention_spans(content: str, spans: List[str]) -> str:
    """Restore the elements removed by :func:`_mask_mention_spans`."""
    for i, original in enumerate(spans):
        content = content.replace(_MASK_TEMPLATE.format(i), original)
    return content


def mention_markup(user_id: int) -> str:
    """Render the one mention form the Tallyfy API notifies on."""
    return f"@[{user_id}]"


def _has_mention_token(content: str) -> bool:
    """Quick check: does the body contain any @-token worth resolving?"""
    if not content:
        return False
    if "@" not in content and "data-id" not in content.lower():
        return False
    return True


def _finish(content: str, spans: List[str], resolved: List[int]) -> str:
    """Restore masked spans, then guarantee every resolved id has @[id] markup."""
    return _ensure_mention_markup(_unmask_mention_spans(content, spans), resolved)


def _resolve_mentions(content: str, sdk, org_id: str) -> tuple:
    """Normalise every @mention in a comment body to ``@[<user_id>]`` markup.

    Returns ``(rewritten_content, resolved_ids)``. ``resolved_ids`` is a
    deduplicated list of integer user IDs in insertion order; every one of
    them is guaranteed to appear as ``@[<user_id>]`` in ``rewritten_content``,
    which is the only form api-v2 notifies on (app/Helpers/email.php:458-460).

    A failure to resolve any individual token is silent — the comment is
    posted with whatever IDs we did manage to resolve, and unresolved tokens
    remain in the body as plain text (matching native Tallyfy behaviour).
    """
    if not _has_mention_token(content):
        return content, []

    resolved: List[int] = []

    def _add(uid: Optional[int]) -> None:
        if uid is not None and uid not in resolved:
            resolved.append(uid)

    # 1. HTML mention spans — `data-id` is a verbatim user_id, no lookup
    # needed. The span is then masked so the plain-text rewrites below cannot
    # corrupt its rendered display name; `_ensure_mention_markup` guarantees a
    # matching @[id] token exists so the notification still fires.
    for m in _HTML_MENTION_RE.finditer(content):
        try:
            _add(int(m.group(1)))
        except ValueError:
            continue

    content, _spans = _mask_mention_spans(content)

    # 2. Already-canonical @[<numeric_id>] markup — count it as resolved so
    # the trailing pass does not prepend a duplicate.
    for m in _MARKUP_MENTION_RE.finditer(content):
        try:
            _add(int(m.group(1)))
        except ValueError:
            continue

    # 3. @<numeric_id> — verbatim user_id, no lookup needed. Rewrite to markup.
    def _sub_user_id(m):
        try:
            uid = int(m.group(1))
        except ValueError:
            return m.group(0)
        _add(uid)
        return mention_markup(uid)

    content = _USER_ID_RE.sub(_sub_user_id, content)

    # The remaining patterns (email, quoted name, username) need the org
    # users index to translate text -> integer ID. Build it lazily — we only
    # pay the API cost when there is at least one such token in the body.
    needs_directory = bool(
        _EMAIL_RE.search(content)
        or _QUOTED_NAME_RE.search(content)
        or _USERNAME_RE.search(content)
    )
    if not needs_directory:
        return _finish(content, _spans, resolved), resolved

    try:
        users_list = sdk.users.get_organization_users_list(org_id)
        users = getattr(users_list, "data", None) or []
    except Exception:
        logger.warning(
            "Failed to fetch org users list for @mention resolution on org %s",
            org_id,
            exc_info=True,
        )
        return _finish(content, _spans, resolved), resolved

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

    def _sub_from(index: dict, key_fn):
        """Build a re.sub callback that rewrites a resolvable token to markup."""

        def _repl(m):
            key = key_fn(m.group(1))
            uid = index.get(key) if key is not None else None
            if uid is None:
                # Unresolvable — leave the token exactly as the author wrote it.
                return m.group(0)
            _add(uid)
            return mention_markup(uid)

        return _repl

    # 4. @<email>
    content = _EMAIL_RE.sub(_sub_from(by_email, lambda t: t.lower()), content)

    # 5. @"Display Name"
    content = _QUOTED_NAME_RE.sub(
        _sub_from(by_full_name, lambda t: t.strip().lower()), content
    )

    # 6. @<username> — last because the email and quoted-name patterns subsume
    # it; running it earlier would rewrite the local part of an @email token.
    # Purely numeric tokens were already handled by _USER_ID_RE above.
    content = _USERNAME_RE.sub(
        _sub_from(by_username, lambda t: t.lower() if not t.isdigit() else None),
        content,
    )

    return _finish(content, _spans, resolved), resolved


def _ensure_mention_markup(content: str, user_ids: List[int]) -> str:
    """Guarantee every resolved user_id appears as ``@[uid]`` in the body.

    Covers the ids that could not be rewritten in place — Froala
    ``<span data-id="…">`` markup, and ids supplied out-of-band by the
    caller. Without a literal ``@[uid]`` token api-v2 never sees the mention
    and no notification is sent.
    """
    if not user_ids:
        return content
    already = {int(m.group(1)) for m in _MARKUP_MENTION_RE.finditer(content)}
    missing = [uid for uid in user_ids if uid not in already]
    if not missing:
        return content
    prefix = " ".join(mention_markup(uid) for uid in missing)
    return f"{prefix} {content}".strip()


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
- 'sent_to': numeric user IDs to @mention. Look up IDs via get_organization_users first.

@MENTIONS: a user is notified ONLY when the stored body contains @[<user_id>] markup,
so 'sent_to' entries are rendered into the body as that. You can also write the mention
straight into 'content' — @[20059], @20059, @alice@acme.com, @"Alice Smith" and @alice
all resolve and are normalised to @[20059]. An unresolvable token stays plain text and
notifies nobody, so prefer a numeric ID. Mentioning someone also grants them task access.

LABEL='resolve' SEMANTICS (issue #172):
- If the task HAS an unresolved label='problem' thread, this clears that flag via the resolve endpoint, matching the native app. Pass 'run_id' to avoid an extra lookup.
- If it has NO open problem flag, label='resolve' is cosmetic only — the comment is stored with the label but nothing is cleared. A WARNING is logged so that is visible.
- To clear a specific known problem thread without a separate comment, call resolve_task_issues(task_id, thread_id) directly — unambiguous, and returns the outcome.

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
            sent_to: List of numeric user IDs to @mention (optional). Rendered into
                the body as @[<user_id>] markup — the API has no request-side
                sent_to parameter, body markup is what notifies.

        Returns:
            Created comment object
        """
        if not content or not content.strip():
            raise ToolError("content cannot be empty")

        effective_content = content.strip()

        # run_id is intentionally not forwarded — the write endpoint only needs task_id.
        # It is accepted here so callers (e.g. Claude) keep it in context for get_task_comments.
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # W-bug6 / issue #173: resolve every @mention shorthand in the body
            # and rewrite it to @[uid]. Note what deliberately does NOT happen
            # here: `sent_to` is not forwarded to the API. PostCommentRequest
            # validates only content/state/label, and api-v2 never reads a
            # request-side `sent_to` — sending it notified nobody, which is why
            # mentions silently failed.
            effective_content, _mention_ids = _resolve_mentions(
                effective_content, sdk, org_id
            )
            # Seed markup for out-of-band `sent_to` ids AFTER resolving, not before.
            # _resolve_mentions already guarantees @[uid] for everything it found in
            # the body (including Froala spans, via its own tail call at :150). Doing
            # this first instead meant a user named BOTH ways — sent_to=[101] plus
            # "@john@example.com" in the body — got @[101] twice. This call is
            # idempotent against tokens already present, so ordering is the whole fix.
            effective_content = _ensure_mention_markup(
                effective_content, list(sent_to or [])
            )
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
                result = sdk.threads.add_task_comment(
                    org_id, task_id, effective_content,
                    state=state, label=label,
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
        description="Update an existing comment on a task. REQUIRED: 'task_id' (32-char hex), 'comment_id', and 'content' (new text). Optional: 'run_id' (pass if available), 'label', 'state', 'sent_to' (list of numeric user IDs to @mention — rendered into the body as @[<user_id>] markup, which is what notifies them; you can also write @[20059] / @20059 / @alice@acme.com / @\"Alice Smith\" / @alice directly in 'content' and it is normalised for you). Never call this without all three required parameters.",
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
            sent_to: List of numeric user IDs to @mention (optional). Rendered into
                the body as @[<user_id>] markup — the API has no request-side
                sent_to parameter, body markup is what notifies.

        Returns:
            Updated comment object
        """
        if not content or not content.strip():
            raise ToolError("content cannot be empty")

        effective_content = content.strip()

        # run_id intentionally not forwarded — write endpoint only needs task_id.
        api_key, org_id = get_authenticated_credentials()
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            # Same contract and same ordering as add_task_comment: resolve the body
            # first, then seed out-of-band sent_to ids, so a user named both ways
            # does not get duplicate @[uid] markup.
            effective_content, _mention_ids = _resolve_mentions(
                effective_content, sdk, org_id
            )
            effective_content = _ensure_mention_markup(
                effective_content, list(sent_to or [])
            )
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
