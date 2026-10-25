"""The commentary duo: two fictional commentators, their voices in three languages, the lines the template writer can speak, and the per-match memory that keeps the desk from repeating itself.

Wren Calder lives the moment and the story; Joss Ferrier sees the detail, the number, the pattern nobody noticed. Both are invented: no real commentator or pundit is named, quoted or imitated. The personas are data (`PERSONAS`), rendered into the Writer prompt by `persona_brief`; the deterministic lines (`LINES`) are the fallback when no model is used, and every number they say is claimed against the card facts like the card texts.

`PhraseMemory` remembers, per match, the headlines, openings, verbs, phrases and tics already used (texts and duo). The Writer prompt receives what to avoid, the templates pick the least used variant deterministically, and the Verifier warns on a reused phrase and fails an identical headline.
"""

import hashlib
import re
from dataclasses import dataclass, field
from string import Formatter

LANGS = ("en", "fr", "es")
NB = " "  # French typography: a no-break space before % : ; ? !


@dataclass(frozen=True)
class Voice:
    """How a persona sounds in one language."""
    vocabulary: tuple[str, ...]  # words and turns of phrase they reach for
    tics: tuple[str, ...]  # signature phrases, each allowed once per match
    sample: str  # one line in character, for the prompt


@dataclass(frozen=True)
class Persona:
    id: str
    name: str
    short: str  # how the other one calls them on air
    role: str
    background: str
    notices: tuple[str, ...]
    rhythm: str
    interaction: str
    avoids: tuple[str, ...]
    voices: dict[str, Voice] = field(default_factory=dict)


PERSONAS: dict[str, Persona] = {
    "wren": Persona(
        id="wren", name="Wren Calder", short="Wren", role="play-by-play: the heart and the story",
        background="Grew up behind the goal of a small harbour-town club, called lower-league matches on local radio for twenty years before television. Has never lost the supporter's eye.",
        notices=("the story of the match: comebacks, redemption, a substitute's day, a keeper who keeps his side alive", "faces, the bench, the noise of the crowd", "the clock and what is left to play for"),
        rhythm="Short bursts that build with the moment; an exclamation when it earns one, then a breath. Lets a big moment land before explaining it.",
        interaction="Throws the emotion first and hands over to Joss for the why; reacts to Joss's number with feeling, sometimes teases him for loving numbers.",
        avoids=("shouting every line", "insulting players or referees", "cliches repeated from one moment to the next"),
        voices={
            "en": Voice(vocabulary=("here we go", "what a moment", "lifeline", "turned on its head", "the noise", "they are not done"),
                        tics=("Hold on to your seats!", "Write it down!", "Oh, the drama of it!"),
                        sample="Level! And every Rovers heart in the ground skips a beat!"),
            "fr": Voice(vocabulary=("c'est parti", "quel moment", "bouée de sauvetage", "tout bascule", "le bruit du stade", "pas encore morts"),
                        tics=(f"Accrochez vos ceintures{NB}!", f"Notez-le quelque part{NB}!", f"Quel scénario{NB}!"),
                        sample=f"Égalisation{NB}! Tout le stade se réveille{NB}!"),
            "es": Voice(vocabulary=("allá vamos", "qué momento", "salvavidas", "se da la vuelta", "el rugido de la grada", "no están muertos"),
                        tics=("¡Agárrense fuerte!", "¡Apúntenlo!", "¡Qué guion!"),
                        sample="¡Empata el Rovers y el estadio despierta!"),
        }),
    "joss": Persona(
        id="joss", name="Joss Ferrier", short="Joss", role="co-commentator: the eye for detail and numbers",
        background="Former goalkeeper in a second division, then a match analyst. Watches the play nobody replays: the pass before the pass, the line, the keeper's feet.",
        notices=("the number behind the moment: the change in the chance of winning, the value of a save", "positions and margins: the offside line, how tight a call was", "patterns across the match: who keeps scoring, what keeps repeating"),
        rhythm="Calm, one precise sentence, often after a beat. Dry humour. Never two numbers where one will do.",
        interaction="Grounds Wren's emotion with one fact; now and then he is the one surprised. Calls her Wren; she calls him Joss.",
        avoids=("lecturing", "numbers that are not in the facts", "jargon: no swing, no momentum, no expected-goals talk without explaining it"),
        voices={
            "en": Voice(vocabulary=("look at", "the detail", "put a number on it", "worth", "quietly", "the line"),
                        tics=("Look at the line.", "Small detail, big consequence.", "Numbers don't celebrate, but that one might."),
                        sample="Put a number on it: Rovers go from 28% to 79% to win."),
            "fr": Voice(vocabulary=("regardez", "le détail", "mettons un chiffre dessus", "ça vaut", "en silence", "la ligne"),
                        tics=("Regardez la ligne.", "Petit détail, grosses conséquences.", "Les chiffres ne célèbrent pas, mais celui-là, peut-être."),
                        sample=f"Mettons un chiffre dessus{NB}: Rovers passe de 28{NB}% à 79{NB}% de chances de gagner."),
            "es": Voice(vocabulary=("miren", "el detalle", "pongámosle número", "vale", "sin hacer ruido", "la línea"),
                        tics=("Miren la línea.", "Pequeño detalle, gran consecuencia.", "Los números no celebran, pero ese quizá sí."),
                        sample="Pongámosle número: el Rovers pasa del 28% al 79% de opciones de ganar."),
        }),
}
HEART, EYE = "wren", "joss"
SPEAKERS = tuple(PERSONAS)
# Words a duo line may capitalise that are not in the facts: the personas, the referee's video assistant, the English "I".
ALLOWED_NAMES = {p.short for p in PERSONAS.values()} | {w for p in PERSONAS.values() for w in p.name.split()} | {"VAR", "I", "I'm", "I'll", "I've", "I'd"}


