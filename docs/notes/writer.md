# Writer: broadcast style in three languages

A live run on the demo produced verified cards with language problems: "swing de 49 points", "l'United", "d'United", "Le but fait bondir les chances" (whose?), "a +24-point swing", "con un +51 en puntos de diferencia", "Win chance jumped..." without a team, a headline starting with "52'". The numbers were right; the text did not read like a TV graphic. This change adds style rules to the instructions, the Verifier and the templates. The number checks, the JSON format and the claims are unchanged.

## Instructions (`WRITER_INSTRUCTIONS`)

- Numbers: whole, no sign, no decimals; `_pct` with %, `_pts` always followed by points / puntos, never %.
- Every sentence with a win probability names its team (the team in the field name).
- Headline at most 8 words, no minute and no number at the start (the graphic shows minute and score). Why: one sentence, at most 25 words.
- Per language: plain English; French without article before a club name, no "swing" or "momentum" ("points", "points de probabilité", "bascule"), space before %; Spanish with the article before a club ("el Rovers", "del United", "al Town"), "opciones de ganar", "puntos".
- One good and one bad "why" per language, and the worked example now in all three languages. The example is data (`EXAMPLE_FACTS`, `EXAMPLE_REPLY`) and a test runs it through the Verifier.

## Verifier style rules

Rules are data: `LANGUAGE_RULES` (per language: code, severity, pattern; `{team}` expands to each club) and `STRUCTURAL_RULES` (checks that need the claims). Errors make the draft fail, which triggers the existing retry, then the template. Warnings are reported in `style_warnings` only. The retry now gets a note listing what was rejected (`feedback`).

