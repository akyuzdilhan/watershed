"""The producers' feedback on every sentence the desk writes, and what the system does with it now (in context, no retraining) and later (an export for a preference set).

A feedback is a thumbs up or down on one sentence (a headline, a why, a duo line, in one language), with reasons and an optional note. `Desk.feedback` stores it (`type: "feedback"`, contract) and audits it. This module turns the stored feedback of every match into a producer profile:

- house rules (from the reasons and the notes, the most frequent and the most recent first) and examples of rejected and approved sentences, which the Writer prompt receives as the control room's preferences (`prompt_block`);
- an approval rate per insight family and per card moment, smoothed towards a prior, which weighs the insight ranking after enough votes, never below a floor, and never pushes a goal or red card fact out (`adjust_value`);
- the template variants with repeated thumbs down, which the template writer picks last (`demoted`, matched by `skeleton`).

Nothing here trains a model. `python -m worker.feedback export` writes a dataset (judgements, and approved against rejected pairs for the same sentence) in a shape a later preference fine-tuning or an evaluation set can read; `python -m worker.feedback build` writes the profile. See docs/notes/feedback.md.
"""

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path

PARTS = ("headline", "why", "duo")
VERDICTS = ("up", "down")
REASONS = {
    "wrong_fact": "Wrong fact",
    "not_relevant": "Not relevant",
    "repetitive": "Repetitive",
    "wrong_tone": "Wrong tone",
    "too_long": "Too long",
    "wrong_moment": "Wrong moment",
}
RULES = {  # what a reason asks of the next sentences
    "wrong_fact": "State only what the facts say; when a fact is uncertain, leave it out.",
    "not_relevant": "Say why the moment matters for this match (the result, the stakes); skip trivia.",
    "repetitive": "Never reuse an opening, a verb or a turn of phrase already said in the match.",
    "wrong_tone": "Keep a measured broadcast tone: no hype words, no exclamation pile-ups.",
    "too_long": "Keep it short: a headline under eight words, a why of one short sentence, duo lines of one breath.",
    "wrong_moment": "Speak to the moment as it stands now; do not anticipate or look back too far.",
}
NOTE_MAX = 200
TEXT_MAX = 240

PRIOR_RATE = 0.75  # what the desk expects before any vote
PRIOR_N = 4  # weight of the prior, in votes
MIN_VOTES = 5  # below this, a family or a moment keeps its ranking
FLOOR = 0.6  # the lowest weight feedback can give
CEILING = 1.1
PROTECTED_MOMENTS = {"goal", "red_card"}
PROTECTED_FAMILIES = {"scorer", "ten-men"}  # facts about a goal or a red card

MAX_RULES = 6
MAX_EXAMPLES = 4
MAX_DEMOTED = 60
PROMPT_MAX_CHARS = 1600
DEMOTE_AFTER = 2  # thumbs down on the same template variant

PROFILE_PATH = "data/feedback/profile.json"


# --- identity of a sentence ---------------------------------------------------------------------

def family_of(key: str | None) -> str | None:
    """The family of an insight from its key or group: the words before the first id, side or number (`scorer-P000123-3` -> `scorer`, `comeback-home-2-3` -> `comeback`)."""
    if not key:
        return None
    words = []
    for w in str(key).split("-"):
        if w in ("home", "away") or any(ch.isdigit() for ch in w):
            break
        words.append(w)
    return "-".join(words) or str(key)


def subject(doc: dict) -> tuple[str, str | None]:
    """(`moment` or `family`, its value) of a card, insight or storyline."""
    kind = doc.get("type", "card")
    if kind == "card":
        return "moment", doc.get("moment")
    if kind == "storyline":
        return "family", "storyline"
    return "family", family_of(doc.get("key") or doc.get("group"))


def skeleton(text: str, facts: dict) -> str:
    """The shape of a sentence: lower case, every name or label of the facts as {}, every number as #. Two fillings of one template variant share it."""
    s = str(text).lower()
    values = sorted({str(v).lower() for v in (facts or {}).values() if isinstance(v, str) and len(str(v)) >= 2}, key=len, reverse=True)
    for v in values:
        s = s.replace(v, "{}")
    s = re.sub(r"\d+(?:[.,]\d+)?", "#", s)
    return re.sub(r"\s+", " ", s).strip()


