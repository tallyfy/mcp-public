"""
FastMCP Error Handling Utilities
Standardized error handling patterns for MCP tools
"""

import re
from functools import wraps
from typing import Any, Optional
from fastmcp.exceptions import ToolError
from tallyfy import TallyfyError
from constants import TOOL_ERROR_CLASS_ATTR, TOOL_ERROR_CLASSES
import logging
import sentry_sdk

logger = logging.getLogger(__name__)


_LEAKED_INTERNALS_RE = re.compile(
    # SQL errors & queries
    r"SQLSTATE\[|"
    r"\bSELECT\b.*\bFROM\b|"
    r"\bINSERT\b.*\bINTO\b|"
    r"\bUPDATE\b.*\bSET\b|"
    r"\bDELETE\b.*\bFROM\b|"
    r"\bALTER\s+TABLE\b|"
    r"\bCREATE\s+TABLE\b|"
    r"\bDROP\s+TABLE\b|"
    # Laravel / PHP internals
    r"\(Connection:\s*\w+,\s*SQL:|"
    r"\bIlluminate\\|"
    r"\bPDOException\b|"
    r"\bQueryException\b|"
    r"\bErrorException\b|"
    r"in /[^\s]+\.php on line \d+|"
    r"\bvendor/|"
    # PostgreSQL context lines
    r"\bCONTEXT:\s*unnamed portal|"
    r"\bHINT:|"
    r"\bDETAIL:|"
    r"\bFATAL:|"
    # Stack traces (generic, PHP, Python)
    r"Stack trace:|"
    r"#\d+\s+/[^\s]+\.php|"
    r"at /[^\s]+\.py:\d+|"
    r"\bTraceback \(most recent call",
    re.IGNORECASE,
)

_GENERIC_ERROR = "an internal error occurred. Please try again or contact support."


_LEAK_SPLIT_MARKERS = [
    "SQLSTATE", "Stack trace", "Traceback", "Illuminate\\",
    "PDOException", "QueryException", "ErrorException",
    "HINT:", "DETAIL:", "FATAL:", "vendor/",
    "ALTER TABLE", "CREATE TABLE", "DROP TABLE",
    "in /",
]


def _sanitize_api_error(message: str) -> str:
    """Strip leaked internals (SQL, stack traces, file paths) from API error messages."""
    if _LEAKED_INTERNALS_RE.search(message):
        before_leak = message
        for marker in _LEAK_SPLIT_MARKERS:
            before_leak = before_leak.split(marker)[0]
        before_leak = before_leak.strip().rstrip(" ——-:")
        if len(before_leak) > 10:
            return before_leak
        return _GENERIC_ERROR
    return message


# ===========================================================================
# Tool-error classification for `mcp_server_tool_errors_total{error_type}`
# ===========================================================================
#
# WHY THIS EXISTS. The counter has carried an `error_type` label since it was
# added, and on the live Prometheus that label has exactly ONE value:
#
#     GET /api/v1/label/error_type/values -> {"status":"success","data":["unknown"]}
#
# (measured 2026-08-22; control on the same sweep, `tool_name` returns 79
# distinct values, so the metric is genuinely populated and the label endpoint
# works.) So the one label whose job is to answer "what KIND of thing is
# failing" could never answer it, and an operator had to infer the answer from
# Sentry's SILENCE instead. Recorded on #603 as its fifth suppression point.
#
# THE CAUSE IS DECORATOR ORDER, not a broken counter. 107 of the 113 tools are
# declared as
#
#     @track_tool_execution("get_tags")     <- classifies the exception
#     @handle_tallyfy_errors("get tags")    <- converts TallyfyError -> ToolError
#
# so by the time `track_tool_execution` sees the exception, this decorator has
# already replaced the `TallyfyError` with a `ToolError`, whose class name
# matches neither of the two names that decorator tests for. Every tool error
# therefore falls to its `else` branch and is labelled `unknown`.
#
# THE FIX is to hand the classification forward rather than make the outer
# decorator guess at it: this decorator already knows the upstream HTTP status,
# so it stamps the derived class onto the ToolError it raises and the metrics
# decorator reads it back. `classify_upstream_status` is the single place that
# maps a status to a label.
#
# TWO PROPERTIES ARE DELIBERATE AND BOTH ARE ASSERTED BY TESTS.
#
# 1. The label is derived ONLY from an integer status and this closed map. It
#    is never taken from message text and never from anything a caller
#    supplies, so no request can mint an unbounded Prometheus time series.
#
# 2. The enumeration cannot go stale in the way a BEHAVIOURAL list can.
#    `tallyfy_error_handler` in host/core/server.py argues at length against
#    enumerating "expected" statuses, because such a list is always one
#    incident late (#687, #184, #509, #510, #511). That argument is about which
#    codes change what the server DOES. Here an unlisted status still lands in
#    a correct, honest bucket -- any other 4xx is CLIENT_ERROR, any 5xx is
#    UPSTREAM_ERROR -- so adding a name only ever sharpens a label that was
#    already right. Nothing about behaviour depends on this map.
#
# THIS CHANGES NO LOG LEVEL AND NO SENTRY BEHAVIOUR. Which statuses are demoted
# to WARNING is a separate judgement call and is #603's own acceptance
# criterion 2; it is untouched here on purpose.

