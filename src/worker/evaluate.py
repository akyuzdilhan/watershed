"""Evaluation harness: the desk measured against the hidden ground truth, over many seeds, with numbers rather than claims.

Usage:
    python -m worker.evaluate --seeds 100 --out docs/evaluation.md --json evaluation.json
    python -m worker.evaluate --seeds 5 --timing                 # adds the processing time per match (not deterministic)

Everything runs offline: the generator, the pipeline, the Spotter, the desk with the template writer and the Verifier. No Azure, no model call. Benchmark files from an earlier model run (data/bench/*.jsonl) are only read and summarised.

Feed time: every message is handed to the pipeline with `now` = its arrival time on the broadcast clock (the feed's `emitted_at_s` when nothing is perturbed), so a card's production time is the arrival time of the message that released it from the reorder buffer. Definitions of every metric are in docs/notes/evaluation.md.
"""

import argparse
import json
import random
import statistics
import time
from collections import Counter
from pathlib import Path

from generator import generate_match
from generator.publish import load
from worker.desk import Desk
from worker.pipeline import MemorySink, Pipeline
from worker.writer import LANGUAGES, TemplateWriter, card_facts, verify

DEMO_REPLAY = Path("data/replay/demo-0007.jsonl.gz")
DEMO_TRUTH = Path("data/replay/demo-0007.truth.json")
ACTIONS = ("none", "retract", "rewrite")
KINDS = ("goal", "red_card", "save", "chance")
VALUE_BINS = ((0, 5), (5, 10), (10, 20), (20, 30), (30, 50), (50, 101))

# Perturbations replayed on top of the feed's own incidents. Each one changes arrival times only (the messages are untouched), then the stream is sorted by arrival.
# "within buffer" ones keep every extra delay below the 3-second reorder buffer; "beyond buffer" ones exceed it on purpose.
PERTURBATIONS = {
    "jitter_2s": {"jitter": 2.0, "label": "every message delayed by 0-2 s (reorders neighbours)", "within": True},
    "duplicates": {"duplicates": 0.05, "label": "5 % of messages sent again 0-60 s later", "within": True},
    "delay_2s": {"delay": (0.05, 0.5, 2.5), "label": "5 % of messages delayed by 0.5-2.5 s", "within": True},
    "combined": {"jitter": 1.0, "duplicates": 0.05, "delay": (0.05, 0.5, 1.9), "label": "0-1 s jitter, 5 % duplicates, 5 % delayed 0.5-1.9 s", "within": True},
    "jitter_6s": {"jitter": 6.0, "label": "every message delayed by 0-6 s", "within": False},
    "delay_10s": {"delay": (0.05, 4.0, 10.0), "label": "5 % of messages delayed by 4-10 s", "within": False},
    "delay_120s": {"delay": (0.02, 30.0, 120.0), "label": "2 % of messages delayed by 30-120 s", "within": False},
    "goal_after_correction": {"after_correction": 5.0, "label": "each corrected goal arrives 5 s after its correction (matches with a corrected goal only)", "within": False},
}


# --- running a stream -------------------------------------------------------------------------------

def arrivals(stream: list[dict]) -> list[tuple[float, dict]]:
    """The feed as delivered: arrival time = emitted_at_s."""
    return [(m["emitted_at_s"], m) for m in stream]


def perturb(stream: list[dict], name: str, seed: int) -> list[tuple[float, dict]] | None:
    """The feed with extra network trouble, deterministic for (name, seed); None when the perturbation does not apply to this match."""
    spec = PERTURBATIONS[name]
    rng = random.Random(f"{name}-{seed}")
    moved = {}
    if spec.get("after_correction"):
        goals = {m["event_id"] for m in stream if m["type"] == "action" and m["action_type"].startswith("shot") and m["shot"]["outcome"] == "goal"}
        for m in stream:
            if m["type"] == "correction" and m["op"] in ("void", "reassign") and m["target_event_id"] in goals:
                moved.setdefault(m["target_event_id"], m["emitted_at_s"] + spec["after_correction"])
        if not moved:
            return None
    out = []
    for m in stream:
        t = moved.get(m["message_id"], m["emitted_at_s"])
        if spec.get("jitter"):
            t += rng.uniform(0, spec["jitter"])
        if spec.get("delay"):
            share, low, high = spec["delay"]
            if rng.random() < share:
                t += rng.uniform(low, high)
        out.append((t, m))
        if spec.get("duplicates") and rng.random() < spec["duplicates"]:
            out.append((t + rng.uniform(0, 60), m))
    out.sort(key=lambda x: x[0])  # stable: equal times keep the feed order
    return out


