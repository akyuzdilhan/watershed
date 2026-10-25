"""Delete every document of one match, so that a new replay of it starts from an empty desk.

Locally a match is the folder `<desk-dir>/<match_id>/`: cards, insights, storyline, recap, match state, audit trail, producer feedback, and the feed
when one was put there. In Azure it is every document whose partition key `match_id` is the match, in the three containers of the database:
`events` (the feed), `cards` (cards, match state, insights, storyline, recap and feedback) and `audit`. Without `--yes` nothing is deleted:
the command lists what it would delete.

Usage:
    python -m worker.reset --match demo-0007 --desk-dir data/desk          # list
    python -m worker.reset --match demo-0007 --desk-dir data/desk --yes    # delete
    python -m worker.reset --match demo-0007 --cosmos [--yes]
--cosmos reads COSMOS_ENDPOINT and COSMOS_DATABASE (default watershed) from the environment, the .env file or the azd environment, and signs in
with `az login` (AzureCliCredential), else DefaultAzureCredential: Entra ID only, never a key. Deleting needs a Cosmos DB data role that writes
(azd provision gives the developer Cosmos DB Built-in Data Contributor).

What a reset leaves: a worker that has already seen the match keeps it in memory and drops a new replay of it as duplicates, so restart it
(or let it scale to zero) before replaying; the producer profile keeps the deleted feedback until it is rebuilt (`python -m worker.feedback build`).
"""

import argparse
import gzip
import json
import os
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

from generator.publish import load_environment
from worker.azure_io import with_retry

CONTAINERS = ("events", "cards", "audit")  # infra/resources.bicep: every container is partitioned by /match_id
MATCH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")  # a folder name and a partition key, never a path
CARD_TYPES = {"card": "cards", "match_state": "match state", "insight": "insights", "storyline": "storyline", "recap": "recap", "feedback": "feedback"}
DESK_FILES = {"cards.json": "cards", "insights.json": "insights", "storyline.json": "storyline", "recap.json": "recap", "state.json": "match state",
              "audit.jsonl": "audit entries", "feedback.jsonl": "feedback", "feed.jsonl": "events", "feed.jsonl.gz": "events"}
REPLAY_NOTE = "Before replaying {m}: restart the worker if it has already seen this match (it keeps the match in memory and drops its messages as duplicates)."


# --- local desk -------------------------------------------------------------------------------------


def describe(path: Path) -> tuple[int | None, str]:
    """How many documents a desk file holds and what they are ((12, "cards")); (None, "") for a file the desk does not write."""
    what = DESK_FILES.get(path.name)
    if what is None:
        return None, ""
    try:
        if path.name.endswith((".jsonl", ".jsonl.gz")):
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt", encoding="utf-8") as f:
                n = sum(1 for line in f if line.strip())
        else:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("insights"), list):
                doc = doc["insights"]
            n = len(doc) if isinstance(doc, list) else 1
    except (OSError, ValueError, EOFError):
        return None, what
    return n, what


