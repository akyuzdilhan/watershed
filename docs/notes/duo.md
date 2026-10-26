# Commentary duo, anti-repetition, points and stakes (`worker.duo`, `writer`, `desk`, `recap`)

The starting complaint: commentary that always uses the same words for the same actions, and no personality. Every card and insight carries a two-voice exchange (`duo` field, see `docs/contracts.md`), the desk remembers what a match has already said, and three product decisions are settled (goals ruled out in the recap, one meaning for "points", goals with no stake).

Watershed works alongside human commentators. The duo's lines are suggested lines for the commentary team, in two fictional voices, with no audio: the team picks, rewords or ignores them.

## The two personas (`src/worker/duo.py`, `PERSONAS`)

Both are invented. No real commentator or pundit is named, quoted or imitated; the names were checked against the generator's player name pools so that a player can never share a persona's name.

| | Wren Calder (`wren`) | Joss Ferrier (`joss`) |
|---|---|---|
| Role | Play-by-play: the heart and the story | Co-commentator: the eye for detail and numbers |
| Background | Grew up behind the goal of a small harbour-town club, twenty years of lower-league local radio | Former second-division goalkeeper, then match analyst |
| Notices | The story arc (comeback, a substitute's day, a keeper keeping his side alive), faces, the crowd, the clock | The change in the chance of winning, the value of a save, how tight an offside was (tight or clear, never a margin), what keeps repeating |
| Rhythm | Short bursts, an exclamation when it is earned, then a breath | Calm, one precise sentence, dry humour, never two numbers where one will do |
| With the other | Throws the emotion first, reacts to Joss's number with feeling, teases him for loving numbers | Grounds the emotion with one fact; sometimes the surprised one |
| Tics (once per match each) | en "Hold on to your seats!", "Write it down!", "Oh, the drama of it!"; fr "Accrochez vos ceintures !", "Notez-le quelque part !", "Quel scénario !"; es "¡Agárrense fuerte!", "¡Apúntenlo!", "¡Qué guion!" | en "Look at the line.", "Small detail, big consequence.", "Numbers don't celebrate, but that one might."; fr "Regardez la ligne.", ...; es "Miren la línea.", ... |

Each persona also has a vocabulary list and a sample line per language. `persona_brief` renders all of it into the Writer prompt.

## How the lines are produced

- **Model (duo mode).** `FoundryWriter.write_async(..., duo=True, avoid=...)`: one call returns `{"texts": [...], "duo": [...]}`. The prompt adds the personas, the duo rules (2 to 4 lines per language, alternating, spoken, every number claimed, tics once per match, the stadium only for a home goal, the size of the words matching the number, the change of scorer told as a correction) and a worked duo example that a test runs through the Verifier. The request carries `avoid`: per language the headlines, openings, verbs and duo lines already used in the match, and the tics already said.
- **Verifier.** `verify_duo` checks each line like a card text (claims, units, team named with a probability, language and on-air rules) plus count and alternation per language. `verify_reply` combines texts, duo and repetition: a duo line already said in the match fails the duo. If the model's texts pass but its duo fails twice, the texts are kept and the duo comes from the templates (`duo_writer: template`). A writer without duo mode (old signature) gets template lines.
- **Templates.** `LINES` in `duo.py`: per moment (goal by situation: opener, equaliser, go-ahead, extends lead, pulls one back; goal with no stake; save; red card by how the player was sent off; chance; VAR ruling; a goal credited to another player; insight by kind, for a fact no family of `insight_texts` can tell), per language, slots `heart` (Wren, no number), `eye` (Joss, the number), `heart_back`, `eye_more` (story: brace, hat-trick, four or more, late goal). 2 to 4 lines, count chosen by a stable hash; at least 3 for a big moment (30 points or more), at least 4 when the scorer reaches 3 goals. For a save, the old goalkeeper sometimes speaks first, once per match.
- **Conditions.** A line can end with a condition placeholder (`CONDITIONS`), filled by `writer.conditions` with "" when the line is true for the card and None when it is not, so the line is skipped:

| Condition | Lines | True when |
|---|---|---|
| `{if_home}`, `{if_away}` | "this place has woken up", "tout le stade se réveille", "el estadio despierta", the noise or roar of the stadium; for an away goal, the away end ("le parcage visiteur", "la afición visitante") | the scoring team plays at home (or away) |
| `{if_new_scorer}` | "Varleigh pull further clear, X the man this time!" | it is the scorer's first goal of the match and his team already had one: the previous goal was someone else's |
| `{if_tilt}` | "You can feel it, Joss. The whole match just tilted." and translations | the goal moved its team's chance of winning by more than 20 points (`TILT_PTS`) |
| `{if_small_margin}`, `{if_big_margin}` | "Small margins: N points kept", "Quietly huge: that save is worth N points" | the save is worth under 15 points (`SMALL_MARGIN_PTS`), or 15 and more |
| `{if_big_chance}` | "Quietly huge, that one: a goal would have put Varleigh on N% to win" | the miss cost 15 points or more |

- **A goal ruled out, then the next one.** When VAR voids a goal card, the desk records `ruled_out` and writes `duo_ruling` from templates only (no model call for a retraction): the reason, and for offside the call, tight ("by a whisker", "d'un rien", "por muy poco") or clear, from the worker's `var_decision` or else from the margin of `ruled_out`. The margin itself stays with the producer: `var_decision.margin_m` is never copied into what the duo or the recap reads. The memory keeps the score the goal would have made: the next goal at that score opens with "This time it counts!" ("Cette fois, ça compte !", "¡Esta vez sí vale!") instead of a goal line.
- **A goal credited to another player.** When a rewritten card names the previous scorer (`previous_player`, set by the worker on version 2 or later), `card_facts` gives `previous_scorer` and the duo tells the correction ("Correction on that goal: it goes to X, not Y.", "Nothing changes for Yarnford but the name on the scoresheet.") instead of announcing the goal again. The cause of the change (in the feed, a deflection) is not told: no card field carries it.
- **A red card.** With `card_kind` (`second_yellow` or `red`, set by the worker), Wren says how the player was sent off ("Second yellow for X, and that is red! Yarnford are down to ten.", "Straight red for X!"); without it, the generic lines. Every red card headline names the player.

## Anti-repetition (`PhraseMemory`)

One memory per match in the `Desk`. It records headlines, openings (first 3 words, numbers as #, names masked), verbs of the headlines, 5-word phrases, tics and duo lines, for texts and duo lines. A new draft of a document replaces its entries. Before a new version is drafted (a goal credited to another player, an insight updated or back after a goal ruled out), the duo lines of the version in memory are kept apart (`PhraseMemory.begin`, which `writer._draft` calls first): the commentary team may already have said them, so the new draft is checked against them, by the templates and by the Verifier, and never proposes them again. A retry of the same version is not checked against itself.

- The prompt gets `avoid`; the templates pick, deterministically, the variant with an unused headline, the least used verb, the least used opening and the fewest shared phrases, and the why and the duo avoid starting like the card's own headline.
- No duo line is said twice word for word in a match, across every version of every card and insight. An optional line already said is dropped; a required line whose every variant was said is closed with the score and the minute ("3-2 at 66'."), which are new; an insight's line, once its kind and the storyline have no line left, with the minute ("Remember the minute: 65'."). Facts told several times (a brace, then a hat-trick, then the stat of the match; a comeback that draws level, goes ahead, then wins) have lines of their own at each stage, and the stat of the match has its own questions for Wren, never one the fact's own lines already asked.
- The Verifier fails an identical headline (`repeated_headline`, which triggers the retry) or an identical duo line (`repeated_line`, which fails the duo), and warns on a reused opening, phrase or tic. The card keeps the count in `verification.repetition_warnings`.

Measured with the template writer:

| Match | Documents (versions) | Distinct openings of whys and duo lines, en / fr / es | Identical headlines | Duo lines said twice, per language, every version counted |
|---|---|---|---|---|
| Demo (seed 7), cards only | 11 cards (12) | 0.92 / 0.90 / 0.90 | 0 | 0 |
| Seeds 1 to 5, cards only | 1 to 7 cards | 0.88 to 1.00 | 0 | 0 |
| Demo with the league history and the storyline | 11 cards, 10 insights, storyline (28) | 0.83 / 0.83 / 0.84 | 0 | 0 |

On the demo and seeds 1 to 40 with the league history (89 insights), no duo line is proposed twice in a match, every version counted, and every insight text names its feat and who did it.

`tests/test_duo.py` asserts at least 0.8 distinct openings on the demo and no duo line said twice: on the demo, on the demo with its storyline (the goals that change the table), on fourteen goals by the same scorer, on the insights of a match like the demo, and on every version: a card drafted again (the order of the desk: version 1 drafted and recorded, then version 2), the demo's cards, the insights back after a goal ruled out, and five matches against a five-season history.

## Product decisions

- **(a) Goals ruled out in the recap.** `recap.analyse` reads `disallowed_goals` from the retracted goal cards (`ruled_out` from the desk, or `var_decision` from the worker), or `build_recap(..., disallowed=[...])`, and `recap_facts` exposes `disallowed_<i>_minute/_team/_player/_reason/_offside_call`. Templates: "VAR ruled out [substitute]'s goal at 63' (offside by a whisker) and [Yarnford scorer]'s goal at 69' (handball)." and in the analyst text "VAR: 63' (offside by a whisker), 69' (handball)." The margin is not a recap fact: it stays with the producer, and on air an offside is only tight or clear. `verify_recap` still fails a retracted minute in a sentence that does not say VAR or ruled out.
- **(b) One meaning for "points".** "Points" on air is only a gap between two chances, in percentage points, from a `_pts` field. `value_pts` (the ranking key) and league table points are never facts for a text, and no line says take, gain, win, earn or pick up N points (prendre, gagner, empocher; ganar, sumar, conseguir): the Verifier refuses it (`league_points_verb`), and a draw told as "a point" (un point, un punto) too. "Quietly huge" and its translations are kept for saves and chances. A draw in the fan text is "honours even", never "a point".
- **(c) A goal with no stake.** `low_stake(card)`: a goal moving its team's chance of winning by less than 2 points. `card_facts` then drops every probability field and adds `stake: low`, `team_goals_after`, `scorer_match_goals`, so a probability cannot even be claimed; the templates write a milestone ("[scorer] adds the fifth", "it is goal number five for Varleigh today"). The desk sets `on_air_priority: low` (88' in the demo, 99% to 99%).
- **(d) Rarity on air.** A rarity is told in seasons, for the team or the player ("7 seasons since Varleigh last won from 2 goals down, Wren"). League-wide rates, match counts, ratios and bits ("about one match in 101", "about 3 a season", "228 times rarer") stay in the console for the producer (`rarity.basis`, `why_first`): the templates never write them, the model never gets them (`writer.on_air`), and the Verifier refuses them (`league_rate`), while a landmark in seasons ("not once in 50 seasons had Varleigh done it") passes. For a feat the league sees every season, the count and the rank in the history stay off air too: "the 151st brace by a substitute" would be a league-wide count, while "only the 6th hat-trick by a substitute in 50 seasons" tells a rare feat.
- **(e) The feat and who did it.** Every detector of the insight engine has its family of texts in `worker.insight_texts`: a player's goals (a substitute's against substitutes', a starter's against every player's), a comeback, a lost half-time lead, a late equaliser or go-ahead goal, ten men who score or win, a record win or the end of a wait in the fixture, the shots, the passes, the goals of the match or of a club, the fastest hat-trick, a run, the goals against expected goals. When the history gives no landmark in seasons (a club it does not know, a landmark last season), plain lines tell the fact ("Yarnford trailed 2-0 and came back to 2-2."). The lines left for a fact no family can tell name the player or the club, or both clubs. A text an insight keeps from its last version must name its scorer: a late goal credited to a team-mate is told again with the new name.

## Sample exchanges (template writer, demo match with the league history and the storyline)

Players are named by their role: the generator draws the names again. For the 31' correction and the 78' red card, `previous_player` and `card_kind` are set as the worker sets them.

**63', the substitute's goal, then ruled out at 64' (offside, a tight call).**

| | en | fr | es |
|---|---|---|---|
| Wren (goal) | Hold on to your seats! In front! [substitute] puts Varleigh ahead and the bench is on its feet! | Notez-le quelque part ! [substitute] trouve l'ouverture, et Varleigh prend les commandes ! | ¡Apúntenlo! ¡Por delante! ¡[substitute] adelanta al Varleigh! |
| Joss | Look at the jump: 49 points in one go, and Varleigh are on 78% to win. | Ce but vaut 49 points pour Varleigh, qui passe de 29 % à 78 % de chances de gagner. | Pongámosle número: el Varleigh pasa del 29% al 78% de opciones de ganar. |
| Wren, Joss (the table) | What does it do to the table, Joss? / If it stays 3-2, Varleigh go up to 14th, and their chance of going down falls to 5%. | Que change ce 3-2 au classement, Joss ? / Si ça reste 3-2, Varleigh remonte 14e, et son risque de descente tombe à 5 %. | ¿Y en la tabla, Joss? / Si se mantiene el 3-2, el Varleigh sube al 14.º puesto, y su riesgo de descenso baja al 5%. |
| Wren (VAR) | No! Oh no. It's gone. VAR has taken it away from [substitute]. | Et c'est refusé. La VAR annule, et Varleigh n'en revient pas. | El VAR dice que no. El gol de [substitute] no sube al marcador. |
| Joss | Look at the line. Offside by a whisker, judged at the moment of the pass. | Regardez la ligne. Hors-jeu d'un rien au départ du ballon. C'est dire si c'était serré. | Miren la línea. Fuera de juego por muy poco, en el momento del pase. |
| Wren | A whisker, Joss. That is all it took. | Si peu, Joss. Toute une célébration, pour un rien. | Tan poco, Joss. Toda una celebración, por nada. |

**66', the goal at the score VAR took away.** en "Write it down! This time it counts! [substitute] for Varleigh!", fr "Quel scénario ! Encore, et cette fois, ça compte ! [substitute] pour Varleigh !", es "¡Qué guion! ¡Esta vez sí vale! ¡[substitute] para el Varleigh!". The table answer that follows is not the one said at 63' ("If it stays 3-2, Varleigh rise to 14th, with a 5% chance of going down."), and the insights the 63' goal had made true, back as a second version, get lines of their own.

**31', the goal credited to a team-mate.** en "Correction on that goal: it goes to [new scorer], not [first scorer]." / "Nothing changes for Yarnford but the name on the scoresheet."; fr "Changement de buteur : ce but est désormais attribué à [new scorer]."; es "Corrección en ese gol: es para [new scorer], no para [first scorer]."

**78', the second yellow.** Headlines "[player] off after a second yellow", "Deuxième avertissement pour [player]", "[player], expulsado por doble amarilla"; Wren "[player] is booked again, and off he goes. Yarnford down to ten."

**The substitute's four goals (on at half-time).**

| | en | fr | es |
|---|---|---|---|
| 52' Wren | Game on? [substitute] pulls one back for Varleigh! | Une lueur pour Varleigh, et elle vient de [substitute] ! | ¿Hay partido? ¡[substitute] recorta para el Varleigh! |
| 57' Wren | [substitute]! Varleigh are level, and this place has woken up! | Tout est à refaire ! [substitute] égalise pour Varleigh ! | ¡Todo por hacer otra vez! ¡[substitute] empata para el Varleigh! |
| 57' brace (insight) | A brace for [substitute]! And he only came on at 46'! / 2 goals off the bench, Wren. Not a bad way to come on. | | |
| 63' hat-trick (insight, before VAR) | [substitute] again! Joss, has a substitute ever done this? / Only the 6th in 50 seasons, Wren. | | |
| 66' hat-trick (insight, version 2) | A hat-trick for [substitute]! And he only came on at 46'! / Not since [an earlier substitute], in 2021/22, Wren. | | |
| 83' Joss | [substitute], his fourth goal of the match. Write that one in pen. | [substitute], son quatrième but du match. Ça s'écrit à l'encre. | El cuarto de [substitute]. Me quedo sin comparaciones, Wren. |
| Stat of the match | Joss, how will [substitute]'s night be remembered? / A first in 50 seasons, Wren: no substitute had ever scored 4 in this league. | | |

The 88' goal (5-2, no stake): "And another! [scorer] joins the party for Varleigh." / "Nothing changes in the result, but it is goal number five for Varleigh today."

Insights of other matches, with the same history: a starter's four goals "[player] scores a four-goal haul for Varleigh" / "[player] scored at 6', 20', 27', 37': a four-goal haul for Varleigh."; a late equaliser "A late equaliser from [player]" / "At 86', [player] makes it 2-2: a late equaliser for Yarnford."; the shots at the break "Varleigh: 12 shots to 3 at the break" / "Varleigh had 12 shots in the first half, Yarnford 3."; a record win "A record win for Varleigh over Yarnford" / "5-0: never in 50 seasons had Varleigh beaten Yarnford by 5 goals."

## What a native speaker should review

- **French.** Tutoiement between the two ("Je te crois sur parole", "Laisse-les savourer"); "La VAR" (feminine, usage in France) against "le VAR"; "Varleigh était à" (club as singular, as in the card templates); "le parcage visiteur exulte"; "L'air de rien, cet arrêt vaut 57 points"; "Hors-jeu d'un rien au départ du ballon"; "Encore, et cette fois, ça compte !"; "ballon du match est réservé" for a hat-trick; "Souvenez-vous où vous étiez"; "Ça s'écrit à l'encre"; masculine agreement in "On disait Varleigh perdu", "Réduit à dix"; in the insight families, "égalise sur le fil", "met Yarnford devant sur le fil", "Varleigh tente 12 tirs avant la pause", "Yarnford a plus le ballon après la pause", "Victoire record pour Varleigh face à Yarnford".
- **Spanish.** "Pongámosle número", "Como exportero" (one word), "¡Agárrense fuerte!" (ustedes, Spain vs Latin America), "la afición visitante enloquece", "¡Esta vez sí vale!", "el balón del partido ya tiene dueño" for a hat-trick, "¿qué tan rara es una noche así?" (Latin American; "cómo de rara" in Spain); in the insight families, "adelanta al Yarnford al final", "Récord: el Varleigh gana por 5 al Yarnford", "El Varleigh suma 12 tiros al descanso", "Varleigh 4-2 Yarnford, lluvia de goles", "póker" and "repóker" for four and five goals. Spanish stays labelled "AI-written, not reviewed" on air.
- **English.** "noses in front", "the match ball is spoken for", "Write that one in pen" (British idioms, on purpose); "they said Varleigh were gone" (plural club); "Quietly huge" for a big save; in the insight families, "Varleigh fire 12 shots before the break", "Yarnford see more of the ball after half-time", "Late, late drama!", "took it to the wire".
- **All.** Whether Wren's tic on top of an exclamation ("Hold on to your seats! In front! ...") is too much in one line; whether a required line closed with the score and minute ("3-2 at 66'.") reads well when a match runs out of variants.

## What is imperfect

- **Inconsistency across languages.** Each language picks its own variants, so en, fr and es can tell the same card with different sentences (all true, all verified). A producer comparing languages will notice.
- **"The man this time" is conservative.** The card knows the scorer's goals in the match, not who scored his team's previous goal: the line is used only for a scorer's first goal of the match once his team already had one, so a player scoring again after a team-mate does not get it.
- **The cause of a change of scorer is not told.** In the feed it is a deflection, but no card field carries it: the duo says the change of scorer without the reason.
- **A repeated model line costs a call.** A duo line the match already heard fails the duo and sends the draft back once, on the budget of 40 model calls per match; the prompt's `avoid` lists the last 12 lines per language. On the reference replay of 26/10, one card write of twelve needed its retry (for a value if goal, not a repeated line), and no write kept template lines (`docs/samples/measure.md`).
- **Memory is in process.** A restarted worker forgets what the match already said.
- **Counting words are checked lightly.** A model writing "a fifth goal" is only warned (`count_word_unbacked`) when no whole-number fact equals 5; the prompt asks for digits.
- **Duo mode, live.** On the reference replay of 26/10 the model wrote the texts and the lines of all 12 card writes, at medium effort and 5,000 output tokens per call, 24.4 s per attempt at the median. An earlier local pass at 3,000 tokens had replies cut short (`finish_reason` `length`), which set that budget (`docs/evaluation.md`, "Live run, production prompt").
