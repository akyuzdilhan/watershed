"""Traces of the worker in Application Insights, only when APPLICATIONINSIGHTS_CONNECTION_STRING is set.

`configure()` sets up the export: the OpenTelemetry SDK with `AzureMonitorTraceExporter` from azure-monitor-opentelemetry-exporter (both in the `agents`
extra, so in the worker image), or `azure.monitor.opentelemetry.configure_azure_monitor` when that package happens to be installed (the image does not
install it). It then turns on the Agent Framework
instrumentation without sensitive data (`enable_instrumentation(enable_sensitive_data=False)`: model calls are traced with their durations, token counts and
model names, never with the prompts or the replies, whatever ENABLE_SENSITIVE_DATA says). Without the variable nothing is configured, nothing is imported, and
`span()` costs a function call.

The pipeline wraps each draft and each card action of the desk in a span (`span()`), with the ids, the kind, the writer, the verdict of the Verifier and the
fallback reason as attributes. No text of a card, an insight or a recap is ever put on a span, nor the message of an exception that crosses one: the
span keeps only its type (`error.type`), as the Agent Framework does without sensitive data.
"""

import contextlib
import logging
import os
from collections.abc import Iterator, Mapping

ENV = "APPLICATIONINSIGHTS_CONNECTION_STRING"
SERVICE_NAME = "watershed-worker"
TRACER_NAME = "watershed.worker"

log = logging.getLogger("worker")

_tracer = None  # set by configure(); None: span() yields None and records nothing
_provider = None  # the SDK provider when this module built it (not with configure_azure_monitor, which owns its own)


def _azure_monitor():
    """`configure_azure_monitor`, or None when azure-monitor-opentelemetry is not installed."""
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
    except ImportError:
        return None
    return configure_azure_monitor


def _resource():
    try:
        from agent_framework.observability import create_resource
    except ImportError:
        from opentelemetry.sdk.resources import Resource

        return Resource.create({"service.name": SERVICE_NAME})
    return create_resource(service_name=SERVICE_NAME)


def _instrument_agent_framework() -> None:
    try:
        from agent_framework.observability import enable_instrumentation
    except ImportError:  # no Writer agent in this environment: nothing to instrument
        return
    enable_instrumentation(enable_sensitive_data=False)


def _sdk(connection_string: str, resource, exporter):
    """A tracer provider of the OpenTelemetry SDK that exports to `exporter`, or to Application Insights; made the global one, for the Agent Framework spans."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    if exporter is None:
        from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter

        processor = BatchSpanProcessor(AzureMonitorTraceExporter(connection_string=connection_string))
    else:
        processor = SimpleSpanProcessor(exporter)
    provider = TracerProvider(resource=resource)  # flushed and shut down at exit (shutdown_on_exit)
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    return provider.get_tracer(TRACER_NAME), provider


def configure(environ: Mapping[str, str] | None = None, exporter=None) -> bool:
    """Exports the worker's traces to Application Insights when `environ` (default: the process environment) has a connection string; returns whether it did.

    `exporter` replaces the Azure Monitor exporter (tests pass an in-memory one); it is then fed span by span, through the SDK path. A failure to set up the
    export is logged and the worker runs without traces.
    """
    global _tracer, _provider
    environ = os.environ if environ is None else environ
    connection_string = (environ.get(ENV) or "").strip()
    if not connection_string:
        return False
    try:
        resource = _resource()
        distro = _azure_monitor() if exporter is None else None
        if distro is not None:
            try:
                distro(connection_string=connection_string, resource=resource)
            except Exception as error:  # an option it does not take, or a failed setup: the SDK path still exports
                from opentelemetry import trace

                if isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
                    log.warning("configure_azure_monitor failed (%s), exporting through the SDK", type(error).__name__)
                    distro = None
                else:  # it set the global provider before failing, and OpenTelemetry sets it once: the desk's spans go there too, with the Agent Framework's
                    log.warning("configure_azure_monitor failed (%s) after setting the tracer provider; the spans go to that provider", type(error).__name__)
                    _tracer, _provider = trace.get_tracer(TRACER_NAME), None
            else:
                from opentelemetry import trace

                _tracer, _provider = trace.get_tracer(TRACER_NAME), None
        if distro is None:
            _tracer, _provider = _sdk(connection_string, resource, exporter)
        _instrument_agent_framework()
    except Exception as error:  # a bad connection string or a missing package: no traces, the desk still runs
        log.warning("traces not exported: %s", type(error).__name__)
        _tracer = _provider = None
        return False
    log.info("traces exported to Application Insights (%s)", "opentelemetry-sdk" if _provider is not None else "global tracer provider")
    return True


def enabled() -> bool:
    return _tracer is not None


def reset() -> None:
    """Forgets the configuration (tests). The global OpenTelemetry provider, if one was set, stays: OpenTelemetry sets it once per process."""
    global _tracer, _provider
    if _provider is not None:
        _provider.shutdown()
    _tracer = _provider = None


def _clean(attributes: Mapping) -> dict:
    """Attributes OpenTelemetry accepts: None dropped, anything that is not a str, bool, int or float turned into a str."""
    return {k: v if isinstance(v, (str, bool, int, float)) else str(v) for k, v in attributes.items() if v is not None}


@contextlib.contextmanager
def span(name: str, **attributes) -> Iterator[object | None]:
    """A span named `name` with `attributes`, current for the block; None (and nothing recorded) when traces are not configured. Set more with `annotate`."""
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name, attributes=_clean(attributes), record_exception=False, set_status_on_exception=False) as s:
        try:
            yield s
        except Exception as error:  # the type only: an exception message can quote a text or a reply
            from opentelemetry.trace import StatusCode

            s.set_attribute("error.type", type(error).__name__)
            s.set_status(StatusCode.ERROR)
            raise


def annotate(s, **attributes) -> None:
    if s is not None:
        s.set_attributes(_clean(attributes))


def _usage(log_: object) -> dict:
    """Token totals and the last finish reason over `verification.attempts_log`; nothing when the document has no attempt log."""
    attempts = [a for a in log_ if isinstance(a, dict)] if isinstance(log_, list) else []
    if not attempts:
        return {}
    usages = [a["usage"] for a in attempts if isinstance(a.get("usage"), dict)]

    def total(key: str) -> int | None:
        values = [u[key] for u in usages if isinstance(u.get(key), int)]
        return sum(values) if values else None

    return {"input_tokens": total("input_tokens"), "output_tokens": total("output_tokens"), "reasoning_tokens": total("reasoning_tokens"),
            "finish_reason": attempts[-1].get("finish_reason")}


def outcome(doc: dict) -> dict:
    """What a span keeps of a written document: who wrote it, the Verifier's verdict, why the template replaced the model, the tokens of the model
    attempts, and the status. No text."""
    v = doc.get("verification") if isinstance(doc.get("verification"), dict) else {}
    config = doc.get("writer_config") if isinstance(doc.get("writer_config"), dict) else {}
    return {"writer": doc.get("writer"), "verified": v.get("verified"), "attempts": v.get("attempts"), "fallback_reason": v.get("fallback_reason"),
            "status": doc.get("status"), "version": doc.get("version"), "deployment": config.get("deployment"), "prompt_sha": config.get("prompt_sha"),
            **_usage(v.get("attempts_log"))}
