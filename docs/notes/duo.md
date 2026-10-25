# Commentary duo, anti-repetition, points and stakes (`worker.duo`, `writer`, `desk`, `recap`)

The product owner's complaint: commentary that always uses the same words for the same actions, and no personality. This lot gives every card and insight a two-voice exchange (`duo` field, see `docs/contracts.md`), makes the desk remember what a match has already said, and settles three product decisions (goals ruled out in the recap, one meaning for "points", goals with no stake).

## The two personas (`src/worker/duo.py`, `PERSONAS`)

Both are invented. No real commentator or pundit is named, quoted or imitated; the names were checked against the generator's player name pools so that a player can never share a persona's name.

| | Wren Calder (`wren`) | Joss Ferrier (`joss`) |
|---|---|---|
| Role | Play-by-play: the heart and the story | Co-commentator: the eye for detail and numbers |
| Background | Grew up behind the goal of a small harbour-town club, twenty years of lower-league local radio | Former second-division goalkeeper, then match analyst |
| Notices | The story arc (comeback, a substitute's day, a keeper keeping his side alive), faces, the crowd, the clock | The change in the chance of winning, the value of a save, the offside line in centimetres, what keeps repeating |
| Rhythm | Short bursts, an exclamation when it is earned, then a breath | Calm, one precise sentence, dry humour, never two numbers where one will do |
| With the other | Throws the emotion first, reacts to Joss's number with feeling, teases him for loving numbers | Grounds the emotion with one fact; sometimes the surprised one |
| Tics (once per match each) | en "Hold on to your seats!", "Write it down!", "Oh, the drama of it!"; fr "Accrochez vos ceintures !", "Notez-le quelque part !", "Quel scénario !"; es "¡Agárrense fuerte!", "¡Apúntenlo!", "¡Qué guion!" | en "Look at the line.", "Small detail, big consequence.", "Numbers don't celebrate, but that one might."; fr "Regardez la ligne.", ...; es "Miren la línea.", ... |

Each persona also has a vocabulary list and a sample line per language. `persona_brief` renders all of it into the Writer prompt.

## How the lines are produced

- **Model (duo mode).** `FoundryWriter.write_async(..., duo=True, avoid=...)`: one call returns `{"texts": [...], "duo": [...]}`. The prompt adds the personas, the duo rules (2 to 4 lines per language, alternating, spoken, every number claimed, tics once per match) and a worked duo example that a test runs through the Verifier. The request carries `avoid`: per language the headlines, openings and verbs already used in the match, and the tics already said.
- **Verifier.** `verify_duo` checks each line like a card text (claims, units, team named with a probability, language rules) plus count and alternation per language. `verify_reply` combines texts, duo and repetition. If the model's texts pass but its duo fails twice, the texts are kept and the duo comes from the templates (`duo_writer: template`). A writer without duo mode (old signature) gets template lines.
- **Templates.** `LINES` in `duo.py`: per moment (goal by situation: opener, equaliser, go-ahead, extends lead, pulls one back; goal with no stake; save; red card; chance; VAR ruling; insight by kind), per language, slots `heart` (Wren, no number), `eye` (Joss, the number), `heart_back`, `eye_more` (story: brace, hat-trick, four or more, late goal). 2 to 4 lines, count chosen by a stable hash; at least 3 for a big moment (30 points or more), at least 4 when the scorer reaches 3 goals. For a save, the old goalkeeper sometimes speaks first.
- **VAR ruling.** When VAR voids a goal card, the desk records `ruled_out` (reason, decision minute, offside margin in cm from the feed's `update` correction tied to the same VAR decision) and writes `duo_ruling` from templates only (no model call for a retraction).

## Anti-repetition (`PhraseMemory`)

One memory per match in the `Desk`. It records headlines, openings (first 3 words, numbers as #, names masked), verbs of the headlines, 5-word phrases and tics, for texts and duo lines; a new version of a card replaces its old entries.

- The prompt gets `avoid`; the templates pick, deterministically, the variant with an unused headline, the least used verb, the least used opening and the fewest shared phrases; an optional line already said word for word is dropped.
- The Verifier fails an identical headline (`repeated_headline`, which triggers the retry) and warns on a reused opening, phrase or tic. The card keeps the count in `verification.repetition_warnings`.

Measured with the template writer (`tests/test_duo.py` asserts at least 0.8 on the demo):

| Match | Cards | Distinct openings of whys and duo lines, en / fr / es | Identical headlines |
|---|---|---|---|
| Demo (seed 7) | 11 | 0.96 / 0.91 / 0.92 | 0 |
| Seeds 1 to 5 | 1 to 7 | 0.85 to 1.00 | 0 |

Before this lot, every goal card of the demo had the same headline pattern and the same "why" (one template per moment).

## Product decisions

- **(a) Goals ruled out in the recap.** `recap.analyse` reads `disallowed_goals` from the `ruled_out` record of retracted goal cards (or `build_recap(..., disallowed=[...])`), and `recap_facts` exposes `disallowed_<i>_minute/_team/_player/_reason/_margin_cm`. Templates: "VAR ruled out Marijn Scarpetta's goal at 63' (offside by 25 cm) and Jakub Méndez's goal at 69' (handball)." and in the analyst text "VAR: 63' (offside by 25 cm), 69' (handball)." The margin is in centimetres because the Verifier treats any number below 1 as a misused probability. `verify_recap` still fails a retracted minute in a sentence that does not say VAR or ruled out. The recap limit went from 60 to 80 words to make room.
- **(b) One meaning for "points".** `value_pts` (the ranking key) is no longer a recap fact, and "(51 pts)" left the analyst template ("Top moments: 66', 57', 14'."). Cards never had it. The prompts say "points" means only a change in a team's chance of winning.
- **(c) A goal with no stake.** `low_stake(card)`: a goal moving its team's chance of winning by less than 2 points. `card_facts` then drops every probability field and adds `stake: low`, `team_goals_after`, `scorer_match_goals`, so a probability cannot even be claimed; the templates write a milestone ("Joaquim Kosznik adds the fifth", "it is goal number five for Rovers today"). The desk sets `on_air_priority: low` (88' in the demo, 99% to 99%).

## Sample exchanges (template writer, demo match, as the desk writes them today)

**63', Scarpetta's goal, then ruled out at 64' (offside, 25 cm).**

| | en | fr | es |
|---|---|---|---|
| Wren (goal) | Hold on to your seats! In front! Marijn Scarpetta puts Rovers ahead and the bench is on its feet! | Notez-le quelque part ! Marijn Scarpetta trouve l'ouverture, et Rovers prend les commandes ! | ¡Apúntenlo! ¡Por delante! ¡Marijn Scarpetta adelanta al Rovers! |
| Joss | Put a number on it: Rovers go from 29% to 78% to win. | Ce but vaut 49 points pour Rovers, qui passe de 29 % à 78 % de chances de gagner. | Pongámosle número: el Rovers pasa del 29% al 78% de opciones de ganar. |
| Wren (VAR) | And it's off. VAR rules it out, and Rovers cannot believe it. | Et c'est refusé. La VAR annule, et Rovers n'en revient pas. | Y se anula. El VAR lo invalida, y el Rovers no se lo cree. |
| Joss | Look at the line. Offside by 25 centimetres, measured at the moment of the pass. | Regardez la ligne. Hors-jeu de 25 centimètres, mesuré au moment de la passe. | Miren la línea. Fuera de juego por 25 centímetros, medido en el momento del pase. |
| Wren | 25 centimetres, Joss. A whole celebration, for 25 centimetres. | 25 centimètres, Joss. Toute une célébration, pour 25 centimètres. | 25 centímetros, Joss. Toda una celebración, por 25 centímetros. |

**72', the save at 3-2.**

| | en | fr | es |
|---|---|---|---|
| Joss | Speaking as an old goalkeeper: that one deserves its own replay. | En tant qu'ancien gardien, je le dis : celui-là mérite son propre ralenti. | Como exportero, lo digo: esa merece su propia repetición. |
| Wren | Oh, the drama of it! Valdemar McGrory gets something on it! I don't believe it! | Accrochez vos ceintures ! Quel arrêt ! Valdemar McGrory, tu viens de garder Rovers en vie ! | ¡Agárrense fuerte! ¡Qué parada! ¡Valdemar McGrory, acabas de mantener vivo al Rovers! |
| Joss | Had it gone in, Rovers would be down to 24% to win; Valdemar McGrory kept them on 81%. | Si ça rentrait, Rovers tombait à 24 % de chances de gagner ; Valdemar McGrory garde Rovers à 81 %. | Pequeños márgenes: 57 puntos salvados para el Rovers, que sigue en el 81% de opciones de ganar. |
| Wren | | Voilà pourquoi on paie un gardien ! | Joss, estás sonriendo. ¡Nunca te había visto sonreír por un número! |

**The substitute's four goals (Scarpetta, on at half-time).**

| | en | fr | es |
|---|---|---|---|
| 52' Wren | Marijn Scarpetta gives Rovers a lifeline! | Une lueur pour Rovers, et elle vient de Marijn Scarpetta ! | ¿Hay partido? ¡Marijn Scarpetta recorta para el Rovers! |
| 57' Wren | They said Rovers were gone. Marijn Scarpetta disagrees! | Égalisation ! Marijn Scarpetta remet Rovers dans le match ! | Decían que el Rovers estaba muerto. ¡Marijn Scarpetta no opina lo mismo! |
| 57' Joss | | Deux pour Marijn Scarpetta déjà. Retenez ce nom. | |
| 66' Wren | Write it down! Marijn Scarpetta finds the gap, and Rovers have their noses in front! | Quel scénario ! Et voilà ! Marijn Scarpetta met Rovers en tête ! | ¡Qué guion! ¡Marijn Scarpetta encuentra el hueco y el Rovers manda! |
| 66' Joss | Three for Marijn Scarpetta. Remember where you were for this one. | Trois pour Marijn Scarpetta. Souvenez-vous où vous étiez. | Tres de Marijn Scarpetta: el balón del partido ya tiene dueño. |
| 83' Joss | Marijn Scarpetta, goal number four of the match. Write that one in pen. | Marijn Scarpetta, quatrième but du match. Ça s'écrit à l'encre. | Sin hacer ruido, el Rovers acaba de ganar 8 puntos: 99% de opciones de ganar. |

The 88' goal (5-2, no stake): "The result was settled a while ago, but nobody told Joaquim Kosznik!" / "Nothing changes in the result, but it is goal number five for Rovers today."

## What a native speaker should review

- **French.** Tutoiement between the two ("Je te crois sur parole", "Laisse-les savourer"); "La VAR" (feminine, usage in France) against "le VAR"; "Rovers était à" (club as singular, as in the card templates); "ballon du match est réservé" for a hat-trick; "Souvenez-vous où vous étiez" (vs "de l'endroit où"); "Ça s'écrit à l'encre"; "celui-là mérite son propre ralenti"; masculine agreement in "On disait Rovers perdu", "Réduit à dix".
- **Spanish.** "Pongámosle número", "Como exportero" (one word), "¡Agárrense fuerte!" (ustedes, Spain vs Latin America), "el balón del partido ya tiene dueño" for a hat-trick, "hace más grande un día para recordar". Spanish stays labelled machine-translated on air.
- **English.** "noses in front", "the match ball is spoken for", "Write that one in pen" (British idioms, on purpose); "they said Rovers were gone" (plural club).
- **All.** Whether Wren's tic on top of an exclamation ("Hold on to your seats! In front! ...") is too much in one line; whether the old-goalkeeper line on saves should appear at most once per match (today it can come back, as a warned repeat).

## What is imperfect

- **Inconsistency across languages.** Each language picks its own variants, so en, fr and es can tell the same card with different sentences (all true, all verified). A producer comparing languages will notice.
- **"The substitute" is not known to the desk.** Cards do not say a player came off the bench, so the templates say "a fourth for Scarpetta", not "a fourth for the substitute". The insight engine, which reads the substitutions, is where that story belongs; the console still shows `value_pts` as "pts" (`src/web/console.py`, not in this lot), which contradicts decision (b) on the producer screen.
- **Offside margin lookup.** The desk finds the margin in the events the sink keeps in memory (`MemorySink.events`). With Cosmos DB as the sink, `events` is a container, so `margin_cm` is null and the recap says "offside" without the margin. A clean fix is for the pipeline to pass the related correction to the desk.
- **Memory is in process.** A restarted worker forgets what the match already said.
- **Insight templates carry no number.** The history facts are free-form, so the fallback headline and why of an insight are true for their kind ("sets a record") but generic; the document's own verified `texts`, when the insight engine provides them, are preferred. The model path has the numbers.
- **Counting words are checked lightly.** A model writing "a fifth goal" is only warned (`count_word_unbacked`) when no whole-number fact equals 5; the prompt asks for digits.
- **No live run yet.** The duo mode prompt has never been sent to a model: the tests use crafted JSON. Output length per card grows (texts plus up to 12 lines); check latency and the 3,000-token cap at the next Foundry run.
