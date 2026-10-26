# Insight engine

Facts about the match in progress, measured against the match itself and the 50-season league history, ranked by how rare they are. Code in `src/worker/insights.py` (engine and detectors), `src/worker/insight_rarity.py` (rarity and value), `src/worker/insight_history.py` (history view); tests in `tests/test_insights.py` with a small fake History.

## How it runs

`Pipeline(history=...)` (CLI: `python -m worker <replay> --history data/history/<seed>`) creates one `InsightEngine` per match next to the Spotter. After the Spotter has handled a message, the engine runs if the message is relevant: a goal or a red card (as the action now stands), a substitution, a VAR check, a period end, full time, a correction. Every detector reads the match as it stands (goals that count, scorer after reassignment, substitutes, red cards, shots), so a late, repeated or corrected message gives the same facts as a clean feed. Without `history` nothing changes in the pipeline.

Each insight document goes to `sink.insight(doc)`: `MemorySink.insights`, `data/desk/<match_id>/insights.json` for `JsonFileSink`, the `cards` container with `type: "insight"` for `CosmosSink`, the ordered lane of `ThreadedSink`. When the desk has a `write_insight(doc, teams)` method, new or changed insights go through it for their texts; otherwise `texts` stays absent. The worker's desk writes them with the templates (`--insights-writer template`, the default): live, the model only writes the cards and the recap; `--insights-writer writer` sends the insights to the card writer, within the same budget of 40 model calls per match. Each step of an insight's life also goes to `sink.audit_entry`, by `insights`. Insights are not returned by `Pipeline.handle` (which keeps returning cards and card actions); `Pipeline.insights(match_id)` lists them.

## Catalogue

Each detector proposes facts with a `group` (what it is about) and a `level` (how far it went); the key is `<group>-<level>`.

| Detector | Group, level | Kind | When | Counted in the history (occurrences / opportunities) |
|---|---|---|---|---|
| `scorer_milestones` | `scorer-<player>`, goals (2, 3, 4...) | milestone | each goal that counts | matches where one player scored N or more / matches; for a player who came off the bench before his first goal, substitutes only |
| `fastest_hat_trick` | `hat-trick-speed-<player>`, minutes from 1st to 3rd goal | record | third goal | hat-tricks completed within as many minutes / hat-tricks |
| `comebacks` | `comeback-<side>`, `<deficit>-<stage>` (1 level, 2 ahead, 3 won) | rarity | goal, full time | teams D or more down that reached the stage / teams that trailed by D or more; plus the club's last time (`club_last_season`) |
| `half_time_lead_lost` | `ht-lead-lost-<side>`, 1 trailing now, 2 lost | streak | second-half goal, full time | half-time leads of L or more that fell behind (or lost) / half-time leads of L or more; plus the club's last such defeat at the same venue (`club_seasons_since`) |
| `late_goals` | `late-<event>`, equaliser or go-ahead | rarity | goal from the 85th minute | matches with such a goal from the 85th minute / matches |
| `red_card_effects` | `ten-men-<side>`, scored or won | rarity | goal after a red, full time | teams with a player sent off that scored after it (or won) / teams with a player sent off |
| `runs` | `run-<unbeaten or winless>-<side>`, ended or extended and length | streak | full time | runs of that kind of L or more / all runs of that kind; the run is the club's run at the end of the history |
| `head_to_head` | `h2h-drought-<side>` (seasons without a win at this ground), `h2h-margin-<side>` (biggest win over this opponent, new or equalled) | streak, record | full time | wins at that venue after G or more seasons without one / wins at that venue; wins by M or more / matches |
| `team_records` | `goals-total` (6 or more in the match), `club-goals-<side>` (club record equalled or beaten) | record | goal | matches with T or more goals / matches; teams scoring G or more / team matches |
| `match_kpis` | `xg-<p1 or ft>-<side>`, `shots-<p1 or ft>-<side>`, `possession-swing` | tactical | half-time, full time | none: a model probability against the match itself (below) |

The KPI facts are measured against the match norm, since the history holds scores, not shots:

- goals against xG: Poisson probability of scoring at least G from the xG of the team's shots (voided goals excluded), when G is 3 or more and G minus xG at least 1.5;
- shots: binomial probability of taking at least S of the N shots when the team's expected share is its share of the pre-match expected goals, when N is 8 or more and the share 75 % or more;
- possession: change in the share of the passes between the halves, 12 points or more, against a normal law with one possession counted as one trial.

