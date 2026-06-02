"""
Monkey-patch TallyfySDK._make_request to track every Tallyfy API call
via the tallyfy_api_* Prometheus metrics.

Called once at server startup from server.py.
"""
import time
import logging
from functools import wraps

logger = logging.getLogger(__name__)

_patched = False


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
        operation = f"{method.upper()} {endpoint.split('?')[0]}"
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
