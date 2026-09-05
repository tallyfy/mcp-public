"""
Request logging middleware for MCP Server

Provides detailed request/response logging with:
- MCP protocol inspection (method, tool name)
- Session tracking and visual grouping
- User/org context extraction from JWT
- Sentry transaction tracking
- Color-coded terminal output
- A durable off-box record of every failure (#890)

The last one is why this file is the wiring point rather than somewhere tidier.
This middleware already computes the whole answer to "what failed, for whom" -
org, user, tool name, HTTP status, the MCP-level isError flag, the error text and
the duration - and then writes it only to stdout, which a deploy destroys.
Shipping the values it already has costs one call; recomputing them anywhere
else would mean re-parsing the request body and re-peeking the response.
"""

import json
import logging
import os
import time

import jwt

import sentry_sdk
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response, StreamingResponse

import metrics
from utils.org_id_middleware import ORG_ID_HEADERS, get_jwt_claims, get_org_id
from constants import MCP_SESSION_TIMEOUT
from durable_event_log import (
    EVENT_AUTH_FAILURE,
    EVENT_TOOL_ERROR,
    EVENT_UPSTREAM_REJECTED,
    ORG_SOURCE_HEADER,
    ORG_SOURCE_NONE_NO_BEARER,
    ORG_SOURCE_NONE_NO_ORG_CLAIM,
    ORG_SOURCE_NONE_UNPARSABLE_BEARER,
    ORG_SOURCE_SESSION_STORE,
    ORG_SOURCE_TOKEN_CLAIM,
    ORG_SOURCE_TOOL_ARGUMENT,
    ORG_SOURCE_VERIFIED_TOKEN,
    safe_org_label,
    SYNTHETIC_HEADER,
    SYNTHETIC_TRUTHY,
    TRANSPORT_DIRECT,
    TRANSPORT_NONE,
    TRANSPORT_OAUTH,
    TRANSPORT_UNKNOWN,
    get_event_log,
)

# The org_id header spellings OrgIdMiddleware accepts, as lowercase str (it
# holds them as bytes). Imported rather than re-listed so a spelling added there
# is automatically recognised here - the same reasoning utils/auth_context.py
# gives for its own copy of this line.
_ORG_ID_HEADER_NAMES = frozenset(name.decode("ascii") for name in ORG_ID_HEADERS)


# Maximum bytes the middleware will buffer to inspect a tools/call response
# for MCP-level errors (HTTP 200 + isError:true). Anything past this streams
# straight through. See issue #233.
TOOLS_CALL_INSPECT_BUFFER_BYTES = 65536


async def _peek_response_body(body_iterator, max_bytes: int):
    """Read up to ``max_bytes`` from an async chunk iterator.

    Returns ``(head, drained, overflow_chunk)`` where:
      - ``head`` is bytes accumulated up to (but never beyond) ``max_bytes``
      - ``drained`` is True iff the iterator finished within the cap
      - ``overflow_chunk`` is the *remainder* of the chunk that broke the cap
        (empty bytes if ``drained``); the caller is responsible for emitting it.

    Implements the bounded buffer described in issue #233 — the middleware must
    not materialize the full body in memory when the response exceeds the cap.
    """
    head = b""
    overflow = b""
    async for chunk in body_iterator:
        if len(head) + len(chunk) <= max_bytes:
            head += chunk
            continue
        capacity = max_bytes - len(head)
        if capacity > 0:
            head += chunk[:capacity]
        overflow = chunk[capacity:]
        return head, False, overflow
    return head, True, overflow


# ANSI color codes for terminal output
class Colors:
    """ANSI color codes for colored terminal output"""
    RESET = '\033[0m'
    BOLD = '\033[1m'

    # Status code colors
    GREEN = '\033[92m'   # 2xx success
    BLUE = '\033[94m'    # 3xx redirect
    YELLOW = '\033[93m'  # 4xx client error
    RED = '\033[91m'     # 5xx server error
    CYAN = '\033[96m'    # Info
    GRAY = '\033[90m'    # Muted

    @staticmethod
    def status_color(status_code: int) -> str:
        """Get color based on HTTP status code"""
        if 200 <= status_code < 300:
            return Colors.GREEN
        elif 300 <= status_code < 400:
            return Colors.BLUE
        elif 400 <= status_code < 500:
            return Colors.YELLOW
        else:
            return Colors.RED


# Session tracking for visual grouping of MCP requests
_mcp_sessions: dict[str, dict] = {}  # session_id -> {"user_id": str, "last_activity": float, "short_id": str}
_pending_sessions: dict[str, float] = {}  # user_id -> timestamp (for sessions just initialized)
_SESSION_TIMEOUT = MCP_SESSION_TIMEOUT

# Colors for different users (cycling through)
_USER_COLORS = [
    '\033[38;5;39m',   # Blue
    '\033[38;5;208m',  # Orange
    '\033[38;5;141m',  # Purple
    '\033[38;5;49m',   # Teal
    '\033[38;5;204m',  # Pink
    '\033[38;5;227m',  # Yellow
]


def _get_user_color(user_id: str) -> str:
    """Get a consistent color for a user based on their ID."""
    if not user_id:
        return Colors.GRAY
    # Simple hash to pick a color
    color_idx = hash(user_id) % len(_USER_COLORS)
    return _USER_COLORS[color_idx]


def _get_short_session_id(session_id: str) -> str:
    """Get a short 4-char identifier from session ID."""
    if not session_id:
        return "----"
    # Use last 4 chars as they're more likely to be unique
    return session_id[-4:] if len(session_id) >= 4 else session_id.ljust(4, '-')