def persona_brief(languages: list[str]) -> str:
    """The two personas, for the Writer prompt."""
    out = []
    for p in PERSONAS.values():
        out.append(f'- speaker "{p.id}": {p.name} ({p.role}). {p.background} Notices: {"; ".join(p.notices)}. Rhythm: {p.rhythm} With the other: {p.interaction} Avoids: {", ".join(p.avoids)}.')
        for lang in languages:
            v = p.voices[lang]
            out.append(f'  {lang}: vocabulary {", ".join(v.vocabulary)}; tics, once per match each: {" | ".join(v.tics)}; sounds like: "{v.sample}"')
    return "\n".join(out)


# --- counting words: correct by construction, since the templates fill them from the facts --------------------------------

ORDINALS = {  # standalone ordinal: "a fourth for", "un quatrième pour", "el cuarto de"
    "en": ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"],
    "fr": ["premier", "deuxième", "troisième", "quatrième", "cinquième", "sixième", "septième", "huitième", "neuvième", "dixième"],
    "es": ["primero", "segundo", "tercero", "cuarto", "quinto", "sexto", "séptimo", "octavo", "noveno", "décimo"],
}
ORDINALS_BEFORE_NOUN = {**ORDINALS, "es": ["primer", "segundo", "tercer", "cuarto", "quinto", "sexto", "séptimo", "octavo", "noveno", "décimo"]}  # "el quinto gol", "el tercer gol"
CARDINALS = {
    "en": ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"],
    "fr": ["un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf", "dix"],
    "es": ["uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez"],
}


def count_words(lang: str, n: int | None) -> dict:
    """{nth, nth_adj, count} for 1 to 10, else empty: a template that needs them is skipped."""
    if not isinstance(n, int) or not 1 <= n <= 10:
        return {}
    return {"nth": ORDINALS[lang][n - 1], "nth_adj": ORDINALS_BEFORE_NOUN[lang][n - 1], "count": CARDINALS[lang][n - 1]}


def fr_de(name: str) -> str:
    """French "de" before a name: "d'Otto Meyer", "de Marijn Scarpetta"."""
    return f"d'{name}" if name[:1].upper() in "AEIOUYÉÈÂÎH" else f"de {name}"


# --- duo lines --------------------------------------------------------------------------------------
# Slots: "heart" (Wren reacts, no number), "eye" (Joss brings the number or the detail), "heart_back" (Wren answers), "eye_more" (a detail
# keyed by the story: brace, hat_trick, four_plus, late, any). A goal's heart line is keyed by its situation. Placeholders are filled from
# the card facts by `writer.TemplateWriter`; numeric ones are claimed. French lines use a no-break space before % : ; ? !

def _fr(s: str) -> str:
    return re.sub(r" ([%:;?!])", NB + r"\1", s)


GOAL_HEART = {
    "en": {
        "opener": ["{scorer}! There it is, the first goal, and it belongs to {team}!", "And the deadlock is broken! {scorer} for {team}!",
                   "{team} strike first, and {scorer} is away towards the corner flag!", "Somebody had to do it, and it's {scorer}! {team} in front!"],
        "equaliser": ["{scorer}! {team} are level, and this place has woken up!", "All square! {scorer} drags {team} back into it!",
                      "They said {team} were gone. {scorer} disagrees!", "Level! {scorer}, and every {team} heart in the ground skips a beat!"],
        "go_ahead": ["{scorer}! {team} lead, and what a turn this match has taken!", "In front! {scorer} puts {team} ahead and the bench is on its feet!",
                     "{scorer} finds the gap, and {team} have their noses in front!", "There it is! {scorer}, and {team} are ahead!"],
        "extends_lead": ["{scorer} makes it more comfortable for {team}!", "Another one for {team}, and {scorer} is loving every second!",
                         "{team} pull further clear, {scorer} the man this time!", "That should settle a few nerves: {scorer} for {team}!"],
        "pulls_one_back": ["{scorer} gives {team} a lifeline!", "Game on? {scorer} pulls one back for {team}!",
                           "{team} are not done yet: {scorer} with the reply!", "A glimmer for {team}, and it comes from {scorer}!"],
    },
    "fr": {
        "opener": ["{scorer} ! Le premier but est pour {team}, et quel soulagement !", "Le verrou saute ! {scorer} ouvre le score pour {team} !",
                   "{team} frappe en premier, et {scorer} file vers le poteau de corner !", "Il fallait quelqu'un pour débloquer ça : c'est {scorer} !"],
        "equaliser": ["{scorer} ! {team} revient à hauteur, et tout le stade se réveille !", "Égalisation ! {scorer} remet {team} dans le match !",
                      "On disait {team} perdu. {scorer} n'est pas d'accord !", "Tout est à refaire ! {scorer} égalise pour {team} !"],
        "go_ahead": ["{scorer} ! {team} passe devant, et ce match bascule !", "Devant ! {scorer} donne l'avantage à {team} !",
                     "{scorer} trouve l'ouverture, et {team} prend les commandes !", "Et voilà ! {scorer} met {team} en tête !"],
        "extends_lead": ["{scorer} donne de l'air à {team} !", "Encore un pour {team}, et {scorer} savoure !",
                         "{team} creuse l'écart, signé {scorer} !", "De quoi calmer les nerfs : {scorer} pour {team} !"],
        "pulls_one_back": ["{scorer} redonne espoir à {team} !", "Il y a encore un match ? {scorer} réduit l'écart pour {team} !",
                           "{team} n'a pas dit son dernier mot : {scorer} répond !", "Une lueur pour {team}, et elle vient {de_scorer} !"],
    },
    "es": {
        "opener": ["¡{scorer}! ¡El primer gol es para el {team}!", "¡Se rompe el empate! ¡{scorer} adelanta al {team}!",
                   "¡Golpea primero el {team}, y {scorer} se va corriendo hacia el banderín!", "Alguien tenía que hacerlo, ¡y es {scorer}!"],
        "equaliser": ["¡{scorer}! ¡Empata el {team} y el estadio despierta!", "¡Igualdad! ¡{scorer} devuelve al {team} al partido!",
                      "Decían que el {team} estaba muerto. ¡{scorer} no opina lo mismo!", "¡Todo por hacer otra vez! ¡{scorer} empata para el {team}!"],
        "go_ahead": ["¡{scorer}! ¡El {team} se pone por delante!", "¡Por delante! ¡{scorer} adelanta al {team}!",
                     "¡{scorer} encuentra el hueco y el {team} manda!", "¡Ahí está! ¡{scorer} pone al {team} arriba!"],
        "extends_lead": ["¡{scorer} le da aire al {team}!", "¡Otro más para el {team}, y {scorer} lo disfruta!",
                         "¡El {team} se escapa, con la firma de {scorer}!", "Eso calma los nervios: ¡{scorer} para el {team}!"],
        "pulls_one_back": ["¡{scorer} le da vida al {team}!", "¿Hay partido? ¡{scorer} recorta para el {team}!",
                           "El {team} no ha dicho su última palabra: ¡responde {scorer}!", "Un rayo de esperanza para el {team}, y lleva la firma de {scorer}."],
    },
}