Every number a text may show is in `headline_facts`, with its meaning in `field_meanings`: the fact's own fields, then `one_in`, `surprisal_bits`, `history_count`, `history_opportunities`, `history_since`, and `last_season`, `last_match`, `last_player` when there is a last time. Some of them are for the producer only: the desk hands the writers `headline_facts` without `worker.desk.OFF_AIR_FACTS` (bits, the matches counted, the stat of the match's ratio, desk values), and checks their texts against what it handed them; the stored insight keeps everything for the console.

## Rarity and value

- p = (k + 1) / (n + 2) (Laplace), k occurrences over n opportunities. A fact never seen gets a finite p that shrinks with the size of the history, and `basis` says "never before".
- surprisal_bits = -log2(p).
- value = round(100 x (0.75 x min(bits / 12, 1) + 0.25 x relevance)). Relevance is the win-probability move of the moment the fact is tied to (the Spotter card's `value_pts` / 40, capped at 1; 0.3 when no card), 1 for a result fact at full time (comeback won, half-time lead lost), 0.6 to 0.8 for runs and head-to-head, 0.2 to 0.4 for records and KPIs.
- `basis`: "what: k in n opportunities since <first season>", then "never before" or "last time <season>, <match> (<player>)" from `History.last`.

Worked example, the demo match against the test history (12 seasons, 4,560 matches). At 83' substitute #20 scores his fourth goal that counts (52', 57', 66', 83'; the 63' goal was ruled out). No substitute scored four in the history: k = 0, n = 4,560, p = 1 / 4,562, 12.16 bits, over the 12-bit cap, so the rarity part is 75. The goal took Varleigh from 91 % to 99 %, a card worth 8 points: relevance 8 / 40 = 0.2, 5 points. Value 80. Against the real history (about 19,000 matches) the same fact would be 14.2 bits, still capped. The comeback from 0-2 to a 5-2 win is "teams 2 or more goals down that won: 44 in 2,072", p = 45 / 2,074, 5.5 bits, value 60 with the full-time relevance of 1.

## Lifecycle

- New group: proposed if its value reaches `min_value` (40) and its bucket (first half, second half, full time) has had fewer than `cap_per_period` (4) new groups; beyond the cap it replaces the weakest insight of the bucket still `proposed` or `pending` if it is worth more (`supersede_cause: outranked`), otherwise it is not written. A group admitted once is not capped again.
- New level in a group: the previous insight becomes `superseded`, with `superseded_by` and `supersede_cause` (`updated`, `goal ruled out`, `correction`).
- Group no longer true (its goal was voided, or the score moved on): superseded with no successor, `supersede_cause` `goal ruled out` when a goal it counted was voided (whichever message released the void), `no longer true` otherwise. If a correction or a later goal brings a level back, its insight is revived as a new version, `proposed` again.
- Same key, facts changed: new version, back to `proposed` (an approved text would show old numbers).
- VAR check on a goal an insight rests on: `under_review` until the decision. The engine runs the detectors again with that goal left out, as if VAR ruled it out (`InsightEngine.hinged`); an active insight that would then be withdrawn or would change its facts is flagged. That covers the insights that count the goal and those that only state the score it makes: a team down to ten that scored, with the current score, is under review while the other team's goal is checked.
- Before changing an insight, the `under_review` flag alone included, the engine reads the stored one (`load_insight`) and keeps the producer's `status`, `texts` and `history` of that version, as the pipeline does for cards: a fact approved in the console stays approved while VAR checks a goal it rests on. A version the engine already changed for the same message (a goal that updates a fact and flags it) is not in the store yet and keeps the decisions taken when it was changed.
- A comeback is told at the current score too: stage 1 ("level") while the team is level, reached by its own goal, stage 2 ("ahead") while it leads, stage 3 if it won at full time. Behind again, or pegged back to level after leading, the insight is superseded (`no longer true`): "now lead after trailing 0-2" would be false. It is revived when the team goes ahead again or the goal against it is ruled out. Its `score` is the score of the goal that last brought the team to that stage. The history counts stay those of the stage reached at any point (`teams 2 or more goals down that went ahead`).
- A lost half-time lead is told at the current score: stage 1 ("now trail") only while the team trails, stage 2 at full time if it lost. Level again, the insight is superseded (`no longer true`); it is revived when the team trails again or the equaliser is ruled out. Its `score` is the score of the goal that last put the team behind.
- Every step (`proposed`, `updated`, `revived`, `superseded`, the last with `cause`, `superseded_by` and `was_on_air`) is an audit entry by `insights`, next to the desk's `written` and `approved`: a fact VAR takes back is in the audit trail, with whether it was on air (approved in the store the console and the overlay read).

