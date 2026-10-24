"""Generate a synthetic match as JSON Lines plus its hidden ground truth, or check the calibration.

Usage:
    python -m generator --story demo --out data/replay    # the demo match, seed 7: demo-0007.jsonl
    python -m generator --seed 12 --out data/replay       # an unscripted match: synthetic-0012.jsonl
    python -m generator --calibrate 200                   # means over 200 unscripted matches, against real orders of magnitude
"""

import argparse
import json
from pathlib import Path

from generator.calibration import TARGETS, summarise
from generator.engine import STORIES, generate_match


def calibrate(n: int) -> None:
    summary = summarise([generate_match(seed=seed, frames=False)[1]["counts"] for seed in range(1, n + 1)])
    print(f"{n} unscripted matches, mean (min-max) per match against the hand-tuned band")
    for name, (low, high, real) in TARGETS.items():
        st = summary[name]
        flag = "ok" if low <= st["mean"] <= high else "OFF"
        print(f"  {name:<20}{st['mean']:>9.2f}  ({st['min']:g}-{st['max']:g})  band {low:g}-{high:g} {flag:<3}  real: {real}")
    others = [name for name in summary if name not in TARGETS]
    print("  other: " + ", ".join(f"{name} {summary[name]['mean']:.1f}" for name in others))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--story", choices=STORIES, default="random", help="demo: the scripted video story; random: no script")
    parser.add_argument("--match-id")
    parser.add_argument("--out", default="data/replay", help="output directory")
    parser.add_argument("--no-frames", action="store_true", help="leave out the 360-style freeze frames")
    parser.add_argument("--calibrate", type=int, metavar="N", help="print the calibration over N unscripted matches and exit")
    args = parser.parse_args()
    if args.calibrate:
        calibrate(args.calibrate)
        return

    stream, truth = generate_match(seed=args.seed, story=args.story, match_id=args.match_id, frames=not args.no_frames)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    feed = out / f"{truth['match_id']}.jsonl"
    with feed.open("w", encoding="utf-8", newline="\n") as f:
        for message in stream:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")
    (out / f"{truth['match_id']}.truth.json").write_text(json.dumps(truth, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")

    c = truth["counts"]
    score = truth["final_score"]
    print(f"{feed}: {len(stream)} messages, {c['actions']} actions, final score {score['home']}-{score['away']}")
    print("  " + ", ".join(f"{name} {c[name]:g}" for name in TARGETS))
    for m in truth["moments"]:
        print(f"  {m['minute_label']:>6} {m['key']:<8} {m['player']['name']:<24} card: {m['expected_card_action']:<7} score after: {m['score_after']}")
    inc = truth["incidents"]
    print(f"  incidents: {len(inc['late'])} late, {len(inc['duplicates'])} duplicates, {c['corrections']} corrections, {c['var_checks']} VAR checks")


if __name__ == "__main__":
    main()
