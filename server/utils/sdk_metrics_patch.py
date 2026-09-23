"""
Monkey-patch TallyfySDK._make_request to track every Tallyfy API call
via the tallyfy_api_* Prometheus metrics.

Called once at server startup from server.py.

The ``operation`` label carries the endpoint SHAPE, never the endpoint itself
(tallyfy/mcp#1353). Until then it carried the raw path, so every org id, run
id, task id, email address and guest code became its own label value. Measured
on the production Prometheus 2026-09-23: 4,820 distinct ``operation`` values
came from this patch, and ``operation_label`` collapses them to 60. Each value
is about a dozen series, which made this label an accelerant behind the
tallyfy-mcp root disk filling (#1342). It also put customer org ids and guest email
addresses into metric labels, which Prometheus keeps for its whole retention
window.
"""
import re
import time
import logging
from functools import wraps

logger = logging.getLogger(__name__)

_patched = False

# What an id is replaced with. A fixed string, so every call to the same
# endpoint shares one label value whatever it was called with.
ID_PLACEHOLDER = "{id}"

# A route word in a Tallyfy API path: lowercase letters, joined by "-" or "_".
# "runs", "tasks", "completed-tasks", "automated-actions", "guests-list".
# Anything else sitting in a path segment is a value the caller supplied. The
# shapes seen live on 2026-09-23 were 32-hex ids, numeric user ids, 40-char
# mixed-case guest codes and email addresses, and none of them can match this,
# because each carries a digit, an uppercase letter or an "@".
_ROUTE_WORD = re.compile(r"[a-z]+(?:[-_][a-z]+)*")

# The one way an id could still pass as a route word: a hex id whose characters
# all happen to be a-f. Real route words are never 8+ hex letters in a row, so
# anything that long and that shape is an id.
_HEX_RUN = re.compile(r"[0-9a-f]{8,}")

# A route word is short. A lowercase slug or free text in a path position is
# not, so a long all-letter segment is treated as a value too.
_MAX_ROUTE_WORD_LEN = 32


def _is_route_word(segment: str) -> bool:
    return (
        len(segment) <= _MAX_ROUTE_WORD_LEN
        and _ROUTE_WORD.fullmatch(segment) is not None
        and _HEX_RUN.fullmatch(segment) is None
    )


def operation_label(method, endpoint) -> str:
    """Return the ``operation`` label for one SDK call, with every id removed.

    ``GET organizations/<org>/runs/<run>/tasks?page=2`` becomes
    ``GET organizations/{id}/runs/{id}/tasks``. The query string is dropped, as
    it always was. Empty segments (a leading, trailing or doubled slash) are
    kept as they are, so a path that did not carry an id reads exactly as it
    did before this change and existing dashboard queries keep matching.

    A metric must not take down the API call it is measuring, so a method or
    endpoint that is not a string is stringified rather than rejected.
    """
    path = str(endpoint).split("?", 1)[0]
    segments = [
        segment if not segment or _is_route_word(segment) else ID_PLACEHOLDER
        for segment in path.split("/")
    ]
    return f"{str(method).upper()} {'/'.join(segments)}"


def patch_tallyfy_sdk():
    global _patched
    if _patched:
        return
    _patched = True

    try:
        from tallyfy.core import BaseSDK
    except ImportError:
        logger.warning("tallyfy SDK not installed; skipping API metrics patch")
        return

    from metrics import tallyfy_api_calls_total, tallyfy_api_duration_seconds

    original = BaseSDK._make_request

    @wraps(original)
    def _instrumented(self, method, endpoint, **kwargs):
        operation = operation_label(method, endpoint)
        start = time.time()
        status = "success"
        try:
            return original(self, method, endpoint, **kwargs)
        except Exception:
            status = "error"
            raise
        finally:
            duration = time.time() - start
            tallyfy_api_calls_total.labels(operation=operation, status=status).inc()
            tallyfy_api_duration_seconds.labels(operation=operation).observe(duration)

    BaseSDK._make_request = _instrumented
    logger.info("Patched TallyfySDK._make_request with Prometheus metrics")
