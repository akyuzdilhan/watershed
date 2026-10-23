"""Summarise af_trial.py benchmark runs (JSON Lines) as Markdown tables.

Usage:
    python spikes/bench_report.py data/bench/*.jsonl > data/bench/report.md
"""

import json
import statistics
import sys
from collections import defaultdict

# USD per million tokens (input, output). Check against the Azure pricing page before quoting.
PRICES = {"gpt-5-mini": (0.25, 2.00), "gpt-5.4-nano": (0.20, 1.25)}
SIDE_BY_SIDE = [("save-en-es", 0), ("save-en-es", 1), ("save-en-fr", 0), ("save-en-fr", 1), ("trap-shooter", 0), ("trap-extras", 0)]


def load(paths: list[str]) -> dict[tuple[str, str], list[dict]]:
    runs: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                runs[(row["model"], row["effort"])].append(row)
    return runs


def summary(model: str, rows: list[dict]) -> dict:
    expected = sum(1 if r["case"].startswith("trap") else 2 for r in rows)
    reports = [r.get("report") or {} for r in rows]
    cards = [c for rep in reports for c in rep.get("cards", [])]
    # a card counts as verified only if its run returned the right languages
    verified = sum(c["verified"] for rep in reports if rep.get("schema_ok") for c in rep.get("cards", []))
    usage = [r.get("usage") or {} for r in rows]
    tin = statistics.mean(u.get("input_token_count", 0) for u in usage)
    tout = statistics.mean(u.get("output_token_count", 0) for u in usage)
    treason = statistics.mean(u.get("reasoning_output_token_count", 0) for u in usage)
    pin, pout = PRICES[model]
    cost_run = (tin * pin + tout * pout) / 1e6
    per_card = [r["seconds"] / (1 if r["case"].startswith("trap") else 2) for r in rows]
    traps = [r for r in rows if r["case"].startswith("trap")]
    traps_ok = sum(bool(r.get("report", {}).get("verified")) for r in traps)
    return {
        "runs": len(rows),
        "errors": sum(bool(r.get("error")) for r in rows),
        "tool": f"{sum(any(c['ok'] for c in r['tool_calls']) for r in rows)}/{len(rows)}",
        "verifier": f"{verified}/{expected} ({100 * verified / expected:.0f} %)",
        "json": f"{sum(rep.get('json_valid', False) for rep in reports)}/{len(rows)}",
        "length": f"{sum(c['length_ok'] for c in cards)}/{len(cards)}",
        "s_card": f"{statistics.median(per_card):.1f} (max {max(per_card):.1f})",
        "tokens": f"{tin:.0f} / {tout:.0f} ({treason:.0f} raisonnement)",
        "cost_1000": f"{1000 * cost_run * len(rows) / expected:.2f}",
        "traps": f"{traps_ok}/{len(traps)}",
    }


def main() -> None:
    runs = load(sys.argv[1:])
    cols = ["runs", "errors", "tool", "verifier", "json", "length", "s_card", "tokens", "cost_1000", "traps"]
    heads = ["Runs", "Erreurs", "Tool calls", "Verifier", "JSON valide", "Longueur OK", "s/carte (médiane)", "Tokens in / out par run", "USD / 1000 cartes", "Pièges tenus"]
    print("| Config | " + " | ".join(heads) + " |")
    print("|" + "---|" * (len(heads) + 1))
    for (model, effort), rows in sorted(runs.items()):
        s = summary(model, rows)
        print(f"| {model} `{effort}` | " + " | ".join(str(s[c]) for c in cols) + " |")

    for label, efforts in (("effort minimal", ("minimal", "none")), ("effort medium", ("medium",))):
        side_by_side({m: rows for (m, e), rows in sorted(runs.items()) if e in efforts}, label)


def side_by_side(group: dict[str, list[dict]], label: str) -> None:
    print(f"\n### Dix cartes par modèle, {label}\n")
    print("| # | Cas | Langue | " + " | ".join(group) + " |")
    print("|" + "---|" * (3 + len(group)))
    n = 0
    for case, rep in SIDE_BY_SIDE:
        by_model = {m: next((r for r in rows if r["case"] == case and r["rep"] == rep), {}) for m, rows in group.items()}
        langs = ["en"] if case.startswith("trap") else [l for l in ("en", "es", "fr") if l in case]
        for lang in langs:
            n += 1
            cells = []
            for r in by_model.values():
                card = next((c for c in r.get("cards", []) if c.get("lang") == lang), None)
                cells.append(f"**{card['headline']}** {card['why']}".replace("|", "/") if card else "(aucune carte)")
            print(f"| {n} | {case} #{rep} | {lang} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
