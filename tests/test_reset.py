"""`python -m worker.reset` on a local desk and on fake Cosmos DB containers: nothing leaves the machine, nothing is deleted without --yes."""

import json
import types

import pytest

from worker import reset


# --- local desk -------------------------------------------------------------------------------------


@pytest.fixture
def desk(tmp_path):
    """Two matches in a desk folder, with the files the worker and the console write."""
    root = tmp_path / "desk"
    for match_id, n_cards in (("demo-a", 3), ("demo-b", 1)):
        d = root / match_id
        d.mkdir(parents=True)
        cards = [{"id": f"card-{match_id}-{i}", "type": "card", "match_id": match_id} for i in range(n_cards)]
        (d / "cards.json").write_text(json.dumps(cards), encoding="utf-8")
        (d / "state.json").write_text(json.dumps({"id": f"state-{match_id}", "type": "match_state", "match_id": match_id}), encoding="utf-8")
        (d / "insights.json").write_text(json.dumps([{"id": "i1", "match_id": match_id}, {"id": "i2", "match_id": match_id}]), encoding="utf-8")
        (d / "storyline.json").write_text(json.dumps({"id": f"storyline-{match_id}", "match_id": match_id}), encoding="utf-8")
        (d / "recap.json").write_text(json.dumps({"id": f"recap-{match_id}", "type": "recap", "match_id": match_id}), encoding="utf-8")
        (d / "audit.jsonl").write_text("".join(json.dumps({"id": f"a{i}", "match_id": match_id}) + "\n" for i in range(4)), encoding="utf-8")
        (d / "feedback.jsonl").write_text(json.dumps({"id": "f1", "match_id": match_id}) + "\n", encoding="utf-8")
    return root


def test_without_yes_the_local_desk_is_only_listed(desk, capsys):
    before = sorted(p.relative_to(desk) for p in desk.rglob("*"))
    assert reset.main(["--match", "demo-a", "--desk-dir", str(desk)]) == 0
    out = capsys.readouterr().out
    assert sorted(p.relative_to(desk) for p in desk.rglob("*")) == before
    for line in ("3  cards", "2  insights", "1  storyline", "1  recap", "1  match state", "4  audit entries", "1  feedback", "Nothing was deleted"):
        assert line in out


def test_yes_deletes_the_match_folder_and_nothing_else(desk, capsys):
    (desk / "demo-a" / "notes.txt").write_text("a file the desk does not know", encoding="utf-8")
    assert reset.main(["--match", "demo-a", "--desk-dir", str(desk), "--yes"]) == 0
    assert not (desk / "demo-a").exists()
    assert len(json.loads((desk / "demo-b" / "cards.json").read_text(encoding="utf-8"))) == 1, "the other match is untouched"
    out = capsys.readouterr().out
    assert "Deleted 8 files" in out and "restart the worker" in out


def test_a_missing_match_is_not_an_error(desk, capsys):
    assert reset.main(["--match", "demo-z", "--desk-dir", str(desk), "--yes"]) == 0
    assert "Nothing to delete" in capsys.readouterr().out


@pytest.mark.parametrize("match_id", ["..", ".", "demo-a/..", "../desk", "demo a", ""])
def test_a_match_id_that_is_not_a_plain_name_is_refused(desk, match_id):
    before = sorted(p.relative_to(desk) for p in desk.rglob("*"))
    with pytest.raises(SystemExit) as stop:
        reset.main(["--match", match_id, "--desk-dir", str(desk), "--yes"])
    assert stop.value.code == 2
    assert sorted(p.relative_to(desk) for p in desk.rglob("*")) == before


def test_a_place_is_needed(desk):
    with pytest.raises(SystemExit):
        reset.main(["--match", "demo-a", "--yes"])
    with pytest.raises(SystemExit):
        reset.main(["--match", "demo-a", "--desk-dir", str(desk), "--cosmos"])
    assert (desk / "demo-a" / "cards.json").exists()


# --- Cosmos DB --------------------------------------------------------------------------------------


class NotFound(Exception):
    status_code = 404


class Forbidden(Exception):
    status_code = 403


class FakeContainer:
    """The two calls reset makes, on documents kept in a list; every call must name the match's partition."""

    def __init__(self, docs: list[dict], fail: dict | None = None) -> None:
        self.docs = [dict(d) for d in docs]
        self.fail = fail or {}  # document id -> exception raised on delete
        self.queries: list[tuple[str, object]] = []

    def query_items(self, query, parameters=None, partition_key=None, enable_cross_partition_query=None, **kwargs):
        assert partition_key is not None and not enable_cross_partition_query, "a reset stays within the match's partition"
        self.queries.append((query, partition_key))
        return iter([{k: d[k] for k in ("id", "type") if k in d} for d in self.docs if d["match_id"] == partition_key])

    def delete_item(self, item, partition_key, **kwargs):
        if item in self.fail:
            raise self.fail.pop(item)
        for d in self.docs:
            if d["id"] == item and d["match_id"] == partition_key:
                self.docs.remove(d)
                return
        raise NotFound(item)


