"""Check once per access token that the caller still belongs to the token's workspace (#1135).

WHY THIS EXISTS
---------------
``initialize`` and ``tools/list`` are answered from a local JWT signature check
and never call api-v2. So a signature-valid token for a workspace the person
can no longer use still connects, and Claude or ChatGPT shows "Connected". The
first ``tools/call`` then reaches api-v2, which answers 403 "You are not an
active member of this organization!". A 403 is deliberately not an auth
challenge here (see ``downstream_auth_challenge.py``), so nothing ever told the
person what to do.

MCP clients re-run OAuth on a transport 401 and on nothing else. Since the fix
for tallyfy/api-v2#10120 reached api-v2 ``production`` (the 2026-09-14
release), api-v2's authorize page refuses a removed member and shows its
no-workspace page instead of minting the same unusable token again. So a 401
here now sends the person somewhere they can act. Before that fix it would
have been a consent loop, which is why this waited.

WHERE IT RUNS
-------------
Inside FastMCP's ``RequireAuthMiddleware``, directly around the streamable-http
app (``server.py`` installs it on that middleware's ``.app``). So it only ever
sees a request whose token FastMCP has already verified. A no-token
``server/discover`` goes round it through ``DiscoverWithoutAuth``, exactly as
it goes round auth. A 401 sent from here travels up through
``DownstreamAuthChallengeMiddleware``, which only rewrites a 2xx and so leaves
it alone, to ``AuthErrorMiddleware``, which adds the ``WWW-Authenticate``
header with the RFC 9728 ``resource_metadata`` pointer and keeps this
``error_description``. That middleware stays the one place that builds the
header.

WHO IS CHECKED
--------------
Only POST requests (the JSON-RPC messages) whose verified token carries both an
``org_id`` claim and an ``mcp_scopes`` claim. Those two together mark a token
from api-v2's ``McpAccessTokenService::issue``. Most come from the OAuth flow,
and those clients re-run OAuth on a 401. Some are long-lived credentials minted
with ``php artisan mcp:mint-token`` for reviewers and automations, which run no
OAuth. If such a credential's owner is removed, it gets one 401 per process each
hour and is then let through. It is dead either way, because api-v2 refuses
every tool call with a 403.

chat.tallyfy.com and the desktop AI shell forward a raw Tallyfy session token
with no ``mcp_scopes`` claim. Their clients do not run OAuth on a 401, so a 401
would send them nowhere. They are counted as ``skipped`` and passed through.

WHAT IT ASKS API-V2
-------------------
``GET /organizations/{org}/me`` through the SDK's
``users.get_current_user_info``, the same call the ``get_me`` tool makes, with
the same credential a tool would present (``get_downstream_token``). Read from
api-v2 ``production`` on 2026-10-31:

* ``routes/api.php`` puts that route under ``auth:api`` and then the
  ``tenant:user`` group, and ``app/Http/Middleware/Tenant.php`` answers 403
  "You are not an active member of this organization!" when
  ``isActiveMemberOfOrg`` is false. ``isActiveMemberOfOrg`` delegates to
  ``activeOrganizations()``, the #10120 predicate.
* For an active member it reaches ``UsersControllerNew::me``, which returns the
  already-loaded user. ``trial_period`` only refuses writes, and ``me`` is not a
  scope-mapped resource in ``EnforceMcpTokenScopes``, so a GET needs no scope.
* A 403 with any other message is not about membership, and re-authenticating
  could not fix it, so it is passed through. api-v2 raises "on hold" and "AI is
  not enabled" after its membership check. The Cloudflare edge answers its own
  on-hold refusal (``Error Code: apsh864``) and a bare "Forbidden." before
  api-v2 is reached at all.

The body is confirmed from real traffic as well as from the source. Staging's
``api_events`` for the 7 days to 2026-10-31 held 434 rows of exactly
``{"message":"You are not an active member of this organization!",
"code":"YOU_ARE_NOT_AN_ACTIVE_MEM","error":true}``, so the edge passes it through
unchanged, and production's ``api_events`` holds the edge refusals named above.

WHAT EACH ANSWER DOES
---------------------
Every answer except the challenge lets the request through, so the only new
thing a person can ever see is the 401 below.

* 2xx: ``active``. Remembered for this token (up to its own ``exp``, at most an
  hour), so the rest of the session costs nothing. It also clears this person's
  challenge budget for the workspace.
* 403 with the membership message: ``challenged``, a transport 401, unless this
  person has already been challenged for this workspace within the hour, in
  which case ``suppressed_circuit_open`` and the request goes through. One
  challenge sends them to api-v2's authorize page, and since #10120 api-v2 does
  not hand them the same token again. A second negative answer for the same
  person and workspace therefore means a loop, and a loop is #592.
* 403 with another message: ``refused_other``, remembered like ``active``.
* Anything else (no connection, a timeout, a 5xx, a 429, a 401, a 404, a body
  the SDK cannot read): ``failed_open`` with a WARNING. This check is a second
  layer and api-v2 is the gate, so availability wins. The answer is remembered
  for 60 seconds only, so an outage does not add a timeout to every message.

Two first requests that arrive together with the same new token share one
api-v2 call and one decision, so neither a second call nor a split answer is
possible. Each request still counts once in the metric.

THE BREAKER
-----------
The challenge budget lives in ``downstream_auth_challenge``'s circuit breaker,
in the same store, under the same lock, LRU cap and one-hour TTL as the
per-session budget for downstream 401s. Its keys are tuples, so no
``mcp-session-id`` header value can ever collide with one.

WHAT IT DOES NOT COVER
----------------------
* A person removed DURING a session keeps the cached answer for that token, up
  to an hour, and sees api-v2's 403 text on each tool call, exactly as before.
* After the one challenge, a later connect by the same person for the same
  workspace within the hour goes through, as it did before this check, and the
  first tool call shows api-v2's 403 text. That is the price of not looping.
* The cache and the budget live in this process's memory, as the breaker always
  has. A restart clears both, and a second instance keeps its own.
* OAuth tokens minted before the #10120 fix reached production lived at most an
  hour. That fix reached production on 2026-09-14, so none of those is still
  live. A long-lived credential from ``mcp:mint-token`` lives 90 days by default
  and up to 180 (api-v2 ``config/mcp.php``), so older ones can still be live.
  That command refuses anyone who is not an active member when it runs, so it
  was never the gap #10120 closed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, MutableMapping, Optional, Tuple

import anyio
import jwt

from middleware.downstream_auth_challenge import (
    CHALLENGE_ERROR,
    consume_membership_challenge,
    note_membership_confirmed,
)

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------

MODE_ENV = "MCP_CONNECT_MEMBERSHIP_CHECK"
MODE_ENFORCE = "enforce"
MODE_OFF = "off"
_VALID_MODES = (MODE_ENFORCE, MODE_OFF)


def _mode() -> str:
    """Read the mode on every request, so a test or an operator can flip it.

    Unset, empty and unrecognised all mean ``enforce``. An empty value is the
    trap ``ENFORCE_JWT_AUDIENCE`` and ``MCP_TOOL_SCOPE_ENFORCEMENT`` both fell
    into: a line reading ``MCP_CONNECT_MEMBERSHIP_CHECK=""`` looks like the
    default and must behave like it.
    """
    raw = (os.getenv(MODE_ENV) or "").strip().lower()
    if not raw:
        return MODE_ENFORCE
    if raw not in _VALID_MODES:
        logger.warning(
            "%s=%r is not one of %s; using %s", MODE_ENV, raw, _VALID_MODES, MODE_ENFORCE
        )
        return MODE_ENFORCE
    return raw


# ---------------------------------------------------------------------------
# Outcomes. A closed vocabulary, because it is a Prometheus label.
# ---------------------------------------------------------------------------

OUTCOME_DISABLED = "disabled"
OUTCOME_SKIPPED = "skipped"
OUTCOME_CACHE_HIT = "cache_hit"
OUTCOME_ACTIVE = "active"
OUTCOME_REFUSED_OTHER = "refused_other"
OUTCOME_CHALLENGED = "challenged"
OUTCOME_SUPPRESSED_CIRCUIT_OPEN = "suppressed_circuit_open"
OUTCOME_FAILED_OPEN = "failed_open"

OUTCOMES = (
    OUTCOME_DISABLED,
    OUTCOME_SKIPPED,
    OUTCOME_CACHE_HIT,
    OUTCOME_ACTIVE,
    OUTCOME_REFUSED_OTHER,
    OUTCOME_CHALLENGED,
    OUTCOME_SUPPRESSED_CIRCUIT_OPEN,
    OUTCOME_FAILED_OPEN,
)

# ---------------------------------------------------------------------------
# What api-v2 says, and what we say back
# ---------------------------------------------------------------------------

#: api-v2 ``app/Http/Middleware/Tenant.php`` on ``production``:
#:     abort(Response::HTTP_FORBIDDEN, 'You are not an active member of this organization!');
#: rendered by ``setErrorResponse`` in ``app/Helpers/errors-helper.php`` as
#: ``{"message": <that text>, "code": ..., "error": true}``, which the SDK hands
#: back as ``TallyfyError.response_data``. A caller that opts in to problem
#: details gets ACCOUNT_DISABLED instead; the SDK sends ``Accept:
#: application/json`` and does not opt in.
INACTIVE_MEMBER_MESSAGE = "You are not an active member of this organization!"

#: The wording tallyfy/mcp#1135 asks for. It reaches the person through the
#: ``error_description`` of the 401 and of its ``WWW-Authenticate`` header.
CHALLENGE_DESCRIPTION = (
    "Your Tallyfy account is not an active member of the connected workspace. "
    "Reconnect to choose another."
)

# ---------------------------------------------------------------------------
# Timing and the cache
# ---------------------------------------------------------------------------

#: Seconds the SDK waits to connect and, separately, to read. One attempt only.
CHECK_TIMEOUT_SECONDS = 5

#: Hard bound on the whole check, because a server that trickles bytes can
#: keep a read timeout from ever firing. The worker thread is abandoned, not
#: killed, and runs for as long as the server keeps sending. That costs one
#: idle thread: anyio frees its limiter slot when the wait is cancelled.
CHECK_DEADLINE_SECONDS = 8.0

#: How long a ``failed_open`` answer is remembered.
FAIL_OPEN_RETRY_SECONDS = 60.0

#: The longest any answer is remembered: one access-token lifetime (api-v2
#: ``config/mcp.php``), the same figure the breaker uses.
ALLOW_TTL_SECONDS = 3600.0

#: This process serves every MCP user, so the cache is bounded.
MAX_CACHE_ENTRIES = 4096

# token digest -> time.time() at which the remembered answer lapses
_allowed: "OrderedDict[str, float]" = OrderedDict()
_allowed_lock = threading.Lock()


class _Pending:
    """A check in flight, so a second request with the same token waits for it."""

    def __init__(self) -> None:
        self.done = anyio.Event()
        self.result: Optional[Tuple[bool, str]] = None


_inflight: Dict[str, _Pending] = {}


def reset_state() -> None:
    """Forget every remembered answer. For tests only."""
    with _allowed_lock:
        _allowed.clear()
    _inflight.clear()


def _cache_hit(digest: str, now: float) -> bool:
    with _allowed_lock:
        expires_at = _allowed.get(digest)
        if expires_at is None:
            return False
        if expires_at <= now:
            _allowed.pop(digest, None)
            return False
        _allowed.move_to_end(digest)
        return True


def _cache_store(digest: str, expires_at: float, now: float) -> None:
    if expires_at <= now:
        return
    with _allowed_lock:
        _allowed[digest] = expires_at
        _allowed.move_to_end(digest)
        for stale in [d for d, at in _allowed.items() if at <= now]:
            _allowed.pop(stale, None)
        while len(_allowed) > MAX_CACHE_ENTRIES:
            _allowed.popitem(last=False)


def cache_size() -> int:
    with _allowed_lock:
        return len(_allowed)


# ---------------------------------------------------------------------------
# Who is asking
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Subject:
    token: str
    claims: Dict[str, Any]
    user_id: str
    org_id: str
    digest: str
    expires_at: Optional[float]


def _subject_from_scope(scope: Scope) -> Optional[_Subject]:
    """The verified token's subject, or None when this check does not apply.

    The token was verified by FastMCP before this middleware runs, so decoding
    it without the signature check is safe; ``utils.auth_context`` does the
    same on the same string.
    """
    token = getattr(getattr(scope.get("user"), "access_token", None), "token", None)
    if not isinstance(token, str) or not token:
        return None
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return None
    if not isinstance(claims, dict):
        return None

    org_id = claims.get("org_id")
    user_id = claims.get("sub") or claims.get("user_id")
    if not isinstance(org_id, str) or not org_id:
        return None
    if not isinstance(user_id, (str, int)) or isinstance(user_id, bool) or not str(user_id):
        return None
    if not isinstance(claims.get("mcp_scopes"), list):
        return None

    exp = claims.get("exp")
    expires_at = float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None
    return _Subject(
        token=token,
        claims=claims,
        user_id=str(user_id),
        org_id=org_id,
        digest=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        expires_at=expires_at,
    )


def _allow_until(subject: _Subject, now: float) -> float:
    until = now + ALLOW_TTL_SECONDS
    if subject.expires_at is not None:
        until = min(until, subject.expires_at)
    return until


# ---------------------------------------------------------------------------
# Asking api-v2
# ---------------------------------------------------------------------------


def is_inactive_member_message(message: Any) -> bool:
    """True only for api-v2's own membership refusal, however it is punctuated."""
    if not isinstance(message, str):
        return False

    def norm(text: str) -> str:
        return " ".join(text.split()).casefold().rstrip("!.")

    return norm(message) == norm(INACTIVE_MEMBER_MESSAGE)