def sentence(doc: dict, target: dict) -> dict:
    """The sentence a target points at: `{text, writer, speaker}`. Raises ValueError when the target is not on the document."""
    part, lang, index = target.get("part"), target.get("lang"), target.get("index", 0)
    if part not in PARTS:
        raise ValueError(f"unknown part {part}")
    if not isinstance(index, int) or index < 0:
        raise ValueError("index must be a non-negative integer")
    if part == "duo":
        lines = [d for d in doc.get("duo") or [] if isinstance(d, dict) and d.get("lang") == lang]
        if index >= len(lines):
            raise ValueError(f"no duo line {index} in {lang}")
        return {"text": lines[index].get("line", ""), "writer": doc.get("duo_writer") or doc.get("writer") or "template", "speaker": lines[index].get("speaker")}
    texts = [t for t in doc.get("texts") or [] if isinstance(t, dict) and t.get("lang") == lang]
    if index >= len(texts) or not texts[index].get(part):
        raise ValueError(f"no {part} in {lang}")
    return {"text": texts[index][part], "writer": doc.get("writer") or "template", "speaker": None}


def target_key(doc_id: str, part: str, lang: str, index: int = 0) -> str:
    return f"{doc_id}|{part}|{lang}|{index}"


def feedback_id(doc: dict, target: dict, by: str) -> str:
    """One id per producer, sentence and version: a second click on the same sentence replaces the first (upsert in Cosmos, the last line wins locally)."""
    who = hashlib.sha1(str(by).encode()).hexdigest()[:8]
    return f"feedback-{doc['id']}-v{doc.get('version', 1)}-{target['part']}-{target['lang']}-{target.get('index', 0)}-{who}"


def make_doc(doc: dict, target: dict, verdict: str, reasons: list[str] | None, note: str | None, by: str, facts: dict, meanings: dict, now: float | None = None) -> dict:
    """The feedback document (contract, "Producer feedback"). Raises ValueError on an unknown verdict, reason or target."""
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict}")
    reasons = list(dict.fromkeys(reasons or []))
    unknown = [r for r in reasons if r not in REASONS]
    if unknown:
        raise ValueError(f"unknown reason {', '.join(unknown)}")
    if verdict == "up":
        reasons = []
    target = {"part": target.get("part"), "lang": target.get("lang"), "index": target.get("index", 0)}
    s = sentence(doc, target)
    field, value = subject(doc)
    return {"id": feedback_id(doc, target, by), "type": "feedback", "match_id": doc["match_id"], "doc_id": doc["id"], "doc_type": doc.get("type", "card"),
            "doc_version": doc.get("version", 1), "moment": value if field == "moment" else None, "family": value if field == "family" else None,
            "target": target, "text": s["text"], "speaker": s["speaker"], "writer": s["writer"], "verdict": verdict, "reasons": reasons,
            "note": (note or "").strip()[:NOTE_MAX] or None, "by": by, "facts": facts, "field_meanings": meanings, "pattern": skeleton(s["text"], facts),
            "at": time.time() if now is None else now}


def latest(docs) -> list[dict]:
    """One feedback per id, the last written; oldest first."""
    out: dict[str, dict] = {}
    for d in docs:
        if isinstance(d, dict) and d.get("type", "feedback") == "feedback" and d.get("verdict") in VERDICTS:
            if d["id"] not in out or (d.get("at") or 0) >= (out[d["id"]].get("at") or 0):
                out[d["id"]] = d
    return sorted(out.values(), key=lambda d: d.get("at") or 0)


def by_target(docs) -> dict[str, dict]:
    """The latest feedback on each sentence (any producer), by `target_key`: what the console shows next to the sentence."""
    out = {}
    for d in latest(docs):
        t = d.get("target") or {}
        out[target_key(d["doc_id"], t.get("part"), t.get("lang"), t.get("index", 0))] = d
    return out


# --- profile ------------------------------------------------------------------------------------

def rate(up: int, n: int) -> float:
    """Approval rate smoothed towards the prior (a beta prior of PRIOR_N votes at PRIOR_RATE)."""
    return (up + PRIOR_RATE * PRIOR_N) / (n + PRIOR_N)


