"""A fictional history of the Synthetic League: 50 seasons (1976/77 to 2025/26), 32 clubs over two tiers, fictional players with careers, and every top-tier match with its scorers, assists, substitutions, cards and half-time score.

Built in three layers: clubs and scores (`history_clubs`), players and squads (`history_players`), match events (`history_matches`). Stored as numpy columns in `data/history/<seed>/history.npz` plus the names in `meta.json.gz`. The worker and the insight engine read it through `History` (docs/contracts.md, "Lot 2 additions"); everything is deterministic for a seed.

    python -m league history --seed 9 --seasons 50 --out data/history
"""

import gzip
import json
import random
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from generator.teams import make_teams
from league.history_clubs import POOL, simulate_clubs, season_label
from league.history_matches import CARD_NAMES, FULL, INJURY_MATCHES, P_INJURY, RED, SECOND_YELLOW, MatchPlayer, formation_for, minute_label, minute_of, pick_lineup, substitution_rules
from league.history_players import ROLES, Careers, demo_careers

FORMAT_VERSION = 1
DEMO_SEED = 7  # generator.generate_match(7, "demo")
UNUSED = -2  # a_on of a named substitute who did not come on
EVENTS = ("match", "goal", "substitution", "card")
SIDES = ("home", "away")

_COLUMNS = {
    "m": ("season", "matchday", "home", "away", "hg", "ag", "hth", "hta", "add1", "add2"),
    "g": ("match", "side", "period", "clock", "scorer", "assist", "own", "pen"),
    "s": ("match", "side", "period", "clock", "off", "on"),
    "c": ("match", "side", "period", "clock", "player", "kind"),
    "a": ("match", "side", "player", "started", "on", "off"),
    "ps": ("player", "season", "club", "rating"),
}


def _seeded(seed: int, *key: int) -> random.Random:
    return random.Random(int(np.random.SeedSequence([seed, *key]).generate_state(2, np.uint64)[0]))


def demo_squads(demo_seed: int = DEMO_SEED) -> dict[str, list]:
    """The squads of the demo match, as `generator.generate_match(demo_seed, "demo")` builds them (same generator, same first draws)."""
    home, away, _ = make_teams(np.random.default_rng(demo_seed), 1.5, 1.2)
    return {home.name: home.squad, away.name: away.squad}


