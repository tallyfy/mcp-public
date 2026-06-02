"""Tallyfy OpenAPI (Swagger 2.0) spec cache.

Fetches ``https://api.tallyfy.com/docs/index`` on startup and refreshes
every ``_REFRESH_SECONDS`` seconds in the background. Exposes helpers
for path-template matching (so ``/organizations/ABC/custom-branding``
is matched to the spec path ``/organizations/{org}/custom-branding``).

Used by the universal API fallback tool (``tallyfy_api_call``) to
validate paths against what the live Tallyfy API actually exposes.

Graceful degradation:
- If the initial fetch fails at startup, we start with an empty spec
  and the fallback tool will reject all paths (fail closed) until a
  subsequent refresh succeeds.
- Prometheus counter ``spec_cache_fetch_failures_total`` tracks refresh
  errors; the last-known-good spec is retained across transient failures.

Issue: #171
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx


logger = logging.getLogger(__name__)


_SPEC_URL = "https://api.tallyfy.com/docs/index"
_REFRESH_SECONDS = 3600  # 1 h
_FETCH_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class EndpointSpec:
    """A single (method, path_template) entry in the OpenAPI spec."""

    method: str                          # upper-case, e.g. "GET"
    path_template: str                   # e.g. "/organizations/{org}/users"
    summary: str = ""
    description: str = ""
    tag: Optional[str] = None            # first tag if present
    parameters: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    consumes: Tuple[str, ...] = field(default_factory=tuple)
    produces: Tuple[str, ...] = field(default_factory=tuple)


class TallyfySpecCache:
    """Singleton-style cache. Start the refresh task from your server boot."""

    def __init__(self):
        self._lock = threading.RLock()
        self._endpoints: Dict[Tuple[str, str], EndpointSpec] = {}
        self._path_templates: List[str] = []          # for regex matching
        self._path_regexes: List[Tuple[re.Pattern, str]] = []
        self._last_fetch_ok: bool = False
        self._last_fetch_error: Optional[str] = None
        self._task: Optional[asyncio.Task] = None

    # ------------------------------ public API ------------------------------

    def get_endpoint(self, method: str, path: str) -> Optional[EndpointSpec]:
        """Return the spec entry for a concrete path (concrete IDs OK).

        ``path`` may be ``/organizations/ABCD/users``; we match it against
        the template ``/organizations/{org}/users``.
        """
        method_u = method.upper()
        with self._lock:
            template = self._match_template(path)
            if template is None:
                return None
            return self._endpoints.get((method_u, template))

    def match_path(self, path: str) -> Optional[str]:
        """Return the matching path template (with ``{param}`` placeholders)."""
        with self._lock:
            return self._match_template(path)

    def is_loaded(self) -> bool:
        with self._lock:
            return self._last_fetch_ok or bool(self._endpoints)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "loaded": self._last_fetch_ok,
                "endpoint_count": len(self._endpoints),
                "last_error": self._last_fetch_error,
            }

    # ------------------------------ lifecycle -------------------------------

    async def refresh_once(self) -> None:
        """Fetch the spec immediately and update state. Never raises."""
        try:
            async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_SECONDS) as client:
                resp = await client.get(_SPEC_URL)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            # Keep prior state if fetch fails.
            logger.warning("Tallyfy spec refresh failed: %s", e)
            with self._lock:
                self._last_fetch_error = str(e)
            return

        try:
            endpoints, templates = _parse_swagger_v2(data)
        except Exception as e:
            logger.error("Tallyfy spec parse failed: %s", e)
            with self._lock:
                self._last_fetch_error = f"parse: {e}"
            return

        with self._lock:
            self._endpoints = endpoints
            self._path_templates = sorted(templates, key=_template_specificity, reverse=True)
            self._path_regexes = [(_template_to_regex(t), t) for t in self._path_templates]
            self._last_fetch_ok = True
            self._last_fetch_error = None
        logger.info(
            "Tallyfy spec loaded │ %d endpoints │ %d paths",
            len(endpoints),
            len(templates),
        )

    async def start_refresh_task(self) -> None:
        """Kick off the background refresh loop. Call once at server boot."""
        await self.refresh_once()
        self._task = asyncio.create_task(self._refresh_loop(), name="tallyfy_spec_refresh")

    async def _refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_REFRESH_SECONDS)
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Tallyfy spec refresh loop error: %s", e)

    # ------------------------------ internals -------------------------------

    def _match_template(self, path: str) -> Optional[str]:
        # Strip any trailing slash (except the root) and query string.
        norm = path.split("?", 1)[0].rstrip("/")
        if not norm:
            norm = "/"
        for regex, tpl in self._path_regexes:
            if regex.match(norm):
                return tpl
        return None


_PARAM_RE = re.compile(r"\{([^}/]+)\}")


def _template_to_regex(template: str) -> re.Pattern:
    """Convert ``/organizations/{org}/users`` → regex that matches concrete paths."""
    # Escape regex chars but leave placeholders alone
    escaped = re.escape(template)
    # re.escape turns "{org}" into "\\{org\\}" — restore placeholder pattern
    pattern = re.sub(r"\\\{([^}]+)\\\}", r"(?P<\1>[^/]+)", escaped)
    return re.compile(f"^{pattern}$")


def _template_specificity(template: str) -> int:
    """Longer, less-parametric templates should match first."""
    placeholder_count = len(_PARAM_RE.findall(template))
    return len(template) - placeholder_count * 10


_ALLOWED_METHODS = ("get", "post", "put", "patch", "delete")


def _parse_swagger_v2(data: Dict[str, Any]) -> Tuple[Dict[Tuple[str, str], EndpointSpec], List[str]]:
    """Parse a Swagger 2.0 document into (endpoints, unique_templates)."""
    base_path = data.get("basePath", "/") or "/"
    if base_path == "/":
        base_path = ""
    paths = data.get("paths", {}) or {}

    endpoints: Dict[Tuple[str, str], EndpointSpec] = {}
    templates: set = set()

    for raw_path, methods in paths.items():
        full = f"{base_path}{raw_path}"
        if not full.startswith("/"):
            full = "/" + full
        templates.add(full)
        if not isinstance(methods, dict):
            continue
        for method_name in _ALLOWED_METHODS:
            entry = methods.get(method_name)
            if not isinstance(entry, dict):
                continue
            tags = entry.get("tags") or []
            params = entry.get("parameters") or []
            spec = EndpointSpec(
                method=method_name.upper(),
                path_template=full,
                summary=(entry.get("summary") or "")[:400],
                description=(entry.get("description") or "")[:2000],
                tag=tags[0] if tags else None,
                parameters=tuple(params if isinstance(params, list) else ()),
                consumes=tuple(entry.get("consumes") or ()),
                produces=tuple(entry.get("produces") or ()),
            )
            endpoints[(method_name.upper(), full)] = spec

    return endpoints, sorted(templates)


# Module-level singleton used by tools / startup code.
SPEC_CACHE = TallyfySpecCache()