def run(timed: list[tuple[float, dict]], desk: bool = True) -> dict:
    """Runs the pipeline on (arrival, message) pairs; returns the pipeline, the sink and every result with the feed time it was produced at."""
    sink = MemorySink()
    pipeline = Pipeline(sink=sink, desk=Desk(sink, TemplateWriter(), auto_approve=True) if desk else None)
    produced = []
    start = time.perf_counter()
    for t, m in timed:
        produced += [(t, r) for r in pipeline.handle(m, now=t)]
    end_t = (timed[-1][0] if timed else 0.0) + pipeline.hold_s  # the buffer would give up on its last gap hold_s later
    produced += [(end_t, r) for r in pipeline.flush()]
    elapsed = time.perf_counter() - start
    return {"pipeline": pipeline, "sink": sink, "produced": produced, "elapsed_s": elapsed, "messages": len(timed)}


def snapshot(result: dict) -> dict:
    """What must not depend on arrival order: every card as it ends up, the card actions taken, the final score."""
    pipeline = result["pipeline"]
    spotter = next(iter(pipeline.spotters.values()))
    cards = {}
    for c in spotter.cards.values():
        cards[c["id"]] = (c["moment"], c["status"], c["version"], c["team"], c["player"]["id"], c["shot_event_id"], c["value_pts"], json.dumps(c["facts"], sort_keys=True), json.dumps(c["score_before"], sort_keys=True))
    actions = sorted((r["card_id"], r["action"], r["message_id"]) for _, r in result["produced"] if "action" in r)
    match = next(iter(pipeline.matches.values()))
    return {"cards": cards, "actions": actions, "score": match.score()}


def diff(a: dict, b: dict) -> list[str]:
    """Human-readable differences between two snapshots."""
    out = []
    for cid in sorted(set(a["cards"]) | set(b["cards"])):
        if cid not in b["cards"]:
            out.append(f"card {cid} missing")
        elif cid not in a["cards"]:
            out.append(f"extra card {cid}")
        elif a["cards"][cid] != b["cards"][cid]:
            out.append(f"card {cid} differs")
    if a["actions"] != b["actions"]:
        out.append(f"card actions differ: {sorted(set(a['actions']) ^ set(b['actions']))}")
    if a["score"] != b["score"]:
        out.append(f"final score {b['score']} instead of {a['score']}")
    return out


# --- scoring one match --------------------------------------------------------------------------

def final_action(actions: list[str]) -> str:
    """What happened to a card, from the corrections' point of view: none, retract, rewrite, or both."""
    done = [a for a in actions if a in ("retract", "rewrite")]
    if not done:
        return "none"
    return "+".join(sorted(set(done), key=done.index))


