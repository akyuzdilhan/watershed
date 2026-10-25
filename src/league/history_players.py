"""Player layer of the league history: fictional players, ratings by age, squads of 25, a transfer window between seasons, youth intake, retirements.

A player has a peak rating (about 66, standard deviation 6.5), reached between 27 and 29; the rating is the peak minus 0.22 point per squared year before 27 and 0.35 per squared year after 29, plus a slow form term. Careers start at 17 to 19 (youth intake) or, for players signed from outside the league system, at 20 to 28, and end at a retirement age drawn from 33 to 37, earlier for a player no club wants.

Between seasons, players retire, some are listed (a good player at a modest club, a fringe player at a strong one, or a random move), each club takes two academy players, and clubs fill their gaps in a draft: strongest club first, best available value first (current rating, or potential for a player under 23). Better players drift to stronger clubs.
"""

import math
import random
from dataclasses import dataclass, field

import numpy as np

from generator.names import FIRST_NAMES, SURNAMES

GROUPS = ("GK", "DEF", "MID", "FWD")
TARGET = {"GK": 3, "DEF": 8, "MID": 8, "FWD": 6}  # squad of 25
ROLES = ("GK", "RB", "CB", "LB", "DM", "CM", "AM", "RW", "LW", "ST")
GROUP_OF = {"GK": "GK", "RB": "DEF", "CB": "DEF", "LB": "DEF", "DM": "MID", "CM": "MID", "AM": "MID", "RW": "FWD", "LW": "FWD", "ST": "FWD"}
ROLE_MIX = {"GK": (("GK", 1.0),), "DEF": (("CB", 0.5), ("RB", 0.25), ("LB", 0.25)), "MID": (("DM", 0.3), ("CM", 0.45), ("AM", 0.25)), "FWD": (("ST", 0.45), ("RW", 0.275), ("LW", 0.275))}
IDEAL = {"GK": 3, "CB": 4, "RB": 2, "LB": 2, "DM": 2, "CM": 4, "AM": 2, "ST": 2, "RW": 2, "LW": 2}
PEAK_MEAN, PEAK_SD = 66.0, 6.5
RETIRE_AGES = (33, 37)
MAX_PLAYERS = 100_000
# Each player of the history is drawn as a key on this grid, then named from his own stream. The grid is the size of the name pools the histories were
# first built with (48 first names, 72 surnames): a seed keeps its careers, scores and cards, and the demo its story, whatever the name pools.
KEY_GRID = (48, 72)


def rating_at(peak: float, age: int, form: float) -> float:
    if age < 27:
        curve = -0.22 * (27 - age) ** 2
    elif age > 29:
        curve = -0.35 * (age - 29) ** 2
    else:
        curve = 0.0
    return peak + curve + form


@dataclass
class People:
    """Every player of the history, by index (id P000001 is index 0)."""

    first: list[int] = field(default_factory=list)
    last: list[int] = field(default_factory=list)
    key: list[tuple[int, int]] = field(default_factory=list)  # draw on KEY_GRID
    role: list[str] = field(default_factory=list)
    born: list[int] = field(default_factory=list)
    peak: list[float] = field(default_factory=list)
    retire_age: list[int] = field(default_factory=list)
    finishing: list[float] = field(default_factory=list)  # scoring trait, lognormal around 1
    temper: list[float] = field(default_factory=list)  # booking trait, lognormal around 1
    form: list[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.born)

    def name(self, p: int) -> str:
        return f"{FIRST_NAMES[self.first[p]]} {SURNAMES[self.last[p]]}"


@dataclass
class DemoCareer:
    """A scripted player of the demo match: his name, role and the club of each season (start year -> club index)."""

    key: str  # demo id, e.g. home-20
    name: str
    role: str
    born: int
    peak: float
    path: dict[int, int]


