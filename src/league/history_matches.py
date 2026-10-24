"""Match layer of the league history: line-ups, substitutions, cards and goals for a score already drawn at club level.

The score comes from the club layer. Here each team picks its eleven and bench from the available players (injury spells, one-match bans after a red card, a little rotation), then the match is played as a timeline: substitution times, bookings and goal times are drawn, and each event picks its player among those on the pitch at that moment. A goal goes to an on-pitch player with weight role x finishing trait x exp((rating - 68) / 14); 7.5 % are penalties (three in four taken by the on-pitch player with the highest weight) and 3 % own goals. A substitute can only score after he came on, a sent-off player leaves the timeline.

Times are (period, clock_s) as in the feed: period 1 from 0 to 45 minutes plus added time, period 2 from 2700 s (46') to 90 minutes plus added time. The order key is period * 10000 + clock_s.
"""

import heapq
import math
import random

from generator.teams import FORMATIONS

FORMATION_ROLES = {
    "4-4-2": ("GK", "RB", "CB", "CB", "LB", "RW", "CM", "CM", "LW", "ST", "ST"),
    "4-3-3": tuple(slot[0] for slot in FORMATIONS["4-3-3"]),
    "4-2-3-1": tuple(slot[0] for slot in FORMATIONS["4-2-3-1"]),
}
GROUP_OF = {"GK": "GK", "RB": "DEF", "CB": "DEF", "LB": "DEF", "DM": "MID", "CM": "MID", "AM": "MID", "RW": "FWD", "LW": "FWD", "ST": "FWD"}
SCORING = {"GK": 0.0, "CB": 0.22, "RB": 0.12, "LB": 0.12, "DM": 0.2, "CM": 0.38, "AM": 0.6, "RW": 0.65, "LW": 0.65, "ST": 0.9}  # share of the goals by role, before rating and trait
ASSIST = {"GK": 0.02, "CB": 0.15, "RB": 0.5, "LB": 0.5, "DM": 0.35, "CM": 0.7, "AM": 1.0, "RW": 1.0, "LW": 1.0, "ST": 0.6}
BOOKING = {"GK": 0.15, "CB": 1.3, "RB": 1.1, "LB": 1.1, "DM": 1.5, "CM": 1.1, "AM": 0.8, "RW": 0.8, "LW": 0.8, "ST": 0.9}
OWN_GOAL = {"GK": 0.3, "CB": 1.0, "RB": 0.6, "LB": 0.6, "DM": 0.4, "CM": 0.15, "AM": 0.1, "RW": 0.1, "LW": 0.1, "ST": 0.1}
TAKEN_OFF = {"GK": 0.0, "DEF": 0.6, "MID": 1.2, "FWD": 1.4}
P_PENALTY, P_OWN_GOAL, P_ASSIST, P_FIRST_TAKER = 0.075, 0.03, 0.72, 0.75
P_INJURY, INJURY_MATCHES = 0.035, (1, 6)
P_STRAIGHT_RED, P_SECOND_YELLOW = 0.03, 0.04
YELLOWS = ((1976, 0.6), (1990, 1.0), (2000, 1.45), (2025, 1.95))  # bookings per team and match, rough shape

YELLOW, SECOND_YELLOW, RED = 0, 1, 2
CARD_NAMES = ("yellow", "second_yellow", "red")
FULL = -1  # off_k of a player still on the pitch at full time


def substitution_rules(year: int) -> tuple[int, int, tuple[float, ...]]:
    """(substitutions allowed, bench size, chances of using 0, 1, ... of them) by era."""
    if year < 1987:
        return 1, 1, (0.35, 0.65)
    if year < 1995:
        return 2, 2, (0.1, 0.3, 0.6)
    if year < 2020:
        return 3, 5 if year < 1996 else 7, (0.0, 0.04, 0.2, 0.76)
    return 5, 9, (0.0, 0.0, 0.0, 0.2, 0.3, 0.5)


def formation_for(rng: random.Random, year: int) -> str:
    if year < 1995:
        weights = (0.85, 0.1, 0.05)
    elif year < 2010:
        weights = (0.45, 0.25, 0.3)
    else:
        weights = (0.1, 0.45, 0.45)
    return rng.choices(("4-4-2", "4-3-3", "4-2-3-1"), weights)[0]


def added_time(rng: random.Random, year: int) -> tuple[int, int]:
    if year >= 2023:
        return rng.randint(2, 7), rng.randint(5, 11)
    return rng.choice((1, 1, 2, 2, 3, 4)), rng.choice((2, 3, 3, 4, 4, 5, 6))


def minute_label(period: int, clock_s: int) -> str:
    m = clock_s // 60 + 1
    end = 45 if period == 1 else 90
    return f"{end}+{m - end}'" if m > end else f"{m}'"


