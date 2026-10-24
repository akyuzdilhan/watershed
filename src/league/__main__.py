"""Check the league calibration, print a table, or show what the league gives the demo fixture.

Usage:
    python -m league --seed 1 --calibrate 1000           # means over 1,000 independent seasons, against real orders of magnitude
    python -m league table --seed 1 --matchday 30        # the table after 30 matchdays, with the observer's chances
    python -m league demo --seed 1 --matchday 30         # expected goals and card context for Rovers v United on matchday 30
"""

import argparse
import json
import time

from league.calibration import TARGETS, calibrate
from league.coupling import demo_fixture
from league.model import Params
from league.season import DEMO_MATCHDAY, N_SIMS, League, season_state


def print_calibration(n: int, seed: int, params: Params) -> None:
    start = time.perf_counter()
    summary = calibrate(n, seed, params)
    print(f"{n} seasons, seed {seed}, elite tier {'on' if params.elite else 'off'} ({time.perf_counter() - start:.2f} s)")
    for name, (target, low, high, real) in TARGETS.items():
        value = summary[name]
        flag = "ok" if low <= value <= high else "OFF"
        print(f"  {name:<28}{value:>8.3f}  target {target:g}, band {low:g}-{high:g} {flag:<3}  real: {real}")
    others = [name for name in summary if name not in TARGETS]
    print("  other: " + ", ".join(f"{name} {summary[name]:.3f}" for name in others))


def print_table(seed: int, matchday: int, params: Params, n_sims: int) -> None:
    state = season_state(League.create(seed, params), matchday, n_sims)
    print(f"Synthetic League, seed {seed}, after matchday {matchday} ({n_sims} simulations of the remaining fixtures)")
    print(f"  {'':>2} {'club':<22}{'P':>3}{'W':>4}{'D':>4}{'L':>4}{'GF':>5}{'GA':>5}{'GD':>5}{'Pts':>5}  {'title':>6}{'top 4':>7}{'releg.':>7}")
    for r in state["table"]:
        print(
            f"  {r['rank']:>2} {r['club']:<22}{r['played']:>3}{r['won']:>4}{r['drawn']:>4}{r['lost']:>4}{r['goals_for']:>5}{r['goals_against']:>5}{r['goal_difference']:>+5}{r['points']:>5}"
            f"  {100 * r['title_probability']:>5.1f}%{100 * r['top4_probability']:>6.1f}%{100 * r['relegation_probability']:>6.1f}%"
        )


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m league")
    parser.add_argument("command", nargs="?", choices=("table", "demo"), help="table: standings and chances; demo: what the league gives the demo fixture")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--matchday", type=int, default=DEMO_MATCHDAY)
    parser.add_argument("--sims", type=int, default=N_SIMS, help="Monte Carlo simulations of the remaining fixtures")
    parser.add_argument("--elite", action="store_true", help="elite tier: the two strongest clubs pulled further clear")
    parser.add_argument("--calibrate", type=int, metavar="N", help="print the calibration over N independent seasons and exit")
    args = parser.parse_args()
    params = Params(elite=args.elite)
    if args.calibrate:
        print_calibration(args.calibrate, args.seed, params)
    elif args.command == "table":
        print_table(args.seed, args.matchday, params, args.sims)
    elif args.command == "demo":
        print(json.dumps(demo_fixture(args.seed, args.matchday, params, args.sims), indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