def generate(seed: int = 9, n_seasons: int = 50, demo_seed: int = DEMO_SEED) -> "History":
    """Simulate the history of `seed`: `n_seasons` seasons ending in 2025/26."""
    layer = simulate_clubs(seed, n_seasons)
    years = layer.years
    spec = {}
    for club, squad in demo_squads(demo_seed).items():
        spec[layer.names.index(club)] = [(p.id, p.name, p.role, i < 11) for i, p in enumerate(squad)]
    careers = Careers(_seeded(seed, 7679), years, layer.q, demo_careers(_seeded(seed, 7680), years, POOL, spec))
    rng = _seeded(seed, 7681)
    people = careers.people
    player = MatchPlayer(rng, people.role, careers.ratings, people.finishing, people.temper)
    cols: dict[str, dict[str, list]] = {t: {k: [] for k in ks} for t, ks in _COLUMNS.items()}

    def add(table: str, *values) -> None:
        for k, v in zip(_COLUMNS[table], values):
            cols[table][k].append(v)

    n_match = 0
    for s, year in enumerate(years):
        squads = careers.initial() if s == 0 else careers.window(s)
        for c, squad in enumerate(squads):
            for p in squad:
                add("ps", p, s, c, careers.ratings[p])
        formation = {c: formation_for(rng, year) for c in range(POOL)}
        _, bench_size, _ = substitution_rules(year)
        out_until: dict[int, int] = {}  # player -> last matchday he misses (injury or ban)
        fx = layer.fixtures[s]
        for i in range(fx["home"].size):
            md = int(fx["matchday"][i])
            clubs = (int(fx["home"][i]), int(fx["away"][i]))
            goals = (int(fx["home_goals"][i]), int(fx["away_goals"][i]))
            sides = []
            for c in clubs:
                missing = {p for p in squads[c] if out_until.get(p, 0) >= md}
                sides.append(pick_lineup(rng, squads[c], people.role, careers.ratings, missing, formation[c], bench_size))
            ev = player.play(year, goals, tuple(sides))
            add("m", s, md, clubs[0], clubs[1], goals[0], goals[1], ev["half_time"][0], ev["half_time"][1], *ev["added"])
            for side, period, clock, scorer, assist, own, pen in ev["goals"]:
                add("g", n_match, side, period, clock, scorer, assist, own, pen)
            for side, period, clock, off, on in ev["subs"]:
                add("s", n_match, side, period, clock, off, on)
            for side, period, clock, p, kind in ev["cards"]:
                add("c", n_match, side, period, clock, p, kind)
                if kind in (SECOND_YELLOW, RED):
                    out_until[p] = md + 1
            for p, (side, started, on_k, off_k) in ev["apps"].items():
                add("a", n_match, side, p, started, on_k, off_k)
                if rng.random() < P_INJURY:
                    out_until[p] = max(out_until.get(p, 0), md + rng.randint(*INJURY_MATCHES))
            for side, (_, bench, _) in enumerate(sides):
                for p in bench:
                    if p not in ev["apps"]:
                        add("a", n_match, side, p, False, UNUSED, FULL)
            n_match += 1

    arrays: dict[str, np.ndarray] = {}
    for t, ks in cols.items():
        for k in ks:
            dtype = np.float32 if k == "rating" else np.int32
            arrays[f"{t}_{k}"] = np.asarray(cols[t][k], dtype=dtype)
    arrays["club_q"] = layer.q.astype(np.float32)
    arrays["club_tier"] = layer.tier.astype(np.int8)
    arrays["club_rank"] = layer.rank.astype(np.int8)
    for k, v in layer.table.items():
        arrays[f"club_{k}"] = v.astype(np.int16)
    arrays["p_role"] = np.asarray([ROLES.index(r) for r in people.role], np.int8)
    arrays["p_born"] = np.asarray(people.born, np.int16)
    arrays["p_peak"] = np.asarray(people.peak, np.float32)
    meta = {
        "format_version": FORMAT_VERSION,
        "seed": seed,
        "demo_seed": demo_seed,
        "years": years,
        "clubs": layer.names,
        "founded": layer.founded,
        "players": [people.name(p) for p in range(len(people))],
        "demo_ids": {k: _pid(p) for k, p in careers.demo_ids.items()},
    }
    return History(arrays, meta)


def _pid(p: int) -> str:
    return f"P{p + 1:06d}"


def _index(pid: str) -> int:
    if not (isinstance(pid, str) and len(pid) == 7 and pid[0] == "P" and pid[1:].isdigit()):
        raise KeyError(pid)
    return int(pid[1:]) - 1