def minute_of(period: int, clock_s: int) -> tuple[int, int]:
    """(minute, stoppage): 45+2 is (45, 2)."""
    m = clock_s // 60 + 1
    end = 45 if period == 1 else 90
    return (end, m - end) if m > end else (m, 0)


def _poisson(rng: random.Random, lam: float) -> int:
    k, p, limit = 0, 1.0, math.exp(-lam)
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _pick(rng: random.Random, items: list, weights: list[float]):
    total = sum(weights)
    if total <= 0:
        return rng.choice(items)
    x = rng.random() * total
    for item, w in zip(items, weights):
        x -= w
        if x < 0:
            return item
    return items[-1]


def _time(rng: random.Random, t1: float, t2: float, slope: float) -> tuple[int, int]:
    """A moment of the match, with a density rising linearly through it: 1 - slope / 2 at kick-off, 1 + slope / 2 at the end."""
    a, b = 1 - slope / 2, slope / 2
    u = rng.random()
    x = (-a + math.sqrt(a * a + 4 * b * u)) / (2 * b)
    return divmod(_key_at(x * (t1 + t2), t1), 10000)


def pick_lineup(rng: random.Random, squad: list[int], role: dict[int, str], rating: dict[int, float], unavailable: set[int], formation: str, bench_size: int) -> tuple[list[int], list[int], list[str]]:
    """Starting eleven (formation slot order, with the role each plays), then the bench (a keeper first when the bench has five places or more)."""
    available = [p for p in squad if p not in unavailable]
    if len(available) < 11 + min(bench_size, 1):
        available = list(squad)
    value = {p: rating[p] + rng.gauss(0, 2.0) for p in squad}
    slots = FORMATION_ROLES[formation]
    chosen: list[int | None] = [None] * 11
    used: set[int] = set()
    for test in (lambda p, r: role[p] == r, lambda p, r: GROUP_OF[role[p]] == GROUP_OF[r], lambda p, r: role[p] != "GK"):
        for i, slot in enumerate(slots):
            if chosen[i] is not None:
                continue
            cands = [p for p in available if p not in used and test(p, slot)]
            if i == 0 and not cands:
                cands = [p for p in squad if p not in used and role[p] == "GK"] or [p for p in available if p not in used]
            if cands:
                best = max(cands, key=value.__getitem__)
                chosen[i] = best
                used.add(best)
    xi = [p for p in chosen if p is not None]
    rest = sorted((p for p in available if p not in used), key=lambda p: -value[p])
    bench: list[int] = []
    if bench_size >= 5:
        keepers = [p for p in rest if role[p] == "GK"]
        if keepers:
            bench.append(keepers[0])
            rest.remove(keepers[0])
    bench += rest[: bench_size - len(bench)]
    return xi, bench, list(slots[: len(xi)])