class Careers:
    """Squads season by season. `window(s)` runs the summer before season s and returns the squads of season s."""

    def __init__(self, rng: random.Random, years: list[int], q: np.ndarray, demo: list[DemoCareer], names: random.Random, demo_keys: dict[str, tuple[int, int]]) -> None:
        """`names` draws the names, apart from `rng`; `demo_keys` gives each demo player (by demo id) his key on KEY_GRID."""
        self.rng, self.years, self.q, self.names = rng, years, q, names
        self.n_clubs = q.shape[1]
        self.people = People()
        self.squads: list[list[int]] = [[] for _ in range(self.n_clubs)]
        self.free: dict[int, int] = {}  # free agent -> seasons without a club
        self.active_keys: set[tuple[int, int]] = set()  # keys and names of the players in a squad or free: never two at a time
        self.active_names: set[tuple[int, int]] = set()
        self.reserved_keys: set[tuple[int, int]] = set()  # those of the demo players, kept for them
        self.demo_surnames: set[int] = set()  # no other player of the history shares a surname with a demo player
        self.pinned: dict[int, dict[int, int]] = {}
        self.demo_ids: dict[str, int] = {}
        first_index, last_index = {f: i for i, f in enumerate(FIRST_NAMES)}, {s: j for j, s in enumerate(SURNAMES)}
        named = []
        for d in demo:
            first, last = d.name.split(" ", 1)
            named.append((first_index[first], last_index[last]))
            self.demo_surnames.add(named[-1][1])
            self.reserved_keys.add(demo_keys[d.key])
        for d, name in zip(demo, named):  # demo players get the first ids
            p = self._add(name, demo_keys[d.key], d.role, d.born, d.peak, 37)
            self.pinned[p] = d.path
            self.demo_ids[d.key] = p
        self.ratings: dict[int, float] = {}

    # --- people ---------------------------------------------------------------------------------

    def _add(self, name: tuple[int, int], key: tuple[int, int], role: str, born: int, peak: float, retire_age: int) -> int:
        pp, r = self.people, self.rng
        if len(pp) >= MAX_PLAYERS:
            raise RuntimeError("player ids exhausted")
        pp.first.append(name[0])
        pp.last.append(name[1])
        pp.key.append(key)
        pp.role.append(role)
        pp.born.append(born)
        pp.peak.append(peak)
        pp.retire_age.append(retire_age)
        pp.finishing.append(math.exp(r.gauss(0, 0.2)))
        pp.temper.append(math.exp(r.gauss(0, 0.35)))
        pp.form.append(0.0)
        return len(pp) - 1

    def _new(self, year: int, age: int, group: str, peak: float) -> int:
        r = self.rng
        while True:
            key = (r.randrange(KEY_GRID[0]), r.randrange(KEY_GRID[1]))
            if key not in self.active_keys and key not in self.reserved_keys:
                break
        while True:
            name = (self.names.randrange(len(FIRST_NAMES)), self.names.randrange(len(SURNAMES)))
            if name not in self.active_names and name[1] not in self.demo_surnames:
                break
        roles, weights = zip(*ROLE_MIX[group])
        role = r.choices(roles, weights)[0]
        p = self._add(name, key, role, year - age, peak, r.randint(*RETIRE_AGES))
        self._activate(p)
        return p

    def _activate(self, p: int) -> None:
        self.active_keys.add(self.people.key[p])
        self.active_names.add((self.people.first[p], self.people.last[p]))

    def _leave(self, p: int) -> None:
        self.active_keys.discard(self.people.key[p])
        self.active_names.discard((self.people.first[p], self.people.last[p]))
        self.free.pop(p, None)

    def group(self, p: int) -> str:
        return GROUP_OF[self.people.role[p]]

    def _value(self, p: int, year: int) -> float:
        """What a club sees: the rating, or for a player under 23 a blend with his peak (potential)."""
        rating = self.ratings[p]
        age = year - self.people.born[p]
        return rating + 0.6 * (self.people.peak[p] - rating) if age < 23 else rating

    # --- the summer window ----------------------------------------------------------------------

    def window(self, s: int) -> list[list[int]]:
        r, pp, year = self.rng, self.people, self.years[s]
        q = self.q[s]
        everyone = [p for sq in self.squads for p in sq] + list(self.free)
        for p in everyone:
            pp.form[p] = 0.5 * pp.form[p] + r.gauss(0, 1.5)
            self.ratings[p] = rating_at(pp.peak[p], year - pp.born[p], pp.form[p])
        pool: list[int] = []
        # free agents: one more chance if under 28
        for p in list(self.free):
            self.free[p] += 1
            if self.free[p] > 1 or year - pp.born[p] >= 28:
                self._leave(p)
            else:
                pool.append(p)
        # retirements and listings
        strength_rank = {c: i for i, c in enumerate(np.argsort(-q))}
        for c in range(self.n_clubs):
            level = _level([self.ratings[p] for p in self.squads[c]])
            keep = []
            for p in self.squads[c]:
                if p in self.pinned:
                    keep.append(p)
                    continue
                age, rating = year - pp.born[p], self.ratings[p]
                if age > pp.retire_age[p] or (age >= 30 and rating < 50):
                    self._leave(p)
                    continue
                chance = 0.07
                if rating > level + 5 and strength_rank[c] >= 6:
                    chance += 0.35
                if rating < level - 7 and age >= 22:
                    chance += 0.35
                if r.random() < chance:
                    pool.append(p)
                else:
                    keep.append(p)
            self.squads[c] = keep
        # scripted demo careers
        for p, path in self.pinned.items():
            target = path.get(year)
            current = next((c for c in range(self.n_clubs) if p in self.squads[c]), None)
            if target is None:
                if current is not None:
                    self.squads[current].remove(p)
                continue
            if p not in self.ratings:
                pp.form[p] = 0.0
                self.ratings[p] = rating_at(pp.peak[p], year - pp.born[p], 0.0)
            if current != target:
                if current is not None:
                    self.squads[current].remove(p)
                self.squads[target].append(p)
                self._activate(p)
        for c in range(self.n_clubs):
            pool += self._trim(c, year)
        # academy: two players per club, a little better at stronger clubs
        for c in range(self.n_clubs):
            for _ in range(2):
                age = r.choice((17, 17, 18, 18, 19))
                group = self._needed_group(c, random_pick=True)
                p = self._new(year, age, group, r.gauss(PEAK_MEAN + 8 * q[c], PEAK_SD))
                self.ratings[p] = rating_at(pp.peak[p], age, 0.0)
                self.squads[c].append(p)
            pool += self._trim(c, year)
        self._draft(pool, q, year)
        for p in pool:
            if not any(p in sq for sq in self.squads):
                self.free.setdefault(p, 0)
        return [list(sq) for sq in self.squads]

    def _needed_group(self, c: int, random_pick: bool = False) -> str:
        counts = {g: 0 for g in GROUPS}
        for p in self.squads[c]:
            counts[self.group(p)] += 1
        gaps = {g: TARGET[g] - counts[g] for g in GROUPS}
        if random_pick:
            weights = [max(gaps[g], 0) + 0.5 * TARGET[g] / 8 for g in GROUPS]
            return self.rng.choices(GROUPS, weights)[0]
        return max(GROUPS, key=lambda g: gaps[g])

    def _trim(self, c: int, year: int) -> list[int]:
        """List the lowest-valued unscripted players of every group over its target, academy players under 20 excepted when possible."""
        out = []
        for g in GROUPS:
            members = [p for p in self.squads[c] if self.group(p) == g]
            excess = len(members) - TARGET[g]
            if excess <= 0:
                continue
            movable = sorted((p for p in members if p not in self.pinned), key=lambda p: (year - self.people.born[p] < 20, self._value(p, year)))
            for p in movable[:excess]:
                self.squads[c].remove(p)
                out.append(p)
        return out

    def _draft(self, pool: list[int], q: np.ndarray, year: int) -> None:
        r, pp = self.rng, self.people
        by_group: dict[str, list[int]] = {g: [] for g in GROUPS}
        for p in pool:
            by_group[self.group(p)].append(p)
        for g in GROUPS:
            by_group[g].sort(key=lambda p: -self._value(p, year))
        order = sorted(range(self.n_clubs), key=lambda c: -(q[c] + r.gauss(0, 0.12)))
        while True:
            picked = False
            for c in order:
                g = self._needed_group(c)
                counts = sum(1 for p in self.squads[c] if self.group(p) == g)
                if counts >= TARGET[g]:
                    continue
                picked = True
                roles = {}
                for p in self.squads[c]:
                    roles[pp.role[p]] = roles.get(pp.role[p], 0) + 1
                shortlist = by_group[g][:8]
                if shortlist:
                    best = max(shortlist, key=lambda p: self._value(p, year) + r.gauss(0, 3) + (4 if roles.get(pp.role[p], 0) < IDEAL[pp.role[p]] else 0) - (3 if year - pp.born[p] >= 31 else 0))
                    by_group[g].remove(best)
                    self.free.pop(best, None)
                else:  # signed from outside the league system
                    age = r.randint(20, 28)
                    best = self._new(year, age, g, r.gauss(PEAK_MEAN + 6 * q[c], PEAK_SD * 0.8))
                    pp.form[best] = r.gauss(0, 1.5)
                    self.ratings[best] = rating_at(pp.peak[best], age, pp.form[best])
                self.squads[c].append(best)
            if not picked:
                return

    def initial(self) -> list[list[int]]:
        """Squads of the first season: players of every age, drafted like a normal window."""
        r, year = self.rng, self.years[0]
        pool = []
        for g in GROUPS:
            for _ in range(TARGET[g] * self.n_clubs + 6):
                age = r.choice(range(17, 34))
                p = self._new(year, age, g, r.gauss(PEAK_MEAN, PEAK_SD))
                self.people.form[p] = r.gauss(0, 1.5)
                self.ratings[p] = rating_at(self.people.peak[p], age, self.people.form[p])
                pool.append(p)
        for p, path in self.pinned.items():
            if year in path:
                self.ratings[p] = rating_at(self.people.peak[p], year - self.people.born[p], 0.0)
                self.squads[path[year]].append(p)
                self._activate(p)
        self._draft(pool, self.q[0], year)
        for p in pool:
            if not any(p in sq for sq in self.squads):
                self.free.setdefault(p, 0)
        return [list(sq) for sq in self.squads]


