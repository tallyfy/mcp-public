"""Durable record of MCP server failures, shipped off the box (#890).

Why this exists
---------------
The MCP server's only per-request record used to be its own stdout. Measured on
the tallyfy-mcp droplet 2026-08-22: ``docker inspect mcp-server -f
'{{json .Mounts}}'`` returns ``[]`` and ``server/docker-compose.yml`` declares no
``volumes:`` key, so the json-file log lives inside the container filesystem and
is destroyed when a deploy recreates the container. A customer's connector failed
at 14:59 on 2026-08-21; the container that held the evidence was replaced at
20:47, and the incident became permanently unanswerable 5h48m after it happened.

This module ships the failures the server already detects to the estate's
existing self-hosted logging stack, so the record outlives the container.

What it ships, and what it deliberately does not
------------------------------------------------
Three event types, all failures:

* ``mcp_tool_error``        a ``tools/call`` that failed. This includes the
  HTTP-200-with-``isError:true`` shape from #652, which is invisible to any
  status-code-based monitor and is exactly the class the customer hit.
* ``mcp_auth_failure``      a 401 or 403 on the MCP transport.
* ``mcp_upstream_rejected`` any other 4xx/5xx on the MCP transport.

Successful tool calls are NOT shipped. Prometheus already counts them
(``mcp_server_requests_total``), they are high-volume, and they answer no
incident question. "Log the useful thing, not everything" is a volume bound as
well as a design preference: this table is shared with the whole estate.

Nothing that could be a secret is ever shipped
----------------------------------------------
Never sent, at any log level, ever: the ``Authorization`` header, any token, the
request body, the response body, and the tool's ARGUMENTS (which can carry both
customer content and credentials). Only the tool NAME travels.

The raw ``Mcp-Session-Id`` is not sent either. It travels as a truncated SHA-256
(:func:`session_ref`), which keeps the only property an incident needs - two rows
from one conversation match - without putting a live session handle in a table
the whole estate can read.

The one free-text field is the error message, and it goes through :func:`redact`
first, which blanks Bearer tokens, bare JWTs, ``sk-``/``sk-ant-`` keys and
``key=value`` pairs whose key looks secret. It is then truncated. Redaction is
belt-and-braces rather than the primary control: the message the middleware
hands us is already the user-facing text, capped at 100 chars upstream.

Where it lands
--------------
``POST https://logs-queue.tallyfy.com/api/v1/ingest/system`` - the CF-Queue-backed
producer, which buffers for up to 4 days when the tallyfy-logging droplet is
unreachable. Rows arrive in the ``system_events`` hypertable tagged
``log_type='mcp'`` (18-month retention), queryable through Grafana,
``logging-api.tallyfy.com`` and the ``/log-query`` skill.

``log_type='mcp'`` mirrors ``tallyfy/vault``'s ``log_type='vault'``, which is the
existing precedent for a Python service on this same droplet writing to this same
table (58,116 rows measured 2026-08-22). ``ValidateSystemEvent`` in
``tallyfy/logging`` is permissive for every producer that is not tagged
``vault``, so this needs no change in the logging repo.

Identity is the point, not a bonus
----------------------------------
Every MCP row already in the logging stack is unattributable: all 2,013
``/mcp/oauth/*`` rows in ``api_events`` carry empty ``org_id``/``user_id``, and
all 1,804 MCP ``system_events`` rows carry the literal string ``system``. These
events carry the real ``org_id`` (from the verified JWT context, falling back to
the ``org_id`` tool argument) and the real ``user_id`` (the JWT ``sub``), because
"which customer did this happen to" is the first question anyone asks.

Failure of this module is NOT silent
------------------------------------
A POST that fails bumps ``mcp_server_event_log_total{status="fail"}`` **and**
logs at WARNING, which reaches Sentry through ``LoggingIntegration``. That is a
deliberate difference from the host's ``log_bridge``, whose only failure signal
is a counter with no alert behind it - the exact mechanism that let
tallyfy/mcp#697 drop every batched flush unnoticed for three months. A record
that can go missing silently is not a record.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Contract with tallyfy/logging
# ---------------------------------------------------------------------------

# The CF-Queue-backed producer. Same auth token as every other producer in the
# estate, and a 4-day durable buffer in front of the droplet. There is NO
# /batch sibling for the system route (tallyfy/logging cmd/ingest/main.go
# registers /api/v1/ingest, /ingest/batch, /ingest/email and /ingest/system -
# system takes exactly one event per POST), so this ships one event per POST.
# That is affordable only because it ships failures alone.
DEFAULT_INGEST_URL = "https://logs-queue.tallyfy.com/api/v1/ingest/system"

# system_events.log_type. Mirrors vault's, so one predicate selects this
# producer's rows and nothing else.
LOG_TYPE = "mcp"

EVENT_TOOL_ERROR = "mcp_tool_error"
EVENT_AUTH_FAILURE = "mcp_auth_failure"
EVENT_UPSTREAM_REJECTED = "mcp_upstream_rejected"

# system_events.category, one per event type. Defined ABOVE the class that reads
# it: a module-level name defined below its only reader resolves fine on import
# and raises NameError when the file is executed directly, and this repo has hit
# that exact shape before.
_CATEGORY_BY_EVENT = {
    EVENT_TOOL_ERROR: "tool",
    EVENT_AUTH_FAILURE: "auth",
    EVENT_UPSTREAM_REJECTED: "transport",
}

#: Every event name this module can emit. Derived from the category map rather
#: than written out a second time, so the two cannot disagree - a name in one
#: and not the other would either be rejected at emit or raise a KeyError while
#: building the payload.
EVENT_TYPES = frozenset(_CATEGORY_BY_EVENT)

DEFAULT_HTTP_TIMEOUT_S = 3.0
DEFAULT_MAX_QUEUE_SIZE = 500
ERROR_MESSAGE_MAX_CHARS = 500

_REDACTED = "<redacted>"

# Bearer tokens, bare JWTs, Anthropic/OpenAI-style keys, and key=value pairs
# whose key reads as secret. Ordered longest-context-first so `Bearer <jwt>`
# collapses to one placeholder rather than two.
_REDACTION_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-+/=]+", re.IGNORECASE),
    re.compile(r"\bey[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]{8,}\.[A-Za-z0-9._-]*"),
    re.compile(r"\bsk-[A-Za-z0-9._\-]{8,}"),
    re.compile(
        r"\b(api[_-]?key|token|secret|password|passwd|credential|authorization)"
        r"\s*[=:]\s*\S+",
        re.IGNORECASE,
    ),
)

_instance: Optional["ServerEventLog"] = None
_token_warning_emitted = False


def redact(text: Optional[str]) -> str:
    """Blank anything token-shaped in ``text``. Never raises.

    Deliberately conservative in the safe direction: an over-redacted message
    still names the tool, the org and the outcome, which is enough to act on. An
    under-redacted one puts a credential in a shared production database that
    the whole estate can query, and there is no taking it back.
    """
    if not text:
        return ""
    try:
        out = str(text)
        for pattern in _REDACTION_PATTERNS:
            out = pattern.sub(_REDACTED, out)
        return out[:ERROR_MESSAGE_MAX_CHARS]
    except Exception:  # pragma: no cover - str() on an exotic object
        return _REDACTED


def session_ref(session_id: Optional[str]) -> str:
    """A stable, non-reversible reference for one MCP session.

    The raw ``Mcp-Session-Id`` is NOT shipped. It is not an authentication
    credential on this server - FastMCP verifies the bearer token on every
    request and ``server.py`` reads the header only to tell a browser from an
    MCP client - but it is still a live session handle, and this table is
    readable by the whole estate. A truncated SHA-256 keeps the only property an
    incident actually needs, which is that two rows from the same conversation
    carry the same value, while being useless to anyone who reads it.

    An operator holding a session id from a customer's client log can find the
    rows with:

        python3 -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:16])" <id>
    """
    if not session_id:
        return ""
    try:
        return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:16]
    except Exception:  # pragma: no cover - encode() on an exotic object
        return ""


def _ingest_url() -> str:
    # Resolved per call so a test can monkeypatch the environment, and so an
    # operator can repoint at the direct Go path for debugging without a rebuild.
    return os.getenv("LOGGING_INGEST_SYSTEM_URL") or DEFAULT_INGEST_URL


def _ingest_token() -> Optional[str]:
    return os.getenv("LOGGING_INGEST_TOKEN") or None


# Measured on the droplet 2026-08-22, and this order is the whole reason the
# helper exists rather than a bare getenv. The two MCP units disagree about
# which variable carries this, in opposite directions:
#
#   mcp-server / staging-mcp-server : ENVIRONMENT unset (rc=1),
#                                     TALLYFY_ENVIRONMENT = production/staging
#   mcp-host                        : ENVIRONMENT = production,
#                                     TALLYFY_ENVIRONMENT unset (rc=1)
#
# Reading only ENVIRONMENT, which is what the host-side log_bridge does, would
# label EVERY production row from this server "development". Nothing would
# error; the rows would simply be filtered out of every production dashboard
# and every incident query, which is the failure this module exists to prevent.
# Falling back keeps the fix deployable with no droplet env change at all.
_ENVIRONMENT_VARS = ("ENVIRONMENT", "TALLYFY_ENVIRONMENT", "SENTRY_ENVIRONMENT")


def _environment() -> str:
    for name in _ENVIRONMENT_VARS:
        value = os.getenv(name)
        if value:
            return value
    return "development"


def _now_rfc3339() -> str:
    """RFC3339 UTC, seconds resolution, ``Z``-suffixed.

    The timestamp is stamped HERE and sent explicitly, rather than letting
    ``ingestSystemHandler`` default it to ``time.Now()``. That default is wrong
    for this producer: the CF Queue buffers for up to four days during an ingest
    outage, so a server-side default would stamp a 14:59 failure with its drain
    time and destroy the very timeline this module exists to preserve.
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