# The attribute name and the closed vocabulary live in `constants` so that
# `metrics` can read both without importing this module (it would be a heavier
# import and an easy cycle to create later). This module owns the LOGIC that
# produces a member; `constants` owns the SET of members.
ERROR_CLASS_VALIDATION = "validation"
ERROR_CLASS_NOT_FOUND = "not_found"
ERROR_CLASS_AUTH = "auth"
ERROR_CLASS_BAD_REQUEST = "bad_request"
ERROR_CLASS_CONFLICT = "conflict"
ERROR_CLASS_RATE_LIMITED = "rate_limited"
ERROR_CLASS_CLIENT_ERROR = "client_error"
ERROR_CLASS_UPSTREAM_ERROR = "upstream_error"
ERROR_CLASS_UPSTREAM_UNAVAILABLE = "upstream_unavailable"
ERROR_CLASS_INTERNAL_ERROR = "internal_error"
ERROR_CLASS_TOOL_REJECTED = "tool_rejected"
ERROR_CLASS_PAYLOAD_REJECTED = "payload_rejected"
ERROR_CLASS_UNKNOWN = "unknown"

_STATUS_ERROR_CLASSES = {
    400: ERROR_CLASS_BAD_REQUEST,
    401: ERROR_CLASS_AUTH,
    403: ERROR_CLASS_AUTH,
    404: ERROR_CLASS_NOT_FOUND,
    409: ERROR_CLASS_CONFLICT,
    422: ERROR_CLASS_VALIDATION,
    429: ERROR_CLASS_RATE_LIMITED,
}


def classify_upstream_status(status: Any) -> str:
    """Map an upstream Tallyfy API status onto the closed error-class vocabulary.

    A non-integer status is NOT a fallback case worth burying in `unknown`: the
    SDK raises ``TallyfyError`` with ``status_code=None`` when retries are
    exhausted on a transport failure, so "no status at all" is precisely the
    shape a real upstream outage takes. It gets its own value.
    """
    if not isinstance(status, int) or isinstance(status, bool):
        return ERROR_CLASS_UPSTREAM_UNAVAILABLE
    named = _STATUS_ERROR_CLASSES.get(status)
    if named:
        return named
    if 400 <= status < 500:
        return ERROR_CLASS_CLIENT_ERROR
    if 500 <= status < 600:
        return ERROR_CLASS_UPSTREAM_ERROR
    return ERROR_CLASS_UNKNOWN


# ===========================================================================
# Pre-flight SDK payload errors: refused before a socket was opened (#835)
# ===========================================================================
#
# The SDK's `TallyfyPayloadError` family is raised by the payload builders
# BEFORE any HTTP request is made, so there is no status to attach and
# `status_code` is None. `EXPECTED_UPSTREAM_STATUSES` therefore cannot demote
# it and it lands at ERROR, which pages Sentry.
#
# WHY THAT MATTERS, AND WHY IT IS NOT THEORETICAL. Six builders are reached by
# tools that splat an LLM-supplied free-form dict straight in
# (`update_template_metadata`, `update_form_field`, `add_form_field_to_step`,
# `add_step_to_template`, `create_automation_rule`, `update_automation_rule`,
# all typed `GenericDict`). The SDK ships a hint map -- `description` to
# `summary`, `name` to `title` -- precisely BECAUSE models pass those names. So
# the expected failure is a model getting a field name slightly wrong, which is
# routine, and every occurrence would page an operator who can do nothing about
# it. That is the "alert nobody can act on" that trains people to ignore the
# channel, which is #603's whole subject.
#
# ⚠️ A STATUS TEST CANNOT DO THIS JOB, and that is the entire reason this is a
# TYPE test. `EXPECTED_UPSTREAM_STATUSES` says in its own comment that a
# missing status must never join it, because an exhausted transport also has
# no status and demoting it would remove the only signal that Tallyfy is
# unreachable. Both failures wear the same shape. The exception TYPE is the one
# thing that separates them, which is why the fix lives here rather than as a
# new member of that tuple.
#
# ⚠️ `side_effect_possible` IS THE WRONG DISCRIMINATOR, and it is the obvious
# one to reach for. Read off the SDK on origin/main: the flag is declared on
# the BASE class as `side_effect_possible: bool = False`, and
# `TallyfyNotDeliveredError` -- a ConnectTimeout, i.e. Tallyfy being
# unreachable -- also carries False. Keying on it would demote every plain
# `TallyfyError` with no status, destroying exactly the signal the paragraph
# above protects. Only `TallyfyIndeterminateError` sets it True.
#
# ⚠️ `TallyfyNotDeliveredError` IS NOT A MEMBER OF THIS FAMILY. It is a sibling
# of `TallyfyError`, not a subclass of `TallyfyPayloadError`, so the isinstance
# test below already excludes it. Stated explicitly because #835's own summary
# table lists it beside the payload errors, and a future reader widening this
# to "the SDK errors with no status" would silence an outage.
#
# THE IMPORT IS DEFENSIVE ON PURPOSE. The pin is an exact `tallyfy==1.3.12` in
# every install path, and `tallyfy/payload.py` does not exist at that tag
# (verified: `git cat-file -e v1.3.12:tallyfy/payload.py` fails, while the same
# path resolves on origin/main). A plain `from tallyfy import
# TallyfyPayloadError` would therefore fail at import time and take the server
# down today, to guard against something the pinned SDK cannot yet raise. With
# the class absent the tuple is empty, `isinstance(x, ())` is False, and this
# whole path is inert -- which is correct, because nothing can produce one.
try:  # pragma: no cover - exercised by whichever SDK version is installed
    from tallyfy import TallyfyPayloadError as _TallyfyPayloadError