def evaluate_match(stream: list[dict], truth: dict, robustness: bool = True) -> dict:
    main = run(arrivals(stream))
    pipeline, sink, produced = main["pipeline"], main["sink"], main["produced"]
    spotter = next(iter(pipeline.spotters.values()))
    match = next(iter(pipeline.matches.values()))
    cards = spotter.cards
    first = {}
    for m in stream:  # the first copy of each message, for its broadcast and emission times
        first.setdefault(m["message_id"], m)

    # truth: what deserves a card
    goals = {g["event_id"]: g for g in truth["goals"]}
    reds = {c["event_id"]: c for c in truth["cards"] if c["card"] in ("red", "second_yellow")}
    planted_saves = {m["event_id"] for m in truth["moments"] if m["kind"] == "save"}  # the shot the keeper stopped
    shots = {s["event_id"]: s for s in truth["shots"]}
    by_event = {c["event_id"]: c for c in cards.values()}

    goal_found = {e for e in goals if e in by_event and by_event[e]["moment"] == "goal"}
    red_found = {e for e in reds if e in by_event and by_event[e]["moment"] == "red_card"}
    save_found = {c["shot_event_id"] for c in cards.values() if c["moment"] == "save"} & planted_saves

    card_class = {}
    for c in cards.values():
        if c["moment"] == "save" and first[c["event_id"]]["seq"] != first[c["shot_event_id"]]["seq"] + 1:
            card_class[c["id"]] = "mispaired_save"  # in the feed the keeper's action always comes right after the shot; this card credits a later, unrelated save
        elif c["event_id"] in goals and c["moment"] == "goal":
            card_class[c["id"]] = "truth_goal"
        elif c["event_id"] in reds and c["moment"] == "red_card":
            card_class[c["id"]] = "truth_red"
        elif c["moment"] == "save" and c["shot_event_id"] in planted_saves:
            card_class[c["id"]] = "truth_save"
        else:
            s = shots.get(c["shot_event_id"]) if c["shot_event_id"] else None
            grounded = s is not None and ((c["moment"] == "save" and s["outcome"] == "saved") or (c["moment"] == "chance" and s["outcome"] not in ("goal", "saved")))
            card_class[c["id"]] = f"extra_{c['moment']}" if grounded else f"ungrounded_{c['moment']}"

    # card actions against the expected ones
    actions_by_event: dict[str, list[str]] = {}
    action_times: dict[tuple[str, str], float] = {}
    card_times: dict[str, float] = {}
    for t, r in produced:
        if "action" in r:
            actions_by_event.setdefault(r["event_id"], []).append(r["action"])
            action_times.setdefault((r["event_id"], r["action"]), t)
        else:
            card_times.setdefault(r["event_id"], t)
    confusion = Counter()
    for e, g in goals.items():
        confusion[(g["expected_card_action"], final_action(actions_by_event.get(e, [])) if e in by_event else "no card")] += 1
    false_retractions = sorted(e for e, acts in actions_by_event.items() if "retract" in acts and goals.get(e, {}).get("expected_card_action") != "retract")
    false_rewrites = sorted(e for e, acts in actions_by_event.items() if "rewrite" in acts and goals.get(e, {}).get("expected_card_action") != "rewrite")

    # VAR checks on carded moments: a hold, then a release (stands) or a retraction (overturned)
    var_ok, var_total = 0, 0
    for v in truth["var_checks"]:
        e = v["target_event_id"]
        if e not in by_event:
            continue
        var_total += 1
        acts = actions_by_event.get(e, [])
        var_ok += "hold" in acts and (("release" in acts) if v["decision"] == "stands" else ("retract" in acts))

    # timing in feed time
    card_delays = []
    for c in cards.values():
        if c["event_id"] not in card_times:
            continue
        ev = first[c["event_id"]]
        origin = first[c["shot_event_id"]] if c["moment"] == "save" else ev  # a save card waits for the keeper's action after the shot
        produced_at = card_times[c["event_id"]]
        card_delays.append({"moment": c["moment"], "total_s": produced_at - origin["wall_s"], "latency_s": ev["emitted_at_s"] - ev["wall_s"], "buffer_s": produced_at - ev["emitted_at_s"]})
    correction_delays = []
    for c in truth["corrections"]:
        e = c["target_event_id"]
        if e not in card_times:
            continue
        for act in ("retract", "rewrite"):
            if (e, act) in action_times:
                correction_delays.append({"action": act, "after_card_s": action_times[(e, act)] - card_times[e], "after_correction_s": action_times[(e, act)] - c["wall_s"]})
    holds = [{"hold_after_card_s": action_times[(e, "hold")] - card_times[e]} for e in card_times if (e, "hold") in action_times]

    # the Writer through the Verifier: every write in the audit trail, and the texts each card ends with
    teams = {side: t["name"] for side, t in match.info["teams"].items()}
    writes = [a for a in sink.audit if a.get("action") == "written"]
    text_checks = {"cards": 0, "verified": 0, "length_ok": 0, "texts": 0, "unknown_names": Counter(), "extreme_pct": 0}
    for card in sink.cards.values():
        if "texts" not in card:
            continue
        facts, _ = card_facts(card, teams)
        _, rep = verify(json.dumps(card["texts"], ensure_ascii=False), list(LANGUAGES), facts)
        text_checks["cards"] += 1
        text_checks["verified"] += rep["verified"]
        for e in rep["cards"]:
            text_checks["texts"] += 1
            text_checks["length_ok"] += e["length_ok"]
            text_checks["unknown_names"].update(e["unknown_names"])
        text_checks["extreme_pct"] += any(p in t["why"] for t in card["texts"] for p in ("100%", "100 %", " 0%", " 0 %"))

    out = {
        "match_id": truth["match_id"],
        "story": truth["story"],
        "messages": len(stream),
        "elapsed_s": main["elapsed_s"],
        "score_ok": match.score() == truth["final_score"],
        "goals": len(goals), "goals_found": len(goal_found),
        "disallowed_goals": sum(not g["counts"] for g in goals.values()),
        "disallowed_found": sum(not goals[e]["counts"] for e in goal_found),
        "reds": len(reds), "reds_found": len(red_found),
        "planted_saves": len(planted_saves), "saves_found": len(save_found),
        "cards": len(cards),
        "card_kinds": dict(Counter(c["moment"] for c in cards.values())),
        "card_class": dict(Counter(card_class.values())),
        "values": [(c["moment"], c["value_pts"]) for c in cards.values()],
        "confusion": {f"{a}|{b}": n for (a, b), n in sorted(confusion.items())},
        "false_retractions": false_retractions,
        "false_rewrites": false_rewrites,
        "var_checks_on_cards": var_total, "var_checks_handled": var_ok,
        "card_delays": card_delays,
        "correction_delays": correction_delays,
        "holds": holds,
        "writes": len(writes), "writes_verified": sum(bool(a.get("verified")) for a in writes),
        "texts": {**text_checks, "unknown_names": dict(text_checks["unknown_names"])},
        "missed_goals": sorted(set(goals) - goal_found),
        "missed_reds": sorted(set(reds) - red_found),
    }
    if truth["story"] == "demo":
        out["demo"] = demo_windows(truth, card_times, action_times)
    if robustness:
        base = snapshot(run(arrivals(stream), desk=False))
        out["robustness"] = {}
        for name in PERTURBATIONS:
            timed = perturb(stream, name, truth["seed"])
            if timed is not None:
                out["robustness"][name] = diff(base, snapshot(run(timed, desk=False)))
    return out