LINES: dict[str, dict[str, dict]] = {
    "goal": {
        "en": {
            "heart": GOAL_HEART["en"],
            "eye": ["{team} were on {before}% to win before that; now {after}%, a jump of {swing} points.", "That goal is worth {swing} points to {team}: from {before}% to {after}% to win.",
                    "Put a number on it: {team} go from {before}% to {after}% to win.", "{team} now on {after}% to win, up {swing} points from {before}%.",
                    "Before the goal, {team} had {before}% to win; it is {after}% now.", "Quietly huge: {swing} points for {team}, who are up to {after}% to win."],
            "heart_back": ["You can feel it, Joss. The whole match just tilted.", "And look at {scorer}'s face. That is what it means.",
                           "Numbers or no numbers, that is a moment {team} fans will keep.", "I'll take your word for it. I'm just enjoying the noise!",
                           "That's the thing about this game: one touch, and the story changes."],
            "eye_more": {"brace": ["Two for {scorer} now. Keep an eye on that name.", "A second of the match for {scorer}. Something is happening here."],
                         "hat_trick": ["That's three for {scorer}: the match ball is spoken for.", "Three for {scorer}. Remember where you were for this one."],
                         "four_plus": ["A {scorer_nth} for {scorer}. I have run out of comparisons, Wren.", "{scorer}, goal number {scorer_count} of the match. Write that one in pen."],
                         "late": ["And look at the clock: there is not much time left to answer that."], "any": ["Remember the minute: {minute}."]},
        },
        "fr": {
            "heart": GOAL_HEART["fr"],
            "eye": ["{team} était à {before} % de chances de gagner avant ce but ; {after} % maintenant, soit {swing} points de mieux.",
                    "Ce but vaut {swing} points pour {team}, qui passe de {before} % à {after} % de chances de gagner.",
                    "Mettons un chiffre dessus : {team} passe de {before} % à {after} % de chances de gagner.",
                    "{team} monte à {after} % de chances de gagner, {swing} points de plus qu'avant le but.",
                    "Avant le but, {team} avait {before} % de chances de gagner ; c'est {after} % désormais.",
                    "En silence, {team} vient de prendre {swing} points : {after} % de chances de gagner."],
            "heart_back": ["On le sent, Joss. Tout le match vient de pencher.", "Et regardez le visage {de_scorer}. Tout est là.",
                           "Chiffres ou pas, c'est un moment que {team} gardera longtemps.", "Je te crois sur parole. Moi, j'écoute le bruit du stade !",
                           "C'est ça, le football : une touche, et l'histoire change."],
            "eye_more": {"brace": ["Deux pour {scorer} déjà. Retenez ce nom.", "Et de deux pour {scorer}. Il se passe quelque chose."],
                         "hat_trick": ["Trois pour {scorer} : le ballon du match est réservé.", "Trois pour {scorer}. Souvenez-vous où vous étiez."],
                         "four_plus": ["Un {scorer_nth} pour {scorer}. Je n'ai plus de comparaison, Wren.", "{scorer}, {scorer_nth} but du match. Ça s'écrit à l'encre."],
                         "late": ["Et regardez l'horloge : il ne reste plus beaucoup de temps pour répondre."], "any": ["Retenez la minute : {minute}."]},
        },
        "es": {
            "heart": GOAL_HEART["es"],
            "eye": ["El {team} tenía un {before}% de opciones de ganar antes del gol; ahora, un {after}%: {swing} puntos más.",
                    "Ese gol vale {swing} puntos para el {team}, que pasa del {before}% al {after}% de opciones de ganar.",
                    "Pongámosle número: el {team} pasa del {before}% al {after}% de opciones de ganar.",
                    "El {team} sube al {after}% de opciones de ganar, {swing} puntos más que antes del gol.",
                    "Antes del gol, el {team} tenía un {before}% de opciones de ganar; ahora, un {after}%.",
                    "Sin hacer ruido, el {team} acaba de ganar {swing} puntos: {after}% de opciones de ganar."],
            "heart_back": ["Se nota, Joss. Todo el partido acaba de inclinarse.", "Y mira la cara de {scorer}. Ahí está todo.",
                           "Con números o sin ellos, es un momento que el {team} no olvidará.", "Te creo. ¡Yo solo escucho el rugido del estadio!",
                           "Así es el fútbol: un toque y la historia cambia."],
            "eye_more": {"brace": ["Ya van dos de {scorer}. Apunten ese nombre.", "Y van dos de {scorer}. Algo está pasando aquí."],
                         "hat_trick": ["Tres de {scorer}: el balón del partido ya tiene dueño.", "Tres de {scorer}. Recuerden dónde estaban."],
                         "four_plus": ["El {scorer_nth} de {scorer}. Me quedo sin comparaciones, Wren.", "{scorer}, {scorer_nth} gol del partido. Eso se escribe con tinta."],
                         "late": ["Y miren el reloj: queda poco tiempo para responder."], "any": ["Recuerden el minuto: el {minute}."]},
        },
    },
    "goal_low": {  # a goal that hardly moves the result: a milestone or a story line, never a probability
        "en": {
            "heart": ["{team} make it {team_count}! This one is for the scrapbook.", "And another! {scorer} joins the party for {team}.",
                      "The result was settled a while ago, but nobody told {scorer}!", "{team_count_cap} for {team}! The fans are singing every name on the team sheet."],
            "eye": ["Nothing changes in the result, but it is goal number {team_count} for {team} today.", "It barely moves the result; what it gives {team} is a {team_nth} goal on the board.",
                    "Not a goal that decides anything. A goal for the story of the day, though."],
            "heart_back": ["Let them enjoy it, Joss. Days like this do not come every week.", "Some goals win matches; some just make people happy."],
            "eye_more": {"brace": ["Two for {scorer} today."], "hat_trick": ["And that is three for {scorer}: a hat-trick."],
                         "four_plus": ["A {scorer_nth} of the day for {scorer}, Wren. Remember that."], "any": []},
        },
        "fr": {
            "heart": ["{team} en est à {team_count} ! Celui-là, c'est pour l'album souvenir.", "Et encore un ! {scorer} se joint à la fête pour {team}.",
                      "Le résultat était acquis depuis longtemps, mais personne ne l'a dit à {scorer} !", "{team_count_cap} pour {team} ! Les tribunes chantent tous les noms de la feuille de match."],
            "eye": ["Rien ne change pour le résultat, mais c'est le {team_nth} but pour {team} aujourd'hui.", "Ça ne bouge presque rien ; ça offre à {team} un {team_nth} but au tableau d'affichage.",
                    "Ce but ne décide rien. Il raconte quand même quelque chose de la soirée."],
            "heart_back": ["Laisse-les savourer, Joss. Des soirées comme ça, il n'y en a pas toutes les semaines.", "Certains buts gagnent des matchs ; d'autres rendent juste les gens heureux."],
            "eye_more": {"brace": ["Deux pour {scorer} ce soir."], "hat_trick": ["Et de trois pour {scorer} : un triplé."],
                         "four_plus": ["Un {scorer_nth} de la soirée pour {scorer}, Wren. Retiens-le."], "any": []},
        },
        "es": {
            "heart": ["¡El {team} ya lleva {team_count}! Este es para el álbum de recuerdos.", "¡Y otro más! {scorer} se suma a la fiesta del {team}.",
                      "El resultado estaba decidido hace rato, ¡pero nadie se lo dijo a {scorer}!", "¡{team_count_cap} del {team}! La grada canta todos los nombres de la alineación."],
            "eye": ["No cambia nada en el resultado, pero es el {team_nth_adj} gol del {team} hoy.", "Apenas mueve el resultado; lo que le da al {team} es un {team_nth_adj} gol en el marcador.",
                    "No es un gol que decida nada. Pero sí uno para la historia del día."],
            "heart_back": ["Déjalos disfrutar, Joss. Días así no llegan cada semana.", "Hay goles que ganan partidos; otros solo hacen feliz a la gente."],
            "eye_more": {"brace": ["Dos de {scorer} hoy."], "hat_trick": ["Y van tres de {scorer}: un triplete."],
                         "four_plus": ["El {scorer_nth} del día para {scorer}, Wren. Recuérdalo."], "any": []},
        },
    },
    "save": {
        "en": {
            "heart": ["What a save! {player}, you have just kept {team} in this!", "No! Not past {player}! What a stop!", "{player} gets something on it! I don't believe it!",
                      "Denied! {player} says no for {team}!"],
            "eye_open": ["Speaking as an old goalkeeper: that one deserves its own replay."],
            "eye": ["Credit the keeper, not the miss: {team} stay on {before}% to win instead of {other}%.", "That save is worth {value} points to {team}: {before}% to win, not {other}%.",
                    "Had it gone in, {team} would be down to {other}% to win; {player} kept them on {before}%.",
                    "The shot had a {xg}% chance of going in, and {team} stay on {before}% to win because of {player}.",
                    "Small margins: {value} points kept for {team}, still on {before}% to win."],
            "heart_back": ["Joss, you're smiling. I have never seen you smile at a number before!", "That's why you pay for a goalkeeper!",
                           "{team} fans will be singing that name on the way home.", "Breathe, everybody. Just breathe."],
            "eye_more": {"any": []},
        },
        "fr": {
            "heart": ["Quel arrêt ! {player}, tu viens de garder {team} en vie !", "Non ! Pas avec {player} ! Quelle parade !", "{player} la touche ! Je n'y crois pas !",
                      "Refusé ! {player} dit non pour {team} !"],
            "eye_open": ["En tant qu'ancien gardien, je le dis : celui-là mérite son propre ralenti."],
            "eye": ["Saluons le gardien plutôt que le tireur : {team} reste à {before} % de chances de gagner au lieu de {other} %.",
                    "Cet arrêt vaut {value} points pour {team} : {before} % de chances de gagner, pas {other} %.",
                    "Si ça rentrait, {team} tombait à {other} % de chances de gagner ; {player} garde {team} à {before} %.",
                    "Le tir avait {xg} % de chances de rentrer, et {team} reste à {before} % de chances de gagner grâce à {player}.",
                    "Petites marges : {value} points préservés pour {team}, toujours à {before} % de chances de gagner."],
            "heart_back": ["Joss, tu souris. Je ne t'ai jamais vu sourire devant un chiffre !", "Voilà pourquoi on paie un gardien !",
                           "Les supporters vont chanter ce nom en rentrant.", "Respirez, tout le monde. Respirez."],
            "eye_more": {"any": []},
        },
        "es": {
            "heart": ["¡Qué parada! ¡{player}, acabas de mantener vivo al {team}!", "¡No! ¡Con {player} no! ¡Qué intervención!", "¡{player} la toca! ¡No me lo creo!",
                      "¡Negado! ¡{player} dice que no para el {team}!"],
            "eye_open": ["Como exportero, lo digo: esa merece su propia repetición."],
            "eye": ["Mérito del portero, no del fallo: el {team} sigue en el {before}% de opciones de ganar en lugar del {other}%.",
                    "Esa parada vale {value} puntos para el {team}: {before}% de opciones de ganar, no {other}%.",
                    "Si entraba, el {team} caía al {other}% de opciones de ganar; {player} mantiene al {team} en el {before}%.",
                    "El tiro tenía un {xg}% de probabilidades de entrar, y el {team} sigue en el {before}% de opciones de ganar gracias a {player}.",
                    "Pequeños márgenes: {value} puntos salvados para el {team}, que sigue en el {before}% de opciones de ganar."],
            "heart_back": ["Joss, estás sonriendo. ¡Nunca te había visto sonreír por un número!", "¡Para eso se paga a un portero!",
                           "La afición va a cantar ese nombre de camino a casa.", "Respiren todos. Respiren."],
            "eye_more": {"any": []},
        },
    },
    "red_card": {
        "en": {"heart": ["Red card! {player} is off, and {team} must change everything now.", "And that's that for {player}. {team} will have to find another way."],
               "eye": ["Ten men change the sums: {team} go from {before}% to {after}% to win.", "With ten, {team} slip from {before}% to {after}% to win.",
                       "{team} were on {before}% to win; with a man less it is {after}%."],
               "heart_back": ["Long, lonely walk, that.", "The noise in here has changed completely.", "You can feel the mood change in the stands."], "eye_more": {"any": []}},
        "fr": {"heart": ["Carton rouge ! {player} doit sortir, et {team} doit tout repenser.", "Et c'est fini pour {player}. {team} va devoir trouver une autre solution."],
               "eye": ["À dix, les calculs changent : {team} passe de {before} % à {after} % de chances de gagner.", "Réduit à dix, {team} glisse de {before} % à {after} % de chances de gagner.",
                       "{team} était à {before} % de chances de gagner ; avec un homme de moins, {team} est à {after} %."],
               "heart_back": ["Longue marche solitaire vers le vestiaire.", "Le bruit dans le stade a complètement changé.", "On sent l'ambiance basculer en tribunes."], "eye_more": {"any": []}},
        "es": {"heart": ["¡Roja! {player} se va, y el {team} tiene que cambiarlo todo.", "Y se acabó para {player}. El {team} tendrá que buscar otro camino."],
               "eye": ["Con diez cambian las cuentas: el {team} pasa del {before}% al {after}% de opciones de ganar.", "Con uno menos, el {team} baja del {before}% al {after}% de opciones de ganar.",
                       "El {team} estaba en el {before}% de opciones de ganar; con un hombre menos, en el {after}%."],
               "heart_back": ["Larga y solitaria caminata hacia el vestuario.", "El ruido del estadio ha cambiado por completo.", "Se nota cómo cambia el ambiente en la grada."], "eye_more": {"any": []}},
    },
    "chance": {
        "en": {"heart": ["Oh, how has that not gone in?", "So close for {team}! {player} cannot believe it!", "That was the chance! {team} will be thinking about that one.",
                         "Oh, {player}! That had to be the one for {team}!"],
               "eye": ["That was a {xg}% chance, and a goal would have taken {team} from {before}% to {other}% to win.", "Big one: {team} stay on {before}% to win when it could have been {other}%.",
                       "One finish from {other}% to win, and {team} stay on {before}% instead.", "On a {xg}% chance, {team} were close to going from {before}% to {other}% to win."],
               "heart_back": ["Those are the ones you remember at the final whistle.", "You have to take those, Joss, you just have to.", "Somewhere on that bench, someone has their head in their hands."],
               "eye_more": {"any": []}},
        "fr": {"heart": ["Mais comment ce ballon n'est pas rentré ?", "Si près pour {team} ! {player} n'en revient pas !", "C'était l'occasion ! {team} va y repenser.",
                         "Oh, {player} ! Celle-là devait être la bonne pour {team} !"],
               "eye": ["C'était une occasion à {xg} %, et un but aurait fait passer {team} de {before} % à {other} % de chances de gagner.",
                       "Grosse occasion : {team} reste à {before} % de chances de gagner alors que ça pouvait être {other} %.",
                       "Un geste de plus, et {team} passait de {before} % à {other} % de chances de gagner.", "Sur une occasion à {xg} %, {team} est resté à {before} % de chances de gagner au lieu de {other} %."],
               "heart_back": ["Ce sont celles dont on se souvient au coup de sifflet final.", "Il faut les mettre, celles-là, Joss.", "Sur le banc, quelqu'un a la tête entre les mains."],
               "eye_more": {"any": []}},
        "es": {"heart": ["¿Pero cómo no ha entrado eso?", "¡Qué cerca para el {team}! ¡{player} no se lo cree!", "¡Era la ocasión! El {team} se va a acordar de esa.",
                         "¡Ay, {player}! ¡Esa tenía que ser la buena para el {team}!"],
               "eye": ["Era una ocasión del {xg}%, y un gol habría llevado al {team} del {before}% al {other}% de opciones de ganar.",
                       "Ocasión grande: el {team} sigue en el {before}% de opciones de ganar cuando pudo ser el {other}%.",
                       "Un remate más fino y el {team} pasaba del {before}% al {other}% de opciones de ganar.", "Con una ocasión del {xg}%, el {team} se quedó en el {before}% de opciones de ganar en vez del {other}%."],
               "heart_back": ["Esas son las que se recuerdan al final.", "Esas hay que meterlas, Joss.", "En el banquillo, alguien se lleva las manos a la cabeza."],
               "eye_more": {"any": []}},
    },
    "ruling": {  # VAR rules a goal out; "eye" is keyed by the reason, offside split into tight and clear (the margin in cm stays off air: frames are 30 cm apart at sprint speed)
        "en": {"heart": ["No! Oh no. It's gone. VAR has taken it away from {scorer}.", "And it's off. VAR rules it out, and {team} cannot believe it."],
               "eye": {"offside_tight": ["Offside by a whisker, judged at the moment of the pass."], "offside_clear": ["Clearly offside at the moment of the pass. No debate."], "offside": ["Offside, says the review. The goal comes off the board."],
                       "handball": ["Handball, says the review, and the goal comes off the board."], "other": ["The review says no, and the goal comes off the board."]},
               "heart_back": {"offside_tight": ["That close, Joss. A whole celebration, for a whisker."], "any": ["All that joy, and it counts for nothing."]}},
        "fr": {"heart": ["Non ! Oh non. C'est annulé. La VAR reprend le but à {scorer}.", "Et c'est refusé. La VAR annule, et {team} n'en revient pas."],
               "eye": {"offside_tight": ["Hors-jeu d'un rien, au moment de la passe."], "offside_clear": ["Hors-jeu net au moment de la passe. Pas de débat."], "offside": ["Hors-jeu, dit la vidéo. Le but disparaît du tableau."],
                       "handball": ["Main, dit la vidéo, et le but disparaît du tableau."], "other": ["La vidéo dit non, et le but disparaît du tableau."]},
               "heart_back": {"offside_tight": ["Si peu, Joss. Toute une célébration, pour un rien."], "any": ["Toute cette joie, et elle ne compte pas."]}},
        "es": {"heart": ["¡No! Ay, no. Anulado. El VAR le quita el gol a {scorer}.", "Y se anula. El VAR lo invalida, y el {team} no se lo cree."],
               "eye": {"offside_tight": ["Fuera de juego por muy poco, en el momento del pase."], "offside_clear": ["Fuera de juego claro en el momento del pase. Sin debate."], "offside": ["Fuera de juego, dice la revisión. El gol desaparece del marcador."],
                       "handball": ["Mano, dice la revisión, y el gol desaparece del marcador."], "other": ["La revisión dice que no, y el gol desaparece del marcador."]},
               "heart_back": {"offside_tight": ["Tan poco, Joss. Toda una celebración, por nada."], "any": ["Toda esa alegría, y no cuenta para nada."]}},
    },
    "insight": {  # keyed by insight kind; numbers stay with the model, which has the history facts and their meanings
        "en": {"heart": {"record": ["Hold on, Joss, are you telling me nobody has done this in the league before?"], "rarity": ["Joss, how often do we see something like this?"],
                         "streak": ["{subject} just keep going!"], "milestone": ["A landmark for {subject}, and what a day to reach it!"],
                         "storyline": ["Every match has its story, and this one is a good one."], "tactical": ["Joss, you have been waiting to tell me something."]},
               "eye": {"record": ["Not in this league's history. That is a record."], "rarity": ["Almost never. That is what makes it worth telling."],
                       "streak": ["The run goes on, and it is worth watching where it ends."], "milestone": ["One for the record books, and earned the hard way."],
                       "storyline": ["Keep that in mind for the next few minutes."], "tactical": ["Yes: the same idea keeps coming back. Watch for it."]}},
        "fr": {"heart": {"record": ["Attends, Joss, tu me dis que personne n'avait fait ça dans ce championnat ?"], "rarity": ["Joss, on voit ça tous les combien ?"],
                         "streak": ["{subject} ne s'arrête plus !"], "milestone": ["Un cap pour {subject}, et quel jour pour l'atteindre !"],
                         "storyline": ["Chaque match a son histoire, et celle-ci est belle."], "tactical": ["Joss, tu attends de me dire quelque chose."]},
               "eye": {"record": ["Jamais dans l'histoire de ce championnat. C'est un record."], "rarity": ["Presque jamais. C'est pour ça qu'il faut le raconter."],
                       "streak": ["La série continue, et on a envie de voir où elle s'arrête."], "milestone": ["Une page pour les livres d'histoire, et pas volée."],
                       "storyline": ["Gardez ça en tête pour les prochaines minutes."], "tactical": ["Oui : la même idée revient sans cesse. Surveillez-la."]}},
        "es": {"heart": {"record": ["Espera, Joss, ¿me estás diciendo que nadie había hecho esto en la liga?"], "rarity": ["Joss, ¿cada cuánto vemos algo así?"],
                         "streak": ["¡{subject} no se detiene!"], "milestone": ["¡Una marca para {subject}, y qué día para alcanzarla!"],
                         "storyline": ["Cada partido tiene su historia, y esta es buena."], "tactical": ["Joss, estás deseando contarme algo."]},
               "eye": {"record": ["Nunca en la historia de esta liga. Es un récord."], "rarity": ["Casi nunca. Por eso merece contarse."],
                       "streak": ["La racha sigue, y apetece ver dónde termina."], "milestone": ["Una para los libros de historia, y bien ganada."],
                       "storyline": ["Tengan eso en cuenta para los próximos minutos."], "tactical": ["Sí: la misma idea vuelve una y otra vez. Atentos."]}},
    },
}