def _read_membership(token: str, org_id: str, claims: Dict[str, Any]) -> None:
    """Make the one api-v2 call. Returns on a 2xx; raises otherwise.

    Runs in a worker thread: the SDK is synchronous, and so is the downstream
    token exchange when it is switched on.
    """
    from tallyfy import TallyfySDK

    from constants import TALLYFY_API_BASE_URL
    from utils.downstream_token import get_downstream_token
    from utils.sdk_metrics_patch import record_request

    api_key = get_downstream_token(token, org_id, claims)
    # The SDK's own class, looked up here at call time, so the hook that
    # counts the call in tallyfy_api_* is passed by hand (utils/sdk_client.py).
    with TallyfySDK(
        api_key=api_key,
        base_url=TALLYFY_API_BASE_URL,
        timeout=CHECK_TIMEOUT_SECONDS,
        max_retries=0,
        on_request=record_request,
    ) as sdk:
        sdk.users.get_current_user_info(org_id)


async def _ask_api_v2(subject: _Subject) -> Tuple[bool, str]:
    """Run the check and remember the answer. Never raises an ``Exception``."""
    now = time.time()
    try:
        with anyio.fail_after(CHECK_DEADLINE_SECONDS):
            await anyio.to_thread.run_sync(
                _read_membership,
                subject.token,
                subject.org_id,
                subject.claims,
                abandon_on_cancel=True,
            )
    except TimeoutError:
        return _fail_open(subject, now, f"no answer within {CHECK_DEADLINE_SECONDS:g}s")
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        data = getattr(exc, "response_data", None)
        message = data.get("message") if isinstance(data, dict) else data

        if status == 403 and is_inactive_member_message(message):
            if consume_membership_challenge(subject.user_id, subject.org_id):
                logger.warning(
                    "Connect-time membership check: user %s is not an active member "
                    "of org %s; answering 401 so the client re-runs OAuth",
                    subject.user_id, subject.org_id,
                )
                return False, OUTCOME_CHALLENGED
            logger.warning(
                "Connect-time membership check: user %s is still not an active member "
                "of org %s after a challenge, so this is a loop; letting the request "
                "through without challenging again",
                subject.user_id, subject.org_id,
            )
            _cache_store(subject.digest, _allow_until(subject, now), now)
            return True, OUTCOME_SUPPRESSED_CIRCUIT_OPEN

        if status == 403:
            # An edge refusal, or one api-v2 raises after its membership check.
            # Either way it is not about membership.
            _cache_store(subject.digest, _allow_until(subject, now), now)
            return True, OUTCOME_REFUSED_OTHER

        return _fail_open(
            subject, now, f"{type(exc).__name__}, status {status}: {exc}"
        )

    note_membership_confirmed(subject.user_id, subject.org_id)
    _cache_store(subject.digest, _allow_until(subject, now), now)
    return True, OUTCOME_ACTIVE