def demo_windows(truth: dict, card_times: dict, action_times: dict) -> dict:
    """For the demo: how long the 63' card can be on air before VAR takes it back, and the same for every retracted card."""
    out = {}
    for m in truth["moments"]:
        e = m["event_id"]
        if m["expected_card_action"] != "retract" or e not in card_times:
            continue
        v = next((v for v in truth["var_checks"] if v["target_event_id"] == e), None)
        out[m["key"]] = {
            "minute": m["minute_label"],
            "card_ready_s": card_times[e],
            "goal_wall_s": m["wall_s"],
            "hold_after_card_s": action_times.get((e, "hold"), float("nan")) - card_times[e],
            "retract_after_card_s": action_times.get((e, "retract"), float("nan")) - card_times[e],
            "var_started_after_card_s": v["started_wall_s"] - card_times[e] if v else None,
            "var_duration_s": v["completed_wall_s"] - v["started_wall_s"] if v else None,
        }
    return out


# --- aggregating ----------------------------------------------------------------------------------

def stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    s = sorted(xs)
    return {"n": len(s), "mean": statistics.fmean(s), "median": statistics.median(s), "p95": s[min(len(s) - 1, round(0.95 * (len(s) - 1)))], "min": s[0], "max": s[-1]}


def aggregate(matches: list[dict]) -> dict:
    def total(key):
        return sum(m[key] for m in matches)

    confusion = Counter()
    classes = Counter()
    for m in matches:
        confusion.update(m["confusion"])
        classes.update(m["card_class"])
    values = [v for m in matches for v in m["values"]]
    cards = total("cards")
    truth_cards = sum(n for k, n in classes.items() if k.startswith("truth_"))
    grounded = sum(n for k, n in classes.items() if k.startswith(("truth_", "extra_")))
    robust = {}
    for name in PERTURBATIONS:
        runs = [(m["match_id"], m["robustness"][name]) for m in matches if name in m.get("robustness", {})]
        robust[name] = {"matches": len(runs), "identical": sum(not d for _, d in runs), "examples": [f"{mid}: {d[0]}" for mid, d in runs if d][:3]}
    texts = Counter()
    names = Counter()
    for m in matches:
        texts.update({k: v for k, v in m["texts"].items() if k != "unknown_names"})
        names.update(m["texts"]["unknown_names"])
    return {
        "matches": len(matches),
        "messages": total("messages"),
        "score_ok": sum(m["score_ok"] for m in matches),
        "goals": total("goals"), "goals_found": total("goals_found"),
        "disallowed_goals": total("disallowed_goals"), "disallowed_found": total("disallowed_found"),
        "reds": total("reds"), "reds_found": total("reds_found"),
        "planted_saves": total("planted_saves"), "saves_found": total("saves_found"),
        "cards": cards,
        "truth_cards": truth_cards,
        "grounded_cards": grounded,
        "card_class": dict(sorted(classes.items())),
        "cards_per_match": stats([m["cards"] for m in matches]),
        "kinds_per_match": {k: stats([m["card_kinds"].get(k, 0) for m in matches]) for k in KINDS},
        "values": {k: stats([v for kind, v in values if kind == k]) for k in KINDS},
        "value_bins": {k: [sum(1 for kind, v in values if kind == k and lo <= v < hi) for lo, hi in VALUE_BINS] for k in KINDS},
        "confusion": dict(sorted(confusion.items())),
        "false_retractions": [f"{m['match_id']}: {e}" for m in matches for e in m["false_retractions"]],
        "false_rewrites": [f"{m['match_id']}: {e}" for m in matches for e in m["false_rewrites"]],
        "var_checks_on_cards": total("var_checks_on_cards"), "var_checks_handled": total("var_checks_handled"),
        "card_delay": {k: stats([d[k] for m in matches for d in m["card_delays"]]) for k in ("total_s", "latency_s", "buffer_s")},
        "card_delay_by_kind": {k: stats([d["total_s"] for m in matches for d in m["card_delays"] if d["moment"] == k]) for k in KINDS},
        "correction_delay": {a: {k: stats([d[k] for m in matches for d in m["correction_delays"] if d["action"] == a]) for k in ("after_card_s", "after_correction_s")} for a in ("retract", "rewrite")},
        "hold_after_card": stats([h["hold_after_card_s"] for m in matches for h in m["holds"]]),
        "writes": total("writes"), "writes_verified": total("writes_verified"),
        "texts": dict(texts), "unknown_names": dict(names.most_common(10)),
        "missed": [f"{m['match_id']}: {e}" for m in matches for e in m["missed_goals"] + m["missed_reds"]],
        "robustness": robust,
        "elapsed": stats([m["elapsed_s"] for m in matches]),
        "elapsed_per_message_ms": stats([1000 * m["elapsed_s"] / m["messages"] for m in matches]),
    }