def resolve_transport(auth_header, jwt_claims):
    """How the credential arrived: ``oauth``, ``direct``, ``none`` or ``unknown``.

    Derived by the SERVER from what was presented (#996 AC2). It is deliberately
    not ``X-Client-Type``, which is free text the caller picks and which read
    ``direct`` on all 39 rows of the burst that started #996 while also reading
    ``issue890-verify`` on the two rows that gave that burst away.

    The discriminator is the ``mcp_scopes`` claim, and it is the one this
    codebase already relies on: ``McpAccessTokenService::issue`` mints it on
    every OAuth-brokered MCP token, and a raw Tallyfy session token forwarded by
    chat.tallyfy.com or the desktop AI shell carries no such claim
    (``utils.auth_context.get_mcp_scopes`` documents both halves).

    The claims come from OrgIdMiddleware's UNVERIFIED decode, which is correct
    here and would not be for an authorization decision: the question is what
    shape of credential the caller presented, not whether it was any good. On a
    401 the answer is still wanted, and by then there is nothing verified left
    to read.
    """
    if not auth_header or "bearer" not in auth_header.lower():
        return TRANSPORT_NONE
    if not jwt_claims:
        # A bearer arrived and did not decode as a JWT, or the path skipped
        # OrgIdMiddleware entirely. Either way this row cannot say.
        return TRANSPORT_UNKNOWN
    return TRANSPORT_OAUTH if "mcp_scopes" in jwt_claims else TRANSPORT_DIRECT


def resolve_claimed_org(*, resolved_org, resolved_source, jwt_claims, has_auth,
                        header_org=None):
    """The organization the caller NAMED, and where the name came from (#996 AC1).

    Returns ``(claimed_org_id, org_id_source)``. Never asserts that anything was
    verified: on an auth failure nothing was, which is why the shipper blanks the
    ``org_id`` column for that event and keeps only this pair.

    ``jwt_claims`` here is ``OrgIdMiddleware``'s UNVERIFIED decode, which is
    correct for this field and would not be for ``org_id``. The question is what
    the caller NAMED, and on a 401 there is nothing verified left to read. The
    value is bounded by ``safe_org_label`` all the same, because "unverified" is
    not a licence to store arbitrary bytes. A claim that fails that check is
    therefore NOT recorded, and this returns ``none_no_org_claim``: an
    unusable name is not a name, and the closed vocabulary has no member for
    "named something malformed". The row still carries the user, the transport,
    the session reference and the status, so the caller is not lost.

    When no organization can be named at all the source is one of the ``none_*``
    reasons, because "953 of 995 rows name nobody" is only actionable once the
    rows say which kind of nobody. Three kinds exist and they want different
    fixes: no credential was sent, one was sent and was not a JWT, or one was
    sent and simply named no organization.
    """
    org = (resolved_org or "").strip()
    if org and org != "unknown":
        return org, resolved_source
    # Sanitised before it is recorded anywhere. This one is an UNVERIFIED
    # decode, so an unauthenticated caller chooses it; it is honest in
    # `claimed_org_id`, whose name says exactly what it is worth, and it must
    # still not be able to carry a newline into a stored field.
    # A header the caller sent, on a request that at least presented a bearer.
    # It reaches THIS field and never the resolved one: `resolve_request_org`
    # takes a header only from a request that authenticated, so on a refused or
    # anonymous request the header is a claim and nothing more. `jwt_claims`
    # being empty means no bearer was presented at all, and an anonymous caller
    # must not be able to write either column - which is `develop`'s behaviour
    # and is measured in the tests.
    if jwt_claims and header_org:
        return header_org, ORG_SOURCE_HEADER
    claim = safe_org_label((jwt_claims or {}).get("org_id"))
    if claim:
        return claim, ORG_SOURCE_TOKEN_CLAIM
    if not has_auth:
        return None, ORG_SOURCE_NONE_NO_BEARER
    if not jwt_claims:
        return None, ORG_SOURCE_NONE_UNPARSABLE_BEARER
    return None, ORG_SOURCE_NONE_NO_ORG_CLAIM


def request_is_synthetic(request):
    """True when the caller declared itself test traffic (#996 AC5).

    One dedicated header, checked for an explicit truthy value. Absent means
    real, so an unlabelled harness is treated as a customer - the direction that
    gets investigated rather than the direction that gets ignored.
    """
    try:
        value = request.headers.get(SYNTHETIC_HEADER, "")
    except Exception:
        return False
    return str(value).strip().lower() in SYNTHETIC_TRUTHY


# The longest organization id this middleware will record. Tallyfy ids are
# 32-char hex; the bound is deliberately loose so a legitimate value is never
# dropped, and tight enough that nothing long can be smuggled into a log line.
def _org_header_value(request):
    """The organization id this request's headers carry, bounded, or None.

    Last match wins, mirroring ``OrgIdMiddleware._extract_from_headers`` and
    ``utils.auth_context._client_supplied_org_id``. Reading it is not the same
    as trusting it: every caller of this decides for itself whether the request
    earned the right to be believed.
    """
    value = None
    try:
        for name, raw in request.headers.items():
            if name.lower() in _ORG_ID_HEADER_NAMES and raw:
                value = raw
    except Exception:
        return None
    return safe_org_label(value)