def fr_typography(obj):
    """Applies the French no-break spaces to every French line of LINES (and nested lists and dicts)."""
    if isinstance(obj, str):
        return _fr(obj)
    if isinstance(obj, (list, tuple)):
        return type(obj)(fr_typography(x) for x in obj)
    return {k: fr_typography(v) for k, v in obj.items()}


for _kind in LINES.values():
    _kind["fr"] = fr_typography(_kind["fr"])


# --- filling a template ---------------------------------------------------------------------------

def placeholders(template: str) -> list[str]:
    return [name for _, name, _, _ in Formatter().parse(template) if name]


def fill(template: str, values: dict, fields: dict) -> tuple[str, list[dict]] | None:
    """The template with its placeholders filled, and a claim for each numeric one (`fields` maps a placeholder to its fact field); None when a value is missing."""
    names = placeholders(template)
    if any(values.get(n) is None for n in names):
        return None
    claims, seen = [], set()
    for n in names:
        if n in fields and n not in seen:
            seen.add(n)
            v = values[n]
            if isinstance(v, (int, float)):
                claims.append({"value": v, "source_field": fields[n]})
            else:  # a minute or a score: claim each number it holds
                claims += [{"value": int(x), "source_field": fields[n]} for x in re.findall(r"\d+", str(v))]
    text = template.format(**values)
    return text[:1].upper() + text[1:] if text[:1].islower() else text, claims


