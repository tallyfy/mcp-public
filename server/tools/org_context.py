"""
Org Memory Tools (#1036)

The connector's persistent memory of an organization: one curated markdown
document per org, stored on Cloudflare R2. A human product expert remembers
the account; these two tools are how the assistant does.

Siloing rests on two things together, and it needs both. Neither tool takes an
org parameter, so a caller cannot name an organization at the tool surface. And
the org id is never trusted merely because it was resolved: it is either a
VERIFIED token claim, or it is checked against api-v2 before anything is read
or written.

That second half is not belt-and-braces. ``get_authenticated_credentials``
deliberately falls back to the request-scoped org id, meaning an
``X-Organization-ID`` header or this user's persisted organization, when the
token carries no ``org_id`` claim. For every other tool that is safe, because
the org id is handed to api-v2, which enforces membership and refuses an
organization the caller does not belong to. Here there is no downstream: the
document is fetched from R2 under a per-org key and nothing else checks
anything, so the org id would BE the entire access-control decision.

Requiring the claim outright was the obvious answer and it was wrong. Measured
2026-08-27, NEITHER the staging nor the production Tallyfy token carries an
``org_id`` claim, so that version would have refused every real caller while
every test passed, because the tests supplied the claim themselves. So the
no-claim path asks api-v2 the membership question the other tools get answered
for free, and fails closed when it cannot get an answer.
"""

import logging
import re

from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
from mcp.types import ToolAnnotations
from typing import Annotated
from pydantic import Field

from tallyfy import TallyfySDK, TallyfyError

from constants import TALLYFY_API_BASE_URL
from utils.auth_context import (
    get_authenticated_credentials,
    get_verified_org_claim,
)
from utils import org_context_store
from metrics import track_tool_execution

logger = logging.getLogger(__name__)

# Full-rewrite semantics need a ceiling or the document grows without bound
# and every conversation pays for it. Over the cap the answer is CURATE,
# never a silent truncation -- a memory the model believes it saved and the
# store quietly cut in half is worse than a refused write.
MAX_CONTEXT_BYTES = 16384

