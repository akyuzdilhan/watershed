"""What a live run of the desk cost and how the Writer did, read back from what the desk stored. Read only: nothing is written, no model is called.

Every write is counted once: a card, insight or storyline written or rewritten (the `written` entries of the audit trail), and the recap.
For each write it reports who wrote it (the model, or the template after model attempts, or the template without a model call), whether
the Verifier passed the model's reply at the first or the second attempt, the fallback causes, and, from the per-attempt trace, the
finish_reason, the latency, the HTTP 429 answers, the tokens and the cost. The trace fields are `verification.attempts_log`,
`verification.fallback_reason` and `writer_config` on each document (an audit entry may carry them too). A run stored before they existed
is still measured: the report says which numbers it cannot give, and infers the attempt of a model write from `verification.attempts`.

The documents keep only their latest version, so the trace of an earlier version of a rewritten document is lost unless its audit entry
holds it. The recap is run by hand (`python -m worker.recap`), outside the desk's cap of model calls per match, and is reported apart.
Token counts follow the Responses API: `output_tokens` already includes `reasoning_tokens`, so the cost is input and output tokens only.

Usage:
    python -m worker.measure data/desk/demo-0007 [--price-in USD --price-out USD] [--out file.md]
    python -m worker.measure --cosmos demo-0007 [...]
Prices are US dollars per million tokens and have no default: without them, the cost row says so. --cosmos reads COSMOS_ENDPOINT and
COSMOS_DATABASE (default watershed) from the environment, the .env file or the azd environment, and signs in with `az login`
(AzureCliCredential): Entra ID only, never a key, and only queries (Cosmos DB Built-in Data Reader is enough).
"""

import argparse
import json
import os
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

CALL_CAP = 40  # Desk(max_calls_per_match=40) in worker/desk.py: model calls a match may make for its cards, insights and storyline
DESK_TYPES = ("card", "insight", "storyline")
TYPES = DESK_TYPES + ("recap",)
RATE_LIMITED = re.compile(r"\b429\b|ratelimit|rate limit|too many requests", re.IGNORECASE)
USAGE = ("input_tokens", "output_tokens", "reasoning_tokens")


# --- loading ----------------------------------------------------------------------------------------


def _json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def load_local(folder: Path) -> dict:
    """Documents and audit trail of a local desk folder (`data/desk/<match_id>/`); missing files are skipped and listed."""
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"{folder} is not a desk folder")
    docs, found = [], []
    cards = _json(folder / "cards.json", None)
    if cards is not None:
        found.append("cards.json")
        docs += [dict(c, type=c.get("type", "card")) for c in cards]
    insights = _json(folder / "insights.json", None)
    if insights is not None:
        found.append("insights.json")
        if isinstance(insights, dict):
            insights = insights.get("insights", [])
        docs += [dict(d, type=d.get("type", "insight")) for d in insights]
    for name, kind in (("storyline.json", "storyline"), ("recap.json", "recap")):
        doc = _json(folder / name, None)
        if isinstance(doc, dict):
            found.append(name)
            docs.append(dict(doc, type=doc.get("type", kind)))
    audit = []
    path = folder / "audit.jsonl"
    if path.exists():
        found.append("audit.jsonl")
        audit = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    match_id = next((d.get("match_id") for d in docs + audit if d.get("match_id")), folder.name)
    return {"match_id": match_id, "source": str(folder), "found": found, "docs": docs, "audit": audit}


def open_cosmos() -> dict:
    """The `cards` and `audit` containers, signed in with az login (AzureCliCredential): no key."""
    from azure.cosmos import CosmosClient
    from azure.identity import AzureCliCredential

    from generator.publish import load_environment

    load_environment("COSMOS_ENDPOINT")
    endpoint = os.environ.get("COSMOS_ENDPOINT")
    if not endpoint:
        sys.exit("COSMOS_ENDPOINT is not set: run `azd provision`, or copy it from `azd env get-values` into .env")
    db = CosmosClient(endpoint, credential=AzureCliCredential()).get_database_client(os.environ.get("COSMOS_DATABASE", "watershed"))
    return {name: db.get_container_client(name) for name in ("cards", "audit")}


