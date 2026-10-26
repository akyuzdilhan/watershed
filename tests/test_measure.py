"""`python -m worker.measure` on desk fixtures: writes counted once, attempts and trace read where the contract puts them, nothing invented when a field is missing."""

import inspect
import json

import pytest

from worker import measure
from worker.desk import Desk

CONFIG = {"deployment": "model-a", "effort": "low", "max_tokens": 3000, "prompt_sha": "abc123"}


def attempt(n, verified, latency, usage=None, finish="completed", error=None):
    return {"attempt": n, "writer": "model-a", "verified": verified, "rejected": [] if verified else ["number not claimed"], "finish_reason": finish,
            "usage": usage, "latency_s": latency, "error": error}


def usage(i, o, r):
    return {"input_tokens": i, "output_tokens": o, "reasoning_tokens": r}


def written(doc_id, version, by, attempts, verified=True):
    return {"id": f"{doc_id}-v{version}-written", "match_id": "m-1", "card_id": doc_id, "action": "written", "by": by, "version": version,
            "verified": verified, "attempts": attempts}


@pytest.fixture
def desk(tmp_path):
    """A match with a model card, a card rewritten then given to the template, a card verified at the second attempt, an insight past the cap,
    a storyline stored before the trace existed and a recap."""
    d = tmp_path / "m-1"
    d.mkdir()
    cards = [
        {"id": "card-a", "type": "card", "match_id": "m-1", "version": 1, "writer": "model-a", "duo_writer": "model-a", "writer_config": CONFIG,
         "verification": {"verified": True, "attempts": 1, "attempts_log": [attempt(1, True, 2.0, usage(100, 50, 20))], "fallback_reason": None}},
        {"id": "card-b", "type": "card", "match_id": "m-1", "version": 2, "writer": "template", "duo_writer": "template", "writer_config": CONFIG,
         "verification": {"verified": True, "attempts": 2, "fallback_reason": "rejected twice", "attempts_log": [
             attempt(1, False, 3.0, usage(200, 80, 30)), attempt(2, False, 0.5, None, None, "RateLimitError: Error code: 429 - Too Many Requests")]}},
        {"id": "card-c", "type": "card", "match_id": "m-1", "version": 1, "writer": "model-a", "duo_writer": "template", "writer_config": CONFIG,
         "verification": {"verified": True, "attempts": 2, "attempts_log": [attempt(1, False, 1.0, usage(100, 40, 10)), attempt(2, True, 4.0, usage(120, 60, 20))]}},
    ]
    insights = {"insights": [{"id": "insight-d", "match_id": "m-1", "version": 1, "writer": "template", "duo_writer": "template",
                              "verification": {"verified": True, "attempts": 0, "attempts_log": [], "fallback_reason": "call cap"}}]}
    storyline = {"id": "storyline-m-1", "type": "storyline", "match_id": "m-1", "version": 1, "writer": "model-a", "verification": {"verified": True, "attempts": 1}}
    recap = {"id": "recap-m-1", "type": "recap", "match_id": "m-1", "writer": "model-a", "writer_config": CONFIG,
             "verification": {"verified": True, "attempts": 1, "attempts_log": [attempt(1, True, 6.0, usage(300, 100, 40))]}}
    audit = [written("storyline-m-1", 1, "model-a", 1), written("card-a", 1, "model-a", 1), written("card-a", 1, "model-a", 1),  # sent twice
             {"id": "card-a-v1-approved", "match_id": "m-1", "card_id": "card-a", "action": "approved", "by": "auto-approve", "version": 1},
             written("card-b", 1, "model-a", 1), written("card-b", 2, "template", 2), written("card-c", 1, "model-a", 2), written("insight-d", 1, "template", 0)]
    (d / "cards.json").write_text(json.dumps(cards), encoding="utf-8")
    (d / "insights.json").write_text(json.dumps(insights), encoding="utf-8")
    (d / "storyline.json").write_text(json.dumps(storyline), encoding="utf-8")
    (d / "recap.json").write_text(json.dumps(recap), encoding="utf-8")
    (d / "state.json").write_text(json.dumps({"id": "state-m-1", "type": "match_state", "match_id": "m-1"}), encoding="utf-8")
    (d / "audit.jsonl").write_text("".join(json.dumps(e) + "\n" for e in audit), encoding="utf-8")
    return d


def test_each_write_is_counted_once_with_its_writer_and_attempt(desk):
    m = measure.measure(measure.writes(measure.load_local(desk)))
    assert m["writes"] == 7  # a, b v1, b v2, c, d, storyline, recap; the duplicate audit line is dropped
    assert (m["model"], m["fallback"], m["template_only"]) == (5, 1, 1)
    assert (m["first"], m["second"], m["kept"]) == (4, 1, 0)  # b v1 and the storyline inferred from their attempts
    assert m["duo_template"] == 1  # card c; b v1 is an earlier version, its duo writer is not stored
    assert (m["calls"], m["traced"], m["attempts_traced"]) == (8, 5, 6)
    assert (m["errors"], m["rate_limited"]) == (1, 1)
    assert (m["latency_p50"], m["latency_p95"]) == (2.5, 6.0)
    assert m["usage"] == usage(820, 330, 120)
    assert m["fallback_reasons"] == {"rejected twice": 1, "call cap": 1}
    assert m["finish_reasons"] == {"completed": 5}


