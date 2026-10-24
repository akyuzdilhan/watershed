"""The insight engine's view of the league history: one scan of `History.matches`, each match reduced to the features the detectors count.

Only the read API of the contract is used (`seasons`, `matches`, `head_to_head`, `last`, `player`). The match records are read with tolerant accessors, since the history module is built in parallel: a goal may name its team as `home`/`away` or by club, its player as an id or a `{id, name}` dict, and the half-time score may be a dict, a pair or a "1-0" string. The reduced features are what every count works on, and `last()` is asked with a predicate over the same features, so a count and its "last time" always agree.
"""

from dataclasses import dataclass, field
from typing import Callable

LATE_MINUTE = 85


def _side(value, record: dict) -> str | None:
    if value in ("home", "away"):
        return value
    if value == record.get("home"):
        return "home"
    if value == record.get("away"):
        return "away"
    return None


def _pid(value) -> str | None:
    if isinstance(value, dict):
        return value.get("id") or value.get("player_id")
    return value


def _pair(value) -> tuple[int, int] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return int(value.get("home", 0)), int(value.get("away", 0))
    if isinstance(value, str) and "-" in value:
        a, b = value.split("-", 1)
        return int(a), int(b)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return int(value[0]), int(value[1])
    return None


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


@dataclass
class Goal:
    side: str
    player: str | None
    minute: int
    own_goal: bool = False


@dataclass
class Features:
    """One past match, reduced."""
    index: int  # chronological rank
    season: str
    season_idx: int
    matchday: int
    home: str
    away: str
    hg: int
    ag: int
    ht: tuple[int, int] | None
    goals: list[Goal]  # sorted by minute; empty when the record has no scorers
    sub_in: dict[str, int]  # player id -> minute he came on
    reds: list[tuple[str, int]]  # (side, minute)
    record: dict = field(repr=False, default_factory=dict)

    def club(self, side: str) -> str:
        return self.home if side == "home" else self.away

    def goals_of(self, side: str) -> int:
        return self.hg if side == "home" else self.ag

    def result(self, side: str) -> str:
        mine, theirs = (self.hg, self.ag) if side == "home" else (self.ag, self.hg)
        return "W" if mine > theirs else "D" if mine == theirs else "L"

    def player_goals(self) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        for g in self.goals:
            if g.player and not g.own_goal:
                out.setdefault(g.player, []).append(g.minute)
        return out

    def comeback(self, side: str) -> tuple[int, int]:
        """(largest deficit, best stage reached after first trailing by it): stage 1 level, 2 ahead, 3 won."""
        return comeback_stage([g.side for g in self.goals], side, final=True)


def comeback_stage(order: list[str], side: str, final: bool) -> tuple[int, int]:
    """Goals in order (`home`/`away`), seen from `side`: the largest deficit and the best stage after first trailing by it (1 level, 2 ahead, 3 won, only when `final`)."""
    diff, worst, worst_at = 0, 0, -1
    path = []
    for i, s in enumerate(order):
        diff += 1 if s == side else -1
        path.append(diff)
        if -diff > worst:
            worst, worst_at = -diff, i
    if worst == 0:
        return 0, 0
    after = path[worst_at + 1:]
    stage = 0
    if any(d >= 0 for d in after):
        stage = 1
    if any(d > 0 for d in after):
        stage = 2
    if final and path and path[-1] > 0:
        stage = 3
    return worst, stage


def reduce(record: dict, index: int, season_idx: dict[str, int]) -> Features:
    hg, ag = int(_first(record, "home_goals", "hg", default=0)), int(_first(record, "away_goals", "ag", default=0))
    goals = []
    for g in _first(record, "goals", "scorers", default=[]) or []:
        side = _side(_first(g, "team", "side", "club"), record)
        if side is None:
            continue
        goals.append(Goal(side, _pid(_first(g, "player_id", "player", "scorer")), int(_first(g, "minute", default=0)), bool(g.get("own_goal"))))
    goals.sort(key=lambda g: g.minute)
    sub_in = {}
    for s in _first(record, "substitutions", "subs", default=[]) or []:
        pid = _pid(_first(s, "player_in", "on", "in"))
        if pid:
            sub_in[pid] = int(_first(s, "minute", default=0))
    reds = []
    for c in record.get("cards", []) or []:
        if _first(c, "card", "colour", "color", "type") in ("red", "second_yellow"):
            side = _side(_first(c, "team", "side", "club"), record)
            if side:
                reds.append((side, int(_first(c, "minute", default=0))))
    season = record.get("season", "")
    return Features(index, season, season_idx.get(season, 0), int(record.get("matchday", 0)), record.get("home", ""), record.get("away", ""), hg, ag,
                    _pair(_first(record, "half_time", "ht")), goals, sub_in, sorted(reds, key=lambda r: r[1]), record)


def next_season(label: str) -> str:
    try:
        a, b = label.split("/")
        y = int(a) + 1
        return f"{y}/{(y + 1) % 100:02d}"
    except ValueError:
        return label