class History:
    """Read API of the league history (docs/contracts.md). Returned dicts are shared: read them, do not modify them."""

    def __init__(self, arrays: dict[str, np.ndarray], meta: dict) -> None:
        self.a, self.meta = arrays, meta
        self.years: list[int] = meta["years"]
        self.labels = [season_label(y) for y in self.years]
        self.club_names: list[str] = meta["clubs"]
        self.names: list[str] = meta["players"]
        self.ids = [_pid(p) for p in range(len(self.names))]
        self.demo_ids: dict[str, str] = meta["demo_ids"]
        self._club_index = {n: i for i, n in enumerate(self.club_names)}
        self._built = self._players_built = False

    # --- storage --------------------------------------------------------------------------------

    def save(self, out: str | Path) -> Path:
        """Write to `out/<seed>/` and return that folder."""
        folder = Path(out) / str(self.meta["seed"])
        folder.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(folder / "history.npz", **self.a)
        with gzip.open(folder / "meta.json.gz", "wt", encoding="utf-8") as f:
            json.dump(self.meta, f)
        return folder

    @classmethod
    def load(cls, path: str | Path) -> "History":
        """Read a folder written by `save` (`data/history/<seed>`)."""
        folder = Path(path)
        with np.load(folder / "history.npz") as z:
            arrays = {k: z[k] for k in z.files}
        with gzip.open(folder / "meta.json.gz", "rt", encoding="utf-8") as f:
            meta = json.load(f)
        if meta.get("format_version") != FORMAT_VERSION:
            raise ValueError(f"history format {meta.get('format_version')} in {folder}, expected {FORMAT_VERSION}: regenerate it")
        return cls(arrays, meta)

    # --- contract API ---------------------------------------------------------------------------

    def seasons(self) -> list[str]:
        return list(self.labels)

    def club(self, name: str) -> dict:
        c = self._club(name)
        top = self.a["club_tier"][:, c] == 1
        ranks = self.a["club_rank"][:, c][top]
        return {
            "name": name,
            "founded_season": season_label(self.meta["founded"][c]),
            "seasons_in_league": int(top.sum()),
            "titles": int((ranks == 1).sum()),
            "best_finish": int(ranks.min()) if ranks.size else None,
        }

    def head_to_head(self, home: str, away: str) -> list[dict]:
        """Every past league match between the two clubs, at either ground, newest first."""
        a, b = self._club(home), self._club(away)
        m = self.a
        hit = np.flatnonzero(((m["m_home"] == a) & (m["m_away"] == b)) | ((m["m_home"] == b) & (m["m_away"] == a)))
        out = []
        for i in hit[::-1]:
            out.append({
                "season": self.labels[m["m_season"][i]],
                "matchday": int(m["m_matchday"][i]),
                "home": self.club_names[m["m_home"][i]],
                "away": self.club_names[m["m_away"][i]],
                "home_goals": int(m["m_hg"][i]),
                "away_goals": int(m["m_ag"][i]),
                "half_time": {"home": int(m["m_hth"][i]), "away": int(m["m_hta"][i])},
            })
        return out

    def matches(self, where: Callable[[dict], bool] | None = None) -> list[dict]:
        """Every past match (oldest first) that satisfies `where`. A match: id, season, matchday, home, away, home_goals, away_goals, half_time, added_time, goals, substitutions, cards, lineups."""
        self._build()
        return [m for m in self._matches if where is None or where(m)]

    def player(self, player_id: str) -> dict:
        p = _index(player_id)
        if not 0 <= p < len(self.names):
            raise KeyError(player_id)
        self._build_players()
        rows = self._player_rows[p]
        return {
            "id": player_id,
            "name": self.names[p],
            "position": ROLES[self.a["p_role"][p]],
            "born": int(self.a["p_born"][p]),
            "clubs": [{"season": self.labels[s], "club": self.club_names[c]} for s, c, _ in rows],
            "goals": int(self._goals[p]),
            "appearances": int(self._apps[p]),
            "sub_appearances": int(self._sub_apps[p]),
        }

    def count(self, event: str, where: Callable[[dict], bool] | None = None) -> int:
        return sum(1 for e in self._events(event) if where is None or where(e))

    def last(self, event: str, where: Callable[[dict], bool] | None = None) -> dict | None:
        """The most recent `event` that satisfies `where`; every event carries its season, matchday, match_id and match."""
        for e in reversed(self._events(event)):
            if where is None or where(e):
                return e
        return None

    # --- additions (not in the contract yet) ----------------------------------------------------

    def clubs(self, season: str | None = None) -> list[str]:
        """Every club of the pool, or the 20 clubs of the top tier in `season`."""
        if season is None:
            return list(self.club_names)
        s = self.labels.index(season)
        return [n for c, n in enumerate(self.club_names) if self.a["club_tier"][s, c] == 1]

    def table(self, season: str, tier: int = 1) -> list[dict]:
        """Final table of `season` in `tier` (1 or 2): rank, club, played, won, drawn, lost, goals_for, goals_against, points."""
        s = self.labels.index(season)
        rows = []
        for c in np.flatnonzero(self.a["club_tier"][s] == tier):
            row = {"rank": int(self.a["club_rank"][s, c]), "club": self.club_names[c]}
            row |= {k: int(self.a[f"club_{k}"][s, c]) for k in ("played", "won", "drawn", "lost", "goals_for", "goals_against", "points")}
            rows.append(row)
        return sorted(rows, key=lambda r: r["rank"])

    def champions(self) -> list[dict]:
        """`{season, club, points}` for every season, oldest first."""
        return [{"season": label, "club": self.table(label)[0]["club"], "points": self.table(label)[0]["points"]} for label in self.labels]

    def squad(self, club: str, season: str) -> list[dict]:
        """The players of `club` in `season`: id, name, position, born, rating (0 to 100 scale, peak about 66)."""
        c, s = self._club(club), self.labels.index(season)
        hit = np.flatnonzero((self.a["ps_club"] == c) & (self.a["ps_season"] == s))
        return [{"id": self.ids[p], "name": self.names[p], "position": ROLES[self.a["p_role"][p]], "born": int(self.a["p_born"][p]), "rating": round(float(self.a["ps_rating"][i]), 1)} for i in hit for p in [int(self.a["ps_player"][i])]]

    def find_players(self, name: str, season: str | None = None) -> list[str]:
        """Ids of the players called `name` (names repeat across eras, never within a season)."""
        found = [self.ids[p] for p, n in enumerate(self.names) if n == name]
        if season is None:
            return found
        return [pid for pid in found if any(c["season"] == season for c in self.player(pid)["clubs"])]

    def player_seasons(self, player_id: str) -> list[dict]:
        """Season by season: season, club, tier, rating, appearances, sub_appearances, goals, assists (top-tier matches only)."""
        p = _index(player_id)
        if not 0 <= p < len(self.names):
            raise KeyError(player_id)
        self._build_players()
        out = []
        for s, c, rating in self._player_rows[p]:
            st = [int(x) for x in self._season_stats[p * len(self.years) + s]]
            out.append({"season": self.labels[s], "club": self.club_names[c], "tier": int(self.a["club_tier"][s, c]), "rating": round(rating, 1), "appearances": st[0], "sub_appearances": st[1], "goals": st[2], "assists": st[3]})
        return out

    def appearances(self, match_id: str) -> list[dict]:
        """Who played in a match: player, team, started, on and off as {period, clock_s} (None: from kick-off, to full time). Named substitutes who stayed on the bench are left out."""
        self._build()
        i = self._match_index[match_id]
        a = self.a
        lo, hi = np.searchsorted(a["a_match"], [i, i + 1])
        out = []
        for k in range(lo, hi):
            if a["a_on"][k] == UNUSED:
                continue
            on, off = int(a["a_on"][k]), int(a["a_off"][k])
            out.append({
                "player": self.ids[a["a_player"][k]],
                "team": SIDES[a["a_side"][k]],
                "started": bool(a["a_started"][k]),
                "on": None if on == 0 else dict(zip(("period", "clock_s"), divmod(on, 10000))),
                "off": None if off == FULL else dict(zip(("period", "clock_s"), divmod(off, 10000))),
            })
        return out

    # --- internals ------------------------------------------------------------------------------

    def _club(self, name: str) -> int:
        if name not in self._club_index:
            raise KeyError(f"unknown club {name!r}")
        return self._club_index[name]

    def _events(self, event: str) -> list[dict]:
        if event not in EVENTS:
            raise ValueError(f"event must be one of {EVENTS}, not {event!r}")
        self._build()
        return self._event_lists[event]

    def _col(self, table: str) -> list[list]:
        return [self.a[f"{table}_{k}"].tolist() for k in _COLUMNS[table]]

    def _build(self) -> None:
        """Turn the columns into match and event dicts, once (about a second for 50 seasons)."""
        if self._built:
            return
        ids, names, clubs, labels = self.ids, self.names, self.club_names, self.labels
        match_ids = [f"hist-{self.years[s]}{(self.years[s] + 1) % 100:02d}-{i:05d}" for i, s in enumerate(self.a["m_season"].tolist())]
        matches = []
        for i, (s, md, h, a, hg, ag, hth, hta, add1, add2) in enumerate(zip(*self._col("m"))):
            matches.append({
                "id": match_ids[i], "season": labels[s], "matchday": md, "home": clubs[h], "away": clubs[a],
                "home_goals": hg, "away_goals": ag, "half_time": {"home": hth, "away": hta}, "added_time": {"first": add1, "second": add2},
                "goals": [], "substitutions": [], "cards": [],
                "lineups": {"home": {"starting": [], "bench": []}, "away": {"starting": [], "bench": []}},
            })
        self._matches = matches
        self._match_index = {m["id"]: i for i, m in enumerate(matches)}
        labels_cache: dict[tuple[int, int], tuple[int, int, str]] = {}

        def base(i: int, side: int, period: int, clock: int) -> dict:
            m = matches[i]
            k = (period, clock // 60)
            if k not in labels_cache:
                labels_cache[k] = (*minute_of(period, clock), minute_label(period, clock))
            minute, stoppage, label = labels_cache[k]
            return {"season": m["season"], "matchday": m["matchday"], "match_id": m["id"], "match": m, "team": SIDES[side], "club": m[SIDES[side]],
                    "period": period, "clock_s": clock, "minute": minute, "stoppage": stoppage, "minute_label": label}

        substitutes = set()
        for i, side, p, st, _, _ in zip(*self._col("a")):
            matches[i]["lineups"][SIDES[side]]["starting" if st else "bench"].append(ids[p])
            if not st:
                substitutes.add((i, p))
        score: dict[int, list[int]] = {}
        for i, side, period, clock, scorer, assist, own, pen in zip(*self._col("g")):
            sc = score.setdefault(i, [0, 0])
            sc[side] += 1
            matches[i]["goals"].append(base(i, side, period, clock) | {
                "player": ids[scorer], "name": names[scorer],
                "assist": ids[assist] if assist >= 0 else None, "assist_name": names[assist] if assist >= 0 else None,
                "own_goal": bool(own), "penalty": bool(pen),
                "by_substitute": (i, scorer) in substitutes,
                "score": {"home": sc[0], "away": sc[1]},
            })
        for i, side, period, clock, off, on in zip(*self._col("s")):
            matches[i]["substitutions"].append(base(i, side, period, clock) | {"player_off": ids[off], "name_off": names[off], "player_on": ids[on], "name_on": names[on]})
        for i, side, period, clock, p, kind in zip(*self._col("c")):
            matches[i]["cards"].append(base(i, side, period, clock) | {"player": ids[p], "name": names[p], "card": CARD_NAMES[kind], "sent_off": kind != 0})
        for m in matches:  # rows are written in timeline order; keep it explicit
            for key in ("goals", "substitutions", "cards"):
                m[key].sort(key=lambda e: (e["period"], e["clock_s"]))
        self._event_lists = {"match": matches}
        for event, key in (("goal", "goals"), ("substitution", "substitutions"), ("card", "cards")):
            self._event_lists[event] = [e for m in matches for e in m[key]]
        self._built = True

    def _build_players(self) -> None:
        if self._players_built:
            return
        a = self.a
        n_players, n_seasons = len(self.names), len(self.years)
        used = a["a_on"] != UNUSED
        real = a["g_own"] == 0
        self._apps = np.bincount(a["a_player"][used], minlength=n_players)
        self._sub_apps = np.bincount(a["a_player"][used & (a["a_started"] == 0)], minlength=n_players)
        self._goals = np.bincount(a["g_scorer"][real], minlength=n_players)
        rows: list[list[tuple[int, int, float]]] = [[] for _ in range(n_players)]
        for p, s, c, r in zip(a["ps_player"].tolist(), a["ps_season"].tolist(), a["ps_club"].tolist(), a["ps_rating"].tolist()):
            rows[p].append((s, c, r))
        for r in rows:
            r.sort()
        self._player_rows = rows
        season = a["m_season"]
        size = n_players * n_seasons

        def per_season(players: np.ndarray, matches: np.ndarray) -> np.ndarray:
            return np.bincount(players.astype(np.int64) * n_seasons + season[matches], minlength=size)

        assisted = a["g_assist"] >= 0
        self._season_stats = np.stack([
            per_season(a["a_player"][used], a["a_match"][used]),
            per_season(a["a_player"][used & (a["a_started"] == 0)], a["a_match"][used & (a["a_started"] == 0)]),
            per_season(a["g_scorer"][real], a["g_match"][real]),
            per_season(a["g_assist"][assisted], a["g_match"][assisted]),
        ], axis=1)
        self._players_built = True


def link_demo(history: History, match_info: dict) -> dict[str, str | None]:
    """Map the demo squads (`match_info["teams"]`, ids like `home-20`) to history player ids, by name within the club's 2025/26 squad (the last season).

    The history built with the same demo seed holds every demo player, with a scripted career ending at his demo club; any other squad maps to None for the players it does not know.
    """
    last = history.seasons()[-1]
    out: dict[str, str | None] = {}
    for side in SIDES:
        team = match_info["teams"][side]
        by_name = {p["name"]: p["id"] for p in history.squad(team["name"], last)} if team["name"] in history.club_names else {}
        for p in team["starting"] + team["bench"]:
            out[p["id"]] = by_name.get(p["name"])
    return out


def frequencies(history: History) -> dict[str, float]:
    """Rare events per season (means over the seasons) and in total: the sanity figures of docs/notes/history.md."""
    n = len(history.seasons())
    counts = {k: 0 for k in ("hat_tricks", "four_or_more", "sub_braces", "sub_hat_tricks", "comebacks_from_two_down", "late_winners", "red_cards", "own_goals", "penalties", "goals")}
    for m in history.matches():
        per_player: dict[str, list[int]] = {}
        for g in m["goals"]:
            if not g["own_goal"]:
                per_player.setdefault(g["player"], [0, 0])
                per_player[g["player"]][0] += 1
                per_player[g["player"]][1] += g["by_substitute"]
        for total, by_sub in per_player.values():
            counts["hat_tricks"] += total >= 3
            counts["four_or_more"] += total >= 4
            counts["sub_braces"] += by_sub >= 2
            counts["sub_hat_tricks"] += by_sub >= 3
        counts["goals"] += len(m["goals"])
        counts["own_goals"] += sum(g["own_goal"] for g in m["goals"])
        counts["penalties"] += sum(g["penalty"] for g in m["goals"])
        counts["red_cards"] += sum(c["sent_off"] for c in m["cards"])
        hg, ag = m["home_goals"], m["away_goals"]
        if hg != ag:
            winner = "home" if hg > ag else "away"
            loser = "away" if winner == "home" else "home"
            if any(g["score"][loser] - g["score"][winner] >= 2 for g in m["goals"]):
                counts["comebacks_from_two_down"] += 1
            if abs(hg - ag) == 1 and m["goals"]:
                g = m["goals"][-1]
                if g["team"] == winner and g["minute"] >= 90:
                    counts["late_winners"] += 1
    out = {f"{k}_per_season": v / n for k, v in counts.items()}
    out |= {f"{k}_total": float(v) for k, v in counts.items()}
    out["matches"] = float(len(history.matches()))
    return out


def build(seed: int, n_seasons: int, out: str | Path) -> tuple[History, Path, float]:
    start = time.perf_counter()
    history = generate(seed, n_seasons)
    folder = history.save(out)
    return history, folder, time.perf_counter() - start


__all__ = ["History", "build", "demo_squads", "frequencies", "generate", "link_demo"]
