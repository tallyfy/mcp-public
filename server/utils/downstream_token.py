"""Exchange the caller's MCP access token for a short-lived downstream token.

WHY THIS EXISTS
---------------
An MCP access token minted by api-v2 is accepted by the *entire* Tallyfy API,
as the consenting user, for everything that user can do. The consent screen
asks about MCP scopes; the token ignores them everywhere except the handful of
route groups ``EnforceMcpTokenScopes`` covers. That is not a check somebody
forgot to write. It is a check nobody *can* write while the MCP server has no
credential of its own and simply re-presents the caller's:

    server/utils/auth_context.py::get_authenticated_credentials
        -> returns access_token.token, the caller's own JWT
    server/tools/api_fallback.py
        -> sends it upstream as `Authorization: Bearer <that same token>`

The MCP specification (2026-07-28) forbids exactly this, twice and
independently:

    Access Token Privilege Restriction
        "The access token used at the upstream API is a separate token ...
         The MCP server MUST NOT pass through the token it received from the
         MCP client."

    Token Handling
        "... or transit any other tokens."

So this module obtains the separate token. api-v2 exposes
``POST {TALLYFY_AUTH_SERVER}/mcp/oauth/token-exchange``, which takes the
caller's MCP token as the subject and returns a short-lived token that carries
the same ``mcp_scopes`` and deliberately does NOT carry ``mcp_resource``.
Dropping that claim is what lets api-v2 refuse the caller's token everywhere
else without refusing ours.

WHAT THIS MODULE IS NOT
-----------------------
It is not a security boundary on its own. Until api-v2 sets
``MCP_DOWNSTREAM_REJECT_MODE=enforce``, the caller's token still works upstream
and this only changes which token we happen to send. The two halves are useless
apart, which is why both ship defaulted off.

THE CACHE, AND WHY ITS KEY LOOKS OVER-SPECIFIED
-----------------------------------------------
Exchanging on every tool call would put a synchronous round trip in front of
every one of the 123 call sites that reach ``get_authenticated_credentials``.
So results are cached, and concurrent misses for the same key are collapsed
into one exchange by a per-key lock. Without that collapse a cold start or a
TTL rollover sends one request per concurrent caller: measured at 8 round trips
for 8 concurrent callers sharing a key.

The key is ``(sub, org_id, subject_jti)`` and the ``jti`` is load-bearing: it
stops two DIFFERENT tokens for the same user and org from sharing one entry, so
a re-issued or narrowed token never inherits the credential minted for an older
one.

⚠️ It does NOT make revocation immediate, and an earlier version of this
docstring claimed it did. Nothing in this server checks revocation:
``TallyfyAuthProvider.verify_token`` and its fastmcp parent verify the RS256
signature, ``exp``, and optionally ``mcp_resource``, and stop there. The only
step that would observe a revocation is the exchange itself, because api-v2's
``auth:api`` looks the subject up. A cache HIT skips the exchange.

So the true bound is the TTL. A token revoked at T goes on working until its
cached entry expires, at most ``token_ttl`` seconds (300 by default) after the
last exchange. That is a deliberate, bounded window, not the "revocation
propagates for free" this file used to assert. Shortening the TTL shortens the
window; removing the cache removes it entirely at the cost of a round trip per
call. See tallyfy/mcp#652 for how healthy an unusable token can look.

TTL is ``min(expires_in - CLOCK_SKEW_S, subject_exp - now)``. The second term
is not belt-and-braces: a downstream token must never outlive the consent it
was derived from.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

#: Modes, least to most active.
#:
#:   off      - never exchange. Return the caller's token. Today's behaviour.
#:   shadow   - exchange, log the outcome, then return the CALLER'S token
#:              anyway. This is the census mode: it proves the exchange works
#:              at real traffic before anything depends on it.
#:   enforce  - exchange, and return the downstream token. A failure RAISES.
#:
#: Default is ``off`` on purpose, and deliberately the opposite of its sibling
#: ``ENFORCE_JWT_AUDIENCE``. api-v2's half of this fix is not in production, so
#: a default of anything else would call an endpoint that does not answer.
MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
_VALID_MODES = (MODE_OFF, MODE_SHADOW, MODE_ENFORCE)

#: Seconds trimmed off the issued lifetime so a token is never presented in the
#: last moments of its validity.
CLOCK_SKEW_S = 30

#: Hard ceiling on cache entries. This process serves every MCP user, so an
#: unbounded dict keyed on user identity is a memory leak with a slow fuse.
MAX_CACHE_ENTRIES = 2048


def _mode() -> str:
    """Read the mode fresh on every call.

    Deliberately not a module constant. A constant is captured at import time,
    which makes the flag untestable without reloading the module and makes a
    runtime flip impossible. The read is a dict lookup; it is not the expensive
    part of anything here.
    """
    raw = os.getenv("MCP_DOWNSTREAM_TOKEN_EXCHANGE", MODE_OFF).strip().lower()
    if raw not in _VALID_MODES:
        logger.warning(
            "MCP_DOWNSTREAM_TOKEN_EXCHANGE=%r is not one of %s; treating as %r",
            raw, _VALID_MODES, MODE_OFF,
        )
        return MODE_OFF
    return raw


def _auth_server() -> str:
    from constants import TALLYFY_AUTH_SERVER
    return TALLYFY_AUTH_SERVER


def _shared_secret() -> str:
    from constants import MCP_PROXY_SHARED_SECRET
    return MCP_PROXY_SHARED_SECRET


def _timeout() -> float:
    """Never raises. A malformed env var must not break a request in shadow."""
    raw = os.getenv("MCP_DOWNSTREAM_TIMEOUT", "10")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("MCP_DOWNSTREAM_TIMEOUT=%r is not a number; using 10", raw)
        return 10.0
    return value if value > 0 else 10.0


class DownstreamTokenError(Exception):
    """The exchange did not produce a usable downstream token.

    Raised only in ``enforce``. In ``shadow`` a failure is logged and swallowed,
    because shadow exists precisely to discover failures without causing them.
    """


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

_CacheKey = Tuple[str, str, str]

_cache: "OrderedDict[_CacheKey, Tuple[str, float]]" = OrderedDict()
_cache_lock = threading.Lock()

#: One lock per in-flight key, so N concurrent callers sharing a key perform ONE
#: exchange rather than N. Measured before this existed: 8 concurrent callers on
#: a cold cache produced 8 round trips. The map is guarded by _flight_map_lock
#: and entries are dropped once nobody holds them.
_flight_locks: "dict[_CacheKey, Tuple[threading.Lock, int]]" = {}
_flight_map_lock = threading.Lock()


class _SingleFlight:
    """Serialise callers sharing a cache key, without a global lock.

    Deliberately NOT the cache lock: holding that across an HTTP call would
    serialise every user in the process behind one slow exchange.
    """

    def __init__(self, key: _CacheKey):
        self._key = key
        self._lock: Optional[threading.Lock] = None

    def __enter__(self):
        with _flight_map_lock:
            lock, waiters = _flight_locks.get(self._key, (threading.Lock(), 0))
            _flight_locks[self._key] = (lock, waiters + 1)
        self._lock = lock
        lock.acquire()
        return self

    def __exit__(self, *exc):
        if self._lock is not None:
            self._lock.release()
        with _flight_map_lock:
            entry = _flight_locks.get(self._key)
            if entry is not None:
                lock, waiters = entry
                if waiters <= 1:
                    del _flight_locks[self._key]
                else:
                    _flight_locks[self._key] = (lock, waiters - 1)
        return False


def _cache_get(key: _CacheKey, now: float) -> Optional[str]:
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        token, expires_at = hit
        if expires_at <= now:
            del _cache[key]
            return None
        _cache.move_to_end(key)
        return token


def _cache_put(key: _CacheKey, token: str, expires_at: float) -> None:
    with _cache_lock:
        _cache[key] = (token, expires_at)
        _cache.move_to_end(key)
        while len(_cache) > MAX_CACHE_ENTRIES:
            _cache.popitem(last=False)


def reset_cache() -> None:
    """Drop every cached token. For tests, and for an operator flipping modes."""
    with _cache_lock:
        _cache.clear()


def cache_size() -> int:
    with _cache_lock:
        return len(_cache)


# --------------------------------------------------------------------------
# The exchange
# --------------------------------------------------------------------------

def _post_exchange(subject_token: str) -> Dict[str, Any]:
    """One HTTP round trip. Raises DownstreamTokenError on anything unusable.

    Synchronous because ``get_authenticated_credentials`` is a plain ``def``;
    an async client here would force every caller to change.

    ⚠️ Be honest about who pays. fastmcp already offloads SYNC tools to a worker
    thread, so the ~120 sync tools block a thread and not the loop. The caller
    that actually blocks the event loop is the ASYNC one,
    ``tools/api_fallback.py::_execute``. An earlier version of this docstring
    had that exactly backwards.

    The stall is bounded by ``MCP_DOWNSTREAM_TIMEOUT`` and by the cache plus the
    single-flight lock, so it is one timeout per key per TTL rather than one per
    call. It is still a real stall, and an async path for that one caller is
    tracked separately rather than pretended away here.
    """
    secret = _shared_secret()
    if not secret:
        raise DownstreamTokenError(
            "MCP_PROXY_SHARED_SECRET is not set, so the exchange cannot "
            "authenticate to api-v2. Refusing to call it unauthenticated."
        )

    url = f"{_auth_server()}/mcp/oauth/token-exchange"
    headers = {
        "Authorization": f"Bearer {subject_token}",
        "X-Tallyfy-Proxy-Auth": secret,
        # Without this the edge answers a hard 404 whose body reads
        # {"message":"Not found. Error Code: apsh546."} -- the request never
        # reaches Laravel at all, so it looks like an undeployed route.
        "X-Tallyfy-Client": "APIClient",
        "Accept": "application/json",
    }

    try:
        with httpx.Client(timeout=_timeout()) as client:
            response = client.post(url, headers=headers)
    except httpx.RequestError as exc:
        raise DownstreamTokenError(f"token exchange transport error: {exc}") from exc

    if response.status_code != 200:
        raise DownstreamTokenError(
            f"token exchange returned HTTP {response.status_code}"
        )

    # Everything below is parsing a body we do not control, so it is wrapped
    # whole. The narrow `except ValueError` this replaced let an AttributeError
    # escape whenever the body was valid JSON but not an object (a bare string
    # such as "maintenance"), and an escape here breaks the request in SHADOW
    # mode, which is the one mode that must never cause a failure.
    try:
        payload = response.json()
        if not isinstance(payload, dict):
            raise DownstreamTokenError("token exchange returned a non-object body")
        token = payload.get("access_token")
        if not token or not isinstance(token, str):
            raise DownstreamTokenError("token exchange returned no access_token")
    except DownstreamTokenError:
        raise
    except Exception as exc:
        raise DownstreamTokenError(
            f"token exchange returned an unreadable body: {type(exc).__name__}"
        ) from exc

    return payload


def _ttl_for(payload: Dict[str, Any], subject_exp: Optional[int], now: float) -> float:
    """Seconds this downstream token may be cached.

    Never longer than the subject's own remaining life. A downstream token that
    outlived the consent it came from would be a privilege the user cannot
    withdraw by signing out.
    """
    try:
        expires_in = int(payload.get("expires_in", 0))
    except (TypeError, ValueError):
        expires_in = 0

    ttl = expires_in - CLOCK_SKEW_S
    if subject_exp:
        ttl = min(ttl, subject_exp - now - CLOCK_SKEW_S)
    return ttl


def _exchange_or_none(subject_token: str, mode: str) -> Optional[Dict[str, Any]]:
    """Run the exchange under this mode's error policy.

    Returns None only in shadow, and only after logging. In enforce it raises,
    and there is no branch here that can return the caller's token instead.
    """
    try:
        return _post_exchange(subject_token)
    except DownstreamTokenError as exc:
        if mode == MODE_ENFORCE:
            raise
        logger.warning("downstream token exchange failed in shadow mode: %s", exc)
        return None


def get_downstream_token(
    subject_token: str,
    org_id: str,
    claims: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the credential to present to the Tallyfy API for this request.

    In ``off`` this is ``subject_token`` unchanged, and nothing is called.
    In ``shadow`` the exchange runs and is logged, and ``subject_token`` is
    still what comes back.
    In ``enforce`` the downstream token comes back, or this raises.

    🔴 There is no ``except: return subject_token`` in the enforce path, and
    there must never be one. Falling back would silently reinstate the
    passthrough this module exists to remove, and would do it in exactly the
    conditions nobody is watching. A hard failure is the only outcome that
    stays visible.
    """
    mode = _mode()
    if mode == MODE_OFF:
        return subject_token

    claims = claims or {}
    sub = str(claims.get("sub") or claims.get("user_id") or "")
    jti = str(claims.get("jti") or "")
    subject_exp = claims.get("exp")
    try:
        subject_exp = int(subject_exp) if subject_exp is not None else None
    except (TypeError, ValueError):
        subject_exp = None

    now = time.time()

    # A subject we cannot key on cannot be cached safely. Keying such a request
    # on a partial tuple would let two different tokens share one entry.
    cacheable = bool(sub and jti and org_id)
    key: Optional[_CacheKey] = (sub, str(org_id), jti) if cacheable else None

    if key is not None:
        cached = _cache_get(key, now)
        if cached is not None:
            return cached if mode == MODE_ENFORCE else subject_token

    if key is not None:
        with _SingleFlight(key):
            # Re-check inside the lock. Whoever we queued behind has very likely
            # just populated it, and without this re-read the collapse buys
            # nothing: every waiter would still exchange, just in single file.
            cached = _cache_get(key, time.time())
            if cached is not None:
                return cached if mode == MODE_ENFORCE else subject_token
            payload = _exchange_or_none(subject_token, mode)
            if payload is None:
                return subject_token
            token = payload["access_token"]
            ttl = _ttl_for(payload, subject_exp, now)
            if ttl > 0:
                _cache_put(key, token, now + ttl)
    else:
        payload = _exchange_or_none(subject_token, mode)
        if payload is None:
            return subject_token
        token = payload["access_token"]

    if mode == MODE_SHADOW:
        logger.info(
            "downstream token exchange OK in shadow mode (scope=%r, expires_in=%s); "
            "still sending the caller's token",
            payload.get("scope"), payload.get("expires_in"),
        )
        return subject_token

    return token