# A memory store must never become a credential store. These shapes cover
# API keys, bearer headers, JWTs and PEM blocks; the check is on the WRITE
# path only, so nothing already stored is ever unreadable.
#
# The WORD BOUNDARY is what does the work, not the length. A bare substring
# test is unusable here: "sk-" occurs inside "task-", "Risk-", "desk-" and
# "Ask-", and in a workflow product "task-level" is close to the most likely
# hyphenated word an org memory document will contain. \b rules all of those
# out because the "s" is preceded by another word character.
#
# An earlier version leaned on long character runs instead ({16,}) and got both
# directions wrong. It MISSED a real 12-character key, a 12-character bearer
# token and a JWT wrapped across lines, and it REFUSED two ordinary sentences:
# "documented under Bearer\nauthentication" (\s+ crosses a newline, where a
# literal space does not) and "## BEGIN THE ANNUAL PRIVATE KEY ROTATION" (a
# heading, not a PEM block). So the runs are now short enough to catch small
# real credentials, and the shapes are tightened where the false positives came
# from: a bearer token's separator cannot span lines, and a private key must
# carry the actual PEM dashes. The JWT run is shortest of all because "eyJ" at
# a word boundary is base64 for the two characters that open every JSON header
# and is not an English letter sequence, so the boundary alone carries it.
_CREDENTIAL_PATTERNS = (
    ("an API key", re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")),
    ("a bearer token", re.compile(r"\bBearer[ \t]+[A-Za-z0-9._~+/=-]{8,}")),
    ("a JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{2,}")),
    ("a private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

# The refusal a caller sees when the store is not configured. Role-based:
# it says WHO can fix it, and deliberately names no environment variables --
# an env var name in a customer-facing error is operator documentation
# leaking into customer AI transcripts. A regression test sweeps for it.
_UNCONFIGURED_MESSAGE = (
    "Org memory is not set up on this server yet. A Tallyfy administrator "
    "needs to enable it; everything else keeps working without it."
)

_EMPTY_CONTEXT_MESSAGE = (
    "No org memory exists yet for this organization. As you learn durable "
    "facts (naming conventions, key templates, decisions), save them with "
    "update_org_context."
)


def _authenticated_org_id() -> str:
    """The org this caller is proven to be working in, or a ToolError.

    Two accepted paths, and the second is the common one today. If the token
    carries a verified ``org_id`` claim, that wins outright: ``auth_context``
    has already refused any header naming a different organization. If it does
    not, the resolved org id came from an ``X-Organization-ID`` header or this
    user's persisted organization, neither of which anything has checked, so
    membership is confirmed against api-v2 before anything is read or written.

    Requiring the claim outright was the first fix attempted and it was wrong:
    measured 2026-08-27, NEITHER the staging nor the production Tallyfy token
    carries an ``org_id`` claim, so it would have made org memory unusable for
    every direct API caller while looking correct in tests.

    These tools are deliberately exempt from ``@handle_tallyfy_errors``, so
    every failure below is translated here. Without that, FastMCP masks a
    non-ToolError into a generic "error calling tool" the model can only guess
    about, which is exactly what the honest-error rule exists to prevent.
    """
    try:
        api_key, org_id = get_authenticated_credentials()
    except TallyfyError as exc:
        raise ToolError(
            f"Org memory could not identify which organization you are "
            f"working in ({exc}). Ask the user to reconnect or to say which "
            "organization they mean, and continue without memory meanwhile."
        )
    except Exception as exc:
        raise ToolError(
            f"Org memory could not read your authenticated session "
            f"({type(exc).__name__}). Continue without memory and say it was "
            "unavailable if it matters."
        )

    # A verified claim is self-sufficient: auth_context has already refused any
    # header that named a different organization, so nothing further to check.
    claim_org_id = get_verified_org_claim()
    if claim_org_id:
        return claim_org_id

    # No claim, so org_id came from an X-Organization-ID header or this user's
    # persisted organization, and NOTHING has checked that the caller belongs
    # to it. Every other tool survives this because it hands the org id to
    # api-v2, which enforces membership and refuses an organization the caller
    # is not in. Org memory has no such downstream: the document is fetched
    # from R2 under a per-org key and nothing else checks anything, so the org
    # id would BE the entire access-control decision. So ask api-v2 the
    # question the other tools get answered for free.
    _require_membership(api_key, org_id)
    return org_id


class _UnexpectedShape(Exception):
    """The organization list came back in a shape we cannot read."""


# The API caps a page at 100, so a single call cannot answer for a user in more
# organizations than that. Ten pages is a generous ceiling; past it we say we
# could not tell rather than guessing.
_MEMBERSHIP_PAGE_SIZE = 100
_MEMBERSHIP_MAX_PAGES = 10

_MEMBERSHIP_UNAVAILABLE = (
    "Org memory could not confirm which organizations you belong to ({reason}), "
    "so it will not read or write anything. Continue without memory and say it "
    "was unavailable."
)


def _require_membership(api_key: str, org_id: str) -> None:
    """Refuse unless the caller actually belongs to ``org_id``.

    Fails CLOSED. If membership cannot be established, for any reason, the
    answer is no: a memory tool that degrades to "allow" when the check breaks
    is worse than one that is briefly unavailable.

    It pages, and it distinguishes "you are not in that organization" from "your
    list is longer than I read". A single page of 100 is the API's maximum, so a
    user in 101 organizations would have been told they are not a member of one
    they own. That is the estate's own rule about a count that equals the limit
    you asked for: it is your own limit wearing the costume of a measurement.
    """
    seen = set()
    exhausted = False
    try:
        with TallyfySDK(api_key=api_key, base_url=TALLYFY_API_BASE_URL) as sdk:
            for page in range(1, _MEMBERSHIP_MAX_PAGES + 1):
                result = sdk.get_current_user_organizations(
                    page=page, per_page=_MEMBERSHIP_PAGE_SIZE
                )
                organizations = getattr(result, "data", None)
                if not isinstance(organizations, list):
                    raise _UnexpectedShape()
                seen.update(getattr(o, "id", None) for o in organizations)
                if org_id in seen:
                    return
                if len(organizations) < _MEMBERSHIP_PAGE_SIZE:
                    exhausted = True
                    break
    except _UnexpectedShape:
        raise ToolError(
            _MEMBERSHIP_UNAVAILABLE.format(
                reason="your organization list came back in an unexpected shape"
            )
        )
    except Exception as exc:
        raise ToolError(
            _MEMBERSHIP_UNAVAILABLE.format(reason=type(exc).__name__)
        )

    if not exhausted:
        raise ToolError(
            _MEMBERSHIP_UNAVAILABLE.format(
                reason="your organization list is longer than this check reads"
            )
        )
    raise ToolError(
        "Org memory is scoped to organizations you are a member of, and "
        "this request names one you are not in. Nothing was read or "
        "written. Ask the user which of their organizations they mean."
    )


def register_org_context_tools(mcp):
    """Register org memory tools with the FastMCP server"""

    @mcp.tool(
        name="get_org_context",
        meta={
            "openai/toolInvocation/invoking": "Recalling org context...",
            "openai/toolInvocation/invoked": "Org context loaded",
        },
        description="""Read this organization's saved memory: a curated markdown document of durable facts learned in past conversations (naming conventions, key templates, who does what, decisions already made).

READ IT ONCE at the start of substantive work, before designing or building anything, so your advice is grounded in what is already true for this org rather than generic.

RETURNS: the document as markdown, or an honest empty answer when nothing has been saved yet. It is scoped to the authenticated organization only; there is no way to read another org's memory.

Pair with update_org_context to keep it current. If the user asks what you know about their org, show them this document and let them correct it.""",
        tags={"memory", "organization", "read-only"},
        annotations=ToolAnnotations(
            title="Read the org's saved memory",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        output_schema=None
    )
    @track_tool_execution("get_org_context")
    def get_org_context() -> ToolResult:
        """
        Read the authenticated organization's context document.

        Returns
        -------
        ToolResult
            The markdown document, or an honest empty-state message.
        """
        if not org_context_store.is_configured():
            raise ToolError(_UNCONFIGURED_MESSAGE)
        org_id = _authenticated_org_id()
        try:
            content = org_context_store.read_context(org_id)
        except org_context_store.OrgContextStoreError as exc:
            raise ToolError(
                f"Org memory could not be read right now ({exc}). Continue "
                "without it and say memory was unavailable if it matters."
            )
        if content is None or not content.strip():
            return ToolResult(content=_EMPTY_CONTEXT_MESSAGE, structured_content=None)
        return ToolResult(content=content, structured_content=None)

    @mcp.tool(
        name="update_org_context",
        meta={
            "openai/toolInvocation/invoking": "Saving org context...",
            "openai/toolInvocation/invoked": "Org context saved",
        },
        description="""Save this organization's memory: replace the whole curated markdown document of durable facts (naming conventions, key templates, who does what, decisions made).

FULL-REWRITE SEMANTICS: what you send REPLACES the document. Read get_org_context first, merge the new fact in, and send the complete updated document. The previous version is retained server-side so an administrator can restore it.

SAVE SILENTLY: save durable facts as you learn them, without announcing routine saves to the user. If the user asks what you know, show the document and let them correct it.

WHAT BELONGS HERE: stable, reusable facts (the org calls customers "members"; onboarding runs through the 'Client onboarding' template; fiscal year starts February). NOT conversation history, NOT task lists, NEVER passwords, API keys or tokens (credential-shaped content is refused).

LIMIT: 16 KB. Over the limit, curate: drop stale facts rather than splitting the document.""",
        tags={"memory", "organization", "write"},
        annotations=ToolAnnotations(
            title="Save the org's memory",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        output_schema=None
    )
    @track_tool_execution("update_org_context")
    def update_org_context(
        content: Annotated[str, Field(
            description="The complete, curated org memory document (markdown). Replaces the stored document in full.",
            max_length=65536,
        )]
    ) -> ToolResult:
        """
        Replace the authenticated organization's context document.

        Parameters
        ----------
        content : str
            The full curated markdown document.

        Returns
        -------
        ToolResult
            A short confirmation carrying the stored byte size.
        """
        if not org_context_store.is_configured():
            raise ToolError(_UNCONFIGURED_MESSAGE)
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError:
            # A lone surrogate is legal JSON, so this is caller-controlled
            # input reaching an encode. Unhandled it becomes FastMCP's generic
            # "error calling tool", which the model cannot act on.
            raise ToolError(
                "Org memory could not store this document because it contains "
                "characters that are not valid text. Remove any unusual "
                "symbols and save again. Nothing was changed."
            )
        if len(encoded) > MAX_CONTEXT_BYTES:
            raise ToolError(
                f"Org memory is limited to {MAX_CONTEXT_BYTES} bytes and this "
                f"document is {len(encoded)}. Curate it: keep durable facts, "
                "drop stale or conversational detail, then save again. "
                "Nothing was changed."
            )
        for label, pattern in _CREDENTIAL_PATTERNS:
            if pattern.search(content):
                raise ToolError(
                    "Org memory must never hold credentials, and this "
                    f"document contains what looks like {label}. Remove it "
                    "and save again. Nothing was changed."
                )
        org_id = _authenticated_org_id()
        try:
            org_context_store.write_context(org_id, content)
        except org_context_store.OrgContextStoreError as exc:
            raise ToolError(
                f"Org memory could not be saved ({exc}). The previous "
                "document is unchanged; try again later."
            )
        return ToolResult(
            content=(
                f"Org memory saved ({len(encoded)} bytes)."
            ),
            structured_content=None,
        )
