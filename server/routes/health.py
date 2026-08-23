"""
Health Check Routes

Provides health and readiness endpoints for monitoring and load balancers.

⚠️ Until #933 both handlers here returned a hardcoded literal. `/health` was
``return JSONResponse({"status": "healthy"})`` -- it did not read its `request`
argument, so it was a CONSTANT FUNCTION and could not discriminate any server
state whatsoever. Driven with `None`, an integer and a dict in place of a
request, it returned 200 healthy for all three.

That is not a small gap, because it is the signal everything else trusted:

- ``staging.mcp.tallyfy.com`` returned ``/health`` 200 while rejecting EVERY
  ``initialize`` with 401 (#933). The health check could not see it.
- During the #785 incident ``Deploy`` and ``Build and Restart`` both reported
  `skipped`, the merged PR never shipped, and ``/health`` kept returning 200
  off the OLD container the whole time. Root CLAUDE.md rule 13 concluded
  "Health endpoints cannot distinguish 'deployed' from 'never restarted';
  version-stamp the running container instead."

⚠️ **THAT BUILD STAMP IS DELIBERATELY NOT IN THIS RESPONSE, and putting it back
is a decision rather than an improvement.** A ``releaseId`` field carrying
``MCP_GIT_SHA`` was written here and removed before shipping. Both deploy
workflows read the same value over ssh instead
(``docker exec <container> printenv MCP_GIT_SHA``), which detects a stale
container exactly as well because they are already on the droplet.

The reason is where this response goes. ``server/`` is mirrored to the PUBLIC
repo ``tallyfy/mcp-public``, so the full source of this server is readable by
anyone, and this endpoint needs no credential. Publishing the running commit
beside public source states precisely which patches an environment is missing,
and lets anyone watch in real time how long production stays unpatched after a
fix lands. Production SHAs are ALREADY public in the mirror's own commit
messages (``Sync from production <7 chars>``), so the marginal disclosure there
is small -- but the mirror syncs on ``production`` only, so STAGING's commit
would have been newly published, and staging runs ahead of production. That is
a preview of unreleased fixes.

Format. The MCP specification defines NO health endpoint: its liveness
mechanism is the JSON-RPC ``ping`` utility, and readiness is really the
``initialize`` handshake. So an HTTP health endpoint is a DEPLOYMENT
convention, and the convention followed here is IETF
draft-inadarei-api-health-check-06 (`application/health+json`): a top-level
`status`, plus optional `version`, `serviceId` and a `checks` object keyed
``component:measurement``. The draft's optional ``releaseId`` is deliberately
NOT emitted; see the warning above.

⚠️ ONE DELIBERATE DEVIATION from that draft: it specifies the status vocabulary
`pass` / `warn` / `fail`, and this returns `healthy` / `degraded` / `unhealthy`
instead. Two reasons, both about not creating a fresh asymmetry:
``host/routes/health.py`` already ships `healthy`/`degraded` and monitoring
reads both units, and `tests/integration/test_health_endpoints.py` pins
`healthy`. A repo where the two units disagree about the word for "up" is worse
than one that deviates from a draft nobody else here implements. The draft's
HTTP-code rule IS followed: pass/warn -> 2xx, fail -> 5xx.

⚠️ WHAT IS DELIBERATELY NOT REPORTED. This endpoint is PUBLIC and
unauthenticated on ``mcp.tallyfy.com``. The draft's own security section warns
that "malicious actors could use this information for orchestrating attacks".
So every value here is a boolean, a count, or a fixed enum -- never a
configured URL, never an issuer, never a key, never a file path, never an
upstream error body. `version` and the tool count are already public via the
`initialize` response, so neither is a new disclosure.

⚠️ NO NETWORK CALLS. Everything is read in-process. A health endpoint that
reaches Tallyfy would fail whenever Tallyfy has a bad minute, turn a dependency
outage into our outage, and hand an unauthenticated caller a free way to make
us generate upstream traffic. Reachability of upstream belongs in monitoring,
not on a public liveness probe.
"""

import logging
import os
from typing import Any, Dict, Tuple

from starlette.responses import JSONResponse

from constants import SERVER_VERSION

logger = logging.getLogger(__name__)

HEALTH_MEDIA_TYPE = "application/health+json"
SERVICE_ID = "tallyfy-mcp-server"

# draft-inadarei per-check vocabulary. The top-level vocabulary differs on
# purpose; see the module docstring.
PASS, WARN, FAIL = "pass", "warn", "fail"

STATUS_HEALTHY = "healthy"
STATUS_DEGRADED = "degraded"
STATUS_UNHEALTHY = "unhealthy"

