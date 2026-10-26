# Synthetic League

A light league simulated at score level (N1): 20 fictional clubs, one score per fixture, no event feed. It gives the demo match a season around it (positions, points, what is at stake) and, later, the pre-match expected goals of the demo fixture. Code in `src/league/`, tests in `tests/test_league.py`.

## Model

| Piece | Choice |
|---|---|
| Clubs | Varleigh and Yarnford (the demo clubs) plus 18 invented names, none sharing a word with a current Premier League club (`league/clubs.py`) |
| Strengths | Log scale: home rate = exp(mu + h + attack[home] + defence[away]), away rate = exp(mu + attack[away] + defence[home]); a high defence concedes more |
| Distribution | Bivariate normal on (attack quality, defensive solidity), sigma 0.30 each, correlation 0.8 |
| Elite tier (option) | The two strongest clubs (attack plus solidity) gain 0.13 on both, the others drawn with sigma 0.28 |
| Home advantage | Home rate 1.25 times the away rate (h = log 1.25) |
| Base rate | mu set for 2.85 goals a match, corrected for Jensen's effect: divided by E[exp(attack)] E[exp(defence)], i.e. exp(sigma^2) without the elite tier, a fixed-seed Monte Carlo estimate with it |
| Scores | Independent Poisson, then Dixon-Coles (rho = -0.10) by rejection sampling: a draw is kept with probability tau / max tau, rejected ones are drawn again |
| Format | Double round robin (circle method, calendar shuffled by the seed), 3-1-0, ranking by points, goal difference, goals scored, then club index |
| Seeds | `League.create(seed)` spawns four independent streams (strengths, prior, calendar, results); every Monte Carlo has its own generator keyed on (seed, matchday, fixture) |

## Calibration

`python -m league --seed 1 --calibrate 1000` (0.1 s; 0.2 s with `--elite`). Each season draws new strengths. The reference values are the ones of our design notes, recalled and not yet checked against published tables: they must be checked before being quoted in public.

| Measure | Target | Band (test) | Normal | Elite tier |
|---|---|---|---|---|
| Champion's points | 88 (87.6 over 1995/96 to 2024/25; 92.6 from 2016/17) | 84 to 93 | 88.5 | 92.6 |
| 18th club's points | 30 | 26 to 34 | 30.4 | 30.8 |
| Standard deviation of points | 18.5 (15 to 21 by season) | 16 to 21 | 18.5 | 18.8 |
| Draws | 24 % (21 to 25 %) | 21 to 27 % | 24.0 % | 24.0 % |
| Home wins | 44 % (41 to 48 %) | 41 to 48 % | 43.8 % | 43.9 % |
| Points per goal of goal difference | 0.56 (about 0.6) | 0.50 to 0.65 | 0.559 | 0.554 |
| Goals per match | 2.85 | 2.79 to 2.91 | 2.85 | 2.85 |
| Seasons with a club on 95 or more | 4 in 30 (13 %) | none | 20.5 % | 39.9 % |
| Seasons with two clubs on 95 or more | 1 in 30 | none | 1.1 % | 4.9 % |
| Strongest club champion | 60 to 70 % (our earlier simulation) | none | 62 % | 72 % |

The normal setting reproduces the earlier local simulation (88.3, 30.3, 18.5, 24.3 %, 43.7 %, 0.56). The elite tier reproduces its elite figures (92.4 points, 39.1 % and 4.3 %) only with the reading "the two strongest clubs gain 0.13": two clubs picked at random and shifted by 0.30 give 89.0 points and 25 % of seasons with a club on 95, because a +0.30 shift is about one standard deviation, below where the best club of 20 already sits. The tier is off by default.

## Observer and context

- **Prior.** The public pre-season prior is the truth plus a normal noise of 0.15 on each attack and defence.
- **Update.** After each matchday, each club's scoring and conceding rates are updated by a gamma-Poisson step, with the opponents held at their prior: rate times (alpha + goals) / (alpha + expected goals), alpha = 1 / 0.15^2. The mean error on the log rates falls from about 0.21 before the season to about 0.15 after 30 matchdays (seeds 1 to 5), with no bias on the mean rate.
- **`season_state(league, matchday)`.** The table after `matchday` matchdays: rank, record, goals, points, `points_behind_leader`, `points_from_top4`, `points_clear_of_drop_zone`, and the chances of the title, the top four and relegation (18th to 20th) from 4,000 Monte Carlo runs of the remaining fixtures with the observer's strengths. `field_meanings` explains each field.
- **`match_importance(league, home, away, matchday)`.** For a fixture still to play: each club's chances if it wins (1-0) and if it loses (0-1), with the other fixtures simulated once (common random numbers), and the stake = chance if it wins minus chance if it loses. The stake is positive for the title and the top four and negative for relegation in every run, not only on average.