# --- writer benchmark (read only) ----------------------------------------------------------------

def bench_summary(bench_dir: Path) -> list[dict]:
    """Summary of earlier benchmark runs of the Writer model (JSON Lines written by spikes/af_trial.py); no model is called."""
    rows: dict[tuple[str, str], list[dict]] = {}
    files = sorted(bench_dir.glob("*.jsonl")) if bench_dir and bench_dir.is_dir() else []
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                rows.setdefault((r["model"], r["effort"]), []).append(r)
    out = []
    for (model, effort), rs in sorted(rows.items()):
        expected = sum(1 if r["case"].startswith("trap") else 2 for r in rs)
        reports = [r.get("report") or {} for r in rs]
        verified = sum(bool(c.get("verified")) for rep in reports if rep.get("schema_ok") for c in rep.get("cards", []))
        traps = [r for r in rs if r["case"].startswith("trap")]
        out.append({
            "model": model, "effort": effort, "runs": len(rs), "errors": sum(bool(r.get("error")) for r in rs),
            "json_valid": sum(bool(rep.get("json_valid")) for rep in reports), "cards_verified": verified, "cards_expected": expected,
            "traps": len(traps), "traps_held": sum(bool((r.get("report") or {}).get("verified")) for r in traps),
            "seconds_per_card_median": statistics.median(r["seconds"] / (1 if r["case"].startswith("trap") else 2) for r in rs),
            "files": sorted({p.name for p in files if p.stem == f"{model}_{effort}"}),
        })
    return out


# --- the report ---------------------------------------------------------------------------------

def pct(a: int, b: int) -> str:
    return f"{a}/{b} ({100 * a / b:.1f} %)" if b else "n/a (0)"


def f1(x) -> str:
    return "n/a" if x is None or x != x else f"{x:.1f}"


def stat_row(label: str, s: dict, digits: int = 1) -> str:
    if not s.get("n"):
        return f"| {label} | 0 | | | | | |"
    fmt = lambda x: f"{x:.{digits}f}"
    return f"| {label} | {s['n']} | {fmt(s['mean'])} | {fmt(s['median'])} | {fmt(s['p95'])} | {fmt(s['min'])} | {fmt(s['max'])} |"


STAT_HEAD = "| | n | mean | median | p95 | min | max |\n|---|---|---|---|---|---|---|"