def _level(ratings: list[float]) -> float:
    top = sorted(ratings, reverse=True)[:14]
    return sum(top) / len(top) if top else 0.0


def demo_careers(rng: random.Random, years: list[int], n_clubs: int, squads: dict[int, list[tuple[str, str, str, bool]]]) -> list[DemoCareer]:
    """Scripted careers of the demo players: `squads` maps the club index to (demo id, name, role, starter).

    Starters are born 1991 to 2003 (keepers 1988 to 2000) with a peak around 70, bench players 1994 to 2006 around 66. Each has spent his last 0 to 7 seasons (bench: 0 to 4) at his demo club, after zero to two other clubs from his debut at 18.
    """
    out = []
    for club, players in squads.items():
        for key, name, role, starter in players:
            if starter:
                born = rng.randint(1988, 2000) if role == "GK" else rng.randint(1991, 2003)
                peak, stay = rng.gauss(70, 3), rng.randint(0, 7)
            else:
                born, peak, stay = rng.randint(1994, 2006), rng.gauss(66, 3), rng.randint(0, 4)
            debut = born + 18
            start = max(debut, years[-1] - stay)
            path = {y: club for y in range(start, years[-1] + 1)}
            earlier = list(range(debut, start))
            if earlier:
                n_other = min(len(earlier), rng.choice((1, 1, 2)))
                cuts = sorted(rng.sample(range(1, len(earlier)), n_other - 1)) if n_other > 1 else []
                bounds = [0, *cuts, len(earlier)]
                previous = club
                for a, b in zip(bounds, bounds[1:]):
                    other = rng.choice([c for c in range(n_clubs) if c not in (club, previous)])
                    for y in earlier[a:b]:
                        path[y] = other
                    previous = other
            out.append(DemoCareer(key, name, role, born, peak, path))
    return out