except ImportError:  # SDK older than the payload-error family (<= 1.3.12)
    _TallyfyPayloadError = None

#: Exception types that mean "refused locally, nothing was sent". Module-level
#: so a test can substitute a stand-in on an SDK that predates the real class,
#: and so the predicate below reads it at call time rather than capturing it.
PREFLIGHT_PAYLOAD_ERROR_TYPES: tuple = (
    (_TallyfyPayloadError,) if _TallyfyPayloadError is not None else ()
)


def is_preflight_payload_error(error: BaseException) -> bool:
    """True when the SDK refused the payload before any request went out.

    Deliberately NOT keyed on a missing status, on `side_effect_possible`, or
    on the exception's class NAME -- see the block comment above for why each
    of those three is wrong. Subclasses (`TallyfyUnknownFieldError`,
    `TallyfyMissingFieldError`, `TallyfyEmptyPayloadError`) are covered by
    inheritance rather than by enumeration, so a new one added by the SDK is
    handled without a change here.
    """
    return isinstance(error, PREFLIGHT_PAYLOAD_ERROR_TYPES)


def classify_tool_error(error: BaseException, status: Any) -> str:
    """Error-class label for a failed tool call, status or no status.

    A pre-flight payload rejection has no status, and `classify_upstream_status`
    correctly maps "no status" to `upstream_unavailable` because that is what an
    exhausted transport looks like. Applying that to a caller's bad field name
    would count a model's typo as evidence that Tallyfy is down, so the type
    test runs first and only then does the status decide.
    """
    if is_preflight_payload_error(error):
        return ERROR_CLASS_PAYLOAD_REJECTED
    return classify_upstream_status(status)


def tag_error_class(error: BaseException, error_class: str) -> BaseException:
    """Stamp an error class onto an exception and return it, for `raise tag_...`.

    Returning the exception keeps the raise site a single expression, so the
    stamp cannot drift away from the `raise` it belongs to.
    """
    setattr(error, TOOL_ERROR_CLASS_ATTR, error_class)
    return error


def read_error_class(error: BaseException) -> Optional[str]:
    """Read back a stamped error class, or None when the exception carries none.

    Guarded rather than trusted: only a member of the closed vocabulary is
    returned, so a stray attribute of the same name can never widen the label
    set that reaches Prometheus.
    """
    value = getattr(error, TOOL_ERROR_CLASS_ATTR, None)
    return value if value in TOOL_ERROR_CLASSES else None


