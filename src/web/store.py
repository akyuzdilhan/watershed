"""Where the console and the overlay read the desk (layout in docs/contracts.md): JSON files under `DESK_DIR`, or Cosmos DB when `COSMOS_ENDPOINT` is set.

Reads are cached for a second, so that every viewer of the overlay costs one read per process and per second. Producer decisions go through `worker.desk.Desk`, with a sink that writes back to the same store; the desk is imported only when a decision is made, so the overlay never loads the Writer.

Besides cards, the store reads insight and storyline documents (`insights.json` and `storyline.json` locally, `type` insight or storyline in the `cards` container) and the feed itself for the match graphics: locally `<match>/feed.jsonl[.gz]` in the desk folder, else `<REPLAY_DIR>/<match>.jsonl[.gz]` (default data/replay), in Azure the `events` container. While the match is not final, the feed is cut at the match state's `last_seq`, so a replay file never shows what the desk has not seen yet.

Producer feedback on sentences (`feedback`, through `Desk.feedback`) is read back with `feedback_docs`: `<match>/feedback.jsonl` locally, `type` feedback in the `cards` container in Azure. Locally, from `from_env`, every feedback also rebuilds the producer profile (`FEEDBACK_PROFILE`, default data/feedback/profile.json) that the worker reads.
"""

import json
import os
import threading
import time
from pathlib import Path

from web import matchfeed


def _clean(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if not k.startswith("_")}


def by_value(cards: list[dict]) -> list[dict]:
    """Rundown order: the biggest swing first, then match clock."""
    return sorted(cards, key=lambda c: (-c.get("value_pts", 0), c.get("clock_s", 0)))


