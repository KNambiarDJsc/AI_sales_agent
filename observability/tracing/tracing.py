"""OpenTelemetry setup (Section 24). Span names match the event list in the spec
(call.created, speech.started, stt.final, llm.first_token, tts.first_audio,
barge_in, tool.called, qualification.created, ...) so a trace reads like the call's
timeline. Exporter is OTLP if OTEL_EXPORTER_OTLP_ENDPOINT is set, otherwise traces are
created but not exported (safe no-op default for local dev).
"""
from __future__ import annotations

from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from config.settings import get_settings

_configured = False


def configure_tracing() -> None:
    global _configured
    if _configured:
        return
    settings = get_settings()
    provider = TracerProvider(resource=Resource.create({"service.name": settings.otel_service_name}))
    if settings.otel_exporter_otlp_endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)))
    trace.set_tracer_provider(provider)
    _configured = True


def get_tracer(name: str):
    configure_tracing()
    return trace.get_tracer(name)


@contextmanager
def span(name: str, **attributes):
    """Usage: `with span("stt.final", call_id=call_id): ...`. Never pass raw
    transcript/phone values as attributes (Section 24: avoid PII in observability)."""
    tracer = get_tracer(__name__)
    with tracer.start_as_current_span(name) as current_span:
        for key, value in attributes.items():
            current_span.set_attribute(key, value)
        yield current_span
