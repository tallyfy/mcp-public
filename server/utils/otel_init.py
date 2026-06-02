"""OpenTelemetry bootstrap for the MCP server (Phase 12.3, closes #173).

Mirror of ``host/observability/otel_init.py`` so that the MCP server can opt
into the same tracing pipeline without a cross-package import. Both modules
honour the same ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var, so spans from the
host and the server land on the same collector and correlate by trace id.

If the env var is unset or the ``opentelemetry-*`` packages are missing,
every helper here is a no-op — production / dev / test behaviour is
unchanged.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

OTEL_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
DEFAULT_SERVICE_NAME = "mcp-server"

_provider: Optional[Any] = None
_initialized: bool = False
_otel_modules: Optional[dict] = None


def _try_import_otel() -> Optional[dict]:
    global _otel_modules
    if _otel_modules is not None:
        return _otel_modules
    try:
        from opentelemetry import trace  # type: ignore
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource  # type: ignore
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore
        from opentelemetry.semconv.resource import ResourceAttributes  # type: ignore
    except ImportError as e:
        logger.info(f"OpenTelemetry not installed; tracing disabled ({e})")
        return None

    _otel_modules = {
        "trace": trace,
        "OTLPSpanExporter": OTLPSpanExporter,
        "Resource": Resource,
        "TracerProvider": TracerProvider,
        "BatchSpanProcessor": BatchSpanProcessor,
        "ResourceAttributes": ResourceAttributes,
    }
    return _otel_modules


OBSERVABILITY_AVAILABLE = _try_import_otel() is not None


def is_tracing_enabled() -> bool:
    return _initialized and _provider is not None


def init_tracing(*, service_name: Optional[str] = None, endpoint: Optional[str] = None) -> bool:
    """Configure the global tracer provider. Idempotent."""
    global _provider, _initialized

    if _initialized:
        return _provider is not None

    endpoint = endpoint or os.getenv(OTEL_ENDPOINT_ENV)
    if not endpoint:
        _initialized = True
        logger.debug(f"OTEL tracing disabled — set {OTEL_ENDPOINT_ENV} to enable")
        return False

    modules = _try_import_otel()
    if modules is None:
        _initialized = True
        logger.warning(
            f"{OTEL_ENDPOINT_ENV} is set but opentelemetry-sdk is not "
            "installed — tracing disabled."
        )
        return False

    svc = service_name or os.getenv("OTEL_SERVICE_NAME", DEFAULT_SERVICE_NAME)
    resource = modules["Resource"].create({
        modules["ResourceAttributes"].SERVICE_NAME: svc,
        modules["ResourceAttributes"].DEPLOYMENT_ENVIRONMENT: os.getenv(
            "ENVIRONMENT", "development"
        ),
    })

    provider = modules["TracerProvider"](resource=resource)
    exporter = modules["OTLPSpanExporter"](endpoint=endpoint)
    provider.add_span_processor(modules["BatchSpanProcessor"](exporter))
    modules["trace"].set_tracer_provider(provider)

    _provider = provider
    _initialized = True
    logger.info(f"OTel tracing enabled | service={svc} | endpoint={endpoint}")
    return True


def shutdown_tracing() -> None:
    global _provider
    if _provider is None:
        return
    try:
        _provider.shutdown()
    except Exception as e:
        logger.warning(f"OTel shutdown failed: {e}")
    finally:
        _provider = None


def get_tracer(name: str = "server") -> Optional[Any]:
    if not is_tracing_enabled():
        return None
    modules = _try_import_otel()
    if modules is None:
        return None
    return modules["trace"].get_tracer(name)


@contextmanager
def trace_span(name: str, /, **attributes: Any) -> Iterator[Optional[Any]]:
    """Context manager that opens a span when tracing is enabled, no-op otherwise."""
    tracer = get_tracer()
    if tracer is None:
        yield None
        return

    safe_attrs = {
        k: v
        for k, v in attributes.items()
        if isinstance(v, (str, int, float, bool)) and v is not None
    }
    with tracer.start_as_current_span(name, attributes=safe_attrs) as span:
        try:
            yield span
        except Exception as e:
            try:
                span.record_exception(e)
                from opentelemetry.trace import Status, StatusCode  # type: ignore
                span.set_status(Status(StatusCode.ERROR, description=str(e)[:200]))
            except Exception:
                pass
            raise