class Store:
    cache_s = 1.0

    def __init__(self) -> None:
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._lock = threading.Lock()
        self._desk = None

    def _cached(self, key: tuple, load):
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self.cache_s:
            return hit[1]
        value = load()
        self._cache[key] = (now, value)
        return value

    def invalidate(self) -> None:
        self._cache.clear()

    # --- reads ------------------------------------------------------------------------------------

    def matches(self) -> list[dict]:
        """`{match_id, teams, minute_label, final}` for every match with a desk."""
        return self._cached(("matches",), self._matches)

    def cards(self, match_id: str) -> list[dict]:
        return self._cached(("cards", match_id), lambda: by_value(self._cards(match_id)))

    def state(self, match_id: str) -> dict | None:
        return self._cached(("state", match_id), lambda: self._state(match_id))

    def audit(self, match_id: str) -> list[dict]:
        """Audit entries in the order they were written."""
        return self._cached(("audit", match_id), lambda: self._audit(match_id))

    def card(self, match_id: str, card_id: str) -> dict | None:
        """One card, read fresh (decisions never act on a cached copy)."""
        return next((dict(c) for c in self._cards(match_id) if c["id"] == card_id), None)

    def insights(self, match_id: str) -> list[dict]:
        """Insight documents, the highest `value` first."""
        return self._cached(("insights", match_id), lambda: sorted(self._insights(match_id), key=lambda d: (-d.get("value", 0), d.get("clock_s") or 0)))

    def storyline(self, match_id: str) -> dict | None:
        return self._cached(("storyline", match_id), lambda: self._storyline(match_id))

    def doc(self, match_id: str, doc_id: str) -> dict | None:
        """A card, insight or storyline document by id, read fresh."""
        for d in self._cards(match_id) + self._insights(match_id) + [self._storyline(match_id) or {}]:
            if d.get("id") == doc_id:
                return dict(d)
        return None

    feed_cache_s = 5.0

    def feed(self, match_id: str) -> dict | None:
        """`web.matchfeed.build` of the match's feed (actions with corrections applied), cut at the desk's last seq while the match runs; None without a feed."""
        now = time.monotonic()
        hit = self._cache.get(("feed", match_id))
        if hit and now - hit[0] < max(self.cache_s, self.feed_cache_s):
            return hit[1]
        messages = self._feed_messages(match_id)
        value = None
        if messages:
            state = self._state(match_id) or {}
            upto = None if state.get("final") or not state.get("last_seq") else state["last_seq"]
            value = matchfeed.build(messages, upto)
        self._cache[("feed", match_id)] = (now, value)
        return value

    # --- producer decisions -----------------------------------------------------------------------

    def desk(self):
        if self._desk is None:
            from worker.desk import Desk

            self._desk = Desk(self.sink())
        return self._desk

    def approve(self, match_id: str, card_id: str, by: str, texts: list[dict] | None = None) -> dict:
        with self._lock:
            card = self.card(match_id, card_id)
            if card is None:
                raise KeyError(card_id)
            try:
                return self.desk().approve(card, by, texts)
            finally:
                self.invalidate()

    def reject(self, match_id: str, card_id: str, by: str) -> dict:
        with self._lock:
            card = self.card(match_id, card_id)
            if card is None:
                raise KeyError(card_id)
            try:
                return self.desk().reject(card, by)
            finally:
                self.invalidate()

    def decide_doc(self, match_id: str, doc_id: str, action: str, by: str) -> dict:
        """Approves or rejects an insight or storyline through the desk: its own `approve_insight` / `reject_insight` (or `_storyline`) when it has them, else the generic `approve` / `reject`, whose sink files the document by its `type`. Raises NotImplementedError when the desk has neither."""
        if action not in ("approve", "reject"):
            raise ValueError(f"unknown action {action}")
        with self._lock:
            doc = self.doc(match_id, doc_id)
            if doc is None:
                raise KeyError(doc_id)
            doc.setdefault("version", 1)
            doc.setdefault("history", [])
            desk = self.desk()
            method = getattr(desk, f"{action}_{doc.get('type', 'insight')}", None) or getattr(desk, action, None)
            if method is None:
                raise NotImplementedError(f"the desk has no {action} for {doc.get('type')}")
            try:
                return method(doc, by)
            finally:
                self.invalidate()

    def feedback_docs(self, match_id: str) -> list[dict]:
        """The producers' feedback on the match's sentences, as written (the latest per id wins: `worker.feedback.latest`)."""
        return self._cached(("feedback", match_id), lambda: self._feedback(match_id))

    def feedback(self, match_id: str, doc_id: str, target: dict, verdict: str, reasons: list[str] | None, note: str | None, by: str) -> dict:
        """A producer's thumbs up or down on one sentence, through `Desk.feedback` (stored and audited); the profile is rebuilt when the store keeps one."""
        with self._lock:
            doc = self.doc(match_id, doc_id)
            if doc is None:
                raise KeyError(doc_id)
            teams = (self._state(match_id) or {}).get("teams")
            try:
                item = self.desk().feedback(doc, target, verdict, reasons, note, by, teams)
                self._rebuild_profile()
                return item
            finally:
                self.invalidate()

    def _rebuild_profile(self) -> None:
        return None

    # --- backends ---------------------------------------------------------------------------------

    def sink(self):
        raise NotImplementedError

    def _feedback(self, match_id: str) -> list[dict]:
        return []

    def _insights(self, match_id: str) -> list[dict]:
        return []

    def _storyline(self, match_id: str) -> dict | None:
        return None

    def _feed_messages(self, match_id: str) -> list[dict]:
        return []

    def _matches(self) -> list[dict]:
        raise NotImplementedError

    def _cards(self, match_id: str) -> list[dict]:
        raise NotImplementedError

    def _state(self, match_id: str) -> dict | None:
        raise NotImplementedError

    def _audit(self, match_id: str) -> list[dict]:
        raise NotImplementedError