def _fail_open(subject: _Subject, now: float, reason: str) -> Tuple[bool, str]:
    logger.warning(
        "Connect-time membership check could not get an answer from the Tallyfy "
        "API for org %s (%s); letting the request through, because api-v2 still "
        "refuses a non-member on every tool call",
        subject.org_id, reason,
    )
    _cache_store(subject.digest, min(now + FAIL_OPEN_RETRY_SECONDS, _allow_until(subject, now)), now)
    return True, OUTCOME_FAILED_OPEN


async def decide(subject: _Subject) -> Tuple[bool, str]:
    """``(let_through, outcome)`` for one request from this subject."""
    if _cache_hit(subject.digest, time.time()):
        return True, OUTCOME_CACHE_HIT

    pending = _inflight.get(subject.digest)
    if pending is not None:
        await pending.done.wait()
        return pending.result or (True, OUTCOME_FAILED_OPEN)

    pending = _Pending()
    _inflight[subject.digest] = pending
    try:
        pending.result = await _ask_api_v2(subject)
    finally:
        _inflight.pop(subject.digest, None)
        pending.done.set()
    return pending.result


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def _record(outcome: str) -> None:
    """Count one outcome. Must never break a request, and must never fail silently."""
    try:
        from metrics import record_connect_membership_check

        record_connect_membership_check(outcome)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Could not record connect-membership outcome %r: %s. "
            "mcp_server_connect_membership_check_total is under-counting.",
            outcome, exc,
        )