# ===========================================================================
# Which upstream answers are EXPECTED, and therefore log at WARNING
# ===========================================================================
#
# A member of this set is a definitive answer from api-v2 that is not a fault
# of this server. It is logged at WARNING and creates no Sentry event. Anything
# else, INCLUDING a missing status, is logged at ERROR.
#
#   400  the client's own input was missing, e.g. MissingOrgIdError when no
#        X-Organization-ID header, JWT claim or env var is present (Sentry
#        MCP-4T, issue #511). The user-facing message names the remedy.
#   401  the credential is expired or no longer accepted.
#   402  a plan limit refused the write. Today that is the trial cap
#        ("Trial limit reached: 100 unique task assignments", code
#        TRIAL_LIMIT_REACHED_100_U). Same class as 409 below: a gate working
#        as designed, on customer-controlled state we cannot fix, whose own
#        message names the remedy. Without it every trial org that reaches its
#        cap pages us once per attempt (Sentry MCP-SERVER-5T, issue #1080).
#   403  insufficient scope, or a business rule refusing the call.
#   404  the resource was deleted, or the model referenced a stale id.
#   409  a business rule refused the write, currently the allocated-seats gate
#        (SEAT_POOL_EXHAUSTED, api-v2 #9206), which fires whenever an org
#        invites, promotes or enables past its committed seat pool.
#   422  request validation failed, e.g. a missing field on an approval task.
#
# ⚠️ A MISSING status is deliberately NOT a member, and must not become one.
# `classify_upstream_status` records why: the SDK raises TallyfyError with
# status_code=None when retries are exhausted on a transport failure, so "no
# status at all" is the shape a real upstream outage takes. Demoting it would
# remove the only signal that Tallyfy is unreachable.
#
# ⚠️ That leaves a second producer of a missing status, and it is a DIFFERENT
# failure wearing the same shape: the SDK also raises a bare TallyfyError for
# an argument it rejected LOCALLY, before any request went out
# (`except ValueError as e: raise TallyfyError(f"Invalid step data: {e}")`).
# Those are NOT fixed here, because widening this set cannot tell the two
# apart. They are fixed where they belong, by refusing the bad shape at the
# tool boundary so the SDK is never reached with it. See issue #1080 and
# `tools/template_management.py::_validate_step_participants`.
#
# The membership is pinned as an EXACT set by
# tests/unit/server/utils/test_fastmcp_errors_status_split.py. Exact rather
# than a superset, because here an ADDITION is the dangerous direction: each
# new member silences a class of error permanently, so it must be a reviewed
# decision rather than a free one.
EXPECTED_UPSTREAM_STATUSES = (400, 401, 402, 403, 404, 409, 422)


_MAX_FIELD_ERRORS = 12
_MAX_FIELD_ERROR_CHARS = 900


def _flatten_detail(detail: Any) -> list:
    """
    Flatten one ``errors`` value into a list of plain strings.

    Laravel nests unpredictably here: a plain string, a list of strings, or —
    for keyed payloads such as ``tasks`` and ``prerun`` — a list of dicts whose
    values are themselves lists. Recursing keeps Python list reprs out of the
    message the caller reads.
    """
    if isinstance(detail, str):
        return [detail]
    if isinstance(detail, dict):
        out = []
        for key, value in detail.items():
            out.extend(f"{key}: {msg}" for msg in _flatten_detail(value))
        return out
    if isinstance(detail, (list, tuple)):
        out = []
        for item in detail:
            out.extend(_flatten_detail(item))
        return out
    return [str(detail)]


def _format_field_errors(errors: Any) -> str:
    """
    Flatten a Laravel ``errors`` block into a compact, agent-readable string.

    Laravel returns ``{"message": ..., "errors": {"<field.path>": ["msg", ...]}}``
    where ``message`` is often just "The given data was invalid." and every
    actionable detail — which field, what was wrong — lives only in ``errors``.
    Dropping it leaves the caller with nothing to correct, so we surface it.

    Values may be a list of strings, a bare string, or (for nested payloads such
    as ``tasks``) a list of dicts keyed by ID, so each shape is handled.
    """
    if not isinstance(errors, dict) or not errors:
        return ""

    parts = []
    for field, detail in list(errors.items())[:_MAX_FIELD_ERRORS]:
        messages = _flatten_detail(detail)

        joined = "; ".join(str(m) for m in messages if m)
        if joined:
            parts.append(f"{field}: {joined}")

    if not parts:
        return ""

    remaining = len(errors) - _MAX_FIELD_ERRORS
    if remaining > 0:
        parts.append(f"(+{remaining} more)")

    rendered = _sanitize_api_error(" | ".join(parts))
    if len(rendered) > _MAX_FIELD_ERROR_CHARS:
        rendered = rendered[:_MAX_FIELD_ERROR_CHARS].rstrip() + "…"
    return rendered


# Markers that mean a 403 really is an authentication or authorization failure
# rather than a domain rule. Kept deliberately narrow: anything not matched here
# keeps its own specific message with no re-authentication hint bolted on.
_AUTH_STYLE_MARKERS = (
    "unauthenticated",
    "unauthorized",
    "token",
    "expired",
    "invalid credentials",
    "access denied",
    "permission denied",
    "forbidden",
    "audience",
)


def _extract_primary_message(error: TallyfyError) -> str:
    """
    Extract only the primary API message for auth-style classification.

    Unlike _extract_api_message, this does NOT append field errors — preventing
    unrelated field text (containing substrings like 'token' or 'expired') from
    influencing the auth classification. It also correctly treats an empty body
    after the SDK prefix as empty (not as the full SDK string).
    """
    response_data = getattr(error, "response_data", None)

    # Same envelope unwrap as _extract_api_message. This is a no-op on every
    # live 403 shape today (only the 409 seat gate nests its message), and is
    # here so the two readers of response_data cannot drift apart: if api-v2
    # ever nests a 403, a reader that saw no message would classify it as
    # auth-style and re-append the misleading "re-authenticate" hint that #592
    # removed.
    envelope = _structured_error_envelope(response_data)
    if envelope is not None and envelope.get("message"):
        return _sanitize_api_error(envelope["message"])

    if isinstance(response_data, dict) and "message" in response_data:
        return _sanitize_api_error(response_data["message"])

    raw = str(error)
    match = re.match(r"API request failed with status \d+:\s*(.*)", raw)
    if match:
        body = match.group(1).strip()
        return _sanitize_api_error(body) if body else ""
    return _sanitize_api_error(raw)