def local_files(desk_dir: Path, match_id: str) -> list[Path]:
    folder = desk_dir / match_id
    if folder.is_symlink():
        raise ValueError(f"{folder} is a link: delete it by hand")
    return sorted(p for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else []


def reset_local(desk_dir: Path, match_id: str, yes: bool) -> int:
    folder = desk_dir / match_id
    files = local_files(desk_dir, match_id)
    if not files:
        print(f"Nothing to delete for match {match_id} in {desk_dir}.")
        return 0
    print(f"Match {match_id} in {folder}:")
    width = max(len(str(p.relative_to(folder))) for p in files)
    for p in files:
        n, what = describe(p)
        print(f"  {str(p.relative_to(folder)):<{width}} {'' if n is None else n:>6}  {what}".rstrip())
    if not yes:
        print(f"{len(files)} files to delete, then the folder. Nothing was deleted: run again with --yes to delete them.")
        return 0
    shutil.rmtree(folder)
    print(f"Deleted {len(files)} files and the folder {folder}.")
    print(REPLAY_NOTE.format(m=match_id))
    return 0


# --- Cosmos DB --------------------------------------------------------------------------------------


def open_cosmos() -> tuple[str, dict]:
    """The database name and its containers, signed in with az login, else DefaultAzureCredential (Entra ID only)."""
    from azure.cosmos import CosmosClient
    from azure.identity import AzureCliCredential, ChainedTokenCredential, DefaultAzureCredential

    load_environment("COSMOS_ENDPOINT")
    endpoint = os.environ.get("COSMOS_ENDPOINT")
    if not endpoint:
        sys.exit("COSMOS_ENDPOINT is not set: run `azd provision`, or copy it from `azd env get-values` into .env")
    database = os.environ.get("COSMOS_DATABASE", "watershed")
    credential = ChainedTokenCredential(AzureCliCredential(), DefaultAzureCredential(exclude_cli_credential=True))
    db = CosmosClient(endpoint, credential=credential).get_database_client(database)
    return database, {name: db.get_container_client(name) for name in CONTAINERS}


def cosmos_documents(containers: dict, match_id: str) -> dict[str, list[dict]]:
    """Id and type of every document of the match, per container: one query within the match's partition each."""
    return {name: list(c.query_items("SELECT c.id, c.type FROM c", partition_key=match_id)) for name, c in containers.items()}


def kinds(container: str, docs: list[dict]) -> Counter:
    if container == "events":
        return Counter({"events": len(docs)})
    if container == "audit":
        return Counter({"audit entries": len(docs)})
    return Counter(CARD_TYPES.get(d.get("type", "card"), f"type {d.get('type')}") for d in docs)


def delete_cosmos(containers: dict, docs: dict[str, list[dict]], match_id: str) -> tuple[int, list[str]]:
    """Deletes the documents one by one, retrying throttling; a document already gone is not a failure."""
    deleted, failed = 0, []
    for name, items in docs.items():
        container = containers[name]
        before = deleted
        for d in items:
            try:
                with_retry(lambda: container.delete_item(d["id"], partition_key=match_id))
            except Exception as error:
                if getattr(error, "status_code", None) != 404:
                    failed.append(f"{name}/{d['id']}: {type(error).__name__}: {str(error)[:200]}")
                continue
            deleted += 1
        if items:
            print(f"  {name}: {deleted - before} deleted", flush=True)  # the events take a while: about 1,800 for the demo
    return deleted, failed


def reset_cosmos(database: str, containers: dict, match_id: str, yes: bool) -> int:
    docs = cosmos_documents(containers, match_id)
    total = sum(len(items) for items in docs.values())
    if total == 0:
        print(f"Nothing to delete for match {match_id} in Cosmos DB database {database}.")
        return 0
    print(f"Match {match_id} in Cosmos DB database {database}:")
    for name, items in docs.items():
        for what, n in sorted(kinds(name, items).items(), key=lambda kv: -kv[1]):
            print(f"  {name:<7} {n:>6}  {what}")
    if not yes:
        print(f"{total} documents to delete. Nothing was deleted: run again with --yes to delete them.")
        return 0
    deleted, failed = delete_cosmos(containers, docs, match_id)
    print(f"Deleted {deleted} of {total} documents.")
    if failed:
        print(f"{len(failed)} could not be deleted; first ones:", *failed[:5], sep="\n  ")
        return 1
    print(REPLAY_NOTE.format(m=match_id))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m worker.reset", description="Delete every document of one match. Without --yes, only lists them.")
    parser.add_argument("--match", required=True, help="match id, for example demo-0007")
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--desk-dir", type=Path, help="local desk, as given to the worker: deletes <desk-dir>/<match>/")
    where.add_argument("--cosmos", action="store_true", help="Cosmos DB: every document of the match in events, cards and audit")
    parser.add_argument("--yes", action="store_true", help="delete; without it nothing is deleted")
    args = parser.parse_args(argv)
    if not MATCH_ID.fullmatch(args.match):
        parser.error(f"not a match id: {args.match!r} (letters, digits, '.', '-' and '_')")
    if args.cosmos:
        database, containers = open_cosmos()
        return reset_cosmos(database, containers, args.match, args.yes)
    try:
        return reset_local(args.desk_dir, args.match, args.yes)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