def _write_atomic(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(value, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _doc_list(value) -> list[dict]:
    """insights.json may hold an array or `{"insights": [...]}`."""
    if isinstance(value, dict):
        value = value.get("insights", [])
    return [d for d in value or [] if isinstance(d, dict)]


class FileDeskSink:
    """Writes the producer's decisions back to `root/<match_id>/`: the document replaced in cards.json, insights.json or storyline.json (by its `type`), the entry appended to audit.jsonl."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def card(self, card: dict) -> None:
        kind = card.get("type", "card")
        folder = self.root / card["match_id"]
        if kind == "storyline":
            _write_atomic(folder / "storyline.json", card)
            return
        path = folder / ("insights.json" if kind == "insight" else "cards.json")
        docs = _doc_list(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else []
        docs = [card if c["id"] == card["id"] else c for c in docs] + ([] if any(c["id"] == card["id"] for c in docs) else [card])
        _write_atomic(path, docs)

    def _append(self, name: str, entry: dict) -> None:
        path = self.root / entry["match_id"] / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def audit_entry(self, entry: dict) -> None:
        self._append("audit.jsonl", entry)

    def feedback(self, item: dict) -> None:
        """A producer feedback, appended to feedback.jsonl (a later line with the same id replaces an earlier one)."""
        self._append("feedback.jsonl", item)


class FileStore(Store):
    """The desk the worker writes with `--desk-dir`: one folder per match. With `profile_path`, every feedback rebuilds the producer profile there (`worker.feedback.build_profile` over every match of the desk)."""

    def __init__(self, root: Path, replay_dir: Path | None = None, profile_path: Path | None = None) -> None:
        super().__init__()
        self.root = Path(root)
        self.profile_path = Path(profile_path) if profile_path is not None else None
        self.replay_dir = Path(replay_dir if replay_dir is not None else os.environ.get("REPLAY_DIR", "data/replay"))
        self._last_good: dict[Path, object] = {}
        self._feeds: dict[Path, tuple[float, list[dict]]] = {}

    def sink(self) -> FileDeskSink:
        return FileDeskSink(self.root)

    def _json(self, path: Path, default):
        """The file's content; while the worker is rewriting it, the last version read in full."""
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except (json.JSONDecodeError, UnicodeDecodeError, PermissionError):
            return self._last_good.get(path, default)
        self._last_good[path] = value
        return value

    def _matches(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        out = []
        for d in sorted(p for p in self.root.iterdir() if p.is_dir() and ((p / "cards.json").exists() or (p / "state.json").exists())):
            state = self._json(d / "state.json", {}) or {}
            out.append({"match_id": d.name, "teams": state.get("teams"), "minute_label": state.get("minute_label"), "final": state.get("final", False)})
        return out

    def _cards(self, match_id: str) -> list[dict]:
        return [c for c in self._json(self.root / match_id / "cards.json", []) if c.get("type", "card") == "card"]

    def _state(self, match_id: str) -> dict | None:
        return self._json(self.root / match_id / "state.json", None)

    def _insights(self, match_id: str) -> list[dict]:
        return [d for d in _doc_list(self._json(self.root / match_id / "insights.json", [])) if d.get("type", "insight") == "insight"]

    def _storyline(self, match_id: str) -> dict | None:
        doc = self._json(self.root / match_id / "storyline.json", None)
        if isinstance(doc, list):
            doc = next((d for d in doc if isinstance(d, dict)), None)
        return doc if isinstance(doc, dict) else None

    def feed_path(self, match_id: str) -> Path | None:
        for path in (self.root / match_id / "feed.jsonl.gz", self.root / match_id / "feed.jsonl", self.replay_dir / f"{match_id}.jsonl.gz", self.replay_dir / f"{match_id}.jsonl"):
            if path.is_file():
                return path
        return None

    def _feed_messages(self, match_id: str) -> list[dict]:
        path = self.feed_path(match_id)
        if path is None:
            return []
        mtime = path.stat().st_mtime
        hit = self._feeds.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            messages = matchfeed.load(path)
        except (OSError, EOFError, json.JSONDecodeError):  # a file being written
            return hit[1] if hit else []
        self._feeds[path] = (mtime, messages)
        return messages

    def _audit(self, match_id: str) -> list[dict]:
        return _jsonl(self.root / match_id / "audit.jsonl")

    def _feedback(self, match_id: str) -> list[dict]:
        return _jsonl(self.root / match_id / "feedback.jsonl")

    def _rebuild_profile(self) -> None:
        if self.profile_path is None:
            return
        from worker import feedback

        feedback.save_profile(feedback.build_profile(feedback.load_local(self.root)), self.profile_path)


def _jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a line being written
    return out


class CosmosStore(Store):
    """Database `COSMOS_DATABASE`, containers `cards` (cards and match states) and `audit`, partitioned by match_id; Entra ID only."""

    def __init__(self, endpoint: str, database: str = "watershed") -> None:
        super().__init__()
        from worker.azure_io import CosmosSink

        self._sink = CosmosSink(endpoint, database)
        self.cards_c, self.audit_c, self.events_c = self._sink.cards, self._sink.audit, self._sink.events
        self._events: dict[str, tuple[int, dict[str, dict]]] = {}  # match id -> (highest _ts read, messages by id)

    def sink(self):
        return self._sink

    def _matches(self) -> list[dict]:
        query = "SELECT c.match_id, c.teams, c.minute_label, c.final FROM c WHERE c.type = 'match_state'"
        return sorted((_clean(d) for d in self.cards_c.query_items(query, enable_cross_partition_query=True)), key=lambda d: d["match_id"])

    def _cards(self, match_id: str) -> list[dict]:
        return [_clean(d) for d in self.cards_c.query_items("SELECT * FROM c WHERE c.type = 'card'", partition_key=match_id)]

    def _state(self, match_id: str) -> dict | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            return _clean(self.cards_c.read_item(f"state-{match_id}", partition_key=match_id))
        except CosmosResourceNotFoundError:
            return None

    def _insights(self, match_id: str) -> list[dict]:
        return [_clean(d) for d in self.cards_c.query_items("SELECT * FROM c WHERE c.type = 'insight'", partition_key=match_id)]

    def _storyline(self, match_id: str) -> dict | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            return _clean(self.cards_c.read_item(f"storyline-{match_id}", partition_key=match_id))
        except CosmosResourceNotFoundError:
            return None

    def _feed_messages(self, match_id: str) -> list[dict]:
        """Incremental: only the messages written since the last read (by `_ts`, one-second resolution, so the last second is read again and deduplicated by id)."""
        ts, seen = self._events.get(match_id, (0, {}))
        query = "SELECT * FROM c WHERE c._ts >= @ts"
        for d in self.events_c.query_items(query, parameters=[{"name": "@ts", "value": ts}], partition_key=match_id):
            ts = max(ts, d.get("_ts", 0))
            seen[d["id"]] = _clean(d)
        self._events[match_id] = (ts, seen)
        return list(seen.values())

    def _feedback(self, match_id: str) -> list[dict]:
        return [_clean(d) for d in self.cards_c.query_items("SELECT * FROM c WHERE c.type = 'feedback'", partition_key=match_id)]

    def _audit(self, match_id: str) -> list[dict]:
        docs = list(self.audit_c.query_items("SELECT * FROM c", partition_key=match_id))
        docs.sort(key=lambda d: d.get("at") or d.get("_ts", 0))
        return [_clean(d) for d in docs]


_store: Store | None = None


def from_env() -> Store:
    """`DESK=cosmos` (or `COSMOS_ENDPOINT` without `DESK_DIR`) reads Cosmos DB; otherwise the files under `DESK_DIR` (default data/desk)."""
    desk = os.environ.get("DESK", "").strip().lower()
    if desk == "cosmos" or (not desk and os.environ.get("COSMOS_ENDPOINT") and not os.environ.get("DESK_DIR")):
        return CosmosStore(os.environ["COSMOS_ENDPOINT"], os.environ.get("COSMOS_DATABASE", "watershed"))
    return FileStore(Path(os.environ.get("DESK_DIR", "data/desk")), profile_path=Path(os.environ.get("FEEDBACK_PROFILE", "data/feedback/profile.json")))


def get_store() -> Store:
    global _store
    if _store is None:
        _store = from_env()
    return _store


def set_store(store: Store | None) -> None:
    """Replaces the process-wide store (tests)."""
    global _store
    _store = store
