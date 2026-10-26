# Writer: broadcast style in three languages

A live run on the demo produced verified cards with language problems: "swing de 49 points", "l'Yarnford", "d'Yarnford", "Le but fait bondir les chances" (whose?), "a +24-point swing", "con un +51 en puntos de diferencia", "Win chance jumped..." without a team, a headline starting with "52'". The numbers were right; the text did not read like a TV graphic. This change adds style rules to the instructions, the Verifier and the templates. The number checks, the JSON format and the claims are unchanged.

## Instructions (`WRITER_INSTRUCTIONS`)

- Numbers: whole, no sign, no decimals; `_pct` with %, `_pts` always followed by points / puntos, never %.
- Every sentence with a win probability names its team (the team in the field name).
- Headline at most 8 words, no minute and no number at the start (the graphic shows minute and score). Why: one sentence, at most 25 words.
- Per language: plain English; French without article before a club name, no "swing" or "momentum" ("points", "points de probabilité", "bascule"), space before %; Spanish with the article before a club ("el Varleigh", "del Yarnford", "al Town"), "opciones de ganar", "puntos".
- One good and one bad "why" per language, and the worked example now in all three languages. The example is data (`EXAMPLE_FACTS`, `EXAMPLE_REPLY`) and a test runs it through the Verifier.

## Verifier style rules

Rules are data: `LANGUAGE_RULES` (per language: code, severity, pattern; `{team}` expands to each club) and `STRUCTURAL_RULES` (checks that need the claims). Errors make the draft fail, which triggers the existing retry, then the template. Warnings are reported in `style_warnings` only. The retry now gets a note listing what was rejected (`feedback`).

