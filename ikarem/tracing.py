"""Distributed tracing without requiring it.

``TracingMiddleware`` wraps each request in an OTel server span (method +
path, ``http.*`` attributes, exceptions recorded) with W3C context
propagation — and the core stays stdlib-only: the SDK imports lazily,
and anything shaped like a tracer works::

    app.use(TracingMiddleware())  # needs pip install ikarem[otel]

    # tests / SDK-free environments: inject a recorder instead
    app.use(TracingMiddleware(tracer=FakeTracer()))

A missing SDK fails at request time naming the extra, never silently
untraced. Cardinality note: span names use the raw path (the router
template isn't known when middleware runs) — aggregate by
``http.target`` prefixes downstream, or filter health probes out.
"""

from __future__ import annotations

from typing import Any

from .middleware import Middleware


class TracingMiddleware(Middleware):
    """OTel-shaped tracing with an injectable tracer.

    ``tracer``: ``None`` (lazy ``opentelemetry-sdk`` tracer named
    ``ikarem``) or any object with
    ``start_as_current_span(name, **kwargs)`` returning a context
    manager whose span has ``set_attribute`` and ``record_exception``.
    """

    def __init__(self, tracer: Any = None):
        self._tracer = tracer

    def _resolve_tracer(self) -> Any:
        if self._tracer is not None:
            return self._tracer
        try:
            from opentelemetry import trace as trace_api
        except ImportError as e:
            raise RuntimeError("pip install ikarem[otel] to use TracingMiddleware") from e
        return trace_api.get_tracer("ikarem")

    async def __call__(self, req: Any, call_next: Any) -> Any:
        tracer = self._resolve_tracer()
        name = f"{req.method} {req.path}"
        extra: dict[str, Any] = {}
        try:  # propagation only exists with the real SDK; fakes skip it
            from opentelemetry import propagate
            from opentelemetry.trace import SpanKind

            extra = {"context": propagate.extract(dict(req.headers)), "kind": SpanKind.SERVER}
        except ImportError:
            pass
        with tracer.start_as_current_span(name, **extra) as span:
            span.set_attribute("http.method", req.method)
            span.set_attribute("http.target", req.path)
            try:
                resp = await call_next(req)
            except Exception as e:  # noqa: BLE001 - record, then let it propagate
                record = getattr(span, "record_exception", None)
                if callable(record):
                    try:
                        record(e)
                    except Exception:
                        pass
                raise
            span.set_attribute("http.status_code", resp.status_code)
            if resp.status_code >= 500:
                # Handler errors render inside the pipeline (never raise
                # past here), so the status is the error signal. Mark it
                # when the span supports OTel status; otherwise the
                # status_code attribute above carries it.
                try:
                    from opentelemetry.trace import Status, StatusCode

                    setter = getattr(span, "set_status", None)
                    if callable(setter):
                        setter(Status(StatusCode.ERROR))
                except ImportError:
                    pass
            return resp