def stable(*parts) -> int:
    """A hash that does not change between runs (Python's hash() is salted)."""
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def rotated(items: list, *seed) -> list:
    if not items:
        return []
    k = stable(*seed) % len(items)
    return items[k:] + items[:k]


# --- per-match memory -------------------------------------------------------------------------------

VERBS = {  # the verbs a headline is built on: the templates avoid reusing one within a match
    "en": {"scores", "strikes", "finds", "delivers", "levels", "brings", "puts", "gives", "extends", "stretches", "pulls", "opens", "breaks", "beats", "denies", "keeps",
           "stands", "misses", "makes", "adds", "joins", "goes", "sets", "reaches", "finishes", "fires", "cannot", "has"},
    "fr": {"marque", "frappe", "trouve", "fait", "conclut", "signe", "égalise", "remet", "donne", "met", "creuse", "enfonce", "réduit", "relance", "ouvre", "débloque", "trompe",
           "interpose", "dit", "ferme", "sauve", "sort", "porte", "inscrit", "joint", "tremble", "doit", "passe", "manque", "profite", "établit", "atteint", "continue"},
    "es": {"marca", "define", "bate", "acierta", "manda", "firma", "abre", "estrena", "empata", "iguala", "adelanta", "pone", "amplía", "agranda", "recorta", "da", "evita",
           "dice", "salva", "cierra", "hace", "suma", "perdona", "rozó", "va", "alcanza", "sigue", "perdona", "establece"},
}
OPENING_WORDS, PHRASE_WORDS = 3, 5