def render(report: dict, timing: bool = False) -> str:
    u, d = report["unscripted"], report.get("demo")
    seeds = report["seeds"]
    L = [
        "# Evaluation of the desk against the ground truth",
        "",
        f"Generated by `python -m worker.evaluate --seeds {seeds['count']}`{' --start ' + str(seeds['start']) if seeds['start'] != 1 else ''}: unscripted seeds {seeds['start']} to {seeds['start'] + seeds['count'] - 1} of the generator {report['generator_version']}, plus the demo replay (`{DEMO_REPLAY.as_posix()}`). Offline: no Azure, no model call; cards written by the template writer and checked by the Verifier, auto-approved. Every number below is deterministic for a given seed range{', except the processing time' if timing else ''}. Definitions and caveats: `docs/notes/evaluation.md`.",
        "",
        "## Headline",
        "",
        "| Metric | Unscripted | Demo |",
        "|---|---|---|",
    ]

    def identical_within(r):
        runs = [m["robustness"] for m in r["_matches"] if "robustness" in m]
        return pct(sum(all(not rob[n] for n, p in PERTURBATIONS.items() if p["within"]) for rob in runs), len(runs))

    def both(fn):
        return f"{fn(u)} | {fn(d) if d else 'n/a'}"

    L += [
        f"| Matches | {both(lambda r: r['matches'])} |",
        f"| Goal recall (every goal in the truth, disallowed ones included) | {both(lambda r: pct(r['goals_found'], r['goals']))} |",
        f"| Red card recall | {both(lambda r: pct(r['reds_found'], r['reds']))} |",
        f"| Planted save recall | {both(lambda r: pct(r['saves_found'], r['planted_saves']))} |",
        f"| Card precision against truth moments (goals, red cards, planted saves) | {both(lambda r: pct(r['truth_cards'], r['cards']))} |",
        f"| Cards grounded in the truth (truth moment, or a truth shot with the matching outcome) | {both(lambda r: pct(r['grounded_cards'], r['cards']))} |",
        f"| Card actions correct on goals (expected retract / rewrite / none) | {both(lambda r: pct(sum(n for k, n in r['confusion'].items() if k.split('|')[0] == k.split('|')[1]), r['goals']))} |",
        f"| False retractions | {both(lambda r: len(r['false_retractions']))} |",
        f"| False rewrites | {both(lambda r: len(r['false_rewrites']))} |",
        f"| VAR checks on a carded moment held then resolved correctly | {both(lambda r: pct(r['var_checks_handled'], r['var_checks_on_cards']))} |",
        f"| Final score derived by the worker equals the truth | {both(lambda r: pct(r['score_ok'], r['matches']))} |",
        f"| Writes verified by the Verifier (template writer) | {both(lambda r: pct(r['writes_verified'], r['writes']))} |",
        f"| Matches identical under extra network trouble within the buffer (all {sum(p['within'] for p in PERTURBATIONS.values())} perturbations) | {both(identical_within)} |",
        f"| Median delay from the action to its card (broadcast s) | {both(lambda r: f1(r['card_delay']['total_s'].get('median')))} |",
        "",
    ]

    L += ["## 1. Moments and cards", "", "### Recall", "", "| Truth moment | Unscripted | Demo |", "|---|---|---|"]
    L += [
        f"| Goals that stand | {both(lambda r: pct(r['goals_found'] - r['disallowed_found'], r['goals'] - r['disallowed_goals']))} |",
        f"| Goals later disallowed (the card must exist, then be retracted) | {both(lambda r: pct(r['disallowed_found'], r['disallowed_goals']))} |",
        f"| Red cards (straight or second yellow) | {both(lambda r: pct(r['reds_found'], r['reds']))} |",
        f"| Planted saves (demo only) | {both(lambda r: pct(r['saves_found'], r['planted_saves']))} |",
        "",
    ]
    missed = u["missed"] + (d["missed"] if d else [])
    L += [f"Missed: {', '.join(missed)}." if missed else "Missed: none.", ""]

    L += ["### Cards by class", "", "The unscripted truth plants no moment: it lists goals, red cards and shots. A card on a goal or a red card matches a truth moment; a chance or save card is an extra card by design (a shot whose xG times its potential swing reaches 8 points); it is grounded (`extra_*`) when its shot is in the truth with the outcome the card claims and, for a save, the keeper's action is the message right after the shot. `mispaired_save`: a save card whose keeper action is not the one right after its shot (see the notes); `ungrounded_*`: no matching truth shot.", "", "| Class | Unscripted | Demo |", "|---|---|---|"]
    classes = sorted(set(u["card_class"]) | set(d["card_class"] if d else {}))
    for k in classes:
        L.append(f"| {k} | {u['card_class'].get(k, 0)} | {d['card_class'].get(k, 0) if d else 'n/a'} |")
    L += [f"| total | {u['cards']} | {d['cards'] if d else 'n/a'} |", ""]

    L += ["### Cards per match (unscripted)", "", STAT_HEAD, stat_row("all cards", u["cards_per_match"])]
    L += [stat_row(k, u["kinds_per_match"][k]) for k in KINDS]
    L += ["", "### Card value (`value_pts`, unscripted)", "", STAT_HEAD]
    L += [stat_row(k, u["values"][k]) for k in KINDS]
    bins = [f"{lo}-{hi - 1}" if hi <= 100 else f"{lo}+" for lo, hi in VALUE_BINS]
    L += ["", "| Kind | " + " | ".join(bins) + " |", "|---|" + "---|" * len(bins)]
    L += [f"| {k} | " + " | ".join(str(n) for n in u["value_bins"][k]) + " |" for k in KINDS]
    L.append("")

    L += ["## 2. Card actions", "", "Rows: action the truth expects on the goal's card; columns: what the desk did (from the corrections: retract, rewrite, both, none).", ""]
    done_cols = ["none", "retract", "rewrite", "rewrite+retract", "retract+rewrite", "no card"]
    for label, r in (("Unscripted", u), ("Demo", d)):
        if not r:
            continue
        cols = [c for c in done_cols if any(k.split("|")[1] == c for k in r["confusion"])] or ["none"]
        L += [f"{label}:", "", "| Expected \\ done | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
        for a in ACTIONS:
            L.append(f"| {a} | " + " | ".join(str(r["confusion"].get(f"{a}|{c}", 0)) for c in cols) + " |")
        L.append("")
    fr = u["false_retractions"] + u["false_rewrites"] + ((d["false_retractions"] + d["false_rewrites"]) if d else [])
    L += [f"False retractions or rewrites: {', '.join(fr)}." if fr else "False retractions or rewrites: none.", ""]

    if not u["robustness"]["jitter_2s"]["matches"]:
        L += ["## 3. Robustness to extra network trouble", "", "Skipped (`--no-robustness`).", ""]
    else:
        L += ["## 3. Robustness to extra network trouble", "", "Each stream is replayed with extra perturbations on top of the feed's own late and duplicated messages (arrival times change, messages do not; the pipeline's clock is the arrival time). A match is identical when every card (kind, status, version, player, value, facts, score before), every card action and the final score are the same as on the unperturbed feed.", "", "| Perturbation | Within the 3 s buffer | Unscripted identical | Demo identical | First differences |", "|---|---|---|---|---|"]
        for name, p in PERTURBATIONS.items():
            ru, rd = u["robustness"][name], (d["robustness"][name] if d else None)
            ex = "; ".join((ru["examples"] + (rd["examples"] if rd else []))[:2]) or ""
            L.append(f"| {p['label']} | {'yes' if p['within'] else 'no'} | {pct(ru['identical'], ru['matches'])} | {pct(rd['identical'], rd['matches']) if rd else 'n/a'} | {ex} |")
        L.append("")

    L += ["## 4. Timing in feed time", "", "Broadcast seconds. A mispaired save card (section 1) shows up as an outlier in the save row. Card delay: from the action's `wall_s` (for a save, the shot's) to the arrival time of the message that let the card out of the reorder buffer; it splits into the provider latency (`emitted_at_s - wall_s` of the action) and the worker's wait (buffer, and for a save the keeper's action). Unscripted matches.", "", STAT_HEAD]
    L += [stat_row("action to card, all cards", u["card_delay"]["total_s"]), stat_row("provider latency", u["card_delay"]["latency_s"]), stat_row("worker wait (buffer)", u["card_delay"]["buffer_s"])]
    L += [stat_row(f"action to card, {k}", u["card_delay_by_kind"][k]) for k in KINDS]
    L += [stat_row("card to VAR hold", u["hold_after_card"])]
    for a in ("retract", "rewrite"):
        for r, label in ((u, "unscripted"), (d, "demo")):
            if r:
                L += [stat_row(f"card to {a} ({label})", r["correction_delay"][a]["after_card_s"]), stat_row(f"correction message to {a} ({label})", r["correction_delay"][a]["after_correction_s"])]
    L.append("")
    if d and d.get("demo_windows"):
        L += ["### The demo's retracted cards", "", "How long a producer can air the card before VAR takes it back: the card is ready at the given broadcast time, the hold comes with the start of the VAR check, the retraction with its decision.", "", "| Moment | Card ready (broadcast s) | Card to VAR hold (s) | Card to retraction (s) | VAR check length (s) |", "|---|---|---|---|---|"]
        for key, w in d["demo_windows"].items():
            L.append(f"| {key} ({w['minute']}) | {f1(w['card_ready_s'])} | {f1(w['hold_after_card_s'])} | {f1(w['retract_after_card_s'])} | {f1(w['var_duration_s'])} |")
        L.append("")

    L += ["## 5. Writer quality without a model", "", "| | Unscripted | Demo |", "|---|---|---|"]
    L += [
        f"| Writes (first versions and rewrites) verified at write time | {both(lambda r: pct(r['writes_verified'], r['writes']))} |",
        f"| Final cards whose three texts pass the Verifier again | {both(lambda r: pct(r['texts'].get('verified', 0), r['texts'].get('cards', 0)))} |",
        f"| Texts within the length limits (8 / 25 words) | {both(lambda r: pct(r['texts'].get('length_ok', 0), r['texts'].get('texts', 0)))} |",
        f"| Cards showing 0 % or 100 % | {both(lambda r: r['texts'].get('extreme_pct', 0))} |",
        "",
    ]
    names = u["unknown_names"]
    if names:
        L += ["Capitalised words the name check flags (informative, not part of `verified`): " + ", ".join(f"{w} ({n})" for w, n in names.items()) + ".", ""]
    bench = report.get("bench") or []
    if bench:
        L += ["### Earlier model benchmark (read from `data/bench/`, no model called)", "", "| Model | Effort | Runs | Errors | JSON valid | Cards verified | Traps held | s per card (median) |", "|---|---|---|---|---|---|---|---|"]
        for b in bench:
            L.append(f"| {b['model']} | {b['effort']} | {b['runs']} | {b['errors']} | {b['json_valid']}/{b['runs']} | {pct(b['cards_verified'], b['cards_expected'])} | {b['traps_held']}/{b['traps']} | {b['seconds_per_card_median']:.1f} |")
        L.append("")
    else:
        L += ["No benchmark file in `data/bench/`.", ""]

    if timing:
        L += ["## 6. Processing time (offline, this machine; not deterministic)", "", STAT_HEAD]
        L += [stat_row("seconds per match (pipeline, desk, template writer)", u["elapsed"], 3), stat_row("ms per message", u["elapsed_per_message_ms"], 3)]
        if d:
            L += [stat_row("demo, seconds", d["elapsed"], 3)]
        L.append("")
    return "\n".join(L)


# --- entry point --------------------------------------------------------------------------------

def evaluate(seeds: range, demo: bool = True, robustness: bool = True, bench_dir: Path | None = None) -> dict:
    unscripted = []
    for seed in seeds:
        stream, truth = generate_match(seed, frames=False)
        unscripted.append(evaluate_match(stream, truth, robustness))
    report = {"seeds": {"start": seeds.start, "count": len(seeds)}, "unscripted": {**aggregate(unscripted), "_matches": unscripted}}
    from generator import GENERATOR_VERSION

    report["generator_version"] = GENERATOR_VERSION
    if demo:
        truth = json.loads(DEMO_TRUTH.read_text(encoding="utf-8"))
        dm = evaluate_match(load(DEMO_REPLAY), truth, robustness)
        report["demo"] = {**aggregate([dm]), "_matches": [dm], "demo_windows": dm.get("demo", {})}
    report["bench"] = bench_summary(bench_dir) if bench_dir else []
    return report


def to_json(report: dict, timing: bool = False) -> str:
    """The report as JSON, without the per-match lists (and without times unless asked)."""
    def strip(r):
        r = {k: v for k, v in r.items() if k != "_matches"}
        if not timing:
            r.pop("elapsed", None)
            r.pop("elapsed_per_message_ms", None)
        return r

    out = {**report, "unscripted": strip(report["unscripted"])}
    if report.get("demo"):
        out["demo"] = strip(report["demo"])
    return json.dumps(out, indent=2, sort_keys=True, default=str)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seeds", type=int, default=100, help="number of unscripted seeds")
    parser.add_argument("--start", type=int, default=1, help="first seed")
    parser.add_argument("--out", type=Path, help="markdown report (default: print it)")
    parser.add_argument("--json", type=Path, help="JSON report")
    parser.add_argument("--bench", type=Path, default=Path("data/bench"), help="directory of earlier Writer benchmark runs (*.jsonl), summarised if present")
    parser.add_argument("--no-demo", action="store_true")
    parser.add_argument("--no-robustness", action="store_true", help="skip the perturbed replays (about 6 times faster)")
    parser.add_argument("--timing", action="store_true", help="add the processing time per match (not deterministic)")
    args = parser.parse_args()
    report = evaluate(range(args.start, args.start + args.seeds), demo=not args.no_demo, robustness=not args.no_robustness, bench_dir=args.bench)
    md = render(report, timing=args.timing) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(md, encoding="utf-8", newline="\n")
    else:
        print(md)
    if args.json:
        args.json.write_text(to_json(report, args.timing) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