def _is_auth_style_message(error: TallyfyError) -> bool:
    """
    Decide whether a 403 is an auth failure (hint helps) or a business rule
    (hint actively misleads, see #592).

    A 403 with no message at all AND no field errors is treated as auth-style,
    preserving the old behaviour for the opaque case where the caller has
    nothing else to go on.  When ``response_data`` carries an ``errors`` block
    the 403 is a business-rule rejection even if ``message`` is blank.
    """
    message = _extract_primary_message(error)
    if not message or not message.strip():
        response_data = getattr(error, "response_data", None)
        if isinstance(response_data, dict) and response_data.get("errors"):
            return False
        return True
    lowered = message.lower()
    return any(marker in lowered for marker in _AUTH_STYLE_MARKERS)


# ---------------------------------------------------------------------------
# 401 classification: is this about the CREDENTIAL, or about the OBJECT? (#652)
# ---------------------------------------------------------------------------
#
# Honest constraint first, because it bounds what this can ever answer.
# ``TallyfyError`` carries only ``message``, ``status_code`` and
# ``response_data`` (tallyfy/models.py:12-15) -- NO RESPONSE HEADERS. So "did
# the Cloudflare edge or Laravel answer this" cannot be decided at the tool
# layer without an SDK change. What follows classifies on the body alone.
#
# Each entry below was read from its emitter's source, not inferred.

#: 401 bodies that DO mean "this credential is no longer accepted". Used only to
#: LABEL the metric; the decision itself does not depend on this set.
_CREDENTIAL_401_MESSAGES = frozenset({
    # cloudflare-workers/app-production-api/index.js:1966 -- the edge refusing a
    # token it could not validate. NOTE: that body is {error, message} and
    # carries NO `code` key, so a classifier keyed on `code` sees nothing.
    "permission denied. error code: apsh213.",
    # Laravel's stock AuthenticationException. api-v2's app/Exceptions/Handler.php
    # has no unauthenticated() override (verified: line 150 delegates to the base
    # handler), so this is the framework default text, trailing stop included.
    "unauthenticated.",
})

#: 401 bodies that are about the OBJECT, not the credential. Re-authenticating
#: cannot fix any of these, so challenging on one sends a client round the OAuth
#: loop over a correct rejection -- #592 arriving one status code down.
#:
#: 🔴 TWO ENTRIES HERE DIFFER BY A SINGLE TRAILING FULL STOP, THEY COME FROM
#: DIFFERENT MIDDLEWARE, AND NEITHER COVERS THE OTHER. Do not "tidy" one into
#: the other, and do not delete either as a duplicate:
#:
#:     "unauthorized"   <- Support.php:18,          abort(401, 'Unauthorized')
#:     "unauthorized."  <- ExplicitUserAccess.php:36, response('Unauthorized.', 401)
#:
#: `_normalized_401_message` folds case and strips surrounding whitespace and
#: does NOT strip punctuation, so the two are distinct keys in this frozenset.
#: The wire shapes differ too: Support.php goes through Laravel's exception
#: handler and arrives as JSON `{"message": "Unauthorized", "code": ...}`, while
#: ExplicitUserAccess.php returns a PLAIN-TEXT body, so `response_data` is a
#: `str`. Collapsing them would re-open whichever one got deleted.
_NON_CREDENTIAL_401_MESSAGES = frozenset({
    # api-v2 app/Services/GuestTasksMessagingService.php:120
    #   throw new UserException('Unauthorized action', 401);
    # The guest is authenticated fine; that thread is not theirs.
    "unauthorized action",
    # api-v2 app/Http/Middleware/Support.php:18  -- NO trailing full stop (#1196)
    #   return abort(401, 'Unauthorized');
    # The `support` middleware guarding /api/support/* (routes/api.php:845 and
    # :648). It refuses a caller who is authenticated perfectly well but is not
    # a SUPPORT user, which is an account property. Re-authenticating cannot
    # grant it, so a challenge here asks the client to do something that cannot
    # possibly help.
    #
    # This reaches us as JSON, unlike its near-twin below: `abort()` raises an
    # HttpException, app/Exceptions/Handler.php falls through to
    # `setErrorResponse($e, 0, ['error' => true])`, and
    # app/Helpers/errors-helper.php:56 emits
    #   {"message": "Unauthorized", "code": "UNAUTHORIZED", "error": true}
    # so `_extract_primary_message` reads response_data["message"].
    "unauthorized",
    # api-v2 app/Http/Middleware/ExplicitUserAccess.php:36  -- WITH a full stop
    #   return response('Unauthorized.', Response::HTTP_UNAUTHORIZED);
    # A PLAIN-TEXT body, so response_data is a `str`. The SDK renders it as
    # "API request failed with status 401: Unauthorized." and
    # _extract_primary_message strips the prefix back to this.
    "unauthorized.",
    # api-v2 app/Http/Middleware/Saml.php:23
    #   abort(401, 'Saml Is Not Enabled In this Organization');
    # An organization configuration fact. No token fixes it.
    "saml is not enabled in this organization",
})