def load_cosmos(match_id: str, containers: dict | None = None) -> dict:
    """Documents and audit trail of one match in Cosmos DB: one query within the match's partition per container."""
    containers = containers or open_cosmos()
    docs = [d for d in containers["cards"].query_items("SELECT * FROM c", partition_key=match_id) if d.get("type", "card") in TYPES]
    audit = list(containers["audit"].query_items("SELECT * FROM c", partition_key=match_id))
    return {"match_id": match_id, "source": f"Cosmos DB, partition {match_id}", "found": ["cards", "audit"], "docs": docs, "audit": audit}


# --- one record per write ---------------------------------------------------------------------------


def _kind(doc_id: str, by_id: dict) -> str:
    if doc_id in by_id:
        return by_id[doc_id]["type"]
    for kind in TYPES:
        if doc_id.startswith(kind + "-"):
            return kind
    return "card"


def _trace(holder: dict) -> dict:
    """The trace fields of a document or an audit entry, where the contract puts them (under `verification`, or at the top level)."""
    v = holder.get("verification") if isinstance(holder.get("verification"), dict) else {}
    log = v.get("attempts_log", holder.get("attempts_log"))
    return {"attempts_log": log if isinstance(log, list) else None, "fallback_reason": v.get("fallback_reason", holder.get("fallback_reason")),
            "writer_config": holder.get("writer_config") if isinstance(holder.get("writer_config"), dict) else None}


def _record(kind: str, doc_id: str, version, writer, attempts, verified, trace: dict, duo_writer=None) -> dict:
    log = trace["attempts_log"]
    if not isinstance(attempts, int):
        attempts = len(log) if log is not None else 0
    return {"type": kind, "id": doc_id, "version": version, "writer": writer or "template", "duo_writer": duo_writer, "attempts": attempts,
            "verified": verified, **trace}


def writes(run: dict) -> list[dict]:
    """One record per write: from the `written` audit entries when the trail has them, else from the documents; the recap from its document."""
    by_id = {d["id"]: d for d in run["docs"] if d.get("id")}
    out, seen, covered = [], set(), set()
    for entry in run["audit"]:
        if entry.get("action") != "written" or entry.get("id") in seen:
            continue
        seen.add(entry.get("id"))
        doc_id, version = entry.get("card_id", ""), entry.get("version", 1)
        trace = _trace(entry)
        doc = by_id.get(doc_id)
        duo_writer = None
        if doc is not None and doc.get("version", 1) == version:  # the document holds this version: its trace and duo writer apply
            duo_writer = doc.get("duo_writer")
            for key, value in _trace(doc).items():
                trace[key] = trace[key] if trace[key] is not None else value
        covered.add(doc_id)
        out.append(_record(_kind(doc_id, by_id), doc_id, version, entry.get("by"), entry.get("attempts"), entry.get("verified"), trace, duo_writer))
    for doc in run["docs"]:
        if doc.get("id") in covered or doc["type"] not in TYPES or "writer" not in doc and "verification" not in doc:
            continue
        v = doc.get("verification") if isinstance(doc.get("verification"), dict) else {}
        out.append(_record(doc["type"], doc.get("id", ""), doc.get("version", 1), doc.get("writer"), v.get("attempts"), v.get("verified"), _trace(doc),
                           doc.get("duo_writer")))
    return out


# --- measures ---------------------------------------------------------------------------------------


def by_model(r: dict) -> bool:
    return r["writer"] != "template"


def verified_attempt(r: dict) -> int | None:
    """Attempt at which the Verifier passed the model's reply (1 or 2); None when the model's texts were kept from a rejected reply, or for a template write.
    Without the trace, inferred from the number of attempts: the desk stops at the first verified reply."""
    if not by_model(r):
        return None
    if r["attempts_log"] is not None:
        for i, a in enumerate(r["attempts_log"], 1):
            if a.get("verified"):
                return a.get("attempt", i)
        return None
    return r["attempts"] if r["attempts"] in (1, 2) else None


def percentile(values: list[float], q: float) -> float | None:
    """Nearest rank on the sorted values, as `worker.evaluate` computes its p95; the median for q = 0.5."""
    if not values:
        return None
    s = sorted(values)
    return statistics.median(s) if q == 0.5 else s[min(len(s) - 1, round(q * (len(s) - 1)))]


