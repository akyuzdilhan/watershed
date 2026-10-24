# League history (Lot 2)

A fictional history of the Synthetic League: 50 seasons, 1976/77 to 2025/26, 32 fictional clubs over two tiers, about 4,000 fictional players with careers, and the 19,000 top-tier matches with scorers, assists, substitutions, cards and half-time score. It is what the desk counts against to say "the first time since 1983/84 that a substitute scores four". Code in `src/league/history*.py`, tests in `tests/test_history.py`, read API as in `docs/contracts.md` ("Lot 2 additions").

## Model

Three layers, each with its own random stream, all derived from the seed.

| Layer | Module | Choice |
|---|---|---|
| Clubs | `history_clubs.py` | 32 clubs: the 20 of `league.clubs` plus 12 invented names (none shares a word with a current Premier League club). 20 in the top tier, 12 in a second tier, 3 down and 3 up every season |
| Strength | | Latent quality q = club mean (sd 0.22) + regime + AR(1) deviation (persistence 0.70, innovation 0.13). A regime is a boom or a decline: 6 % chance per club and season, size sd 0.28, fading by 0.88 a season. Attack and solidity are q plus a per-season style noise (sd 0.13), which gives the league model's correlation of about 0.8 |
| Scores | | `league.model.sample_scores` (Poisson with Dixon-Coles, rho -0.10), double round robin, `league.table` ranking. mu is set per season so that the top tier averages an era level (2.70 goals a match in 1976, 2.55 around 2002, 2.88 in 2025); home ratio 1.45 in 1976 down to 1.22 in 2025. The second tier is played at score level only (2.60 goals a match) and has no match detail |
| Names | | Matched after the simulation: the 20 clubs of the season after the last one (17 survivors plus 3 promoted) get the 20 names of `league.clubs` by strength rank against `League.create(seed)`, the club the 2026/27 league makes strongest is the strongest entering it in the history. Rovers or United matched to a promoted club swap names with the closest-ranked club that stayed up |
| Story search | | Up to 40 deterministic attempts per seed; the first where Rovers and United each spent 40 % of the seasons in the top tier and won at least one title between them is kept |
| Players | `history_players.py` | Ids `P000001` upwards, names from the generator pools (48 x 72 combinations: they repeat across eras, never among the players active in a season; the 40 demo names are reserved). Role (the generator's ten roles), birth year, peak rating N(66, 6.5) reached at 27 to 29 (minus 0.22 per squared year before 27, 0.35 after 29), a form term, a finishing trait and a booking trait |
| Careers | | Squads of 25 (3 keepers, 8 defenders, 8 midfielders, 6 forwards). Each summer: retirements (drawn age 33 to 37, earlier at 30 or more with a rating under 50), listings (7 %, plus 35 % for a good player at a club outside the top 6 and for a fringe player at a strong club), two academy players per club (age 17 to 19, a little better at stronger clubs), then a draft: strongest club first, best value first (rating, or potential under 23). Unsigned players stay free one season, then leave |
| Matches | `history_matches.py` | Line-ups by rating with a little rotation, injury spells (3.5 % per appearance, 1 to 6 matches) and a one-match ban after a red. Formations by era (4-4-2 before 1995, then 4-3-3 and 4-2-3-1). Then a timeline: substitution times, bookings (0.6 per team in 1976 to 1.95 in 2025), straight reds (3 % per team), second yellows (4 % of bookings), goal times with a density rising through the match, added time by era. Each event picks a player among those on the pitch at that moment; a goal by weight role x finishing x exp((rating - 68) / 14), 7.5 % penalties, 3 % own goals, 72 % of other goals assisted |
| Substitutions | | By era, so that "since 1983/84" means something: 1 until 1986/87, 2 until 1994/95, 3 until 2019/20 (bench of 5, then 7), 5 from 2020/21 (bench of 9), used 3 to 5 |

## Demo link

- `demo_squads()` rebuilds the demo squads exactly as `generate_match(7, "demo")` does (`make_teams` on the same first draws; checked against the feed's `match_info` in the tests).
- The 40 demo players get the first ids and a scripted career: starters born 1991 to 2003 (keepers 1988 to 2000), peak about 70; bench players born 1994 to 2006, peak about 66; debut at 18, zero to two other clubs, then the last 0 to 7 seasons (bench 0 to 4) at their demo club, including 2025/26. Their matches are played by the same model as everyone else.
- `link_demo(history, match_info)` maps `home-20` and the others to their history ids by name in the club's 2025/26 squad (`History.demo_ids` holds the same mapping). With seed 9, #20 Dorian Lightfoot (P000020), born 2001, came through Hethermoor Borough and Gorsedale and has 14 league goals in 125 appearances for Rovers, 19 as a substitute.
- Seed 9 of the history fits seed 9 of the league: the 2026/27 clubs are exactly `league.clubs`, ordered by strength like `League.create(9)`, which puts Rovers 17th and United 18th on matchday 30. In the history Rovers have 49 top-tier seasons and 3 titles, United 30 seasons and the 2023/24 title; in 2025/26 they finished 13th and 15th. Gap: the history's 2026/27 strengths are not the league's, only their order; the league season itself is still `League.create(9)`.

## Rare events over 50 seasons (seed 9)

`python -m league history --seed 9` prints these; `league.history.frequencies` computes them. The Premier League references are rough recalled orders of magnitude, not sourced.

| Event | Per season | Total, 50 seasons | Premier League, rough | Test band (50 seasons) |
|---|---|---|---|---|
| Goals | 1,028 (2.70 a match) | 51,390 | 2.5 to 2.9 a match by era | |
| Hat-tricks | 12.6 | 632 | about 8 to 15 | 8 to 18 |
| Four goals or more by one player | 1.7 | 85 | about 1 | 0.5 to 4 |
| Substitute scoring twice | 3.0 (1.2 before 1987, 5.3 since 2020) | 151 | a few | 1 to 6 |
| Substitute hat-trick | 0.1 | 5 | rare, a handful in 30 seasons | 20 at most |
| Substitute scoring four | 0 | 0 | very rare | |
| Win after being two goals down | 2.3 | 114 | about 3 to 6 | 1 to 6 |
| Winner in added time at 90' (by one goal) | 12.2 | 609 | | 5 to 25 |
| Red cards | 49 (38 before 1987, 60 since 2020) | 2,470 | 40 to 60 | 30 to 70 |
| Own goals | 29 | 1,474 | 30 to 40 | |
| Penalties scored | 75 | 3,771 | 70 to 90 | |

By era: substitutes score 2 % of the goals before 1987, 5 % in 1987 to 1994, 8 % in 1995 to 2019 and 11 % since 2020. Top scorer of a season: 27 goals on average (19 to 42). Best career total: 266 league goals. Ratings peak at 27 to 29 (68.9 on average), players are 17 to 37, half of the careers that end inside the window end at 33 or later (the others are released earlier). Home wins fall from 47.6 % (1976 to 1986) to 44.7 % (since 2020), draws 24 to 25 %.

## API

`History.load("data/history/9")` reads the arrays in under 0.1 s; the first query builds the match and event dicts (about 1 s for 50 seasons), then they are cached. Contract methods: `seasons, club, head_to_head, matches, player, count, last`.

- `head_to_head(a, b)` returns the matches at both grounds, newest first; `half_time` is `{home, away}`.
- A match: `id` (`hist-197677-00012`), `season`, `matchday`, `home`, `away`, `home_goals`, `away_goals`, `half_time`, `added_time {first, second}`, `goals`, `substitutions`, `cards`, `lineups {home, away: {starting, bench}}` (player ids; the bench lists every named substitute).
- Events (`count` and `last` pass them to `where`): `season`, `matchday`, `match_id`, `match` (the match dict), `team` (`home` or `away`), `club`, `period`, `clock_s`, `minute`, `stoppage`, `minute_label` (`90+3'`), then for a goal `player`, `name`, `assist`, `assist_name`, `own_goal` (credited to `team`, scored by an opponent), `penalty`, `by_substitute`, `score` after the goal; for a substitution `player_off`, `player_on` and names; for a card `player`, `name`, `card` (`yellow`, `second_yellow`, `red`), `sent_off`.
- `player(id)`: `clubs` lists every season at a club, second tier included; `goals` (own goals excluded), `appearances` (starts plus substitute appearances) and `sub_appearances` count top-tier league matches only.
- `founded_season` is a founding year written as a season label (`1866/67`), not the first season in the history.

Additions, not in the contract yet: `clubs(season=None)`, `table(season, tier=1)`, `champions()`, `squad(club, season)` (with ratings), `find_players(name, season=None)`, `player_seasons(id)` (club, tier, rating, appearances, goals, assists per season), `appearances(match_id)` (on and off times), `demo_ids`, and the module functions `generate`, `build`, `demo_squads`, `link_demo`, `frequencies`.

## How to run

```
PYTHONPATH=src python -m league history --seed 9 --seasons 50 --out data/history   # about 8 s, writes data/history/9/ (2.5 MB)
.venv\Scripts\python.exe -m pytest -q tests/test_history.py                          # 12 tests, about 10 s (one of them generates the 50 seasons)
```

`data/history/` is gitignored: regenerate it on a new machine. Same seed, same `--seasons`, same files. Changing `--seasons` changes the whole history (the first season is drawn differently).

## Limits

- Scores do not react to the match: no game-state effects (a team two down does not push), no effect of a red card or of who plays on the score. The score comes first and the events are placed around it, so comebacks are a little rarer than in real football and a team with ten men scores as often.
- Club strength drives results; players only decide who scores, assists and gets booked. A club that drafts better players does not win more for it (rating and club strength correlate at about 0.3 through the draft).
- The second tier has 12 clubs and no match detail; a promoted club's season below is a table row only. Players' second-tier seasons count in `clubs`, not in appearances or goals.
- 3 points for a win throughout (England moved from 2 points in 1981/82), 20 clubs throughout, 3 promoted directly (no play-off). Tie-breakers as `league.table`.
- No cups, no Europe, no dates: "for 8 years" has to be read as seasons. No managers, no transfer fees, no loans, no nationality.
- Every player plays in one role all his career; out-of-position starts happen only when a role is short.
- The era shapes (goals, home advantage, bookings, substitution rules) and the Premier League figures above are recalled, not sourced: check them before any public claim that compares with real football.
- The 2026/27 season of `League.create(seed)` is consistent with the history in its clubs and their strength order, not in the strengths themselves.