class HistoryView:
    """Counts over the reduced history, cached by query name. A query returns, for one past match, (occurrences, opportunities)."""

    def __init__(self, history) -> None:
        self.history = history
        self.seasons: list[str] = list(history.seasons())
        self.season_idx = {s: i for i, s in enumerate(self.seasons)}
        raw = list(history.matches(lambda m: True))
        raw.sort(key=lambda m: (self.season_idx.get(m.get("season", ""), 0), int(m.get("matchday", 0))))
        self.features = [reduce(m, i, self.season_idx) for i, m in enumerate(raw)]
        self.n = len(self.features)
        self.first_season = self.seasons[0] if self.seasons else "the start"
        self.current_season = next_season(self.seasons[-1]) if self.seasons else ""
        self.current_idx = len(self.seasons)
        self.clubs = {f.home for f in self.features} | {f.away for f in self.features}
        self.has_goals = any(f.goals for f in self.features)
        self._cache: dict = {}
        self._runs: dict | None = None

    def count(self, name: str, query: Callable[[Features], tuple[int, int]]) -> tuple[int, int]:
        if name not in self._cache:
            k = n = 0
            for f in self.features:
                a, b = query(f)
                k += a
                n += b
            self._cache[name] = (k, n)
        return self._cache[name]

    def last(self, match_pred: Callable[[Features], bool], name: str | None = None) -> Features | None:
        """The most recent past match where `match_pred` holds, asked through `History.last` and read back as features; cached under `name`."""
        if name is not None:
            key = "last:" + name
            if key not in self._cache:
                self._cache[key] = self.last(match_pred)
            return self._cache[key]
        found = None
        last = getattr(self.history, "last", None)
        if last is not None:
            hit = last("match", lambda m: match_pred(reduce(m, 0, self.season_idx)))
            if hit is not None:
                found = hit.get("match", hit) if isinstance(hit, dict) else hit
        if found is None:
            return None
        return reduce(found, 0, self.season_idx)

    def player_name(self, pid: str | None) -> str | None:
        if not pid:
            return None
        try:
            p = self.history.player(pid)
        except (KeyError, LookupError, AttributeError):
            return None
        return p.get("name") if isinstance(p, dict) else None

    def head_to_head(self, home: str, away: str) -> list[dict]:
        """Every past meeting of the two clubs, both venues, newest first."""
        both = list(self.history.head_to_head(home, away)) + list(self.history.head_to_head(away, home))
        key = lambda m: (self.season_idx.get(m.get("season", ""), 0), int(m.get("matchday", 0)))
        seen, out = set(), []
        for m in sorted(both, key=key, reverse=True):
            k = (m.get("season"), m.get("matchday"), m.get("home"), m.get("away"))
            if k not in seen:
                seen.add(k)
                out.append(m)
        return out

    def club_max_goals(self, club: str) -> int:
        key = "club-max:" + club
        if key not in self._cache:
            self._cache[key] = max((f.goals_of(s) for f in self.features for s in ("home", "away") if f.club(s) == club), default=0)
        return self._cache[key]

    # --- runs -----------------------------------------------------------------------------------

    def runs(self) -> dict:
        """Per club, the results in order; league-wide, the lengths of every unbeaten and winless run."""
        if self._runs is None:
            seq: dict[str, list[str]] = {}
            for f in self.features:
                seq.setdefault(f.home, []).append(f.result("home"))
                seq.setdefault(f.away, []).append(f.result("away"))
            lengths = {"unbeaten": [], "winless": []}
            current = {}
            for club, results in seq.items():
                cur = {}
                for kind, breaks in (("unbeaten", "L"), ("winless", "W")):
                    n = 0
                    for r in results:
                        if r == breaks:
                            if n:
                                lengths[kind].append(n)
                            n = 0
                        else:
                            n += 1
                    if n:
                        lengths[kind].append(n)
                    cur[kind] = n
                current[club] = cur
            self._runs = {"results": seq, "lengths": lengths, "current": current}
        return self._runs

    def run_tail(self, kind: str, length: int) -> tuple[int, int]:
        lengths = self.runs()["lengths"][kind]
        return sum(1 for x in lengths if x >= length), len(lengths)

    def drought_tail(self, side: str, gap: int) -> tuple[int, int]:
        """League-wide: of the wins by the `side` club of a fixture (same home and away clubs), how many came `gap` or more seasons after its previous win there (or after the first meeting)."""
        name = f"drought-{side}"
        if name not in self._cache:
            first: dict[tuple[str, str], int] = {}
            last_win: dict[tuple[str, str], int] = {}
            gaps = []
            for f in self.features:
                fixture = (f.home, f.away)
                first.setdefault(fixture, f.season_idx)
                if f.result(side) == "W":
                    gaps.append(f.season_idx - last_win.get(fixture, first[fixture]))
                    last_win[fixture] = f.season_idx
            self._cache[name] = gaps
        gaps = self._cache[name]
        return sum(1 for g in gaps if g >= gap), len(gaps)