def weight(up: int, n: int) -> float:
    """How much feedback scales a family's or a moment's value: 1 below MIN_VOTES, else the smoothed rate against the prior, between FLOOR and CEILING."""
    if n < MIN_VOTES:
        return 1.0
    return round(min(CEILING, max(FLOOR, rate(up, n) / PRIOR_RATE)), 3)


def _clip(text: str | None, n: int = TEXT_MAX) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def _rates(votes: list[dict], field: str) -> dict[str, dict]:
    counts: dict[str, list[int]] = {}
    for v in votes:
        key = v.get(field)
        if key:
            c = counts.setdefault(key, [0, 0])
            c[0] += v["verdict"] == "up"
            c[1] += 1
    return {k: {"up": up, "votes": n, "rate": round(rate(up, n), 3), "weight": weight(up, n)} for k, (up, n) in sorted(counts.items())}


def _examples(votes: list[dict], verdict: str) -> list[dict]:
    out, seen = [], set()
    for v in sorted(votes, key=lambda v: -(v.get("at") or 0)):
        text = _clip(v.get("text"))
        if v["verdict"] != verdict or not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append({"text": text, "part": v["target"]["part"], "lang": v["target"]["lang"], "moment": v.get("moment"), "family": v.get("family"),
                    "reasons": v.get("reasons") or [], "note": v.get("note"), "at": v.get("at")})
        if len(out) >= 2 * MAX_EXAMPLES:
            break
    return out


def build_profile(docs, now: float | None = None) -> dict:
    """The producer profile from every stored feedback (bounded: MAX_RULES rules, 2 x MAX_EXAMPLES examples of each verdict, MAX_DEMOTED variants)."""
    votes = latest(docs)
    downs = [v for v in votes if v["verdict"] == "down"]
    rules: dict[str, dict] = {}
    for v in downs:
        for r in v.get("reasons") or []:
            e = rules.setdefault(r, {"rule": RULES[r], "source": r, "count": 0, "last_at": 0})
            e["count"] += 1
            e["last_at"] = max(e["last_at"], v.get("at") or 0)
    notes: dict[str, dict] = {}
    for v in votes:
        if v.get("note"):
            key = v["note"].strip().lower()
            e = notes.setdefault(key, {"rule": _clip(v["note"], 160), "source": "note", "count": 0, "last_at": 0})
            e["count"] += 1
            e["last_at"] = max(e["last_at"], v.get("at") or 0)
    ranked = sorted(list(rules.values()) + list(notes.values()), key=lambda e: (-e["count"], -e["last_at"], e["rule"]))[:MAX_RULES]
    patterns: dict[str, list[int]] = {}
    for v in votes:
        if v.get("writer") == "template" and v.get("pattern"):
            c = patterns.setdefault(v["pattern"], [0, 0])
            c[0 if v["verdict"] == "down" else 1] += 1
    demoted = sorted((p for p, (d, u) in patterns.items() if d >= DEMOTE_AFTER and d > u), key=lambda p: (-patterns[p][0], p))[:MAX_DEMOTED]
    up = sum(v["verdict"] == "up" for v in votes)
    return {"version": 1, "built_at": time.time() if now is None else now, "votes": len(votes), "approval_rate": round(up / len(votes), 3) if votes else None,
            "rules": ranked, "examples": {"rejected": _examples(votes, "down"), "approved": _examples(votes, "up")},
            "families": _rates(votes, "family"), "moments": _rates(votes, "moment"), "demoted": demoted}


def empty_profile() -> dict:
    return build_profile([], now=0)