def _initialize_metric_series() -> None:
    """Publish every outcome at zero at import, so an empty reading means something (#1015)."""
    try:
        from metrics import initialize_connect_membership_series

        initialize_connect_membership_series(OUTCOMES)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Could not pre-create mcp_server_connect_membership_check_total series: %s. "
            "The check still works; the metric is absent until its first increment.",
            exc,
        )


_initialize_metric_series()


# ---------------------------------------------------------------------------
# The ASGI wrapper
# ---------------------------------------------------------------------------


class ConnectMembershipCheck:
    """Answer 401 to a removed member's first request instead of connecting them."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        if _mode() == MODE_OFF:
            _record(OUTCOME_DISABLED)
            await self.app(scope, receive, send)
            return

        subject = _subject_from_scope(scope)
        if subject is None:
            _record(OUTCOME_SKIPPED)
            await self.app(scope, receive, send)
            return

        let_through, outcome = await decide(subject)
        _record(outcome)
        if let_through:
            await self.app(scope, receive, send)
            return
        await _send_challenge(send)


async def _send_challenge(send: Send) -> None:
    """The 401 body ``AuthErrorMiddleware`` decorates with ``WWW-Authenticate``."""
    body = json.dumps(
        {"error": CHALLENGE_ERROR, "error_description": CHALLENGE_DESCRIPTION}
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})