In the demo the scorer group goes brace (57') to hat-trick (63'), back to brace when VAR rules the 63' goal out (64'), hat-trick again (66', version 2), four (83'). Yarnford's lost half-time lead (0-2 at the break) is proposed at 63', superseded at 64' (`goal ruled out`), revived at 66', superseded at 69' when Yarnford equalise (`no longer true`), revived at 70' when VAR rules that goal out (version 3), and superseded at full time by the defeat (`ht-lead-lost-away-2`). Varleigh's comeback is told level at 57' (2-2), ahead at 63', level again at 64' when VAR rules the 63' goal out, ahead again at 66' (version 2 of `comeback-home-2-2`, score 3-2), withdrawn at 69' when Yarnford are level (`no longer true`), revived at 70' (version 3), and superseded at full time by the win (`comeback-home-2-3`).

Fields beyond the contract table: `group`, `version`, `evidence` (goals the fact counts), `under_review`, `superseded_by`, `supersede_cause`, `last_time`; on the stat of the match, `stat_of_the_match`, `why_first` and `ranked`.

## Stat of the match

At full time (and again after a late correction) `insight-<match_id>-stat-of-the-match` takes the top fact by value, then surprisal, among the candidates worth at least `min_value`, including those the cap kept off the desk. Its `headline_facts` are the top fact's, plus `top_fact`, `top_value`, `runner_up`, `runner_up_value`, `runner_up_surprisal_bits` and `times_rarer_than_runner_up` (2 to the power of the difference in bits); `why_first` says it in a sentence, `ranked` lists the top five with their basis.

## History records read

Only the contract API (`seasons`, `matches`, `head_to_head`, `last`, `player`). One `matches()` scan per pipeline; counts are cached by query. Match records are read with tolerant accessors: `home_goals`/`away_goals`, `half_time` as `{home, away}`, a pair or "1-0", `goals` (or `scorers`) with `team` (`home`/`away` or the club), `player_id` (or `player` as an id or `{id}`), `minute`, `own_goal`; `substitutions` with `player_in` and `minute`; `cards` with `card` `red` or `second_yellow`. `History.last("match", where)` may return the match or `{season, match}`. `python -m worker --history PATH` loads the history with the first of `league.history.load`, `History.load`, `History.from_dir`, `History(path)` that exists.

## Limits

- The fake History in the tests is random, not the real league: the counts above are those of the tests. The real history module may name fields differently; the accessors in `insight_history.reduce` are the one place to adapt.
- Live players (`home-20`) have no history id, so there is no career fact (first goal for the club, 100th goal).
- Runs start at the end of the history: the season of the demo match is not in it.
- History minutes are taken as given; first-half added time may be recorded as 45. "Late" means the 85th minute or later.
- Conditional rarities (comebacks, half-time leads, ten men) count over their opportunities, match-level ones over all matches; the two kinds of p are not on the same footing, and the value formula does not correct for it.
- KPI probabilities are model probabilities (Poisson, binomial, normal) with independence assumptions that real football breaks; the possession model in particular treats each possession as an independent trial.
- Weights (75/25, 12-bit cap, relevance per detector), `min_value` 40 and the cap of 4 per period are hand-set, not fitted to what producers keep.
- Many related facts can describe one story (Varleigh's comeback and Yarnford's lost lead): both are kept, the cap and the ranking decide.
- A lost half-time lead or a comeback withdrawn by an equaliser that VAR then rules out comes back as a new version: with a producer, it waits for approval again although its facts are those it had (in the demo, `ht-lead-lost-away-1` and `comeback-home-2-2` at 70'). In auto-approve mode it goes back on air at once.
- `headline_facts` also carry league-wide figures for the console. What a writer gets goes through two filters: the desk drops `OFF_AIR_FACTS` (bits, the matches counted, the stat of the match's ratio, desk values), and `writer.on_air` drops the league-wide rates too (`one_in`, `history_per_season`, `OFF_AIR_FIELDS`). The texts of `worker.insight_texts` tell a rarity in seasons, for the team or the player, and the Verifier refuses a league-wide rate, ratio or count of matches (`league_rate`). The producer still sees those figures in the console, under "Producer details".
- While a goal is under VAR check, the detectors run twice for each relevant message (with and without the goal).
