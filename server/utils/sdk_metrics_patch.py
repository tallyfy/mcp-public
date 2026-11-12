"""
Record every Tallyfy API call in the tallyfy_api_* Prometheus metrics, through
the SDK's public request hook.

``record_request`` is a request hook (SDK 4.0, ``tallyfy.request_events``):
every client the server builds carries it (``utils/sdk_client.py``), and the
SDK calls it once per HTTP attempt on all three transports, JSON calls, file
downloads and uploads. It counts CALLS, not attempts: it acts on the one event
per call whose ``final`` is True, so a call the SDK retried is one call, with
the status it finally ended in and the duration of the whole call, backoff
included. ``enable_api_metrics()`` turns it on, once, at server startup
(``server.py``); until then it records nothing, as before.

Until tallyfy/mcp#1545 this module monkey-patched the PRIVATE
``BaseSDK._make_request`` (and later ``_download_bytes`` and
``_upload_multipart``). The hook replaces all of that; no SDK method is wrapped.

The ``operation`` label carries the endpoint SHAPE, never the endpoint itself
(tallyfy/mcp#1353). Until then it carried the raw path, so every org id, run
id, task id, email address and guest code became its own label value. Measured
on the production Prometheus 2026-09-23: 4,820 distinct ``operation`` values
came from the old patch, and ``operation_label`` collapses them to 60. Each
value is about a dozen series, which made this label an accelerant behind the
tallyfy-mcp root disk filling (#1342). It also put customer org ids and guest
email addresses into metric labels, which Prometheus keeps for its whole
retention window. The SDK's ``RequestEvent.path_template`` is that same
algorithm, moved into the SDK, so the label is ``f"{method} {path_template}"``
and reads byte for byte as it did; ``test_sdk_metrics_patch.py`` pins that
against a frozen copy of the algorithm this module used to carry.
"""
import logging

from tallyfy.request_events import ID_PLACEHOLDER, path_template

logger = logging.getLogger(__name__)

__all__ = ["ID_PLACEHOLDER", "enable_api_metrics", "operation_label", "record_request"]

_enabled = False


def operation_label(method, endpoint) -> str:
    """Return the ``operation`` label for one SDK call, with every id removed.

    ``GET organizations/<org>/runs/<run>/tasks?page=2`` becomes
    ``GET organizations/{id}/runs/{id}/tasks``. The query string is dropped, as
    it always was, and empty segments are kept as they are. This is exactly
    what ``record_request`` writes for a ``RequestEvent``: the SDK's own
    ``path_template``, after the upper-case method.

    A metric must not take down the API call it is measuring, so a method or
    endpoint that is not a string is stringified rather than rejected.
    """
    return f"{str(method).upper()} {path_template(endpoint)}"


def record_request(event) -> None:
    """The request hook: count one finished call and observe its duration.

    Non-final events (an attempt the SDK retries) are ignored, so a call is
    counted once. ``status`` is ``error`` when the call raised, whatever it
    raised, and ``success`` when it returned, as the wrapper this replaced
    recorded it. Nothing is recorded until ``enable_api_metrics()`` runs.
    Whatever this raises the SDK logs and drops, so a metric can never be why
    a call fails.
    """
    if not _enabled or not event.final:
        return
    from metrics import tallyfy_api_calls_total, tallyfy_api_duration_seconds

    operation = f"{event.method} {event.path_template}"
    status = "error" if event.error is not None else "success"
    tallyfy_api_calls_total.labels(operation=operation, status=status).inc()
    tallyfy_api_duration_seconds.labels(operation=operation).observe(event.call_duration)


def enable_api_metrics() -> None:
    """Start recording Tallyfy API calls. Called once from ``server.py``."""
    global _enabled
    if _enabled:
        return
    _enabled = True
    logger.info("Recording Tallyfy API calls through the SDK request hook")