def _normalized_401_message(error: TallyfyError) -> str:
    """The comparable form of a 401's primary message.

    ``_extract_primary_message`` already does the envelope unwrap, the SDK
    prefix strip and the plain-string body, so this only normalises case and
    whitespace. Kept as a named function so the classifier and the metric label
    cannot normalise differently.
    """
    return _extract_primary_message(error).strip().lower()


def is_known_401_message(error: TallyfyError) -> bool:
    """Whether this 401's body is one we have actually read the emitter for.

    Only ever used to label ``mcp_server_auth_challenge_total``. A False here
    with a challenge issued means api-v2 has a 401 shape nobody has classified,
    which is a thing to go and read rather than a thing to suppress.
    """
    message = _normalized_401_message(error)
    return message in _CREDENTIAL_401_MESSAGES or message in _NON_CREDENTIAL_401_MESSAGES


def is_credential_401(error: TallyfyError) -> bool:
    """Would re-authenticating plausibly fix this 401?

    🔴 THIS IS A DENYLIST AND IT MUST STAY ONE. Unknown means "challenge".

    The temptation is an allowlist -- challenge only on messages we recognise --
    and it is wrong in the one direction that matters. The day api-v2 adds a new
    credential-failure message, an allowlist silently stops challenging, and the
    failure is invisible: the connector just goes quiet. That is #652 verbatim,
    reintroduced by a change that looks like tightening.

    A denylist fails the other way: an unclassified 401 produces one extra
    re-authentication. The runaway cost of that is what the circuit breaker in
    ``middleware/downstream_auth_challenge`` bounds, which is why the two shipped
    together.

    Returns:
        False when the body names a known object-scoped refusal, or carries a
        field-``errors`` block (a validation rejection, never a credential
        problem). True otherwise.
    """
    response_data = getattr(error, "response_data", None)
    if isinstance(response_data, dict) and response_data.get("errors"):
        return False

    return _normalized_401_message(error) not in _NON_CREDENTIAL_401_MESSAGES


def _auth_error_hint(decision: Optional[str]) -> str:
    """The parenthetical that follows the API's own message on a 401 or 403.

    ``decision`` is None for a 403 (which never reaches the challenge path) and
    one of ``CHALLENGE_DECISIONS`` for a 401.

    A non-credential 401 gets NO re-authentication hint. The API's own message
    already names the real problem, and telling a user to re-authenticate over a
    correct object-scoped refusal is exactly the trade #592 made one status code
    up.
    """
    from middleware.downstream_auth_challenge import (
        CHALLENGE_SUPPRESSED_CIRCUIT_OPEN,
        CHALLENGE_SUPPRESSED_NOT_CREDENTIAL,
        circuit_open_notice,
    )

    if decision == CHALLENGE_SUPPRESSED_NOT_CREDENTIAL:
        return ""

    if decision == CHALLENGE_SUPPRESSED_CIRCUIT_OPEN:
        session_id = None
        try:
            from fastmcp.server.dependencies import get_http_request

            session_id = get_http_request().headers.get("mcp-session-id")
        except Exception:  # pragma: no cover - defensive
            pass
        return circuit_open_notice(session_id)

    return (
        "(Your session may be expired or misconfigured. "
        "Please re-authenticate the MCP connector and retry.)"
    )


def _structured_error_envelope(response_data: Any) -> Optional[dict]:
    """
    Return api-v2's structured ``{"error": {...}}`` payload, or None.

    Guarded rather than assumed: `error` is a common key and some responses
    carry it as a bare string. Only a dict is an envelope.
    """
    if not isinstance(response_data, dict):
        return None
    envelope = response_data.get("error")
    return envelope if isinstance(envelope, dict) else None


# Codes whose ``details`` carry something the caller can act on. Keep this
# narrow: a hint is only useful if it names the fix, and an unrecognised code
# must fall through to the plain API message rather than guessing at one.
_ACTIONABLE_ERROR_HINTS = {
    # Allocated-seats billing v2 (api-v2 #9206). The org is at its committed
    # seat cap, so the invite / role-change / enable was refused. `pool_type`
    # says WHICH pool, which is the whole difference between "buy light seats"
    # and "buy full seats".
    "SEAT_POOL_EXHAUSTED": (
        "The organization has no {pool_type} seats left in its committed pool. "
        "An organization admin can purchase more seats in Settings > Billing, "
        "or free one by disabling an existing {pool_type} member."
    ),
}