| Rule | Languages | Severity | Bad example |
|---|---|---|---|
| `forbidden_word` (swing, momentum, game changer) | fr, es | error | "swing de 24 points" |
| `article_before_team` (l', d', le, la, les, du, des, au, aux + club) | fr | error | "l'United", "d'United", "du Rovers" |
| `probability_without_team` (chances, opciones, win chance... in a sentence naming no club) | all | error | "Le but fait bondir les chances." |
| `probability_number_without_team` (a number from `<team>_wp_` in a sentence without that team) | all | error | "The goal takes them from 30% to 54%." |
| `signed_number` | all | error | "+24-point swing", "+24 en puntos" |
| `decimal_percent` | all | error | "54.5%", "54,5 %" |
| `points_unit_missing` (a `_pts` number not followed by points / puntos) | all | error | "un bond de 24." |
| `points_not_from_pts_field` ("N points" where N is not a `_pts` value) | all | error | "54 points de probabilité" |
| `headline_starts_with_number` | all | error | "52' Rovers pull one back" |
| `minute_in_headline` | all | warning | "Rovers level at 52'" |
| `too_long` (headline > 8 or why > 25 words) | all | warning | |
| `jargon` ("win chance") | en | warning | "Rovers' win chance goes..." |
| `anglicism` (goal, save, clean sheet...) | fr, es | warning | "Quel goal" |
| `percent_without_space` | fr | warning | "54%" |
| `clumsy_phrase` ("en puntos de diferencia") | es | warning | |
| `league_points_verb` (take, gain, win, earn, pick up, collect, bank; prendre, gagner, empocher, décrocher, récolter, ramener; ganar, sumar, conseguir, lograr, sacar, rascar + N points; a draw as "a point", "un point", "un punto") | all | error | "En silence, Rovers vient de prendre 8 points", "Rovers pick up 24 points", "Rovers take a point" |
| `league_rate` (one match in N, N a season, N times rarer, bits, a count of 100 matches or more; "not once in 50 seasons" passes) | all | error | "about one match in 101 goes that way", "228 fois plus rare" |
| `em_dash` | all | error | the reply is normalised first (`no_em_dash`: a comma, or nothing after a punctuation mark), so the rule only catches a text checked directly |
| `if_goal_as_start` (a value from an `_if_goal` field after from, de, del, d'; "au lieu de", "en lugar del", "en vez del" pass, and so do été, plus, moins, près, loin, autour, sido, paso, lejos, cerca, más, menos, alrededor, away, far before them, where the value is the value itself: "auraient été de 70 %", "a un paso del 70%", "a goal away from 70%") | all | error | "lifted United from 18% to 34%" when 18% is the chance had the shot gone in |
| `card_kind_unbacked` (second yellow or straight red when the `card_kind` fact does not say so) | all | error | "Second yellow for X" on a card without `card_kind` |
| `player_not_named` (a red card whose headline and why do not name the player) | all, card texts | error | "United down to ten" |

Also fixed: `unknown_names` now checks headline and why separately, so the first word of "why" is no longer reported as an invented name. Word counts treat "44 %" and "Rovers :" as one word (`word_count`), so French is no longer penalised by its typography.

## On-air rules (decisions of 25/10)

- **One meaning for "points".** A gap between two chances, from a `_pts` field; never with a verb of league points. League table points are never sent to the model: `card_facts` drops them from the season context (`rovers_points_if_score_holds`), `on_air` from an insight or the storyline.
- **Rarity in seasons, for the team or the player.** The model gets an insight's facts without `one_in`, `surprisal_bits`, `history_opportunities`, `history_per_season`, `times_rarer_than_runner_up` and the runner-up's values (`OFF_AIR_FIELDS`, `on_air`); the producer still sees them in the console (`rarity.basis`, `why_first`). The templates read every fact to know which lines are true, but write none of those, and a document's own text is kept only if it passes the Verifier on the on-air facts, names nobody the facts do not and names who did it. For a feat the league sees at least once a season, the count and the rank in the history are left out too ("the 151st brace by a substitute" is a league-wide count; "only the 6th hat-trick by a substitute in 50 seasons" tells a rare feat). The instructions no longer ask for numbers "without thousands separators: 18620".
- **Offside.** Tight ("by a whisker") or clear, never a margin. `var_ruling` takes the reason and the call from the worker's `var_decision`, else from the desk's `ruled_out`; the margin only from `ruled_out.margin_cm`: `var_decision.margin_m` is never read, so it never reaches the duo or the recap document.
- **The feat and who did it.** Every detector of the insight engine has its family of texts (`worker.insight_texts`, see `docs/notes/duo.md`), so an insight's headline and why name the feat and the player or club; the generic lines of `INSIGHT_TEMPLATES`, left for a fact no family can tell, name the player or the club, or both clubs.
- **A value if goal is never a starting point**, a meaning error the model benchmark showed on saves and chances: a line in the instructions, the `if_goal_as_start` rule, and the chance templates rephrased ("One finish more, and Rovers would be on 70% to win; they stay on 40%" instead of "one finish away from 70%").
- **No em dash**, in the instructions and in the replies (normalised in `parse_reply`, refused by `em_dash`).
- **Red cards.** `card_facts` passes `card_kind` (`second_yellow`, or `straight_red` for the worker's `red`) with its meaning; templates per kind ("Second yellow for X: United are down to ten and go from 2% to 1% to win."), the generic ones when the feed does not say; every headline names the player.
- **The duo.** The instructions call it "suggested lines for the human commentary team, in two fictional voices"; facts carry `team_at_home` (only a home goal makes the stadium roar) and `previous_scorer` (a goal credited to another player: tell the correction).

## Templates, before and after (United goal 30% to 54%, save 81% vs 24%)

| | Before | After |
|---|---|---|
| en goal | United go from 30% to 54% to win: a swing of 24 points. | United go from 30% to 54% to win, up 24 points. |
| fr goal | United passe de 30 % à 54 % de chances de gagner : 24 points de bascule. | United passe de 30 % à 54 % de chances de gagner, soit 24 points de mieux. |
| es goal | {scorer} marca para United / United pasa del 30% al 54% de opciones de ganar: 24 puntos de cambio. | Gol de {scorer} para el United / El United pasa del 30% al 54% de opciones de ganar: 24 puntos más. |
| en save | {keeper} keeps United ahead (false when United trail) | Big save from {keeper} / United stay on 81% to win instead of 24% had it gone in: the save is worth 57 points. |
| fr save | {keeper} garde United devant | {keeper} s'interpose / United reste à 81 % de chances de gagner, contre 24 % si le tir était entré : l'arrêt vaut 57 points. |
| es save | La parada dejó a United con un 81% de ganar, frente al 24% si entraba | Paradón de {keeper} / El United se mantiene en el 81% de opciones de ganar, frente al 24% si hubiera entrado: la parada vale 57 puntos. |
| red card | United drop from 55% to 40% to win with ten men. | Down to ten men, United go from 55% to 40% to win. / Réduit à dix, United passe de 55 % à 40 %... / Con diez, el United pasa del 55% al 40%... |
| chance (es) | Ocasión clara para United | Ocasión clara del United / Un gol habría llevado al United del 40% al 70%... |

French templates use a no-break space before % and :. Every template passes every check, with no warning, for all four moment kinds, both teams and probabilities at 1% and 99% (`tests/test_writer.py`).

## Still to review by a native speaker

- French: "Rovers passe" (club treated as singular) and "Réduit à dix, United passe" (masculine agreement); "soit 24 points de mieux" vs "24 points de probabilité" vs "bascule de 24 points"; whether a TV graphic would rather say "les Rovers" (the rule forbids it, by request).
- French: "de United" is allowed (no elision before the /j/ sound) while "d'United" fails; check that reading.
- Spanish: the article before English club names ("el Rovers", "al United") and "Paradón" for every save; "si hubiera entrado" vs "si entraba".
- English: "United go" / "Rovers stay on" (British plural agreement); "up 24 points" for a win-probability change.
- The probability-word lists (`PROBABILITY_WORDS`) are short; French "chance" in the sense of luck would also require a club in the sentence.
- Whether the stricter checks raise the template fallback rate on a live run: measure it at the next Foundry run (the tests made no model call).
- `league_rate` is a pattern list: a rate written in words ("twice a season" is caught, "every other season" is not) or a new turn of phrase can slip through; the number checks still refuse any number the facts do not hold.
- `if_goal_as_start` looks at the words just before the number: a turn of phrase where the value is the value itself and whose word is not in `NOT_A_START` would be refused, sending a right answer to the retry. Measure it at the next Foundry run.