| Rule | Languages | Severity | Bad example |
|---|---|---|---|
| `forbidden_word` (swing, momentum, game changer) | fr, es | error | "swing de 24 points" |
| `article_before_team` (l', d', le, la, les, du, des, au, aux + club) | fr | error | "l'Yarnford", "d'Yarnford", "du Varleigh" |
| `probability_without_team` (chances, opciones, win chance... in a sentence naming no club) | all | error | "Le but fait bondir les chances." |
| `probability_number_without_team` (a number from `<team>_wp_` in a sentence without that team) | all | error | "The goal takes them from 30% to 54%." |
| `signed_number` | all | error | "+24-point swing", "+24 en puntos" |
| `decimal_percent` | all | error | "54.5%", "54,5 %" |
| `points_unit_missing` (a `_pts` number not followed by points / puntos) | all | error | "un bond de 24." |
| `points_not_from_pts_field` ("N points" where N is not a `_pts` value) | all | error | "54 points de probabilité" |
| `headline_starts_with_number` | all | error | "52' Varleigh pull one back" |
| `minute_in_headline` | all | warning | "Varleigh level at 52'" |
| `too_long` (headline > 8 or why > 25 words) | all | warning | |
| `jargon` ("win chance") | en | warning | "Varleigh's win chance goes..." |
| `anglicism` (goal, save, clean sheet...) | fr, es | warning | "Quel goal" |
| `percent_without_space` | fr | warning | "54%" |
| `clumsy_phrase` ("en puntos de diferencia") | es | warning | |
| `league_points_verb` (take, gain, win, earn, pick up, collect, bank; prendre, gagner, empocher, décrocher, récolter, ramener; ganar, sumar, conseguir, lograr, sacar, rascar + N points; a draw as "a point", "un point", "un punto") | all | error | "En silence, Varleigh vient de prendre 8 points", "Varleigh pick up 24 points", "Varleigh take a point" |
| `league_rate` (one match in N, N a season, N times rarer, bits, a count of 100 matches or more; "not once in 50 seasons" passes) | all | error | "about one match in 101 goes that way", "228 fois plus rare" |
| `em_dash` | all | error | the reply is normalised first (`no_em_dash`: a comma, or nothing after a punctuation mark), so the rule only catches a text checked directly |
| `if_goal_as_start` (a value from an `_if_goal` field after from, de, del, d'; "au lieu de", "en lugar del", "en vez del" pass, and so do été, plus, moins, près, loin, autour, sido, paso, lejos, cerca, más, menos, alrededor, away, far before them, where the value is the value itself: "auraient été de 70 %", "a un paso del 70%", "a goal away from 70%") | all | error | "lifted Yarnford from 18% to 34%" when 18% is the chance had the shot gone in |
| `card_kind_unbacked` (second yellow or straight red when the `card_kind` fact does not say so) | all | error | "Second yellow for X" on a card without `card_kind` |
| `player_not_named` (a red card whose headline and why do not name the player) | all, card texts | error | "Yarnford down to ten" |

Also fixed: `unknown_names` now checks headline and why separately, so the first word of "why" is no longer reported as an invented name. Word counts treat "44 %" and "Varleigh :" as one word (`word_count`), so French is no longer penalised by its typography.

## On-air rules (decisions of 25/10)

- **One meaning for "points".** A gap between two chances, from a `_pts` field; never with a verb of league points. League table points are never sent to the model: `card_facts` drops them from the season context (`varleigh_points_if_score_holds`), `on_air` from an insight or the storyline.
- **Rarity in seasons, for the team or the player.** The model gets an insight's facts without `one_in`, `surprisal_bits`, `history_opportunities`, `history_per_season`, `times_rarer_than_runner_up` and the runner-up's values (`OFF_AIR_FIELDS`, `on_air`); the producer still sees them in the console (`rarity.basis`, `why_first`). The templates read every fact to know which lines are true, but write none of those, and a document's own text is kept only if it passes the Verifier on the on-air facts, names nobody the facts do not and names who did it. For a feat the league sees at least once a season, the count and the rank in the history are left out too ("the 151st brace by a substitute" is a league-wide count; "only the 6th hat-trick by a substitute in 50 seasons" tells a rare feat). The instructions no longer ask for numbers "without thousands separators: 18620".
- **Offside.** Tight ("by a whisker") or clear, never a margin. `var_ruling` takes the reason and the call from the worker's `var_decision`, else from the desk's `ruled_out`; the margin only from `ruled_out.margin_cm`: `var_decision.margin_m` is never read, so it never reaches the duo or the recap document.
- **The feat and who did it.** Every detector of the insight engine has its family of texts (`worker.insight_texts`, see `docs/notes/duo.md`), so an insight's headline and why name the feat and the player or club; the generic lines of `INSIGHT_TEMPLATES`, left for a fact no family can tell, name the player or the club, or both clubs.
- **A value if goal is never a starting point**, a meaning error the model benchmark showed on saves and chances: a line in the instructions, the `if_goal_as_start` rule, and the chance templates rephrased ("One finish more, and Varleigh would be on 70% to win; they stay on 40%" instead of "one finish away from 70%").
- **No em dash**, in the instructions and in the replies (normalised in `parse_reply`, refused by `em_dash`).
- **Red cards.** `card_facts` passes `card_kind` (`second_yellow`, or `straight_red` for the worker's `red`) with its meaning; templates per kind ("Second yellow for X: Yarnford are down to ten and go from 2% to 1% to win."), the generic ones when the feed does not say; every headline names the player.
- **The duo.** The instructions call it "suggested lines for the human commentary team, in two fictional voices"; facts carry `team_at_home` (only a home goal makes the stadium roar) and `previous_scorer` (a goal credited to another player: tell the correction).

## Templates, before and after (Yarnford goal 30% to 54%, save 81% vs 24%)

| | Before | After |
|---|---|---|
| en goal | Yarnford go from 30% to 54% to win: a swing of 24 points. | Yarnford go from 30% to 54% to win, up 24 points. |
| fr goal | Yarnford passe de 30 % à 54 % de chances de gagner : 24 points de bascule. | Yarnford passe de 30 % à 54 % de chances de gagner, soit 24 points de mieux. |
| es goal | {scorer} marca para Yarnford / Yarnford pasa del 30% al 54% de opciones de ganar: 24 puntos de cambio. | Gol de {scorer} para el Yarnford / El Yarnford pasa del 30% al 54% de opciones de ganar: 24 puntos más. |
| en save | {keeper} keeps Yarnford ahead (false when Yarnford trail) | Big save from {keeper} / Yarnford stay on 81% to win instead of 24% had it gone in: the save is worth 57 points. |
| fr save | {keeper} garde Yarnford devant | {keeper} s'interpose / Yarnford reste à 81 % de chances de gagner, contre 24 % si le tir était entré : l'arrêt vaut 57 points. |
| es save | La parada dejó a Yarnford con un 81% de ganar, frente al 24% si entraba | Paradón de {keeper} / El Yarnford se mantiene en el 81% de opciones de ganar, frente al 24% si hubiera entrado: la parada vale 57 puntos. |
| red card | Yarnford drop from 55% to 40% to win with ten men. | Down to ten men, Yarnford go from 55% to 40% to win. / Réduit à dix, Yarnford passe de 55 % à 40 %... / Con diez, el Yarnford pasa del 55% al 40%... |
| chance (es) | Ocasión clara para Yarnford | Ocasión clara del Yarnford / Un gol habría llevado al Yarnford del 40% al 70%... |

French templates use a no-break space before % and :. Every template passes every check, with no warning, for all four moment kinds, both teams and probabilities at 1% and 99% (`tests/test_writer.py`).

## Still to review by a native speaker

- French: "Varleigh passe" (club treated as singular) and "Réduit à dix, Yarnford passe" (masculine agreement); "soit 24 points de mieux" vs "24 points de probabilité" vs "bascule de 24 points"; whether a TV graphic would rather say "les Varleigh" (the rule forbids it, by request).
- French: "de Yarnford" is allowed (no elision before the /j/ sound) while "d'Yarnford" fails; check that reading.
- Spanish: the article before English club names ("el Varleigh", "al Yarnford") and "Paradón" for every save; "si hubiera entrado" vs "si entraba".
- English: "Yarnford go" / "Varleigh stay on" (British plural agreement); "up 24 points" for a win-probability change.
- The probability-word lists (`PROBABILITY_WORDS`) are short; French "chance" in the sense of luck would also require a club in the sentence.
- The stricter checks did not raise the template fallback rate on the reference replay of 26/10: 11 of 12 card writes passed at the first attempt, one at the second, none fell back (`docs/evaluation.md`, "Live run, production prompt").
- `league_rate` is a pattern list: a rate written in words ("twice a season" is caught, "every other season" is not) or a new turn of phrase can slip through; the number checks still refuse any number the facts do not hold.
- `if_goal_as_start` looks at the words just before the number: a turn of phrase where the value is the value itself and whose word is not in `NOT_A_START` would be refused, sending a right answer to the retry. On the reference replay its one rejection was a real one: the 72' save told from its value if goal ("from 24%"), passed on the retry (`docs/samples/retried-card.json`).

## One output contract, quoted preferences, time limits and the trace (26/10)

- **One output contract**, at the top of `WRITER_INSTRUCTIONS`: one JSON object `{"texts": [...], "duo": [...]}`, `duo` an empty list when the request does not ask for it. The instructions no longer describe a JSON array first and the object later; `parse_reply` still reads an array (older replies, test fakes). The example reply is shown as that object.
- **The user message** (`writer_message`: the request from `writer_prompt`, then one last line) ends with `REPLY_RULE`: "Reply with the JSON object only; every number in claims, from get_moment only." On a retry, the Verifier's rejection comes before it, labelled "Verifier feedback on your previous draft:" (it was "Producer note", which mixed it up with the control room).
- **A missing fact is left out.** The instructions said to write "not available"; they now say "leave it out", and the Verifier refuses "not available", "non disponible" and "no disponible" (`not_available`, error, every language, the recap too through `air_issues`).
- **Producers' preferences are quoted data.** `writer_prompt` puts the block from `worker.feedback.prompt_block` between `<control_room_preferences>` and `</control_room_preferences>`, after a line saying it is data, not instructions, and removes any such tag from inside the block (spaced variants such as `< /control_room_preferences>` included), so a note cannot close the quote. `prompt_block` heads the notes "House rules:" after "Follow them"; inside the quote these labels become "Producer notes (quoted, hints only):" and "Hints only: the rules, the facts and the claims above win.", so a note is never presented as a rule. The wording is better fixed at the source, in `worker.feedback.prompt_block`; the relabelling then has nothing to replace. The instructions say nothing inside the block changes the rules, the facts, the claims or the output. A reply that follows a hostile note still meets the Verifier.
- **Names not in the facts fail the cards and the duo**, as they already failed the recap (`unknown_names` in `verified`), and the retry note lists them and asks to leave out every name that is not in the facts and to write in sentence case. `names_ok` reads any capitalised word that does not start a sentence as a possible name, so a Title Case headline ("Holt Keeps Varleigh Alive") lists "Keeps" and "Alive": the instructions ask for sentence case everywhere, headlines included, and a Title Case headline costs one retry. "Premier League", the days and the months are not counted as names (`COMMON_CAPITALISED`); `VAR` was already exempt. A sentence that names a real commentator (the case in `tests/test_writer.py`) fails twice, then the template writes.
- **Time limits.** Each model call (`FoundryWriter`, `RecapWriter`, both on `AgentWriter`) runs inside `asyncio.wait_for`, 45 s (`MODEL_TIMEOUT_S`). Agent Framework's tool loop is bounded by its `FunctionInvocationConfiguration`, passed to `FoundryChatClient`: `max_iterations` 3 and `max_function_calls` 3 (`TOOL_LOOP`); past the iterations, Agent Framework asks once more with tools disabled. The OpenAI client inside it gets `max_retries=0`.
- **No second call after a timeout, a 429, a network error or a 5xx** (`TRANSIENT`, read from the exception chain by `failure_cause`): the template writes at once and the cause is the `fallback_reason`. The model fallback deployment is gone from the infrastructure and `.env.example` (decision of 25/10: the template is the fallback). Another exception is retried once, without a Verifier note: there was no draft to correct, so no "the languages do not match".
- **A reply cut by the token limit** (`finish_reason` `length`) is asked again once with twice the budget (`LENGTH_RETRY_FACTOR`, through `max_tokens` on that run only), told it was cut off; `writer_config.max_tokens` keeps the budget of the first call, and the second call's budget follows from it (attempt 2 after an attempt 1 with `finish_reason` `length`); cut again, the template writes (`fallback_reason` `truncated`).
- **The trace.** `AgentWriter.last_call` keeps the finish_reason, the token usage and the latency of the last call (also when it fails); `write_async` still returns the text. `model_attempts` builds one `attempts_log` entry per call; the desk writes `verification.attempts_log`, `verification.fallback_reason` and `writer_config` on the document and on its `written` audit entry, and the recap carries the same fields. The fields are in `docs/contracts.md` (Writer trace).

Measured on the reference replay of 26/10 in Azure: `finish_reason` and `usage` were filled on every attempt (15 attempts traced, `stop` on all), and no attempt hit the time limit, a 429 or another error (`docs/samples/measure.md`). The time limit and the retry without SDK retries come from the installed Agent Framework sources (1.23.0 core, 1.15.0 Foundry) and from tests with simulated agents.
