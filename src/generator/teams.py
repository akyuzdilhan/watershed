"""Players, teams and line-ups. Names are drawn from the frozen pools of `generator.names` (first names and surnames taken from Faker's lists) and the clubs are invented: no real club, player or referee."""

from dataclasses import dataclass, field

import numpy as np

from generator.names import FIRST_NAMES, SURNAMES

# Formation slots: role, then the slot position in the team's own frame with the ball on the centre spot, in possession (ip) and out of possession (oop). Shape moves them with the ball.
FORMATIONS: dict[str, list[tuple[str, float, float, float, float]]] = {
    "4-3-3": [
        ("GK", 10, 34, 5, 34), ("RB", 42, 8, 30, 10), ("CB", 30, 25, 28, 27), ("CB", 30, 43, 28, 41), ("LB", 42, 60, 30, 58),
        ("DM", 45, 34, 38, 34), ("CM", 56, 22, 45, 25), ("CM", 56, 46, 45, 43),
        ("RW", 74, 9, 55, 14), ("ST", 80, 34, 60, 34), ("LW", 74, 59, 55, 54),
    ],
    "4-2-3-1": [
        ("GK", 10, 34, 5, 34), ("RB", 42, 8, 30, 10), ("CB", 30, 25, 28, 27), ("CB", 30, 43, 28, 41), ("LB", 42, 60, 30, 58),
        ("DM", 47, 27, 39, 29), ("DM", 47, 41, 39, 39),
        ("RW", 72, 10, 54, 14), ("AM", 68, 34, 52, 34), ("LW", 72, 58, 54, 54), ("ST", 82, 34, 62, 34),
    ],
}
SHIRTS = {"4-3-3": [1, 2, 5, 4, 3, 6, 8, 10, 7, 9, 11], "4-2-3-1": [1, 2, 5, 6, 3, 4, 8, 7, 10, 11, 9]}
BENCH = [(13, "GK"), (12, "CB"), (15, "RB"), (16, "DM"), (18, "CM"), (21, "AM"), (17, "RW"), (19, "LW"), (20, "ST")]
GROUP = {"GK": "GK", "RB": "DEF", "CB": "DEF", "LB": "DEF", "DM": "MID", "CM": "MID", "AM": "MID", "RW": "FWD", "LW": "FWD", "ST": "FWD"}
# How often a role takes the team's shots, relative to a striker.
SHOOTING = {"GK": 0.0, "RB": 0.12, "LB": 0.12, "CB": 0.2, "DM": 0.2, "CM": 0.35, "AM": 0.6, "RW": 0.7, "LW": 0.7, "ST": 1.0}



@dataclass(frozen=True)
class Player:
    id: str
    side: str
    shirt: int
    name: str
    role: str
    foot: str  # "right" or "left"

    def ref(self) -> dict:
        return {"id": self.id, "shirt": self.shirt, "name": self.name}


@dataclass
class Team:
    side: str
    name: str
    formation: str
    expected_goals: float  # pre-match expectation for this fixture: the prior of the win-probability model
    squad: list[Player]  # the starting eleven in slot order, then the bench
    line_height: float = 0.0  # metres added to the defensive line out of possession

    def player(self, shirt: int) -> Player:
        return next(p for p in self.squad if p.shirt == shirt)


@dataclass
class Lineup:
    """Who is on the pitch, in which formation slot, and who is left on the bench."""

    team: Team
    slots: list[Player | None] = field(default_factory=list)
    bench: list[Player] = field(default_factory=list)
    subs_used: int = 0
    windows_used: int = 0
    sent_off: set[str] = field(default_factory=set)
    subbed_off: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.slots = list(self.team.squad[:11])
        self.bench = list(self.team.squad[11:])

    def on_pitch(self) -> list[Player]:
        return [p for p in self.slots if p is not None]

    def outfield(self) -> list[Player]:
        return [p for p in self.slots[1:] if p is not None]

    def keeper(self) -> Player:
        """Whoever is in goal: slot 0 of every formation."""
        return self.slots[0]

    def is_keeper(self, player: Player) -> bool:
        return self.slots[0] is not None and self.slots[0].id == player.id

    def replace_keeper(self) -> tuple[Player, Player | None]:
        """After the keeper is sent off: the substitute keeper comes on for an outfield player, or, with no substitution left, an outfield player goes in goal. Returns who leaves the field (or moves into goal) and who comes on."""
        mover = self.outfield()[-1]  # a forward makes way
        self.slots[self.slot(mover)] = None
        keepers = [b for b in self.bench if b.role == "GK"]
        if keepers and self.subs_used < 5:
            self.slots[0] = keepers[0]
            self.bench.remove(keepers[0])
            self.subbed_off.add(mover.id)
            self.subs_used += 1
            return mover, keepers[0]
        self.slots[0] = mover
        return mover, None

    def slot(self, player: Player) -> int:
        return next(i for i, p in enumerate(self.slots) if p is not None and p.id == player.id)

    def is_on(self, player: Player) -> bool:
        return any(p is not None and p.id == player.id for p in self.slots)

    def substitute(self, out: Player, incoming: Player) -> None:
        self.slots[self.slot(out)] = incoming
        self.bench.remove(incoming)
        self.subbed_off.add(out.id)
        self.subs_used += 1

    def send_off(self, player: Player) -> None:
        self.slots[self.slot(player)] = None
        self.sent_off.add(player.id)


PROBE = 101  # prime, coprime with the pool sizes: on a name already taken, the next candidate is PROBE places further in the pool


def _pick(rng: np.random.Generator, pool: tuple[str, ...], taken: set[str]) -> str:
    """One name of `pool` not in `taken`, which it joins. A single draw whatever the collisions, so the rest of the match does not depend on them."""
    i = int(rng.choice(len(pool)))
    for _ in range(len(pool)):
        if pool[i] not in taken:
            taken.add(pool[i])
            return pool[i]
        i = (i + PROBE) % len(pool)
    raise ValueError("name pool exhausted")


def _squad(rng: np.random.Generator, side: str, formation: str, surnames: set[str]) -> list[Player]:
    """A starting eleven and its bench: first names unique in the squad, surnames unique in `surnames` (both squads and the referee)."""
    roles = [slot[0] for slot in FORMATIONS[formation]]
    entries = list(zip(SHIRTS[formation], roles)) + BENCH
    first_names: set[str] = set()
    squad = []
    for shirt, role in entries:
        name = f"{_pick(rng, FIRST_NAMES, first_names)} {_pick(rng, SURNAMES, surnames)}"
        left = (role in ("LB", "LW") and rng.random() < 0.7) or rng.random() < 0.18
        foot = "left" if left else "right"
        squad.append(Player(id=f"{side}-{shirt}", side=side, shirt=int(shirt), name=name, role=role, foot=foot))
    return squad


def make_teams(rng: np.random.Generator, home_xg: float, away_xg: float) -> tuple[Team, Team, str]:
    """The two teams of a fixture and a fictional referee name. No surname appears twice among the 40 players and the referee."""
    surnames: set[str] = set()
    home = Team("home", "Varleigh", "4-3-3", home_xg, _squad(rng, "home", "4-3-3", surnames))
    away = Team("away", "Yarnford", "4-2-3-1", away_xg, _squad(rng, "away", "4-2-3-1", surnames))
    referee = f"{_pick(rng, FIRST_NAMES, set())} {_pick(rng, SURNAMES, surnames)}"
    return home, away, referee