def _structured_error_hint(envelope: dict) -> str:
    """Build an actionable suffix for a recognised structured error code."""
    template = _ACTIONABLE_ERROR_HINTS.get(envelope.get("code"))
    if not template:
        return ""

    details = envelope.get("details")
    details = details if isinstance(details, dict) else {}
    # `pool_type` is documented as always present, but a hint that renders
    # "no None seats left" is worse than a slightly vaguer one.
    pool_type = details.get("pool_type") or "available"
    return template.format(pool_type=pool_type)


def _extract_api_message(error: TallyfyError) -> str:
    """
    Extract a clean, user-facing message from a TallyfyError.

    The SDK formats messages as:
      "API request failed with status 400: <actual message>"
    This strips the technical prefix and returns just the API message.
    If response_data contains a 'message' key, prefer that, and append the
    per-field ``errors`` block when present so the caller can self-correct.

    api-v2's newer structured errors nest everything one level deeper, as
    ``{"error": {"code", "message", "details"}}`` — the seat-pool gate
    (SEAT_POOL_EXHAUSTED) is the first of these to reach a tool. A top-level
    ``message`` lookup misses it entirely and falls through to the SDK's
    generic "API request failed with status 409" string, so the human-readable
    reason AND the actionable ``details`` are both discarded. Unwrap the
    envelope generically rather than special-casing one code: every future
    structured error inherits the fix.

    Internal system details (SQL queries, stack traces, file paths) are
    stripped before returning — the full error is already in logs/Sentry.
    """
    response_data = getattr(error, "response_data", None)
    envelope = _structured_error_envelope(response_data)

    # The per-field ``errors`` block is the only part that names the offending
    # field, so it must survive regardless of which source supplies the message.
    # Some api-v2 responses (custom ResourceExceptions) carry `errors` with no
    # `message` at all — dropping them there would defeat the whole point.
    field_errors = ""
    if isinstance(response_data, dict):
        field_errors = _format_field_errors(response_data.get("errors"))

    if envelope is not None and envelope.get("message"):
        message = _sanitize_api_error(envelope["message"])
        hint = _structured_error_hint(envelope)
        if hint:
            message = f"{message} {hint}"
    elif isinstance(response_data, dict) and "message" in response_data:
        message = _sanitize_api_error(response_data["message"])
    else:
        # Strip the SDK's "API request failed with status NNN: " prefix
        raw = str(error)
        match = re.match(r"API request failed with status \d+:\s*(.+)", raw)
        message = _sanitize_api_error(match.group(1) if match else raw)

    if field_errors:
        return f"{message} [{field_errors}]"
    return message


def _build_error_message(operation_name: str, error: TallyfyError) -> str:
    """
    Build a user-facing error message from a TallyfyError.

    Only exposes the API's own message — no HTTP status codes, internal
    error codes, or implementation details. Technical context is already
    captured in logs and Sentry.
    """
    api_msg = _extract_api_message(error)
    return f"Could not {operation_name} — {api_msg}"