class MatchPlayer:
    """Plays one match as a timeline and appends its rows to the history tables."""

    def __init__(self, rng: random.Random, role: dict[int, str], rating: dict[int, float], finishing: list[float], temper: list[float]) -> None:
        self.rng, self.role, self.rating, self.finishing, self.temper = rng, role, rating, finishing, temper

    def play(self, year: int, goals: tuple[int, int], sides: tuple[tuple[list[int], list[int], list[str]], ...]) -> dict:
        """Events of one match. `sides` holds (eleven, bench, slot roles) for home and away. Returns the goals, substitutions, cards, appearances and half-time score."""
        rng = self.rng
        added1, added2 = added_time(rng, year)
        t1, t2 = 45 + added1, 45 + added2
        max_subs, _, use = substitution_rules(year)
        heap: list[tuple[int, int, int, str, int, int]] = []  # (key, kind order, seq, kind, side, player or -1)
        seq = 0

        def push(key: int, order: int, kind: str, side: int, player: int = -1) -> None:
            nonlocal seq
            heapq.heappush(heap, (key, order, seq, kind, side, player))
            seq += 1

        for side in (0, 1):
            n_subs = min(_pick(rng, list(range(len(use))), list(use)), max_subs, len(sides[side][1]))
            for _ in range(n_subs):
                u = rng.random()
                if u < 0.04:
                    period, clock = 1, rng.randint(600, 2650)
                elif u < 0.13:
                    period, clock = 2, 2700
                else:
                    minute = min(max(int(rng.gauss(69, 11)), 47), 89 + added2 // 2)
                    period, clock = 2, (minute - 1) * 60 + rng.randint(0, 59)
                push(period * 10000 + clock, 0, "sub", side)
            for _ in range(_poisson(rng, _era(YELLOWS, year))):
                period, clock = _time(rng, t1, t2, 0.9)
                push(period * 10000 + clock, 1, "yellow", side)
            if rng.random() < P_STRAIGHT_RED:
                period, clock = _time(rng, t1, t2, 0.9)
                push(period * 10000 + clock, 1, "red", side)
            for _ in range(goals[side]):
                period, clock = _time(rng, t1, t2, 0.5)
                push(period * 10000 + clock, 2, "goal", side)

        on = [dict(zip(xi, roles)) for xi, _, roles in sides]  # player -> role played
        bench = [list(b) for _, b, _ in sides]
        apps = {p: [side, True, 0, FULL] for side in (0, 1) for p in sides[side][0]}
        booked: set[int] = set()
        goals_out, subs_out, cards_out = [], [], []
        score = [0, 0]
        half_time = [0, 0]
        while heap:
            key, _, _, kind, side, target = heapq.heappop(heap)
            period, clock = divmod(key, 10000)
            pitch = on[side]
            if kind == "sub":
                outfield = [p for p in pitch if pitch[p] != "GK"]
                if not bench[side] or not outfield:
                    continue
                off = _pick(rng, outfield, [TAKEN_OFF[GROUP_OF[pitch[p]]] * (1.6 if p in booked else 1.0) for p in outfield])
                group = GROUP_OF[pitch[off]]
                like = [p for p in bench[side] if GROUP_OF[self.role[p]] == group]
                outfield_bench = [p for p in bench[side] if self.role[p] != "GK"]
                if not outfield_bench:
                    continue
                incoming = rng.choice(like) if like and rng.random() < 0.7 else _pick(rng, outfield_bench, [1.0 + (GROUP_OF[self.role[p]] == "FWD") for p in outfield_bench])
                bench[side].remove(incoming)
                slot_role = pitch.pop(off)
                pitch[incoming] = slot_role if GROUP_OF[self.role[incoming]] == group else self.role[incoming]
                apps[off][3] = key
                apps[incoming] = [side, False, key, FULL]
                subs_out.append((side, period, clock, off, incoming))
            elif kind in ("yellow", "red", "second_yellow"):
                if kind == "second_yellow":
                    player = target
                    if player not in pitch:
                        continue
                    card = SECOND_YELLOW
                else:
                    cands = [p for p in pitch if kind == "red" or p not in booked]
                    if not cands:
                        continue
                    player = _pick(rng, cands, [BOOKING[pitch[p]] * self.temper[p] for p in cands])
                    card = YELLOW if kind == "yellow" else RED
                cards_out.append((side, period, clock, player, card))
                if card == YELLOW:
                    booked.add(player)
                    if rng.random() < P_SECOND_YELLOW:
                        later = _elapsed(period, clock, t1) + rng.uniform(2, 40)
                        if later < t1 + t2:
                            push(_key_at(later, t1), 1, "second_yellow", side, player)
                else:
                    pitch.pop(player)
                    apps[player][3] = key
            else:  # goal for `side`
                own = rng.random() < P_OWN_GOAL and on[1 - side]
                assist = -1
                penalty = False
                if own:
                    opp = on[1 - side]
                    cands = list(opp)
                    scorer = _pick(rng, cands, [OWN_GOAL[opp[p]] for p in cands])
                else:
                    cands = list(pitch)
                    weights = [SCORING[pitch[p]] * self.finishing[p] * math.exp((self.rating[p] - 68) / 14) for p in cands]
                    if rng.random() < P_PENALTY:
                        penalty = True
                        taker = rng.random() < P_FIRST_TAKER
                        scorer = cands[max(range(len(cands)), key=weights.__getitem__)] if taker else _pick(rng, cands, weights)
                    else:
                        scorer = _pick(rng, cands, weights)
                        mates = [p for p in cands if p != scorer]
                        if mates and rng.random() < P_ASSIST:
                            assist = _pick(rng, mates, [ASSIST[pitch[p]] * math.exp((self.rating[p] - 68) / 8) for p in mates])
                score[side] += 1
                if period == 1:
                    half_time[side] += 1
                goals_out.append((side, period, clock, scorer, assist, bool(own), penalty))
        if score != list(goals):
            raise AssertionError(f"timeline lost goals: {score} for {goals}")
        return {"added": (added1, added2), "half_time": half_time, "goals": goals_out, "subs": subs_out, "cards": cards_out, "apps": apps}


def _elapsed(period: int, clock_s: int, t1: float) -> float:
    """Minutes played since kick-off, added time included."""
    return clock_s / 60 if period == 1 else t1 + (clock_s - 2700) / 60


def _key_at(elapsed: float, t1: float) -> int:
    if elapsed < t1:
        return 10000 + int(elapsed * 60)
    return 20000 + 2700 + int((elapsed - t1) * 60)


def _era(points: tuple[tuple[int, float], ...], year: int) -> float:
    xs, ys = zip(*points)
    if year <= xs[0]:
        return ys[0]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if year <= x1:
            return y0 + (y1 - y0) * (year - x0) / (x1 - x0)
    return ys[-1]