def tokens(text: str, names: set[str]) -> list[str]:
    """Lower-case words, numbers as #, names as @ (consecutive names collapse)."""
    out: list[str] = []
    for w in re.findall(r"[\wÀ-ÿ%']+", text.replace(NB, " ")):
        t = "@" if w in names else re.sub(r"\d+", "#", w.lower())
        if t == "@" and out and out[-1] == "@":
            continue
        out.append(t)
    return out


def names_from(facts: dict) -> set[str]:
    """Capitalised words of the facts' text values: the clubs and players, masked in openings and phrases."""
    return {w for v in facts.values() if isinstance(v, str) for w in re.findall(r"[A-ZÀ-Ý][\wÀ-ÿ'-]+", v)}


class PhraseMemory:
    """What one match has already said, per language: headlines, openings, verbs, phrases and tics, for texts and duo lines alike."""

    def __init__(self) -> None:
        self.docs: dict[str, list[tuple[str, str, str]]] = {}  # doc id -> [(lang, kind, text)], kind "headline", "why" or "duo"
        self.names: set[str] = set()

    def _items(self, exclude: str | None = None):
        for doc_id, items in self.docs.items():
            if doc_id != exclude:
                yield from items

    def headlines(self, lang: str, exclude: str | None = None) -> set[str]:
        return {self.key(t) for l, k, t in self._items(exclude) if l == lang and k == "headline"}

    def openings(self, lang: str, exclude: str | None = None) -> list[str]:
        return [self.opening(t) for l, k, t in self._items(exclude) if l == lang and k != "headline"]

    def phrases(self, lang: str, exclude: str | None = None) -> set[tuple]:
        return {g for l, _, t in self._items(exclude) if l == lang for g in self.ngrams(t)}

    def verbs(self, lang: str, exclude: str | None = None) -> list[str]:
        return [w for l, k, t in self._items(exclude) if l == lang and k == "headline" for w in self.verbs_in(t, lang)]

    def tics_used(self, exclude: str | None = None) -> set[str]:
        all_tics = {t for p in PERSONAS.values() for v in p.voices.values() for t in v.tics}
        return {tic for _, k, t in self._items(exclude) if k == "duo" for tic in all_tics if tic in t}

    @staticmethod
    def key(text: str) -> str:
        return " ".join(re.findall(r"[\wÀ-ÿ%']+", text.replace(NB, " ").lower()))

    def opening(self, text: str) -> str:
        return " ".join(tokens(text, self.names)[:OPENING_WORDS])

    def ngrams(self, text: str) -> set[tuple]:
        t = tokens(text, self.names)
        return {tuple(t[i:i + PHRASE_WORDS]) for i in range(len(t) - PHRASE_WORDS + 1)}

    @staticmethod
    def verbs_in(text: str, lang: str) -> list[str]:
        return [w for w in re.findall(r"[\wÀ-ÿ]+", text.lower()) if w in VERBS.get(lang, set())]

    def learn_names(self, facts: dict) -> None:
        self.names |= names_from(facts)

    def record(self, doc_id: str, texts: list[dict], duo: list[dict], facts: dict | None = None) -> None:
        """Keeps what a document finally says; a new version of the document replaces the old one."""
        if facts:
            self.learn_names(facts)
        items = []
        for t in texts or []:
            items += [(t.get("lang"), "headline", str(t.get("headline", ""))), (t.get("lang"), "why", str(t.get("why", "")))]
        items += [(d.get("lang"), "duo", str(d.get("line", ""))) for d in duo or []]
        self.docs[doc_id] = items

    def issues(self, doc_id: str, texts: list[dict], duo: list[dict]) -> tuple[list[dict], list[dict]]:
        """(errors, warnings) of a draft against the rest of the match: an identical headline fails; a reused opening, phrase or tic is a warning."""
        errors, warnings = [], []
        seen_open: dict[str, set] = {}
        seen_phrase: dict[str, set] = {}
        tics = self.tics_used(exclude=doc_id)
        for t in texts or []:
            lang = t.get("lang")
            if self.key(str(t.get("headline", ""))) in self.headlines(lang, exclude=doc_id):
                errors.append({"rule": "repeated_headline", "lang": lang, "text": t.get("headline")})
        lines = [(t.get("lang"), str(t.get("why", ""))) for t in texts or []] + [(d.get("lang"), str(d.get("line", ""))) for d in duo or []]
        for lang, text in lines:
            if lang not in seen_open:
                seen_open[lang] = set(self.openings(lang, exclude=doc_id))
                seen_phrase[lang] = self.phrases(lang, exclude=doc_id)
            opening = self.opening(text)
            if opening and opening in seen_open[lang]:
                warnings.append({"rule": "repeated_opening", "lang": lang, "text": opening})
            reused = self.ngrams(text) & seen_phrase[lang]
            if reused:
                warnings.append({"rule": "repeated_phrase", "lang": lang, "text": " ".join(sorted(reused)[0])})
            seen_open[lang].add(opening)
            seen_phrase[lang] |= self.ngrams(text)
        for d in duo or []:
            for tic in tics:
                if tic in str(d.get("line", "")):
                    warnings.append({"rule": "repeated_tic", "lang": d.get("lang"), "text": tic})
        return errors, warnings

    def avoid(self, languages: list[str], limit: int = 12) -> dict:
        """What the Writer prompt is told not to repeat."""
        out: dict = {}
        for lang in languages:
            heads = [t for l, k, t in self._items() if l == lang and k == "headline"]
            openings = list(dict.fromkeys(self.opening(t) for l, k, t in self._items() if l == lang and k != "headline"))
            verbs = list(dict.fromkeys(self.verbs(lang)))
            if heads or openings:
                out[lang] = {"headlines": heads[-limit:], "openings": openings[-limit:], "verbs": verbs[-limit:]}
        tics = sorted(self.tics_used())
        if tics:
            out["tics_used"] = tics
        return out