def handle_tallyfy_errors(operation_name: str):
    """
    Decorator to standardize Tallyfy API error handling for MCP tools.

    Converts TallyfyError and other exceptions into FastMCP ToolError
    with user-friendly messaging while preserving detailed logging.

    Args:
        operation_name: Human-readable description of the operation

    Returns:
        Decorator function for tool methods

    Example:
        @mcp.tool(...)
        @handle_tallyfy_errors("get organization users")
        def get_organization_users(...):
            # Clean implementation - error handling is automatic
            with TallyfySDK(api_key=api_key) as sdk:
                return sdk.get_organization_users(org_id, with_groups)
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except TallyfyError as e:
                # See EXPECTED_UPSTREAM_STATUSES for what each member means and
                # why it is there. A status OUTSIDE that set, and a MISSING
                # status, are both logged at ERROR and reach Sentry.
                #
                # The one exception to "a missing status logs at ERROR" is a
                # pre-flight payload rejection, which never reached Tallyfy at
                # all and so cannot be evidence of anything wrong with it. It
                # is recognised by exception TYPE, not by the absent status,
                # because an exhausted transport has the same absent status and
                # must keep paging. See is_preflight_payload_error (#835).
                status = getattr(e, "status_code", None)
                preflight = is_preflight_payload_error(e)
                if preflight:
                    logger.warning(
                        f"{operation_name} rejected before sending "
                        f"({type(e).__name__}): {e}"
                    )
                elif status in EXPECTED_UPSTREAM_STATUSES:
                    logger.warning(f"{operation_name} returned {status}: {e}")
                else:
                    # Set Sentry tags so LoggingIntegration event has context
                    sentry_sdk.set_tag("operation", operation_name)
                    sentry_sdk.set_tag("error_type", "tallyfy_api")
                    if status:
                        sentry_sdk.set_tag("http_status", str(status))
                    # Attach the API response body so it's visible in Sentry
                    # (the HTTP breadcrumb's `reason` field gets server-side
                    # scrubbed to [Filtered]; this context survives scrubbing)
                    response_data = getattr(e, "response_data", None)
                    sentry_sdk.set_context("api_response", {
                        "status_code": status,
                        "message": _extract_api_message(e),
                        "response_body": str(response_data)[:2000] if response_data else None,
                    })
                    # logger.error triggers Sentry via LoggingIntegration — no explicit capture needed
                    logger.error(f"Tallyfy API error in {operation_name}: {e}")

                # 401, and 403s that actually look like auth failures, mean the OAuth
                # token is expired or carries an audience the Tallyfy API rejects, so a
                # re-authentication hint helps. A 403 is ALSO how api-v2 refuses a
                # business rule ("Cannot disable guest with incomplete tasks"), and
                # appending the hint there told users to re-authenticate over a correct,
                # specific rejection they could do nothing about (#592). Trust a specific
                # domain message; only add the hint when the message reads like auth.
                if status == 401 or (status == 403 and _is_auth_style_message(e)):
                    api_msg = _extract_api_message(e)

                    # A 401 means the credential this request carried is no
                    # longer accepted, which is an authentication failure of the
                    # HTTP request and not merely of this tool. FastMCP has no
                    # way to say that from here: every exception a tool raises,
                    # ToolError included, comes back to the client as HTTP 200
                    # with isError:true, and clients re-authenticate on a real
                    # 401 and on nothing else. So flag the request and let
                    # DownstreamAuthChallengeMiddleware answer with the 401
                    # challenge the MCP authorization spec requires (#652).
                    #
                    # 403 is deliberately excluded even when the message reads
                    # like auth: api-v2 answers 403 for business rules too, and
                    # re-authenticating cannot fix those (#592).
                    #
                    # The ToolError below is raised either way. If the flag
                    # cannot be delivered (no HTTP request, SSE streaming, a
                    # background task) the client still gets today's descriptive
                    # result rather than nothing.
                    #
                    # Not every 401 earns a challenge, and not every challenge is
                    # issued. is_credential_401 refuses the object-scoped ones (a
                    # guest thread, an explicit-access middleware, a SAML
                    # refusal), and the circuit breaker refuses one that has
                    # already been asked three times on this session with no
                    # successful upstream call in between. The DECISION decides
                    # the hint, because a re-authentication hint attached to a
                    # rejection re-authenticating cannot fix is #592 verbatim.
                    decision = None
                    if status == 401:
                        from middleware.downstream_auth_challenge import (
                            flag_downstream_auth_failure,
                        )
                        decision = flag_downstream_auth_failure(
                            api_msg,
                            credential_failure=is_credential_401(e),
                            known_message=is_known_401_message(e),
                        )

                    raise tag_error_class(
                        ToolError(
                            f"Could not {operation_name} — {api_msg} "
                            f"{_auth_error_hint(decision)}"
                        ),
                        classify_tool_error(e, status),
                    )

                # Raise descriptive ToolError carrying the API's own message only.
                # Deliberately NO HTTP status code in the text: see
                # _build_error_message's docstring above, which is the contract.
                raise tag_error_class(
                    ToolError(_build_error_message(operation_name, e)),
                    classify_tool_error(e, status),
                )
            except ToolError as e:
                # Re-raise ToolError directly (already properly formatted).
                #
                # This is a tool rejecting the call in its own code -- a missing
                # required argument, a precondition the API would not have
                # caught, a response shape the tool cannot use. 135 sites across
                # server/tools raise this way, and they are a genuinely
                # different kind of failure from an upstream status, so they get
                # their own label rather than falling into `unknown` with
                # everything else.
                #
                # Only stamp when the error carries no class already: a nested
                # decorator's classification is more specific than this one and
                # must survive.
                if read_error_class(e) is None:
                    tag_error_class(e, ERROR_CLASS_TOOL_REJECTED)
                raise
            except Exception as e:
                # Set Sentry tags so LoggingIntegration event has context
                sentry_sdk.set_tag("operation", operation_name)
                sentry_sdk.set_tag("error_type", "unexpected")
                # logger.error triggers Sentry via LoggingIntegration — no explicit capture needed
                logger.error(f"Unexpected error in {operation_name}: {e}", exc_info=True)

                # User-facing message without internal details (type, traceback, etc.)
                # Full context is already captured in the log/Sentry above
                raise tag_error_class(
                    ToolError(
                        f"Could not {operation_name} — {_sanitize_api_error(str(e))}"
                    ),
                    ERROR_CLASS_INTERNAL_ERROR,
                )
        return wrapper
    return decorator
