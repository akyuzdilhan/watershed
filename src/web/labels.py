"""Readable labels shared by the console's desk (`web.console`) and its panels (`web.panels`): fact types, fact fields and audit actions as words, not keys."""

import re

ACTIONS = {"written": "Written", "approved": "Approved", "rejected": "Rejected", "edited": "Edited", "retracted": "Retracted", "rewrite_requested": "Rewrite requested",
           "hold": "Held for a VAR check", "release": "Released after the check", "feedback": "Feedback", "superseded": "Superseded"}
FACT_TYPES = {  # the prefix of an insight key (worker.insights, worker.tactics), longest first when matched
    "hat-trick-speed": "Fastest hat-trick", "ht-lead-lost": "Half-time lead lost", "stat-of-the-match": "Stat of the match", "possession-swing": "Possession swing",
    "goals-total": "Goals in the match", "club-goals": "Club goals record", "h2h-drought": "Head-to-head drought", "h2h-margin": "Head-to-head margin",
    "run-unbeaten": "Unbeaten run", "run-winless": "Winless run", "line-break": "Line-breaking pass", "line-height": "Defensive line height",
    "xg-ft": "Full-time xG", "xg-p1": "First-half xG", "shots-ft": "Full-time shots", "shots-p1": "First-half shots", "ten-men": "Ten men",
    "comeback": "Comeback", "scorer": "Scorer milestone", "late": "Late goal", "run": "Run behind the line", "space": "Space before the goal", "pressing": "Pressing",
    "xg": "Expected goals (xG)", "shots": "Shots"}
FACT_KINDS = {"milestone": "Milestone", "rarity": "Rarity", "streak": "Streak", "record": "Record", "tactical": "Tactical detail"}
WORDS = {"wp": "win probability", "pct": "(%)", "pts": "(points)", "xg": "xG", "ht": "half-time", "ft": "full-time", "h2h": "head-to-head", "var": "VAR",
         "m": "(m)", "cm": "(cm)", "s": "(s)", "p1": "first half", "p2": "second half"}


def readable(key: str | None) -> str:
    """A key as words: "rovers_wp_before_pct" reads "Rovers win probability before (%)", "rewrite_requested" "Rewrite requested"."""
    words = [WORDS.get(w.lower(), w) for w in re.split(r"[-_\s]+", str(key or "").strip()) if w]
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def fact_label(key: str | None) -> str:
    """What kind of fact an insight is, from its key or group ("ht-lead-lost-away-2" reads "Half-time lead lost") or its kind ("streak" reads "Streak")."""
    k = str(key or "").lower()
    for prefix in sorted(FACT_TYPES, key=len, reverse=True):
        if k == prefix or k.startswith(prefix + "-"):
            return FACT_TYPES[prefix]
    if k in FACT_KINDS:
        return FACT_KINDS[k]
    return readable(re.sub(r"(-(home|away|\d+))+$", "", k))


def field_label(field: str | None) -> str:
    """A fact field as words, for the claims and facts tables."""
    return readable(field)


def action_label(action: str | None) -> str:
    return ACTIONS.get(action or "", readable(action))