`python -m league table --seed 1 --matchday 30` prints the table with the three chances.

## Coupling with the demo match

`league.demo_fixture(seed, matchday)` pins Varleigh (home) v Yarnford on `matchday`, by swapping two rounds of the calendar (strengths and every result stay the same), and returns:

- `expected_goals`: from the true strengths, for `generator.generate_match`;
- `prior_expected_goals`: from the observer after the previous matchday, for the win-probability model (`match_info`). The model never reads the generator's truth, so a match does not confirm its own prior;
- `context`: ranks, points, played, points gaps, and for each club and each event (title, top4, relegation) `*_chance_pct`, `*_chance_if_win_pct`, `*_chance_if_loss_pct` and `*_stake_pct_points`, as integers, with `field_meanings`.

The worker reads the season of seed 9 at matchday 30 for the storyline, the season facts of the cards and the recap (`--league-seed 9 --matchday 30`, the container default); the generator keeps its own expected goals for the demo match (1.5 and 1.2). `python -m league demo --seed 1 --matchday 30` shows what the league gives for another seed. On seed 1, Varleigh are 16th (28 points), Yarnford 3rd (57), true expected goals 0.98 and 1.79, which does not fit a 5-2 home win. Seeds that would fit the demo better on matchday 30 (1,000 simulations):

| Seed | Varleigh | Yarnford | Expected goals (truth) | Prior (observer) | Largest stakes |
|---|---|---|---|---|---|
| 9 | 17th, 29 pts | 18th, 28 pts | 1.45 - 1.07 | 1.55 - 1.04 | relegation: -16 points for Varleigh, -25 for Yarnford |
| 22 | 15th, 33 pts | 17th, 26 pts | 1.48 - 1.14 | 1.62 - 1.08 | relegation: -27 points for Yarnford |
| 16 | 7th, 47 pts | 1st, 59 pts | 1.26 - 1.37 | 1.14 - 1.37 | title: +26 points for Yarnford; top four: +19 for Varleigh |
| 30 | 3rd, 55 pts | 6th, 49 pts | 1.69 - 1.38 | 1.20 - 1.11 | top four: +23 for Varleigh, +31 for Yarnford |

Seed 9 is a relegation six-pointer with expected goals close to the demo's 1.5 and 1.2. After the match, `league.with_result("Varleigh", "Yarnford", 5, 2)` puts the generated score into the season.

## How to run

```
.venv\Scripts\python.exe -m pytest -q tests/test_league.py     # 13 tests, about 4 s
python -m league --seed 1 --calibrate 1000 [--elite]
python -m league table --seed 1 --matchday 30 [--sims 4000]
python -m league demo --seed 1 --matchday 30
```

`league*` is in `[tool.setuptools.packages.find] include` of `pyproject.toml`, so `python -m league` works from an installed checkout. The tests do not need it (`pythonpath = ["src"]`).

## What is imperfect

- The reference values are recalled, not sourced; the bands are wide enough to pass on any of them. They are not quoted in public.
- mu and h are fixed, not drawn per season (season environment), and there are no promoted clubs or strengths carried from one season to the next.
- Strengths are constant through the season: no injuries, form or managers (all below the noise of about 7 points per club and season, or channelled elsewhere in the design).
- Tie-breakers stop at goals scored, then club index; the Premier League goes on to head-to-head points, away goals in head-to-head and a play-off.
- The observer's update holds opponents at their prior and treats attack and defence separately: a cheap one-step approximation, not a fitted Dixon-Coles model. It reads goals, not expected goals.
- The forced result in `match_importance` is 1-0 or 0-1; a bigger win would move goal difference a little more.
- The Dixon-Coles weight is clipped at zero for a side expected to score more than 10 goals (never reached in practice).
- Some seeds give extreme clubs (seed 1: Caldermouth Harriers on 8 points after 30 matchdays), as the normal tail allows: a matter of seed choice for the demo.
- Expected goals at N1 and in the generator are not the same thing yet: the generator's unscripted matches average 3.0 goals for about 2.8 expected before kick-off, so feeding league rates would need the generator's scale checked first.