def save_profile(profile: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(profile, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_profile(path: str | Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, PermissionError):
        return empty_profile()
    return value if isinstance(value, dict) else empty_profile()


class ProfileSource:
    """The profile file, read again when it changes (one stat per call): the worker sees the producers' feedback without a restart."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path if path is not None else os.environ.get("FEEDBACK_PROFILE", PROFILE_PATH))
        self._mtime: float | None = None
        self._profile = empty_profile()

    def get(self) -> dict:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return self._profile
        if mtime != self._mtime:
            self._mtime, self._profile = mtime, load_profile(self.path)
        return self._profile


class Fixed:
    """A profile given as a dict (tests, a run with a frozen profile)."""

    def __init__(self, profile: dict | None) -> None:
        self.profile = profile or empty_profile()

    def get(self) -> dict:
        return self.profile


def source(profile=None):
    """A profile source from a dict, a source, a path, or None: the file named by FEEDBACK_PROFILE when it is set, else no profile (opt-in, so that a local profile never changes a run or a test that did not ask for it)."""
    if profile is None:
        path = os.environ.get("FEEDBACK_PROFILE", "").strip()
        return ProfileSource(path) if path else Fixed(None)
    if isinstance(profile, (str, Path)):
        return ProfileSource(profile)
    if isinstance(profile, dict):
        return Fixed(profile)
    return profile


# --- using the profile now ----------------------------------------------------------------------

def prompt_block(profile: dict | None, doc: dict | None = None, languages: list[str] | None = None, max_chars: int = PROMPT_MAX_CHARS) -> str | None:
    """What the Writer prompt receives: the house rules, then rejected and approved examples (the same moment or family and the requested languages first), cut at `max_chars`. None without feedback."""
    if not profile or not profile.get("votes"):
        return None
    field, value = subject(doc) if doc else (None, None)
    langs = set(languages or [])

    def first(e: dict) -> tuple:
        return (0 if value and e.get(field) == value else 1, 0 if not langs or e.get("lang") in langs else 1, -(e.get("at") or 0))

    lines = ["Control room preferences, from the producers' thumbs up and down on earlier sentences. Follow them; the facts and the rules above still win."]
    rules = profile.get("rules") or []
    if rules:
        lines.append("House rules:")
        lines += [f"- {r['rule']}" for r in rules[:MAX_RULES]]
    ex = profile.get("examples") or {}
    for verdict, title in (("rejected", "Sentences the control room rejected (do not write like this):"), ("approved", "Sentences the control room approved:")):
        items = sorted(ex.get(verdict) or [], key=first)[:MAX_EXAMPLES]
        if items:
            lines.append(title)
            for e in items:
                why = ", ".join(REASONS.get(r, r).lower() for r in e.get("reasons") or [])
                lines.append(f"- [{e['lang']} {e['part']}] \"{e['text']}\"" + (f" ({why})" if why else "") + (f" note: {_clip(e['note'], 120)}" if e.get("note") else ""))
    out = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > max_chars:
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out) if len(out) > 1 else None


def family_weight(profile: dict | None, family: str | None) -> float:
    return ((profile or {}).get("families") or {}).get(family or "", {}).get("weight", 1.0)


def moment_weight(profile: dict | None, moment: str | None) -> float:
    """The weight of a card moment; a goal or a red card never goes below 1 (cards are ranked by the Spotter, the weight is shown, not applied)."""
    w = ((profile or {}).get("moments") or {}).get(moment or "", {}).get("weight", 1.0)
    return max(w, 1.0) if moment in PROTECTED_MOMENTS else w


def adjust_value(val: int, group: str, profile: dict | None, min_value: int) -> tuple[int, float]:
    """(value after feedback, weight) of an insight fact. A goal or red card fact that reached `min_value` stays at or above it."""
    family = family_of(group)
    w = family_weight(profile, family)
    if w == 1.0:
        return val, 1.0
    out = round(val * w)
    if family in PROTECTED_FAMILIES and val >= min_value:
        out = max(out, min_value)
    return min(out, 100), w


def demoted(profile: dict | None) -> set[str]:
    return set((profile or {}).get("demoted") or [])


# --- measure ------------------------------------------------------------------------------------

def stats(docs) -> dict:
    """Approval rate of a set of feedback (a match so far): overall, and by writer and language."""
    votes = latest(docs)
    groups: dict[tuple[str, str], list[int]] = {}
    for v in votes:
        g = groups.setdefault((v.get("writer") or "?", (v.get("target") or {}).get("lang") or "?"), [0, 0])
        g[0] += v["verdict"] == "up"
        g[1] += 1
    up = sum(v["verdict"] == "up" for v in votes)
    return {"votes": len(votes), "up": up, "rate": round(up / len(votes), 3) if votes else None,
            "by": [{"writer": w, "lang": lang, "votes": n, "up": u, "rate": round(u / n, 3)} for (w, lang), (u, n) in sorted(groups.items())]}


# --- export -------------------------------------------------------------------------------------

def record(v: dict) -> dict:
    """One judgement, without the producer's identity."""
    t = v.get("target") or {}
    return {"record": "judgement", "id": v["id"], "match_id": v.get("match_id"), "doc_id": v.get("doc_id"), "doc_type": v.get("doc_type"), "doc_version": v.get("doc_version"),
            "moment": v.get("moment"), "family": v.get("family"), "part": t.get("part"), "lang": t.get("lang"), "index": t.get("index", 0), "speaker": v.get("speaker"),
            "facts": v.get("facts") or {}, "field_meanings": v.get("field_meanings") or {}, "text": v.get("text"), "verdict": v["verdict"], "reasons": v.get("reasons") or [],
            "note": v.get("note"), "writer": v.get("writer"), "at": v.get("at")}


def pairs(votes: list[dict]) -> list[dict]:
    """Approved against rejected sentences for the same moment: the same document, part and language (another version, another writer, another duo line)."""
    groups: dict[tuple, dict[str, list[dict]]] = {}
    for v in votes:
        t = v.get("target") or {}
        groups.setdefault((v.get("doc_id"), t.get("part"), t.get("lang")), {"up": [], "down": []})[v["verdict"]].append(v)
    out = []
    for (doc_id, part, lang), g in sorted(groups.items(), key=lambda x: tuple(str(k) for k in x[0])):
        for good in g["up"]:
            for bad in g["down"]:
                if good.get("text") == bad.get("text"):
                    continue
                out.append({"record": "pair", "doc_id": doc_id, "match_id": good.get("match_id"), "part": part, "lang": lang, "moment": good.get("moment"), "family": good.get("family"),
                            "facts": good.get("facts") or {}, "field_meanings": good.get("field_meanings") or {},
                            "chosen": good.get("text"), "chosen_writer": good.get("writer"), "rejected": bad.get("text"), "rejected_writer": bad.get("writer"),
                            "rejected_reasons": bad.get("reasons") or [], "rejected_note": bad.get("note")})
    return out


def export(docs, out: str | Path) -> dict:
    """Writes the dataset (judgements, then pairs) as JSON lines; returns the counts."""
    votes = latest(docs)
    rows = [record(v) for v in votes] + pairs(votes)
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return {"judgements": len(votes), "pairs": len(rows) - len(votes), "out": str(path)}


# --- reading stored feedback ----------------------------------------------------------------------

def read_jsonl(path: Path) -> list[dict]:
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


def load_local(desk_dir: str | Path) -> list[dict]:
    """Every `<desk_dir>/<match_id>/feedback.jsonl`."""
    root = Path(desk_dir)
    if not root.is_dir():
        return []
    return [d for p in sorted(root.glob("*/feedback.jsonl")) for d in read_jsonl(p)]


def load_cosmos(endpoint: str, database: str = "watershed") -> list[dict]:
    """Every feedback document of the `cards` container (cross-partition query; Entra ID)."""
    from worker.azure_io import CosmosSink

    cards = CosmosSink(endpoint, database).cards
    return [{k: v for k, v in d.items() if not k.startswith("_")} for d in cards.query_items("SELECT * FROM c WHERE c.type = 'feedback'", enable_cross_partition_query=True)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m worker.feedback", description="Producer feedback: build the profile, export the dataset.")
    parser.add_argument("command", choices=("build", "export", "stats"))
    parser.add_argument("--desk-dir", default=os.environ.get("DESK_DIR", "data/desk"))
    parser.add_argument("--cosmos", action="store_true", help="read the feedback from Cosmos DB (COSMOS_ENDPOINT, COSMOS_DATABASE)")
    parser.add_argument("--out", default=None, help="profile (build, default FEEDBACK_PROFILE or data/feedback/profile.json) or dataset (export, default data/feedback/dataset.jsonl)")
    args = parser.parse_args(argv)
    docs = load_cosmos(os.environ["COSMOS_ENDPOINT"], os.environ.get("COSMOS_DATABASE", "watershed")) if args.cosmos else load_local(args.desk_dir)
    if args.command == "build":
        path = save_profile(build_profile(docs), args.out or os.environ.get("FEEDBACK_PROFILE", PROFILE_PATH))
        print(json.dumps({"votes": len(latest(docs)), "out": str(path)}))
    elif args.command == "export":
        print(json.dumps(export(docs, args.out or "data/feedback/dataset.jsonl")))
    else:
        print(json.dumps(stats(docs), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
