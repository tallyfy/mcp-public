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

✅ **THAT DECISION WAS REVERSED BY #998 ON 2026-08-25, AND THE BUILD STAMP IS
BACK. Do not restore the old wording from a stale copy; read both halves.** From
2026-08-23 this docstring said the opposite, and it was correct for its date: a
``releaseId`` field carrying ``MCP_GIT_SHA`` had been written here and removed
before shipping, on the grounds that ``server/`` is mirrored to the PUBLIC repo
``tallyfy/mcp-public``, so publishing the running commit beside public source
names precisely which patches an environment is missing. Production SHAs are
already public in the mirror's own commit messages (``Sync from production
<7 chars>``); STAGING's were not, and staging runs ahead of production, so a
staging stamp is a preview of unreleased fixes. That argument is unchanged and
is the real cost of this endpoint's new answer.

**What outweighed it, per #998.** Reading the stamp over ssh
(``docker exec <container> printenv MCP_GIT_SHA``) works only for the two deploy
workflows, because they are already on the droplet. Nobody else can run it. So a
deploy was unverifiable from CI, from a dashboard, from an alert, and from an
incident: during the 2026-08-24 connector investigation the only way to
establish which code was live was to ssh to the droplet and read a source file
inside a container. The same response also could not say WHICH environment you
had reached, so a probe accidentally pointed at the wrong host answered
confidently and wrongly. Both environments returned a byte-identical 289 bytes.

**What is still withheld, so the reversal stays narrow.** ``/ready`` is
unchanged: a load balancer needs the verdict, not the build. Nothing
configuration-shaped is added -- ``environment`` is clamped to a fixed
four-value enum before it is rendered, so an operator who puts a URL or a
hostname in ``TALLYFY_ENVIRONMENT`` gets ``unknown`` rather than a leak, and
``releaseId`` is emitted only when the value is SHAPED like a git object name.
Everything the "NOT REPORTED" warning below forbids is still forbidden.

Format. The MCP specification defines NO health endpoint: its liveness
mechanism is the JSON-RPC ``ping`` utility, and readiness is really the
``initialize`` handshake. So an HTTP health endpoint is a DEPLOYMENT
convention, and the convention followed here is IETF
draft-inadarei-api-health-check-06 (`application/health+json`): a top-level
`status`, plus optional `version`, `serviceId`, ``releaseId`` and a `checks`
object keyed ``component:measurement``.

``version`` is the package version and does not move on a deploy, so it cannot
tell two builds of one release apart; ``releaseId`` is the running commit and is
the field that can. ``environment`` is NOT in the draft at all -- it is a local
extension, because the draft has no field for "which deployment am I" and the
two hosts are otherwise indistinguishable from outside the container.

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
import re
from typing import Any, Dict, Tuple

from starlette.responses import JSONResponse

from constants import SERVER_VERSION
from durable_event_log import _environment as _raw_environment

logger = logging.getLogger(__name__)

HEALTH_MEDIA_TYPE = "application/health+json"
SERVICE_ID = "tallyfy-mcp-server"

# Both of the values below are ABSENT-vs-UNKNOWN sentinels, not decoration
# (#998 criterion 3). A caller has to be able to tell "this build carries no
# SHA" from "this is an older build of the endpoint that has no such field",
# and a field that disappears when unset destroys exactly that distinction:
# absent reads as the old shape. So the key is always present.
BUILD_SHA_UNKNOWN = "unknown"
ENVIRONMENT_UNKNOWN = "unknown"

# A git object name and nothing else. This is a FAIL-CLOSED SHAPE GATE, not
# validation for its own sake: `MCP_GIT_SHA` is set by the deploy shell, and if
# somebody ever exports a branch name, a URL or a path into it, that string
# would otherwise be published verbatim on a public unauthenticated endpoint.
# Anything not shaped like a SHA is reported as unknown.
#
# The upper bound is 64, not 40, ON PURPOSE. `MCP_GIT_SHA` is `github.sha`,
# which is a 40-hex SHA-1 name today, but a git object name under SHA-256 is
# 64. A bound of 40 would clamp a correct value to "unknown" on the day that
# changed, and it would do it SILENTLY and in the safe direction, so nothing
# would go red and the field would simply stop answering. Widening costs
# nothing: the constraint that matters is "hex, no separators", which is what
# keeps a branch name, a path or a URL out of a public response.
_SHA_RE = re.compile(r"\A[0-9a-f]{7,64}\Z")

# The environment label is a CLOSED ENUM for the same reason. Everything in this
# response is a boolean, a count or a fixed enum (see the warning below), and an
# env var read straight through would be the first value here an operator could
# turn into a leak without touching this file.
ENVIRONMENTS = ("production", "staging", "development")


def _build_sha() -> str:
    """The running build's commit, or an explicit unknown.

    Sourced from ``MCP_GIT_SHA`` ONLY. It deliberately does not fall back to
    ``SENTRY_RELEASE`` the way ``constants._resolve_sentry_release`` does:
    that value lives in the droplet's env file, nobody edits it, and both MCP
    Sentry projects carried a stale ``*@1.2.0`` across dozens of deploys. A
    fallback that cannot move per deploy would make this field answer "yes I
    know the build" while naming the wrong one, which is worse than unknown.
    """
    sha = (os.getenv("MCP_GIT_SHA") or "").strip().lower()
    return sha if _SHA_RE.match(sha) else BUILD_SHA_UNKNOWN


def _environment_label() -> str:
    """Which deployment this process is, as a fixed enum.

    The resolution ORDER is not ours and must not be re-derived here: it is
    ``durable_event_log._environment``, whose comment records the droplet
    measurement behind it (the server containers carry ``TALLYFY_ENVIRONMENT``
    with ``ENVIRONMENT`` unset, and the host is the other way round). Importing
    it rather than copying the tuple is deliberate -- two copies of a three-name
    precedence list drift, and the drift is silent in both directions.

    What IS ours is the clamp. That helper returns whatever the variable holds;
    this endpoint is public, so an unrecognised value becomes ``unknown``.
    """
    label = (_raw_environment() or "").strip().lower()
    return label if label in ENVIRONMENTS else ENVIRONMENT_UNKNOWN

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
                # `status` and `checks` keep their exact pre-#998 shape, so
                # anything already reading this endpoint is unaffected. The two
                # new keys are additive.
                "status": status,
                "serviceId": SERVICE_ID,
                "version": SERVER_VERSION,
                "releaseId": _build_sha(),
                "environment": _environment_label(),
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