def _request_has_org_header(request):
    """True when this request carries any organization header spelling at all.

    Asked separately from its VALUE because the presence alone decides whether
    ``get_org_id()`` can be read as the session store: ``OrgIdMiddleware``
    overwrites that ContextVar with the header when one is present, so with a
    header in play the ContextVar is the caller's value wearing the store's
    clothes.
    """
    try:
        return any(
            name.lower() in _ORG_ID_HEADER_NAMES and raw
            for name, raw in request.headers.items()
        )
    except Exception:
        return False


def request_authenticated(request):
    """Did THIS request get past authentication? Read from the same place as
    ``verified_org_claim``, and None-safe everywhere.

    ``AuthenticationMiddleware`` sets ``scope["user"]`` to an
    ``AuthenticatedUser`` carrying the accepted token, or to an
    ``UnauthenticatedUser`` carrying nothing. The discriminator is therefore the
    presence of an access token, not the presence of the key.
    """
    user = request.scope.get("user")
    return getattr(getattr(user, "access_token", None), "token", None) is not None


def verified_org_claim(request):
    """The ``org_id`` claim on the token THIS request AUTHENTICATED with, or None.

    🔴 THIS IS THE ONLY ORGANIZATION ON THIS PATH THAT ANYTHING CHECKED, AND IT
    IS DELIBERATELY NOT ``get_jwt_claims()``. That accessor returns an
    UNVERIFIED decode: ``OrgIdMiddleware`` decodes the Authorization header with
    ``verify_signature=False``, so its ``org_id`` needs no valid signature and
    an unauthenticated caller chooses it outright. Routing that into the durable
    ``org_id`` column would make the one field a reader treats as identity
    caller-writable, in a change whose whole purpose is a trustworthy incident
    record. ``durable_event_log`` says of that column that it "reads as though
    the caller proved it".

    Read instead from ``scope["user"]``, which Starlette's
    ``AuthenticationMiddleware`` sets from fastmcp's ``BearerAuthBackend``
    AFTER the RS256 signature and expiry have been checked
    (``fastmcp/server/auth/auth.py::get_middleware``). That middleware is part
    of the app ``mcp.http_app()`` builds, and every ``app.add_middleware`` call
    in ``server.py`` PREPENDS, so it sits INSIDE this one and has already run by
    the time ``call_next`` returns.

    The ASGI ``scope`` is one dict shared by reference the whole way down, so a
    key set downstream IS visible here afterwards. Measured 2026-09-04 with
    three arms in one run: a key set by the inner app reads None before
    ``call_next`` and its value after, a key set by the outer ASGI middleware
    reads its value both times, and a key nobody sets reads None throughout.

    Returns None whenever the request did not authenticate, which is the whole
    point: an unauthenticated caller gets no say in this value. Callers must
    treat None as "no verified organization" and fall back, never as an error.
    """
    try:
        user = request.scope.get("user")
        access_token = getattr(user, "access_token", None)
        raw_token = getattr(access_token, "token", None)
        if not raw_token:
            return None
        claims = jwt.decode(raw_token, options={"verify_signature": False})
    except Exception:
        return None
    return safe_org_label(claims.get("org_id"))


def best_effort_request_org(request):
    """The organization for the ENTRY log line, printed BEFORE anything authenticates.

    Returns ``(org_id_or_None, org_id_source_or_None)``.

    🔴 THIS AND ``resolve_request_org`` BELOW ARE TWO DIFFERENT CONTRACTS AND THE
    DIFFERENCE IS DELIBERATE. **The entry line is BEST EFFORT. The durable record
    is AUTHORITATIVE.** This one runs before ``call_next``, so nothing has been
    through the auth middleware and no verified answer exists yet; it reads the
    ContextVar exactly as ``develop`` did, which means that when the request
    carries an organization header the value IS that header.

    ⚠️ **So this value CAN be influenced by an unauthenticated caller, and that is
    ACCEPTED here rather than fixed.** It is a best-effort operator hint on one
    terminal log line, structurally nothing better exists at this point in the
    request, and the alternative measured worse: gating it printed ``org=unknown``
    for every request carrying an organization header, which is every MCP request
    from chat.tallyfy.com, since ``host/client/authenticated_client.py:31`` sets
    that header on all of them and those session tokens carry no ``org_id`` claim.
    For a successful ``tools/call`` the entry line is the ONLY terminal line
    carrying an organization at all, so gating it removed the organization from
    the terminal log entirely for the primary first-party client.

    ``safe_org_label`` still applies, so the injection class stays closed: a
    caller may influence WHICH organization this line names, and may not forge a
    log line.

    Nothing here reaches the durable record. ``dispatch`` replaces this value with
    the authoritative one after ``call_next``, and clears it when the
    authoritative pass resolves nothing.
    """
    org = safe_org_label(get_org_id())
    if not org:
        return None, None
    if _request_has_org_header(request):
        return org, ORG_SOURCE_HEADER
    return org, ORG_SOURCE_SESSION_STORE