def test_the_report_gives_the_cap_the_cost_and_the_configuration(desk):
    text = measure.report(measure.load_local(desk), price_in=0.2, price_out=1.25)
    assert "| | card | insight | storyline | recap | all |" in text
    assert "| Writes (first versions and rewrites) | 4 | 1 | 1 | 1 | 7 |" in text
    assert "| Model calls | 6 | 0 | 1 | 1 | 8 |" in text  # card calls: a 1, b 1 + 2, c 2
    assert "| HTTP 429 answers | 1 | 0 | n/a | 0 | 1 |" in text  # the storyline has no trace: n/a, never 0
    assert "| Cost (USD) | 0.0004 | n/a (no usage) | n/a (no usage) | 0.0002 | 0.0006 |" in text
    assert "against the cap of 40 per match: 8/40; cards, insights and storyline 7, recap 1." in text
    assert "Trace fields on 5 of 7 writes" in text
    assert "| rejected twice | 1 |" in text and "| call cap | 1 |" in text
    assert "| completed | 5 |" in text
    assert "| model-a | low | 3000 | abc123 | 4 |" in text


def test_no_cost_without_prices(desk):
    text = measure.report(measure.load_local(desk))
    assert "n/a (no prices)" in text
    assert "pass `--price-in` and `--price-out`" in text


def test_a_run_without_trace_says_what_it_cannot_measure(tmp_path):
    d = tmp_path / "m-2"
    d.mkdir()
    cards = [{"id": f"card-{i}", "match_id": "m-2", "version": 1, "writer": "template", "duo_writer": "template", "verification": {"verified": True, "attempts": 0}}
             for i in range(3)]
    (d / "cards.json").write_text(json.dumps(cards), encoding="utf-8")
    (d / "insights.json").write_text(json.dumps([{"id": "insight-1", "match_id": "m-2", "writer": "model-a", "verification": {"verified": True, "attempts": 2}}]),
                                     encoding="utf-8")
    text = measure.report(measure.load_local(d))
    assert "No trace fields in this run" in text
    assert "| Writes (first versions and rewrites) | 3 | 1 | 4 |" in text  # no audit trail: one write per document
    assert "| Model writes verified at the 2nd attempt | 0 | 1 | 1 |" in text
    assert "| Latency per attempt, p50 (s) | n/a | n/a | n/a |" in text
    assert "| Input tokens | n/a | n/a | n/a |" in text
    assert "0/40" not in text and "2/40" in text
    assert "Fallback cause" not in text and "finish_reason |" not in text


def test_an_empty_folder_is_reported_as_such(tmp_path):
    assert "No write found" in measure.report(measure.load_local(tmp_path))
    with pytest.raises(FileNotFoundError):
        measure.load_local(tmp_path / "missing")


class FakeContainer:
    def __init__(self, docs):
        self.docs, self.queries = docs, []

    def query_items(self, query, partition_key=None, **kwargs):
        self.queries.append((query, partition_key))
        return iter([d for d in self.docs if d.get("match_id") == partition_key])


def test_cosmos_reads_the_same_documents_within_the_partition(desk):
    local = measure.load_local(desk)
    other = {"id": "card-x", "type": "card", "match_id": "m-9", "writer": "model-a", "verification": {"attempts": 2}}
    cards = FakeContainer(local["docs"] + [{"id": "state-m-1", "type": "match_state", "match_id": "m-1"},
                                           {"id": "fb-1", "type": "feedback", "match_id": "m-1"}, other])
    audit = FakeContainer(local["audit"] + [dict(written("card-x", 1, "model-a", 2), match_id="m-9")])
    run = measure.load_cosmos("m-1", {"cards": cards, "audit": audit})
    assert {d["type"] for d in run["docs"]} == {"card", "insight", "storyline", "recap"}
    assert cards.queries == [("SELECT * FROM c", "m-1")] and audit.queries == [("SELECT * FROM c", "m-1")]
    assert measure.measure(measure.writes(run)) == measure.measure(measure.writes(local))


def test_cli_writes_the_markdown_and_checks_its_arguments(desk, tmp_path, capsys):
    out = tmp_path / "measure.md"
    assert measure.main([str(desk), "--out", str(out), "--price-in", "0.2", "--price-out", "1.25"]) == 0
    assert out.read_text(encoding="utf-8").startswith("# Live run measures: m-1")
    with pytest.raises(SystemExit):
        measure.main([])
    with pytest.raises(SystemExit):
        measure.main([str(desk), "--price-in", "0.2"])


def test_the_call_cap_is_the_desk_default():
    assert measure.CALL_CAP == inspect.signature(Desk.__init__).parameters["max_calls_per_match"].default


def test_audit_entries_without_an_id_and_documents_without_a_type_are_counted(desk):
    run = measure.load_local(desk)
    for entry in run["audit"]:
        entry.pop("id")
    run["audit"].append(dict(run["audit"][-1], at=2.0))  # another write of the same version, at another time
    for doc in run["docs"]:
        doc.pop("type")
    records = measure.writes(run)
    assert len(records) == 8  # the 7 writes and the second one; the line sent twice (same document, version, writer and time) counts once
    assert {r["type"] for r in records} == {"card", "insight", "storyline", "recap"}
    assert measure.report(run).startswith("# Live run measures: m-1")


def test_cosmos_documents_without_a_type_are_typed_from_their_id(desk):
    local = measure.load_local(desk)
    docs = [{k: v for k, v in d.items() if k != "type"} for d in local["docs"]] + [{"id": "state-m-1", "match_id": "m-1"}]  # the match state: no desk type
    run = measure.load_cosmos("m-1", {"cards": FakeContainer(docs), "audit": FakeContainer(local["audit"])})
    assert sorted(d["type"] for d in run["docs"]) == ["card", "card", "card", "insight", "recap", "storyline"]