# Tool registration happens once at import and never changes at runtime, so the
# count is cached after the first successful read. Recomputing it per request
# would rebuild every Tool object on a public, unauthenticated endpoint, which
# is a free CPU amplifier for anyone who can reach it.
_tool_count_cache: int | None = None


def _check_verification_key(mcp) -> Dict[str, Any]:
    """Can this process verify ANY token?

    The failure this exists to catch: `build_auth_provider` falls back to
    `NoVerificationKeyVerifier` when no key and no usable https JWKS URL can be
    resolved. That verifier rejects every bearer token unconditionally, so the
    server answers `/health` and the OAuth discovery routes and NOTHING else --
    exactly the "200 healthy, completely unusable" shape #933 is about.

    Reports only the SOURCE as a fixed enum. Never the key, never the JWKS URL.
    """
    from utils.tallyfy_auth_provider import NoVerificationKeyVerifier

    auth = getattr(mcp, "auth", None)

    if auth is None:
        # No auth provider at all means every tool is exposed unauthenticated.
        # Not a configuration this repo ever ships, so treat it as a failure
        # rather than as "nothing to check".
        return {"status": FAIL, "componentType": "component",
                "observedValue": "absent"}

    if isinstance(auth, NoVerificationKeyVerifier):
        return {"status": FAIL, "componentType": "component",
                "observedValue": "none"}

    if getattr(auth, "public_key", None):
        source = "pinned"
    elif getattr(auth, "jwks_uri", None):
        source = "jwks"
    else:
        # A verifier we do not recognise. Fail closed: this check exists to
        # answer "can we verify a token", and an unrecognised provider means we
        # cannot answer it.
        source = "unknown"
        return {"status": FAIL, "componentType": "component",
                "observedValue": source}

    return {"status": PASS, "componentType": "component",
            "observedValue": source}


async def _check_tools_registered(mcp) -> Dict[str, Any]:
    """Are the tools actually registered?

    A server that boots with zero tools satisfies `initialize` and every
    unauthenticated probe while being useless to a client. The count is already
    public (the `initialize` instructions state it), so publishing it here
    discloses nothing new.
    """
    global _tool_count_cache

    if _tool_count_cache is None:
        try:
            _tool_count_cache = len(await mcp.list_tools())
        except Exception as exc:
            # Fail closed. A check that cannot get an answer takes the safe
            # branch; the exception text is logged, never returned, because
            # this response is public.
            logger.error("Health check could not count registered tools: %s", exc)
            return {"status": FAIL, "componentType": "component",
                    "observedValue": None, "observedUnit": "tools"}

    status = PASS if _tool_count_cache > 0 else FAIL
    return {"status": status, "componentType": "component",
            "observedValue": _tool_count_cache, "observedUnit": "tools"}


async def _evaluate(mcp) -> Tuple[str, Dict[str, Any], int]:
    """Run every check once. Returns (status, checks, http_status).

    Shared by `/health` and `/ready` so the two cannot drift apart -- the same
    reason the step-update tools share one preservation helper (rule 16). The
    endpoints differ in what they RENDER, never in what they evaluate.
    """
    checks = {
        "auth:verification-key": [_check_verification_key(mcp)],
        "tools:registered": [await _check_tools_registered(mcp)],
    }

    flat = [entry for entries in checks.values() for entry in entries]
    if any(entry["status"] == FAIL for entry in flat):
        return STATUS_UNHEALTHY, checks, 503
    if any(entry["status"] == WARN for entry in flat):
        # draft-inadarei: warn MUST still be 2xx.
        return STATUS_DEGRADED, checks, 200
    return STATUS_HEALTHY, checks, 200


def register_health_routes(mcp):
    """Register health check routes with the MCP server."""

    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(request):
        """Health check endpoint for monitoring.

        200 when serving, 503 when this process cannot serve MCP requests.
        """
        status, checks, http_status = await _evaluate(mcp)
        return JSONResponse(
            {
                "status": status,
                "serviceId": SERVICE_ID,
                "version": SERVER_VERSION,
                "checks": checks,
            },
            status_code=http_status,
            media_type=HEALTH_MEDIA_TYPE,
        )

    @mcp.custom_route("/ready", methods=["GET"])
    async def readiness_check(request):
        """Readiness check endpoint for load balancers.

        Same evaluation as `/health`, deliberately narrower body: a load
        balancer needs the verdict, not the diagnostics. `status` keeps its
        existing `ready` / `not_ready` vocabulary, which is what
        tests/integration/test_health_endpoints.py pins and what any configured
        probe already reads.
        """
        status, _checks, http_status = await _evaluate(mcp)
        ready = http_status == 200
        return JSONResponse(
            {
                "status": "ready" if ready else "not_ready",
                "serviceId": SERVICE_ID,
            },
            status_code=http_status,
            media_type=HEALTH_MEDIA_TYPE,
        )