def measure(records: list[dict], price_in: float | None = None, price_out: float | None = None) -> dict:
    """The measures of a set of writes."""
    attempts = [a for r in records if r["attempts_log"] is not None for a in r["attempts_log"] if isinstance(a, dict)]
    usage = {k: sum(int((a.get("usage") or {}).get(k) or 0) for a in attempts) for k in USAGE}
    has_usage = any(isinstance(a.get("usage"), dict) for a in attempts)
    latencies = [float(a["latency_s"]) for a in attempts if isinstance(a.get("latency_s"), (int, float))]
    model = [r for r in records if by_model(r)]
    cost = None
    if has_usage and price_in is not None and price_out is not None:
        cost = usage["input_tokens"] * price_in / 1e6 + usage["output_tokens"] * price_out / 1e6
    return {
        "writes": len(records),
        "model": len(model),
        "fallback": sum(1 for r in records if not by_model(r) and r["attempts"] > 0),
        "template_only": sum(1 for r in records if not by_model(r) and r["attempts"] == 0),
        "verified": sum(1 for r in records if r["verified"] is True),
        "first": sum(1 for r in model if verified_attempt(r) == 1),
        "second": sum(1 for r in model if verified_attempt(r) == 2),
        "kept": sum(1 for r in model if verified_attempt(r) is None),
        "duo_template": sum(1 for r in model if r["duo_writer"] == "template"),
        "calls": sum(r["attempts"] for r in records),
        "traced": sum(1 for r in records if r["attempts_log"] is not None),
        "attempts_traced": len(attempts),
        "errors": sum(1 for a in attempts if a.get("error")),
        "rate_limited": sum(1 for a in attempts if RATE_LIMITED.search(str(a.get("error") or "")) or a.get("status_code") == 429),
        "latency_p50": percentile(latencies, 0.5),
        "latency_p95": percentile(latencies, 0.95),
        "latencies": len(latencies),
        "usage": usage if has_usage else None,
        "cost": cost,
        "finish_reasons": Counter(str(a.get("finish_reason")) for a in attempts if a.get("finish_reason") is not None),
        "fallback_reasons": Counter(str(r["fallback_reason"]) if r["fallback_reason"] else "not recorded"
                                    for r in records if r["fallback_reason"] or (not by_model(r) and r["attempts"] > 0)),
        "writer_configs": Counter(json.dumps(r["writer_config"], sort_keys=True) for r in records if r["writer_config"]),
    }


# --- report -----------------------------------------------------------------------------------------


def _n(x, digits: int = 1) -> str:
    if x is None:
        return "n/a"
    return f"{x:.{digits}f}" if isinstance(x, float) else f"{x:,}".replace(",", " ")


def _share(a: int, b: int) -> str:
    return f"{a}/{b} ({100 * a / b:.1f} %)" if b else "n/a (0)"


