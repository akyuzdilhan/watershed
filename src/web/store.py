"""Where the console and the overlay read the desk (layout in docs/contracts.md): JSON files under `DESK_DIR`, or Cosmos DB when `COSMOS_ENDPOINT` is set.

Reads are cached for a second, so that every viewer of the overlay costs one read per process and per second. Producer decisions go through `worker.desk.Desk`, with a sink that writes back to the same store; the desk is imported only when a decision is made, so the overlay never loads the Writer.
"""

import json
import os
import threading
import time
from pathlib import Path


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

    # --- backends ---------------------------------------------------------------------------------

    def sink(self):
        raise NotImplementedError

    def _matches(self) -> list[dict]:
        raise NotImplementedError

    def _cards(self, match_id: str) -> list[dict]:
        raise NotImplementedError

    def _state(self, match_id: str) -> dict | None:
        raise NotImplementedError

    def _audit(self, match_id: str) -> list[dict]:
        raise NotImplementedError


class FileDeskSink:
    """Writes the producer's decisions back to `root/<match_id>/`: the card replaced in cards.json, the entry appended to audit.jsonl."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def card(self, card: dict) -> None:
        path = self.root / card["match_id"] / "cards.json"
        cards = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        cards = [card if c["id"] == card["id"] else c for c in cards] + ([] if any(c["id"] == card["id"] for c in cards) else [card])
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cards, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def audit_entry(self, entry: dict) -> None:
        path = self.root / entry["match_id"] / "audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class FileStore(Store):
    """The desk the worker writes with `--desk-dir`: one folder per match."""

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = Path(root)
        self._last_good: dict[Path, object] = {}

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

    def _audit(self, match_id: str) -> list[dict]:
        path = self.root / match_id / "audit.jsonl"
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
        self.cards_c, self.audit_c = self._sink.cards, self._sink.audit

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
    return FileStore(Path(os.environ.get("DESK_DIR", "data/desk")))


def get_store() -> Store:
    global _store
    if _store is None:
        _store = from_env()
    return _store


def set_store(store: Store | None) -> None:
    """Replaces the process-wide store (tests)."""
    global _store
    _store = store