def cosmos(fail: dict | None = None) -> dict[str, FakeContainer]:
    events = [{"id": f"{m}-m{i:04d}", "match_id": m, "type": "action"} for m in ("demo-a", "demo-b") for i in range(5)]
    cards = []
    for m in ("demo-a", "demo-b"):
        cards += [{"id": f"card-{m}-{i}", "match_id": m, "type": "card"} for i in range(3)]
        cards += [{"id": f"state-{m}", "match_id": m, "type": "match_state"}, {"id": f"storyline-{m}", "match_id": m, "type": "storyline"},
                  {"id": f"recap-{m}", "match_id": m, "type": "recap"}, {"id": f"feedback-{m}-1", "match_id": m, "type": "feedback"}]
        cards += [{"id": f"insight-{m}-{k}", "match_id": m, "type": "insight"} for k in ("scorer", "comeback")]
    audit = [{"id": f"audit-{m}-{i}", "match_id": m} for m in ("demo-a", "demo-b") for i in range(4)]
    return {"events": FakeContainer(events, fail), "cards": FakeContainer(cards, fail), "audit": FakeContainer(audit, fail)}


def run_cosmos(monkeypatch, containers, *args) -> int:
    monkeypatch.setattr(reset, "open_cosmos", lambda: ("watershed", containers))
    return reset.main(["--match", "demo-a", "--cosmos", *args])


def test_without_yes_cosmos_is_only_listed(monkeypatch, capsys):
    containers = cosmos()
    assert run_cosmos(monkeypatch, containers) == 0
    assert [len(c.docs) for c in containers.values()] == [10, 18, 8]
    out = capsys.readouterr().out
    for line in ("5  events", "3  cards", "2  insights", "1  match state", "1  storyline", "1  recap", "1  feedback", "4  audit entries", "18 documents to delete", "Nothing was deleted"):
        assert line in out


def test_yes_deletes_every_document_of_the_match_and_only_those(monkeypatch, capsys):
    containers = cosmos()
    assert run_cosmos(monkeypatch, containers, "--yes") == 0
    for c in containers.values():
        assert {d["match_id"] for d in c.docs} == {"demo-b"}
    assert [len(c.docs) for c in containers.values()] == [5, 9, 4], "the other match keeps all its documents"
    out = capsys.readouterr().out
    assert "Deleted 18 of 18 documents." in out and "restart the worker" in out


def test_a_document_already_gone_is_not_a_failure(monkeypatch, capsys):
    containers = cosmos(fail={"card-demo-a-0": NotFound("card-demo-a-0")})
    assert run_cosmos(monkeypatch, containers, "--yes") == 0
    assert "Deleted 17 of 18 documents." in capsys.readouterr().out


def test_a_refused_delete_is_reported(monkeypatch, capsys):
    containers = cosmos(fail={"state-demo-a": Forbidden("no data role")})
    assert run_cosmos(monkeypatch, containers, "--yes") == 1
    out = capsys.readouterr().out
    assert "1 could not be deleted" in out and "cards/state-demo-a: Forbidden" in out and "restart the worker" not in out
    assert all(d["match_id"] == "demo-b" or d["id"] == "state-demo-a" for c in containers.values() for d in c.docs)


def test_nothing_to_delete_in_cosmos(monkeypatch, capsys):
    containers = {name: FakeContainer([]) for name in reset.CONTAINERS}
    assert run_cosmos(monkeypatch, containers, "--yes") == 0
    assert "Nothing to delete" in capsys.readouterr().out


def test_cosmos_signs_in_with_entra_id_and_never_a_key(monkeypatch):
    cosmos_sdk = pytest.importorskip("azure.cosmos")
    pytest.importorskip("azure.identity")
    seen = {}

    class Client:
        def __init__(self, url, credential=None, **kwargs):
            seen.update(url=url, credential=credential, kwargs=kwargs)

        def get_database_client(self, name):
            seen["database"] = name
            return types.SimpleNamespace(get_container_client=lambda container: f"{name}/{container}")

    monkeypatch.setattr(cosmos_sdk, "CosmosClient", Client)
    monkeypatch.setattr(reset, "load_environment", lambda needed: None)  # no .env or azd environment in a test
    monkeypatch.setenv("COSMOS_ENDPOINT", "https://cosmos-test.documents.azure.com:443/")
    monkeypatch.delenv("COSMOS_DATABASE", raising=False)
    database, containers = reset.open_cosmos()
    assert database == "watershed" and containers == {name: f"watershed/{name}" for name in ("events", "cards", "audit")}
    credential = seen["credential"]
    assert type(credential).__name__ == "ChainedTokenCredential" and callable(getattr(credential, "get_token", None))
    assert [type(c).__name__ for c in credential.credentials] == ["AzureCliCredential", "DefaultAzureCredential"]
