"""Traces: nothing without APPLICATIONINSIGHTS_CONNECTION_STRING; with it, one span per draft and per card action of the desk, with ids and verdicts, never a text.
Every test uses an in-memory exporter or a fake one: no network."""

import pytest

trace = pytest.importorskip("opentelemetry.trace")
pytest.importorskip("opentelemetry.sdk.trace")
pytest.importorskip("azure.monitor.opentelemetry.exporter")

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from generator import generate_match  # noqa: E402
from worker import telemetry  # noqa: E402
from worker.desk import Desk  # noqa: E402
from worker.pipeline import MemorySink, Pipeline  # noqa: E402
from worker.writer import TemplateWriter  # noqa: E402

CONNECTION = "InstrumentationKey=00000000-0000-0000-0000-000000000000;IngestionEndpoint=https://example.invalid/"
ALLOWED = {"match_id", "card_id", "kind", "action", "cause", "cards", "writer", "verified", "attempts", "fallback_reason", "status", "version", "deployment", "prompt_sha",
           "input_tokens", "output_tokens", "reasoning_tokens", "finish_reason", "error.type"}


@pytest.fixture(scope="module")
def demo():
    return generate_match(7, "demo", frames=False)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No global provider is set by these tests, and the module forgets its configuration after each one."""
    providers = []
    monkeypatch.setattr(trace, "set_tracer_provider", providers.append)
    telemetry.reset()
    yield providers
    telemetry.reset()


def run(stream):
    sink = MemorySink()
    pipeline = Pipeline(sink=sink, desk=Desk(sink, TemplateWriter(), auto_approve=True))
    for m in stream:
        pipeline.handle(m)
    pipeline.flush()
    return pipeline


def texts(pipeline) -> set[str]:
    out = set()
    docs = list(pipeline.sink.cards.values()) + list(pipeline.recaps.values())
    for doc in docs:
        for t in doc.get("texts", []):
            out |= {v for v in t.values() if isinstance(v, str) and len(v) > 12}
    return out


def test_no_connection_string_no_trace(demo):
    exporter = InMemorySpanExporter()
    assert telemetry.configure({}, exporter=exporter) is False
    assert telemetry.configure({telemetry.ENV: "  "}, exporter=exporter) is False
    assert not telemetry.enabled()
    run(demo[0])
    assert exporter.get_finished_spans() == ()
    with telemetry.span("anything", card_id="x") as s:
        assert s is None


def test_with_a_connection_string_the_desk_is_traced_without_its_texts(demo, isolated):
    exporter = InMemorySpanExporter()
    assert telemetry.configure({telemetry.ENV: CONNECTION}, exporter=exporter) is True
    assert len(isolated) == 1  # the provider is offered as the global one, for the Agent Framework spans
    pipeline = run(demo[0])
    spans = exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert {"desk.write", "desk.action", "desk.write_recap"} <= names
    cards = pipeline.sink.cards
    writes = [s for s in spans if s.name == "desk.write"]
    assert {s.attributes["card_id"] for s in writes} <= set(cards)
    for s in writes:
        card = cards[s.attributes["card_id"]]
        assert s.attributes["kind"] == card["moment"]
        assert s.attributes["writer"] == "template" and s.attributes["verified"] is True
    actions = [s for s in spans if s.name == "desk.action"]
    assert {s.attributes["action"] for s in actions} >= {"hold", "retract"}
    assert any(s.attributes.get("status") == "retracted" for s in actions)
    recap = next(s for s in spans if s.name == "desk.write_recap")
    assert recap.attributes["kind"] == "recap" and "writer" in recap.attributes
    said = texts(pipeline)
    assert said
    for s in spans:
        assert set(s.attributes) <= ALLOWED, set(s.attributes) - ALLOWED
        assert not {v for v in s.attributes.values() if isinstance(v, str)} & said


def test_the_agent_framework_is_instrumented_without_sensitive_data(monkeypatch):
    from agent_framework.observability import OBSERVABILITY_SETTINGS

    monkeypatch.setenv("ENABLE_SENSITIVE_DATA", "true")
    monkeypatch.setattr(OBSERVABILITY_SETTINGS, "_enable_sensitive_data", True)
    assert telemetry.configure({telemetry.ENV: CONNECTION}, exporter=InMemorySpanExporter())
    assert OBSERVABILITY_SETTINGS.ENABLED and not OBSERVABILITY_SETTINGS.SENSITIVE_DATA_ENABLED


def test_insight_drafts_are_traced_with_their_kind():
    exporter = InMemorySpanExporter()
    telemetry.configure({telemetry.ENV: CONNECTION}, exporter=exporter)

    def write(doc, teams):
        doc.update(writer="template", status="pending", texts=[{"lang": "en", "headline": "never on a span"}],
                   verification={"verified": False, "attempts": 2, "fallback_reason": "rejected twice"})

    Pipeline._write_insight(write, {"id": "insight-1", "match_id": "m-1", "kind": "streak"}, {})
    (s,) = exporter.get_finished_spans()
    assert s.name == "desk.write_insight"
    assert dict(s.attributes) == {"match_id": "m-1", "card_id": "insight-1", "kind": "streak", "writer": "template", "verified": False, "attempts": 2,
                                  "fallback_reason": "rejected twice", "status": "pending"}


def test_configure_azure_monitor_is_preferred_when_installed(monkeypatch):
    calls = []
    monkeypatch.setattr(telemetry, "_azure_monitor", lambda: lambda **kw: calls.append(kw))
    assert telemetry.configure({telemetry.ENV: CONNECTION})
    assert calls and calls[0]["connection_string"] == CONNECTION and calls[0]["resource"].attributes["service.name"] == telemetry.SERVICE_NAME
    assert telemetry.enabled()


def test_without_it_the_sdk_exports_through_the_azure_monitor_exporter(monkeypatch, isolated):
    import azure.monitor.opentelemetry.exporter as exporter_module

    made = []

    class FakeExporter(InMemorySpanExporter):
        def __init__(self, **kwargs):
            super().__init__()
            made.append(kwargs)

    monkeypatch.setattr(telemetry, "_azure_monitor", lambda: None)
    monkeypatch.setattr(exporter_module, "AzureMonitorTraceExporter", FakeExporter)
    assert telemetry.configure({telemetry.ENV: CONNECTION})
    assert made == [{"connection_string": CONNECTION}] and len(isolated) == 1


def test_a_failing_distro_falls_back_to_the_sdk(monkeypatch, isolated):
    import azure.monitor.opentelemetry.exporter as exporter_module

    def broken(**kwargs):
        raise TypeError("unexpected keyword argument")

    monkeypatch.setattr(telemetry, "_azure_monitor", lambda: broken)
    monkeypatch.setattr(exporter_module, "AzureMonitorTraceExporter", lambda **kw: InMemorySpanExporter())
    assert telemetry.configure({telemetry.ENV: CONNECTION})
    assert telemetry.enabled() and len(isolated) == 1


def test_a_bad_connection_string_leaves_the_worker_without_traces(monkeypatch, isolated):
    monkeypatch.setattr(telemetry, "_azure_monitor", lambda: None)
    assert telemetry.configure({telemetry.ENV: "nonsense"}) is False  # the real exporter rejects it, before any network call
    assert not telemetry.enabled()
    with telemetry.span("desk.write", card_id="x") as s:
        assert s is None


def test_an_exception_leaves_its_type_on_the_span_and_not_its_message():
    exporter = InMemorySpanExporter()
    telemetry.configure({telemetry.ENV: CONNECTION}, exporter=exporter)
    with pytest.raises(RuntimeError), telemetry.span("desk.write_recap", card_id="recap-m-1"):
        raise RuntimeError("the reply quoted a whole paragraph of the recap")
    (s,) = exporter.get_finished_spans()
    assert s.attributes["error.type"] == "RuntimeError" and s.status.status_code == trace.StatusCode.ERROR
    assert not s.events and not s.status.description


def test_the_tokens_of_the_model_attempts_are_on_the_span():
    doc = {"writer": "model", "verification": {"verified": True, "attempts": 2, "attempts_log": [
        {"attempt": 1, "writer": "model", "verified": False, "finish_reason": "length", "usage": {"input_tokens": 900, "output_tokens": 300, "reasoning_tokens": 200}},
        {"attempt": 2, "writer": "model", "verified": True, "finish_reason": "stop", "usage": {"input_tokens": 950, "output_tokens": 250, "reasoning_tokens": None}}]},
        "writer_config": {"deployment": "nano", "prompt_sha": "0123456789ab"}}
    out = telemetry.outcome(doc)
    assert (out["input_tokens"], out["output_tokens"], out["reasoning_tokens"], out["finish_reason"]) == (1850, 550, 200, "stop")
    assert set(telemetry.outcome({"verification": {"attempts_log": []}})) <= ALLOWED - {"input_tokens", "output_tokens", "reasoning_tokens", "finish_reason"}


def test_a_distro_that_set_the_global_provider_before_failing_keeps_one_provider(monkeypatch, isolated):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def half_done(**kwargs):
        monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
        raise ValueError("an instrumentation failed")

    monkeypatch.setattr(telemetry, "_azure_monitor", lambda: half_done)
    assert telemetry.configure({telemetry.ENV: CONNECTION})
    assert isolated == []  # no second provider offered
    with telemetry.span("desk.write", card_id="x"):
        pass
    assert [s.name for s in exporter.get_finished_spans()] == ["desk.write"]