def report(run: dict, price_in: float | None = None, price_out: float | None = None, cap: int = CALL_CAP) -> str:
    records = writes(run)
    columns = [(kind, [r for r in records if r["type"] == kind]) for kind in TYPES]
    columns = [(k, rs) for k, rs in columns if rs] + [("all", records)]
    m = {k: measure(rs, price_in, price_out) for k, rs in columns}
    total = m["all"]
    traced = total["traced"]

    def row(label: str, f) -> str:
        return f"| {label} | " + " | ".join(f(m[k]) for k, _ in columns) + " |"

    def cost(x: dict) -> str:
        if x["usage"] is None:
            return "n/a (no usage)"
        if x["cost"] is None:
            return "n/a (no prices)"
        return f"{x['cost']:.4f}"

    L = [f"# Live run measures: {run['match_id']}", "", f"Source: {run['source']} ({', '.join(run['found']) or 'no desk file'}). "
         f"Read by `python -m worker.measure`; nothing was written and no model was called.", ""]
    if not records:
        return "\n".join(L + ["No write found: no `written` audit entry and no document with a writer.", ""])
    if traced == 0:
        L += ["No trace fields in this run (`verification.attempts_log`, `verification.fallback_reason`, `writer_config`): finish_reason, latency, "
              "HTTP 429, tokens and cost cannot be measured, the fallback causes are not recorded, and the attempt at which a model write was "
              "verified is inferred from `verification.attempts`.", ""]
    elif traced < len(records):
        L += [f"Trace fields on {traced} of {len(records)} writes; the others (earlier versions of rewritten documents, or documents stored before the "
              "trace existed) count in the writer and call rows only, and their attempt is inferred from `verification.attempts`.", ""]
    L += ["| | " + " | ".join(k for k, _ in columns) + " |", "|---|" + "---|" * len(columns),
          row("Writes (first versions and rewrites)", lambda x: _n(x["writes"])),
          row("Written by the model", lambda x: _share(x["model"], x["writes"])),
          row("Template after model attempts (fallback)", lambda x: _n(x["fallback"])),
          row("Template, no model call", lambda x: _n(x["template_only"])),
          row("Verified at write time", lambda x: _share(x["verified"], x["writes"])),
          row("Model writes verified at the 1st attempt", lambda x: _n(x["first"])),
          row("Model writes verified at the 2nd attempt", lambda x: _n(x["second"])),
          row("Model texts kept from a rejected reply", lambda x: _n(x["kept"])),
          row("Model writes with the duo by template", lambda x: _n(x["duo_template"])),
          row("Model calls", lambda x: _n(x["calls"])),
          row("Writes with a trace", lambda x: _n(x["traced"])),
          row("Attempts traced", lambda x: _n(x["attempts_traced"])),
          row("Attempts with an error", lambda x: _n(x["errors"]) if x["traced"] else "n/a"),
          row("HTTP 429 answers", lambda x: _n(x["rate_limited"]) if x["traced"] else "n/a"),
          row("Latency per attempt, p50 (s)", lambda x: _n(x["latency_p50"])),
          row("Latency per attempt, p95 (s)", lambda x: _n(x["latency_p95"])),
          row("Input tokens", lambda x: _n(x["usage"]["input_tokens"]) if x["usage"] else "n/a"),
          row("Output tokens (reasoning included)", lambda x: _n(x["usage"]["output_tokens"]) if x["usage"] else "n/a"),
          row("Reasoning tokens", lambda x: _n(x["usage"]["reasoning_tokens"]) if x["usage"] else "n/a"),
          row("Cost (USD)", cost), ""]
    desk_calls = sum(r["attempts"] for r in records if r["type"] in DESK_TYPES)
    recap_calls = sum(r["attempts"] for r in records if r["type"] == "recap")
    L += [f"Model calls of the desk (cards, insights, storyline) against the cap of {cap} per match: {desk_calls}/{cap}"
          + (" (cap reached: later writes used the template)" if desk_calls >= cap else "") + f". Recap, outside the cap: {recap_calls}.", ""]
    if price_in is not None and price_out is not None:
        L += [f"Prices used: {price_in} USD per million input tokens, {price_out} USD per million output tokens.", ""]
    else:
        L += ["No prices given: pass `--price-in` and `--price-out` (USD per million tokens) for the cost.", ""]
    if total["fallback_reasons"]:
        L += ["| Fallback cause | Writes |", "|---|---|"] + [f"| {k} | {v} |" for k, v in total["fallback_reasons"].most_common()] + [""]
    if total["finish_reasons"]:
        L += ["| finish_reason | Attempts |", "|---|---|"] + [f"| {k} | {v} |" for k, v in total["finish_reasons"].most_common()] + [""]
    if total["writer_configs"]:
        L += ["| Deployment | Effort | max_tokens | prompt_sha | Writes |", "|---|---|---|---|---|"]
        for raw, n in total["writer_configs"].most_common():
            c = json.loads(raw)
            L.append(f"| {c.get('deployment', '')} | {c.get('effort', '')} | {c.get('max_tokens', '')} | {c.get('prompt_sha', '')} | {n} |")
        L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("desk", type=Path, nargs="?", help="desk folder of the match (data/desk/<match_id>)")
    parser.add_argument("--cosmos", metavar="MATCH_ID", help="read the match from Cosmos DB instead (queries only, az login)")
    parser.add_argument("--price-in", type=float, help="USD per million input tokens")
    parser.add_argument("--price-out", type=float, help="USD per million output tokens")
    parser.add_argument("--cap", type=int, default=CALL_CAP, help=f"model calls allowed per match (default {CALL_CAP}, as in worker/desk.py)")
    parser.add_argument("--out", type=Path, help="write the Markdown there instead of printing it")
    args = parser.parse_args(argv)
    if (args.desk is None) == (args.cosmos is None):
        parser.error("give a desk folder or --cosmos MATCH_ID")
    if (args.price_in is None) != (args.price_out is None):
        parser.error("--price-in and --price-out go together")
    run = load_cosmos(args.cosmos) if args.cosmos else load_local(args.desk)
    text = report(run, args.price_in, args.price_out, args.cap)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