class ServerEventLog:
    """Fire-and-forget shipper for MCP server failure events.

    ``emit`` is synchronous, non-blocking and NEVER raises: it appends to a
    bounded in-memory queue and returns. A single background task drains that
    queue. The user-facing request path is never made slower or less reliable by
    telemetry.
    """

    def __init__(
        self,
        *,
        max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE,
        http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
    ) -> None:
        self.max_queue_size = max_queue_size
        self.http_timeout_s = http_timeout_s
        self._queue: Deque[Dict[str, Any]] = deque()
        self._worker: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None
        self._client: Any = None
        self._stopping = False

    # -- enqueue ----------------------------------------------------------

    def emit(
        self,
        event: str,
        *,
        org_id: Optional[str],
        user_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        status_code: Optional[int] = None,
        duration_ms: Optional[int] = None,
        error_message: Optional[str] = None,
        session_id: Optional[str] = None,
        client_type: Optional[str] = None,
        mcp_method: Optional[str] = None,
    ) -> None:
        """Queue one failure event. Sync, non-blocking, never raises."""
        try:
            if event not in EVENT_TYPES:
                # A name outside the vocabulary would land as a row no dashboard
                # selects, which is indistinguishable from not logging at all.
                logger.warning("event_log: unknown event %r dropped", event)
                return
            if not _ingest_token():
                self._warn_once_no_token()
                return

            payload = self._build(
                event,
                org_id=org_id,
                user_id=user_id,
                tool_name=tool_name,
                status_code=status_code,
                duration_ms=duration_ms,
                error_message=error_message,
                session_id=session_id,
                client_type=client_type,
                mcp_method=mcp_method,
            )
            self._append(payload)
            self._ensure_worker()
        except Exception as exc:  # never let telemetry break a request
            logger.debug("event_log: emit failed (%s)", type(exc).__name__)

    def _build(
        self,
        event: str,
        *,
        org_id: Optional[str],
        user_id: Optional[str],
        tool_name: Optional[str],
        status_code: Optional[int],
        duration_ms: Optional[int],
        error_message: Optional[str],
        session_id: Optional[str],
        client_type: Optional[str],
        mcp_method: Optional[str],
    ) -> Dict[str, Any]:
        # `unknown` is what the middleware uses as its display placeholder. It
        # is not an org, and storing it would make an unattributable row look
        # attributable to an org literally named "unknown".
        org = (org_id or "").strip()
        if org in ("", "unknown"):
            org = ""

        details: Dict[str, Any] = {
            "tool_name": tool_name or None,
            "mcp_method": mcp_method or None,
            "status_code": status_code,
            "duration_ms": duration_ms,
            "client_type": client_type or None,
            # Groups the calls of one connector conversation, which is how you
            # find the rest of a customer's failing session. Hashed, never raw -
            # see session_ref().
            "session_ref": session_ref(session_id) or None,
            "error_message": redact(error_message) or None,
        }
        return {
            "event": event,
            "timestamp": _now_rfc3339(),
            "org_id": org or None,
            "user_id": (user_id or None),
            "distinct_id": (user_id or None),
            "log_type": LOG_TYPE,
            "category": _CATEGORY_BY_EVENT[event],
            "details": {k: v for k, v in details.items() if v is not None},
            "environment": _environment(),
        }

    def _append(self, payload: Dict[str, Any]) -> None:
        while len(self._queue) >= self.max_queue_size:
            try:
                self._queue.popleft()
            except IndexError:
                break
            self._inc(status="dropped")
        self._queue.append(payload)
        if self._wake is not None:
            try:
                self._wake.set()
            except RuntimeError:  # pragma: no cover - loop torn down
                pass

    @staticmethod
    def _warn_once_no_token() -> None:
        global _token_warning_emitted
        if not _token_warning_emitted:
            _token_warning_emitted = True
            logger.warning(
                "LOGGING_INGEST_TOKEN is not set - MCP server failures will not "
                "be recorded anywhere that survives a deploy. Set it to ship "
                "mcp_* events to tallyfy/logging (see #890)."
            )

    # -- drain ------------------------------------------------------------

    def _ensure_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop (a sync unit test). Events stay queued; a later emit from
            # inside the loop starts the worker and drains them.
            return
        if self._wake is None:
            self._wake = asyncio.Event()
        self._stopping = False
        self._worker = loop.create_task(self._run(), name="mcp_event_log")
        # The very first emit appends BEFORE this Event exists, so its own
        # `_wake.set()` was a no-op. Without this line the worker would sit on
        # its 30s timeout while the queue already had work, and the first
        # failure of a process would be recorded half a minute late - or not at
        # all if the container was replaced in between, which is the exact
        # window this module exists to close.
        if self._queue:
            self._wake.set()

    async def _run(self) -> None:
        assert self._wake is not None
        while not self._stopping:
            try:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                while self._queue:
                    await self._post(self._queue.popleft())
            except asyncio.CancelledError:
                break
            except Exception as exc:  # the worker must never die
                logger.warning("event_log: drain iteration failed (%s)", exc)

    async def _post(self, payload: Dict[str, Any]) -> None:
        url = _ingest_url()
        token = _ingest_token() or ""
        try:
            client = self._get_client()
            response = await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=self.http_timeout_s,
            )
            code = getattr(response, "status_code", None)
            if code is None or not (200 <= int(code) < 300):
                self._inc(status="fail")
                # WARNING, not debug: this reaches Sentry via LoggingIntegration.
                # A durable-record pipeline that fails quietly is the defect this
                # module was written to remove, not one to reproduce.
                logger.warning(
                    "event_log: ingest rejected %s with HTTP %s - this failure "
                    "is now unrecorded",
                    payload.get("event"),
                    code,
                )
                return
            self._inc(status="ok")
        except Exception as exc:
            self._inc(status="fail")
            logger.warning(
                "event_log: ingest POST failed for %s (%s) - this failure is "
                "now unrecorded",
                payload.get("event"),
                type(exc).__name__,
            )

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        import httpx  # local import keeps monkeypatching simple in tests

        self._client = httpx.AsyncClient(timeout=self.http_timeout_s)
        return self._client

    # -- metrics ----------------------------------------------------------

    @staticmethod
    def _inc(*, status: str) -> None:
        try:
            import metrics as metrics_mod

            counter = getattr(metrics_mod, "event_log_total", None)
            if counter is not None:
                counter.labels(status=status).inc()
        except Exception:  # pragma: no cover - metrics are best effort
            pass

    # -- lifecycle --------------------------------------------------------

    async def aclose(self) -> None:
        """Drain and shut down. Only used by tests and an explicit shutdown."""
        self._stopping = True
        if self._wake is not None:
            self._wake.set()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):
                pass
        while self._queue:
            await self._post(self._queue.popleft())
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None


def get_event_log() -> ServerEventLog:
    """Per-process singleton."""
    global _instance
    if _instance is None:
        _instance = ServerEventLog()
    return _instance


__all__ = [
    "ServerEventLog",
    "get_event_log",
    "redact",
    "session_ref",
    "LOG_TYPE",
    "EVENT_TYPES",
    "EVENT_TOOL_ERROR",
    "EVENT_AUTH_FAILURE",
    "EVENT_UPSTREAM_REJECTED",
    "DEFAULT_INGEST_URL",
]