def resolve_request_org(request, verified_org=None, authenticated=False):
    """The organization of the request being handled, and where the name came from.

    Returns ``(org_id_or_None, org_id_source_or_None)``. This feeds the RESOLVED
    column, the one ``durable_event_log`` says "reads as though the caller proved
    it". What the caller merely NAMED travels separately, in ``claimed_org_id``
    via ``resolve_claimed_org`` above.

    ⚠️ **THE GATE THIS BUILDS IS HEADER-SHAPED, NOT COLUMN-SHAPED, AND SAYING
    OTHERWISE WOULD BE FALSE.** It stops an organization HEADER from an
    unauthenticated request reaching the resolved column. It does NOT make that
    column unreachable by every caller-supplied value: ``dispatch`` reads
    ``params.arguments.org_id`` out of the request body and assigns it with source
    ``tool_argument`` under no authentication gate at all. That path is
    pre-existing and byte-identical on ``develop``, a review could not construct a
    production shape in which an anonymous caller reaches it, and it is
    deliberately left alone here rather than changed inside a security fix.
    It is a known exception to the sentence above, not an oversight.

    1. ``verified_org``, the claim on the token this request authenticated with.
       It wins outright there and it wins outright here. **This is the #987
       fix**: with no organization header, ``OrgIdMiddleware`` seeds
       ``org_id_context`` from the per-user session store, which on the first
       request after a token refresh that changes organization still holds the
       PREVIOUS one, and the verified answer is resolved downstream where it
       cannot be written back (see the class docstring below).
    2. An explicit organization header, **only on a request that
       authenticated**. That is the header's legitimate use, a token valid for
       several organizations picking one, and it is exactly the case
       ``get_authenticated_credentials`` honours when the token names none.
    3. The ContextVar, **only when no organization header is present**, at which
       point it can only be this user's persisted organization, written by
       ``store_org_id_for_user`` after a verified request. With a header in play
       the same ContextVar holds the header value instead, which is why the two
       cannot be collapsed.

    🔴 READ THIS BEFORE LOOSENING THE HEADER ARM. An earlier cut read the header
    straight off the request with no authentication gate at all. Measured on both
    refs with NO Authorization header on any arm: a ``GET`` of an unrouted path
    carrying a chosen organization header recorded ``org_id: unknown``, source
    ``none_no_bearer`` on ``develop``, and the CALLER'S CHOSEN VALUE with source
    ``header`` on that cut. Worse, ``/.env`` shipped zero durable rows on
    ``develop`` and one on that cut, because ``is_scanner`` below requires
    ``org_id == 'unknown'``, so any organization header defeated scanner
    suppression outright and turned suppressed probe noise into durable rows
    naming a real company. ``server.py`` registers a catch-all 404 route and the
    rate limiter allows 100 unauthenticated requests a minute per IP.

    ⚠️ **This is deliberately STRICTER than ``develop``, and the difference is
    one case.** ``develop`` honoured the header whenever ``OrgIdMiddleware`` had
    decoded a bearer carrying a ``sub``, and an unsigned JWT with any ``sub`` is
    free to write, so that gate stopped nobody who was trying. Here a refused
    bearer's header does not name the organization on the row. It is still
    recorded as what the caller CLAIMED, which is the #996 contract and is what
    that field is for.

    ``verified_org`` and ``authenticated`` are both false on the early call,
    before ``call_next``, because nothing has authenticated at that point. That
    is not a gap that could be closed by resolving harder: the answer does not
    exist yet.
    """
    if verified_org:
        return verified_org, ORG_SOURCE_VERIFIED_TOKEN

    if authenticated:
        header_org = _org_header_value(request)
        if header_org:
            return header_org, ORG_SOURCE_HEADER

    if not _request_has_org_header(request):
        context_org = safe_org_label(get_org_id())
        if context_org:
            return context_org, ORG_SOURCE_SESSION_STORE

    return None, None


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log HTTP requests with timing, status, and authentication info.

    WHY THE #987 FIX IS HERE AND NOT IN ``get_authenticated_credentials``
    --------------------------------------------------------------------
    The obvious repair for "the log names the previous organization" is to have
    ``utils.auth_context.get_authenticated_credentials`` write its verified
    answer back into ``org_id_context`` with the ``set_org_id()`` that already
    exists and that nothing calls. **That write cannot reach this middleware,
    and a dead write into a ContextVar documented as observability-only is
    worse than none.**

    ``BaseHTTPMiddleware.call_next`` runs the downstream app in a task of its
    own, and a task copies the context at the moment it is spawned, so a
    ``ContextVar.set`` performed downstream is invisible here afterwards.
    Measured 2026-09-04 with both arms in one run, on starlette 1.3.1 (the
    version ``server/requirements.txt`` pins, so the one the container runs)
    and again on 0.46.2: a value set by the OUTER pure-ASGI middleware
    (``OrgIdMiddleware``) IS visible in ``dispatch`` before and after
    ``call_next``, while a value set by the inner app is visible only inside
    the app. Both readings identical. So the two ``get_org_id()`` reads in this
    file, and the durable record built from them, would go on naming the
    previous organization however late the write happened.

    What DOES cross that boundary is the ASGI ``scope``, one dict shared by
    reference from the outermost middleware to the innermost. fastmcp's
    ``AuthenticationMiddleware`` runs inside this one and puts the VERIFIED
    access token on ``scope["user"]``, so by the time ``call_next`` returns the
    answer is here, checked, and reachable without touching ``org_id_context``,
    the per-user store, or any authorization decision. ``verified_org_claim``
    above is that read.

    ⚠️ **The unverified decode in ``jwt_claims_context`` is deliberately NOT the
    source for this.** An earlier cut of this change used it, and an
    unauthenticated caller could then choose the organization on a
    ``mcp_upstream_rejected`` row and force a newline into the terminal log
    line. The unverified claim keeps exactly the job it already had: it fills
    ``claimed_org_id``, whose name says what it is worth.
    """

    async def dispatch(self, request: StarletteRequest, call_next):
        start_time = time.time()

        # Extract request info
        method = request.method
        path = request.url.path

        # Extract auth info (without exposing full token)
        auth_header = request.headers.get("authorization", "")
        has_auth = "Bearer" in auth_header
        # The organization of the request being handled, and which of the three
        # possible origins produced it. Tracked at every assignment rather than
        # reconstructed at the end, because by then the three are
        # indistinguishable.
        # BEST EFFORT, and only for the entry line printed below. Nothing has
        # authenticated at this point in the request, so this reads the
        # ContextVar exactly as `develop` did and can name a header an
        # unauthenticated caller chose. That is an accepted limit on one
        # operator hint, not a hole in the record: the authoritative pass after
        # `call_next` replaces this value, and clears it when it resolves
        # nothing. See `best_effort_request_org`.
        org_id, org_id_source = best_effort_request_org(request)
        org_id = org_id or 'unknown'

        # Extract MCP session ID for request grouping
        mcp_session_id = request.headers.get("mcp-session-id", "")

        # Read pre-decoded JWT claims from OrgIdMiddleware (P2-I — single decode per request)
        claims = get_jwt_claims()
        user_id = None
        if claims:
            user_id = claims.get('sub') or claims.get('user_id') or claims.get('uid')

        # Extract MCP protocol info for requests to the MCP transport paths.
        # The transport is served at '/' and aliased at '/mcp' + '/mcp/'
        # (see server.py), so all three must extract mcp_method / tool name.
        mcp_method = None
        mcp_tool_name = None
        is_mcp_root = path in ("/", "/mcp", "/mcp/") and method in ["POST", "GET"]

        if is_mcp_root and method == "POST":
            try:
                # Read and cache body for MCP protocol inspection
                body = await request.body()
                if body:
                    try:
                        json_body = json.loads(body)
                        mcp_method = json_body.get("method", "")
                        # Extract tool name and org_id for tools/call requests
                        if mcp_method == "tools/call":
                            params = json_body.get("params", {})
                            mcp_tool_name = params.get("name", "unknown")
                            # Extract org_id from tool arguments (more reliable than header for MCP)
                            arguments = params.get("arguments", {})
                            # Sanitised like every other caller-supplied
                            # source: a tool-call argument is arbitrary JSON
                            # from the request body and reaches the log line
                            # below, so a newline in it forges a whole line.
                            body_org_id = safe_org_label(arguments.get("org_id"))
                            if body_org_id:
                                org_id = body_org_id
                                org_id_source = ORG_SOURCE_TOOL_ARGUMENT
                    except json.JSONDecodeError:
                        pass
            except Exception:
                pass

        # Determine if this is a new session (for visual grouping)
        is_new_session = False
        is_known_session = False
        short_session_id = "----"

        if mcp_method == "initialize":
            # initialize always starts a new session
            is_new_session = True
            # Mark this user as having a pending session (for notifications/initialized)
            if user_id:
                _pending_sessions[user_id] = start_time
        elif mcp_session_id:
            short_session_id = _get_short_session_id(mcp_session_id)

            # Check if we've seen this session before
            if mcp_session_id in _mcp_sessions:
                is_known_session = True
            else:
                # New session ID - check if it's from a user with a pending session
                # (this handles notifications/initialized right after initialize)
                if user_id and user_id in _pending_sessions:
                    # This is continuation after initialize, not a new session
                    is_known_session = True
                    del _pending_sessions[user_id]
                else:
                    is_new_session = True

            # Update session tracking
            _mcp_sessions[mcp_session_id] = {
                "user_id": user_id,
                "last_activity": start_time,
                "short_id": short_session_id
            }

            # Cap session dict size to prevent unbounded memory growth
            MAX_SESSIONS = 1000
            if len(_mcp_sessions) > MAX_SESSIONS:
                # Evict oldest sessions
                sorted_sessions = sorted(
                    _mcp_sessions.items(),
                    key=lambda x: x[1].get('last_activity', 0)
                )
                for sid, _ in sorted_sessions[:len(_mcp_sessions) - MAX_SESSIONS]:
                    del _mcp_sessions[sid]

            # Clean up stale sessions and pending sessions
            stale_threshold = start_time - _SESSION_TIMEOUT
            stale_sessions = [sid for sid, info in _mcp_sessions.items()
                           if info["last_activity"] < stale_threshold]
            for sid in stale_sessions:
                del _mcp_sessions[sid]

            # Clean up old pending sessions (> 5 seconds)
            old_pending = [uid for uid, t in _pending_sessions.items()
                         if start_time - t > 5.0]
            for uid in old_pending:
                del _pending_sessions[uid]

            # Publish the session count (#1161). Set here rather than on every
            # request because this is the one place the dict is authoritative:
            # immediately after the MAX_SESSIONS trim and the stale sweep.
            metrics.set_active_sessions(len(_mcp_sessions))

        # For tools/call requests, log the entry BEFORE processing so it appears before tool execution logs
        tools_call_logged = False
        tools_call_prefix = ""
        if mcp_method == "tools/call" and mcp_tool_name:
            tools_call_logged = True
            user_color = _get_user_color(user_id) if user_id else Colors.GRAY
            if is_new_session:
                session_tag = f"{user_color}[new ]{Colors.RESET}"
                tools_call_prefix = f"{Colors.CYAN}┌─{Colors.RESET}{session_tag}"
            elif is_known_session:
                session_tag = f"{user_color}[{short_session_id}]{Colors.RESET}"
                tools_call_prefix = f"{Colors.GRAY}│ {Colors.RESET}{session_tag}"
            else:
                tools_call_prefix = "        "
            display_path = f"mcp:tools/call({mcp_tool_name})"
            context_parts = []
            if user_id:
                context_parts.append(f"user={user_id}")
            if org_id:
                context_parts.append(f"org={org_id}")
            context_str = " │ ".join(context_parts) if context_parts else ""
            logging.info(f"{tools_call_prefix} {Colors.CYAN}{method:6}{Colors.RESET} {display_path:40} │ {context_str}")

        # Skip Sentry transaction for /metrics endpoint (high frequency monitoring traffic)
        should_trace = path != "/metrics"
        transaction = None

        try:
            # Increment INSIDE the try (#1161). It used to sit ~28 lines above,
            # outside this block, so an exception raised while building the log
            # strings leaked a permanent +1 that only a restart cleared. Those
            # lines only format strings, so nothing was observed leaking, but
            # the pairing was one edit away from being broken. The decrement is
            # in this try's finally; keep them in the same block.
            metrics.increment_requests_in_flight()

            # Start Sentry transaction for performance monitoring (skip /metrics)
            if should_trace:
                transaction = sentry_sdk.start_transaction(
                    op="http.server",
                    name=f"{method} {path}"
                )
                transaction.set_tag("method", method)
                transaction.set_tag("path", path)
                transaction.set_tag("has_auth", has_auth)
                if org_id:
                    # Only set first 8 chars for privacy
                    transaction.set_tag("org_id", org_id)
                if mcp_method:
                    transaction.set_tag("mcp_method", mcp_method)
                if mcp_tool_name:
                    transaction.set_tag("mcp_tool", mcp_tool_name)
                transaction.__enter__()

            # Extract X-Client-Type header for Sentry tagging
            client_type = request.headers.get("x-client-type", "direct")
            sentry_sdk.set_tag("client_type", client_type)

            try:
                # Process request
                response = await call_next(request)
                status = response.status_code

                # For tools/call requests, inspect response body for MCP-level errors
                # MCP returns HTTP 200 but includes isError:true in the body for validation failures
                mcp_error = False
                mcp_error_msg = None
                if mcp_method == "tools/call" and status == 200:
                    # Inspect response body for MCP-level errors (HTTP 200 + isError:true).
                    # Memory cap (issue #233): peek up to TOOLS_CALL_INSPECT_BUFFER_BYTES
                    # for inspection. If the body fits, re-emit via Response; otherwise
                    # switch to StreamingResponse so further chunks bypass middleware memory.
                    head_buf, body_drained, overflow_first_chunk = await _peek_response_body(
                        response.body_iterator, TOOLS_CALL_INSPECT_BUFFER_BYTES
                    )

                    # Only attempt error detection on small responses —
                    # large responses (>64KB) are almost certainly successful tool results
                    if body_drained and head_buf:
                        try:
                            # Parse SSE format - extract JSON from "data: " line
                            # Response is in SSE format: "event: message\r\ndata: {json}\r\n\r\n"
                            response_text = head_buf.decode('utf-8')
                            json_data = None

                            for line in response_text.split('\n'):
                                line = line.strip()
                                if line.startswith('data: '):
                                    json_data = line[6:]  # Remove "data: " prefix
                                    break

                            if json_data is None:
                                # NOT SSE. server.py:258 builds the app with
                                # json_response=True, so FastMCP answers every
                                # tools/call with content-type application/json
                                # and no framing. Parsing only `data: ` lines
                                # therefore detected NOTHING on any deployment
                                # of this server: mcp_error could never be True,
                                # and an HTTP 200 carrying isError:true - the
                                # #652 shape, the one #890 was filed about - was
                                # invisible to every consumer of this flag,
                                # including the durable record.
                                #
                                # Measured on live staging 2026-08-22: a failing
                                # tools/call returned HTTP/2 200,
                                # content-type: application/json, isError true,
                                # and this middleware logged it with no error
                                # marker at all.
                                #
                                # The SSE branch is kept because json_response
                                # is a server.py argument that can be changed
                                # back, and a parser that understands only the
                                # framing in use today is how this bug happened.
                                json_data = response_text.strip()

                            if json_data:
                                response_json = json.loads(json_data)

                                # Check for JSON-RPC level error first
                                if "error" in response_json:
                                    mcp_error = True
                                    error_obj = response_json["error"]
                                    mcp_error_msg = error_obj.get("message", str(error_obj))[:100]
                                else:
                                    # Check for tool error in result
                                    result = response_json.get("result", {})
                                    if isinstance(result, dict) and result.get("isError") is True:
                                        mcp_error = True
                                        # Extract error message from content
                                        content = result.get("content", [])
                                        if content and isinstance(content, list):
                                            for item in content:
                                                if isinstance(item, dict) and item.get("type") == "text":
                                                    mcp_error_msg = item.get("text", "")[:100]
                                                    break
                        except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as e:
                            logging.debug(f"Failed to parse tools/call response: {e}")

                    if body_drained:
                        # Whole body is in head_buf — safe to re-emit as a single Response
                        response = Response(
                            content=head_buf,
                            status_code=response.status_code,
                            headers=dict(response.headers),
                            media_type=response.media_type
                        )
                    else:
                        # Body exceeded cap — stream the head + overflow + remainder
                        # without holding the full body in middleware memory.
                        original_iterator = response.body_iterator

                        async def _passthrough(head=head_buf, overflow=overflow_first_chunk,
                                               tail=original_iterator):
                            yield head
                            if overflow:
                                yield overflow
                            async for chunk in tail:
                                yield chunk

                        # Drop content-length: we no longer know the byte total here.
                        passthrough_headers = {
                            k: v for k, v in response.headers.items()
                            if k.lower() != "content-length"
                        }
                        response = StreamingResponse(
                            _passthrough(),
                            status_code=response.status_code,
                            headers=passthrough_headers,
                            media_type=response.media_type,
                        )

                # Calculate duration AFTER body is fully read (includes tool execution time)
                duration = time.time() - start_time

                # Re-read the organization now the response is in hand.
                # This is the read that matters: the request has been through
                # the auth middleware by now, so `verified_org_claim` can
                # answer, and this value is what the terminal log line, the
                # Sentry tag and the durable record all carry.
                #
                # A verified organization DOES displace one named in the tool
                # call's own arguments. Those two can disagree, and when they
                # do the request acted on the verified one, because
                # `get_authenticated_credentials` never reads a tool argument.
                # Recording the argument would name an organization the request
                # did not touch.
                verified_org = verified_org_claim(request)
                org_id_resolved, org_source_resolved = resolve_request_org(
                    request,
                    verified_org=verified_org,
                    authenticated=request_authenticated(request),
                )
                if org_id_resolved:
                    org_id = org_id_resolved
                    org_id_source = org_source_resolved
                elif org_id_source != ORG_SOURCE_TOOL_ARGUMENT:
                    # The authoritative pass resolved nothing, so the best-effort
                    # value from before `call_next` must NOT survive into the
                    # durable record. Without this a refused bearer's header
                    # would reach the column that reads as identity, through the
                    # entry line's back door. The tool-argument case is exempt
                    # because it never came from the best-effort read and its
                    # precedence is deliberately unchanged here.
                    org_id = 'unknown'
                    org_id_source = None
                if should_trace and transaction:
                    # The tag set before call_next carried the best-effort value.
                    # Overwrite it with whatever the authoritative pass settled
                    # on, including 'unknown', rather than leaving Sentry
                    # disagreeing with the durable record beside it.
                    transaction.set_tag("org_id", org_id)
                # Record transaction data
                if should_trace and transaction:
                    transaction.set_data("duration_ms", duration * 1000)
                    transaction.set_data("status_code", status)
                    if mcp_error:
                        transaction.set_data("mcp_error", True)
            except Exception:
                duration = time.time() - start_time
                # Exception propagates to framework — LoggingIntegration captures it
                raise
        finally:
            # Close Sentry transaction if it was created
            if should_trace and transaction:
                transaction.__exit__(None, None, None)

            # Decrement the in-flight gauge. Paired with the increment at the
            # top of this same try.
            metrics.decrement_requests_in_flight()

        # Known scanner/bot paths - demote to DEBUG level when unauthenticated
        _SCANNER_PATTERNS = {
            '.env', '.git', 'wp-login', 'wp-admin', 'xmlrpc.php',
            'wlwmanifest', '.php', 'wp-includes', 'wp-content',
            'cgi-bin', '.asp', '.aspx', 'phpmyadmin', 'adminer',
        }
        is_scanner = (
            not has_auth
            and org_id == 'unknown'
            and any(pattern in path.lower() for pattern in _SCANNER_PATTERNS)
        )

        # Filter out noise:
        # 1. /sse endpoint 404s (expected - SSE not implemented)
        # 2. DELETE 400s (expired session cleanup - normal behavior)
        # 3. /metrics endpoint 200s (successful Prometheus scrapes - high frequency)
        # 4. Scanner/bot probes from unauthenticated sources
        # 5. OAuth discovery probes (POST / → 401 without MCP body — RFC 9728 flow)
        is_sse_404 = path == "/sse" and status == 404
        is_delete_400 = method == "DELETE" and status == 400
        is_metrics_200 = path == "/metrics" and status == 200
        is_health_200 = path == "/health" and status == 200
        is_favicon = path == "/favicon.ico"
        is_oauth_discovery = (
            is_mcp_root and status == 401 and not mcp_method
        )

        if is_scanner:
            logging.debug(f"{Colors.GRAY}[scanner] {method:6} {path:40} │ {status:>3} │ {duration*1000:6.1f}ms{Colors.RESET}")
        elif is_oauth_discovery:
            # RFC 9728: client probes resource to trigger WWW-Authenticate discovery — expected noise
            logging.debug(f"{Colors.GRAY}[oauth-discovery] {method:6} {path:40} │ {status:>3} │ {duration*1000:6.1f}ms{Colors.RESET}")
        elif is_sse_404:
            # Completely suppress /sse 404s - this is expected noise
            pass
        elif is_delete_400:
            # Log DELETE 400s at debug level only
            logging.debug(f"{Colors.GRAY}{method:6} {path:20} │ {status} │ {duration*1000:6.1f}ms │ session_cleanup{Colors.RESET}")
        elif is_metrics_200:
            # Completely suppress successful /metrics requests - high frequency monitoring traffic
            pass
        elif is_health_200:
            pass
        elif is_favicon:
            pass
        else:
            # Build context parts for display
            context_parts = []

            if user_id:
                context_parts.append(f"user={user_id}")

            if org_id:
                context_parts.append(f"org={org_id}")
            elif has_auth:
                context_parts.append("authenticated")
            else:
                context_parts.append("no-auth")

            context_str = " │ ".join(context_parts) if context_parts else "no-context"

            # Color-code status - use RED for MCP errors even if HTTP 200
            if mcp_error:
                color = Colors.RED
                display_status = "ERR"
            else:
                color = Colors.status_color(status)
                display_status = str(status)

            # Determine display path - show MCP method for root requests
            if is_mcp_root and mcp_method:
                if mcp_tool_name:
                    display_path = f"mcp:{mcp_method}({mcp_tool_name})"
                else:
                    display_path = f"mcp:{mcp_method}"
            else:
                display_path = path

            # Get user color for visual distinction between different users
            user_color = _get_user_color(user_id) if user_id else Colors.GRAY

            # Visual grouping prefix for MCP requests with session tag
            if is_new_session:
                # New session starts with a header line
                session_tag = f"{user_color}[new ]{Colors.RESET}"
                prefix = f"{Colors.CYAN}┌─{Colors.RESET}{session_tag}"
            elif is_known_session:
                # Continuation of existing session - show short session ID
                session_tag = f"{user_color}[{short_session_id}]{Colors.RESET}"
                prefix = f"{Colors.GRAY}│ {Colors.RESET}{session_tag}"
            else:
                # Non-MCP request or unknown context
                prefix = "        "  # 8 spaces to align with session tags

            # Build the log message
            log_msg = (
                f"{prefix} {color}{method:6}{Colors.RESET} {display_path:40} │ "
                f"{color}{display_status:>3}{Colors.RESET} │ "
                f"{duration*1000:6.1f}ms │ {context_str}"
            )

            # Add MCP error message if present
            if mcp_error and mcp_error_msg:
                log_msg += f"\n{Colors.GRAY}│ {Colors.RESET}        {Colors.RED}└─ {mcp_error_msg}{Colors.RESET}"

            # Use warning level for MCP errors (error-level Sentry events come from
            # @handle_tallyfy_errors — the single source of truth for tool errors)
            if mcp_error:
                logging.warning(log_msg)
            elif tools_call_logged:
                # For tools/call, log a brief completion line with status and duration
                # Use │ for completion line (not ┌─) since this is a continuation
                if is_new_session:
                    completion_prefix = f"{Colors.GRAY}│ {Colors.RESET}{user_color}[new ]{Colors.RESET}"
                elif is_known_session:
                    completion_prefix = f"{Colors.GRAY}│ {Colors.RESET}{user_color}[{short_session_id}]{Colors.RESET}"
                else:
                    completion_prefix = "        "
                logging.info(
                    f"{completion_prefix} {color}{method:6}{Colors.RESET} {'└─ completed':40} │ "
                    f"{color}{display_status:>3}{Colors.RESET} │ "
                    f"{duration*1000:6.1f}ms"
                )
            else:
                logging.info(log_msg)

        # Durable record (#890). Everything above this line goes to stdout and
        # dies with the container; this call is the only thing that outlives a
        # deploy. It is deliberately the LAST statement before the response is
        # returned, so it can never change what the caller receives.
        claimed_org_id, claimed_org_source = resolve_claimed_org(
            resolved_org=org_id,
            resolved_source=org_id_source,
            jwt_claims=claims,
            has_auth=has_auth,
            header_org=_org_header_value(request),
        )
        self._record_failure_durably(
            mcp_error=mcp_error,
            mcp_error_msg=mcp_error_msg,
            status=status,
            org_id=org_id,
            user_id=user_id,
            tool_name=mcp_tool_name,
            mcp_method=mcp_method,
            duration=duration,
            session_id=mcp_session_id,
            client_type=client_type,
            claimed_org_id=claimed_org_id,
            org_id_source=claimed_org_source,
            transport=resolve_transport(auth_header, claims),
            synthetic=request_is_synthetic(request),
            is_noise=(
                is_scanner
                or is_oauth_discovery
                or is_sse_404
                or is_delete_400
                or is_favicon
            ),
        )

        return response

    @staticmethod
    def _record_failure_durably(
        *,
        mcp_error: bool,
        mcp_error_msg,
        status: int,
        org_id,
        user_id,
        tool_name,
        mcp_method,
        duration: float,
        session_id,
        client_type,
        claimed_org_id=None,
        org_id_source=None,
        transport=None,
        synthetic: bool = False,
        is_noise: bool,
    ) -> None:
        """Ship one failure to the off-box record. Never raises, never blocks.

        Only FAILURES travel. A successful tool call is already counted by
        ``mcp_server_requests_total``, answers no incident question, and would
        multiply the volume landing in a table shared with the whole estate.

        ``mcp_error`` is checked BEFORE ``status``, and that order is the point
        of the whole change: a tool rejected by api-v2 comes back as HTTP 200
        with ``isError:true`` inside the result (#652), so a status-code-only
        rule records nothing for the exact failure mode that prompted #890.

        ``org_id`` is what the request resolved and ``claimed_org_id`` is what
        the caller named. On an auth failure the shipper drops the first and
        keeps the second, because a 401 verified nothing (#996). That demotion
        deliberately does NOT happen here: putting it in the shipper means the
        next call site inherits it without knowing the rule exists.
        """
        try:
            if is_noise:
                # Scanner probes, RFC-9728 discovery 401s, /sse 404s and expired
                # session DELETEs. Each is expected, high-volume, and answers
                # nothing - the same set the display logic already suppresses.
                return
            if mcp_error:
                event = EVENT_TOOL_ERROR
            elif status in (401, 403):
                event = EVENT_AUTH_FAILURE
            elif status >= 400:
                event = EVENT_UPSTREAM_REJECTED
            else:
                return

            get_event_log().emit(
                event,
                org_id=org_id,
                user_id=user_id,
                tool_name=tool_name,
                mcp_method=mcp_method,
                status_code=status,
                duration_ms=int(duration * 1000),
                error_message=mcp_error_msg,
                session_id=session_id,
                client_type=client_type,
                claimed_org_id=claimed_org_id,
                org_id_source=org_id_source,
                transport=transport,
                synthetic=synthetic,
            )
        except Exception as exc:  # pragma: no cover - emit already swallows
            logging.debug("event_log wiring failed (%s)", type(exc).__name__)
