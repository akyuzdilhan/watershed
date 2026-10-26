"""Fallback texts and duo lines of the insights the engine produces, by family of fact, with the numbers of `headline_facts`.

Every detector of `worker.insights` has its family here, so each insight names its feat and who did it: a player's goals (a substitute's against substitutes, a starter's against every player), a comeback, a lost half-time lead, a late equaliser or go-ahead goal, ten men who score or win, a record win or the end of a wait in the fixture, the shots, the passes, the goals of the match or of a club, the fastest hat-trick, a run, and the goals against expected goals. The generic lines of `writer.INSIGHT_TEMPLATES` are left for a fact no family can tell. Each number is a placeholder named after its fact field, so `duo.fill` claims it against that field and the Verifier checks it.

What a viewer can picture: the feat named for who did it ("the first four-goal haul by a substitute in 50 seasons"), and a team's own landmark in seasons ("United's first away defeat after leading at half-time since 2020/21"). League-wide rates, match counts, ratios and bits ("once in 83", "about 3 a season", "228 times rarer", "0 in 19,000") never go on air or into a duo line: they stay for the producer in the console, in `rarity.basis` and `why_first`, and `writer.on_air` keeps them out of what the model sees.

Each fact can be told several times in a match (a brace, then a hat-trick, then the stat of the match at full time), so every case has at least two duo lines: the per-match memory never lets the same line be said twice.
"""

from worker.duo import fr_typography

SEP = {"en": ",", "fr": " ", "es": "."}  # thousands
DEC = {"en": ".", "fr": ",", "es": ","}


class Num(int):
    """An integer that prints with the thousands separator of its language and stays an int for the claims."""

    def __new__(cls, value: int, lang: str):
        obj = super().__new__(cls, value)
        obj.lang = lang
        return obj

    def __format__(self, spec: str) -> str:
        return f"{int(self):,}".replace(",", SEP[self.lang])


class Ord(Num):
    """An ordinal: 6th, 6e, 6.º."""

    def __format__(self, spec: str) -> str:
        n = int(self)
        if self.lang == "fr":
            return "1er" if n == 1 else f"{n}e"
        if self.lang == "es":
            return f"{n}.º"
        return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


class Dec(float):
    def __new__(cls, value: float, lang: str):
        obj = super().__new__(cls, value)
        obj.lang = lang
        return obj

    def __format__(self, spec: str) -> str:
        return f"{float(self):g}".replace(".", DEC[self.lang])


FEAT = {  # what a player's goals in one match are called
    "en": {2: "brace", 3: "hat-trick", 4: "four-goal haul", 5: "five-goal haul"},
    "fr": {2: "doublé", 3: "triplé", 4: "quadruplé", 5: "quintuplé"},
    "es": {2: "doblete", 3: "triplete", 4: "póker", 5: "repóker"},
}
DONE = {  # a comeback's stage, as a past participle after "had not" / "n'avait plus" / "no había"
    "en": {"level": "drawn level", "ahead": "gone ahead", "won": "won a match"},
    "fr": {"level": "égalisé", "ahead": "pris l'avantage", "won": "gagné un match"},
    "es": {"level": "empatado", "ahead": "remontado hasta adelantarse", "won": "ganado un partido"},
}
VENUE = {  # where a team lost: "away defeat", "défaite à l'extérieur", "derrota fuera de casa"
    "en": {"home": "home", "away": "away"},
    "fr": {"home": "à domicile", "away": "à l'extérieur"},
    "es": {"home": "en casa", "away": "fuera de casa"},
}

# Markers: {never} needs a zero history count, {club_first} a team that had never done it, {rare} a feat rarer than once a season,
# {often} a feat the league sees at least once a season (told without the rate), {starter} a player who started the match, {first_win} a
# first win in the fixture at this ground; {plain} is told only when no other template of its part can be (`pools`). A template with a
# placeholder the facts lack is skipped.
T = {  # family -> lang -> part -> templates, or {stage: templates, "any": templates} when the stage changes what is true
    "scorer": {  # a substitute's goals are compared with substitutes' ({came_on} and the bench words), a starter's with every player's ({starter})
        "en": {"headlines": ["{player}: a {feat} off the bench", "On at {came_on}, {player} has a {feat}", "A {feat} for {player}", "{player} scores a {feat} for {team}{starter}"],
               "whys": ["On at {came_on}, {player} scored at {goal_minutes}. The first {feat} by a substitute in {history_seasons} seasons of this league.{never}",
                        "On at {came_on}, {player} scored at {goal_minutes}. Only the {history_rank_ord} {feat} by a substitute in {history_seasons} seasons, the first since {last_player} in {last_season}.",
                        "On at {came_on}, {player} scored at {goal_minutes}: a {feat} off the bench.{often}",
                        "{player} scored at {goal_minutes}: the first {feat} in {history_seasons} seasons of this league.{never}{starter}",
                        "{player} scored at {goal_minutes}. Only the {history_rank_ord} {feat} in {history_seasons} seasons, the first since {last_player} in {last_season}.{starter}",
                        "{player} scored at {goal_minutes}: a {feat} for {team}.{often}{starter}",
                        "On at {came_on}, {player} scored at {goal_minutes}: a {feat} off the bench.{plain}", "{player} scored at {goal_minutes}: a {feat} for {team}.{plain}{starter}"],
               "heart": ["{player} again! Joss, has a substitute ever done this?{rare}", "A {feat} for {player}! And he only came on at {came_on}!",
                         "{player} again, and off the bench too!{often}", "A {feat} for {player}! {team} fans will remember this one.{starter}",
                         "{player} again! Joss, has anyone ever done this here?{never}{starter}", "{player} again! Joss, when did we last see one of these?{rare}{starter}",
                         "{player} again, and that is a {feat}!{often}{starter}"],
               "eye": ["Never, Wren. In {history_seasons} seasons, no player coming off the bench had scored {goals}.{never}",
                       "A first in {history_seasons} seasons, Wren: no substitute had ever scored {goals} in this league.{never}",
                       "The {history_rank_ord} in {history_seasons} seasons, Wren. The last one was {last_player}, in {last_season}.",
                       "Not since {last_player}, in {last_season}, Wren.{rare}",
                       "{goals} goals off the bench, Wren. Not a bad way to come on.{often}",
                       "That is what a substitute is for, Wren: {goals} goals since coming on.{often}",
                       "Never, Wren. In {history_seasons} seasons, nobody had scored {goals} in a match in this league.{never}{starter}",
                       "A first in {history_seasons} seasons, Wren: nobody had scored {goals} in one league match.{never}{starter}",
                       "Only the {history_rank_ord} in {history_seasons} seasons, Wren.",
                       "{goals} goals for {player} today, Wren. A day he will remember.{often}{starter}",
                       "{goals} goals in one match for {player}, Wren: {goal_minutes}.{often}{starter}"]},
        "fr": {"headlines": ["{player}, un {feat} en sortie de banc", "Entré à la {came_on}, {player} signe un {feat}", "Un {feat} pour {player}", "{player} s'offre un {feat} pour {team}{starter}"],
               "whys": ["Entré à la {came_on}, {player} a marqué : {goal_minutes}. Premier {feat} d'un joueur entré en cours de match en {history_seasons} saisons de championnat.{never}",
                        "Entré à la {came_on}, {player} a marqué : {goal_minutes}. Seulement le {history_rank_ord} {feat} d'un remplaçant en {history_seasons} saisons, le premier depuis {last_player} en {last_season}.",
                        "Entré à la {came_on}, {player} a marqué : {goal_minutes}. Un {feat} en sortie de banc.{often}",
                        "{player} a marqué : {goal_minutes}. Premier {feat} en {history_seasons} saisons de championnat.{never}{starter}",
                        "{player} a marqué : {goal_minutes}. Seulement le {history_rank_ord} {feat} en {history_seasons} saisons, le premier depuis {last_player} en {last_season}.{starter}",
                        "{player} a marqué : {goal_minutes}. Un {feat} pour {team}.{often}{starter}",
                        "Entré à la {came_on}, {player} a marqué : {goal_minutes}. Un {feat} en sortie de banc.{plain}", "{player} a marqué : {goal_minutes}. Un {feat} pour {team}.{plain}{starter}"],
               "heart": ["Encore {player} ! Joss, un remplaçant a déjà fait ça ?{rare}", "Un {feat} pour {player} ! Et il n'est entré qu'à la {came_on} !",
                         "Encore {player}, et en sortie de banc !{often}", "Un {feat} pour {player} ! Les supporters de {team} s'en souviendront.{starter}",
                         "Encore {player} ! Joss, quelqu'un a déjà fait ça dans ce championnat ?{never}{starter}", "Encore {player} ! Joss, à quand remonte le dernier ?{rare}{starter}",
                         "Encore {player}, et c'est un {feat} !{often}{starter}"],
               "eye": ["Jamais, Wren. En {history_seasons} saisons, aucun joueur entré en cours de match n'avait marqué {goals} fois.{never}",
                       "Une première en {history_seasons} saisons, Wren : aucun remplaçant n'avait jamais marqué {goals} buts dans ce championnat.{never}",
                       "Le {history_rank_ord} en {history_seasons} saisons, Wren. Le dernier : {last_player}, en {last_season}.",
                       "Pas depuis {last_player}, en {last_season}, Wren.{rare}",
                       "{goals} buts depuis son entrée en jeu, Wren. Pas mal pour un remplaçant.{often}",
                       "Voilà à quoi sert un remplaçant, Wren : {goals} buts depuis son entrée.{often}",
                       "Jamais, Wren. En {history_seasons} saisons, personne n'avait marqué {goals} buts dans un match de ce championnat.{never}{starter}",
                       "Une première en {history_seasons} saisons, Wren : personne n'avait marqué {goals} buts dans un même match.{never}{starter}",
                       "Seulement le {history_rank_ord} en {history_seasons} saisons, Wren.",
                       "{goals} buts pour {player} aujourd'hui, Wren. Une journée qu'il n'oubliera pas.{often}{starter}",
                       "{goals} buts dans le même match pour {player}, Wren : {goal_minutes}.{often}{starter}"]},
        "es": {"headlines": ["{player}, un {feat} desde el banquillo", "Entró en el {came_on} y {player} firma un {feat}", "Un {feat} para {player}", "{player} firma un {feat} para el {team}{starter}"],
               "whys": ["Entró en el {came_on} y {player} marcó: {goal_minutes}. Primer {feat} de un suplente en {history_seasons} temporadas de liga.{never}",
                        "Entró en el {came_on} y {player} marcó: {goal_minutes}. Solo el {history_rank_ord} {feat} de un suplente en {history_seasons} temporadas, el primero desde {last_player} en {last_season}.",
                        "Entró en el {came_on} y {player} marcó: {goal_minutes}. Un {feat} desde el banquillo.{often}",
                        "{player} marcó: {goal_minutes}. Primer {feat} en {history_seasons} temporadas de liga.{never}{starter}",
                        "{player} marcó: {goal_minutes}. Solo el {history_rank_ord} {feat} en {history_seasons} temporadas, el primero desde {last_player} en {last_season}.{starter}",
                        "{player} marcó: {goal_minutes}. Un {feat} para el {team}.{often}{starter}",
                        "Entró en el {came_on} y {player} marcó: {goal_minutes}. Un {feat} desde el banquillo.{plain}", "{player} marcó: {goal_minutes}. Un {feat} para el {team}.{plain}{starter}"],
               "heart": ["¡Otra vez {player}! Joss, ¿un suplente había hecho esto?{rare}", "¡Un {feat} de {player}! ¡Y entró en el {came_on}!",
                         "¡Otra vez {player}, y desde el banquillo!{often}", "¡Un {feat} de {player}! La afición del {team} no lo olvidará.{starter}",
                         "¡Otra vez {player}! Joss, ¿alguien había hecho esto en esta liga?{never}{starter}", "¡Otra vez {player}! Joss, ¿cuándo fue el último?{rare}{starter}",
                         "¡Otra vez {player}, y ya es un {feat}!{often}{starter}"],
               "eye": ["Nunca, Wren. En {history_seasons} temporadas, ningún jugador salido del banquillo había marcado {goals}.{never}",
                       "Una primera vez en {history_seasons} temporadas, Wren: ningún suplente había marcado {goals} goles en esta liga.{never}",
                       "El {history_rank_ord} en {history_seasons} temporadas, Wren. El último: {last_player}, en {last_season}.",
                       "No pasaba desde {last_player}, en {last_season}, Wren.{rare}",
                       "{goals} goles desde que salió del banquillo, Wren. No está mal para un suplente.{often}",
                       "Para eso está un suplente, Wren: {goals} goles desde que entró.{often}",
                       "Nunca, Wren. En {history_seasons} temporadas, nadie había marcado {goals} goles en un partido de esta liga.{never}{starter}",
                       "Una primera vez en {history_seasons} temporadas, Wren: nadie había marcado {goals} goles en un mismo partido.{never}{starter}",
                       "Solo el {history_rank_ord} en {history_seasons} temporadas, Wren.",
                       "{goals} goles de {player} hoy, Wren. Un día para recordar.{often}{starter}",
                       "{goals} goles en el mismo partido para {player}, Wren: {goal_minutes}.{often}{starter}"]},
    },
    "comeback": {
        "en": {"headlines": {"level": ["{team} draw level from {trailing_score} down", "{team} back on terms after {trailing_score}"],
                             "ahead": ["{team} turn {trailing_score} around", "{team} now lead after trailing {trailing_score}"],
                             "won": ["{team} win from {trailing_score} down", "{team} complete the comeback from {trailing_score}"], "any": ["{team} come back from {trailing_score}"]},
               "whys": ["{team} had not {done} from {deficit_goals} goals down since {club_last_season}, {club_seasons_since} seasons ago.",
                        "A first for {team} in {history_seasons} seasons: the club had never {done} from {deficit_goals} goals down.{club_first}",
                        "{team} had not {done} from {deficit_goals} goals down since {club_last_season}.{plain}", "{team} trailed {trailing_score} and came back to {score}.{plain}"],
               "heart": {"level": ["{trailing_score} down, and look at {team} now!", "Back from {trailing_score} down! {team} are level!"],
                         "ahead": ["Who saw this coming at {trailing_score}?", "{team} were {trailing_score} down, and now they lead!"],
                         "won": ["{team} have done it! From {trailing_score} down to a win!", "{trailing_score} down, and {team} have won it!"],
                         "any": ["{trailing_score} down, and look at {team} now!"]},
               "eye": {"level": ["{club_seasons_since} seasons since {team} last drew level from {deficit_goals} goals down, Wren.",
                                 "Not since {club_last_season} had {team} drawn level from {deficit_goals} goals down, Wren.",
                                 "Never, Wren. Not once in {history_seasons} seasons had {team} drawn level from {deficit_goals} goals down.{club_first}",
                                 "A first for {team} in {history_seasons} seasons, Wren: level after {deficit_goals} goals down.{club_first}",
                                 "{trailing_score} down, Wren, and they got back to {score}.{plain}"],
                       "ahead": ["{club_seasons_since} seasons since {team} last went ahead from {deficit_goals} goals down, Wren.",
                                 "Not since {club_last_season} had {team} turned a {deficit_goals}-goal deficit into a lead, Wren.",
                                 "Never, Wren. Not once in {history_seasons} seasons had {team} gone ahead from {deficit_goals} goals down.{club_first}",
                                 "A first for {team} in {history_seasons} seasons, Wren: ahead after {deficit_goals} goals down.{club_first}",
                                 "{trailing_score} down, Wren, and ahead at {score}.{plain}"],
                       "won": ["{club_seasons_since} seasons since {team} last won from {deficit_goals} goals down, Wren. That is how long they had waited.",
                               "Not since {club_last_season} had {team} won from {deficit_goals} goals down, Wren.",
                               "Never, Wren. Not once in {history_seasons} seasons had {team} won from {deficit_goals} goals down.{club_first}",
                               "A first for {team} in {history_seasons} seasons, Wren: a win after {deficit_goals} goals down.{club_first}",
                               "{trailing_score} down, Wren, and they won {score}.{plain}"],
                       "any": ["{club_seasons_since} seasons, Wren. That is how long {team} had waited.", "Never, Wren. Not once in {history_seasons} seasons had {team} done it.{club_first}",
                               "{trailing_score} down, Wren, and look at the score now: {score}.{plain}"]}},
        "fr": {"headlines": {"level": ["{team} recolle après avoir été mené {trailing_score}", "{team} revient de {trailing_score}"],
                             "ahead": ["Mené {trailing_score}, {team} passe devant", "{team} renverse un {trailing_score}"],
                             "won": ["{team} gagne après avoir été mené {trailing_score}", "Mené {trailing_score}, {team} renverse tout"], "any": ["{team} remonte de {trailing_score}"]},
               "whys": ["{team} n'avait plus {done} après avoir été mené de {deficit_goals} buts depuis {club_last_season}, il y a {club_seasons_since} saisons.",
                        "Une première pour {team} en {history_seasons} saisons : jamais le club n'avait {done} après avoir été mené de {deficit_goals} buts.{club_first}",
                        "{team} n'avait plus {done} après avoir été mené de {deficit_goals} buts depuis {club_last_season}.{plain}", "Mené {trailing_score}, {team} est revenu à {score}.{plain}"],
               "heart": {"level": ["Mené {trailing_score}, et regarde {team} maintenant !", "Revenu de {trailing_score} ! {team} recolle au score !"],
                         "ahead": ["Qui y croyait à {trailing_score} ?", "Mené {trailing_score}, et {team} passe devant !"],
                         "won": ["{team} l'a fait ! Mené {trailing_score}, et vainqueur !", "Mené {trailing_score}, et {team} s'impose !"],
                         "any": ["Mené {trailing_score}, et regarde {team} maintenant !"]},
               "eye": {"level": ["{club_seasons_since} saisons que {team} n'était plus revenu à hauteur après avoir été mené de {deficit_goals} buts, Wren.",
                                 "Pas depuis {club_last_season}, Wren : {team} n'avait plus égalisé après avoir été mené de {deficit_goals} buts.",
                                 "Jamais, Wren. En {history_seasons} saisons, {team} n'était jamais revenu à hauteur après avoir été mené de {deficit_goals} buts.{club_first}",
                                 "Une première pour {team} en {history_seasons} saisons, Wren : revenir après {deficit_goals} buts de retard.{club_first}",
                                 "Mené {trailing_score}, Wren, et revenu à {score}.{plain}"],
                       "ahead": ["{club_seasons_since} saisons que {team} n'avait plus pris l'avantage après avoir été mené de {deficit_goals} buts, Wren.",
                                 "Pas depuis {club_last_season}, Wren : {team} n'avait plus renversé un retard de {deficit_goals} buts.",
                                 "Jamais, Wren. En {history_seasons} saisons, {team} n'avait jamais pris l'avantage après avoir été mené de {deficit_goals} buts.{club_first}",
                                 "Une première pour {team} en {history_seasons} saisons, Wren : passer devant après {deficit_goals} buts de retard.{club_first}",
                                 "Mené {trailing_score}, Wren, et devant à {score}.{plain}"],
                       "won": ["{club_seasons_since} saisons que {team} n'avait plus gagné après avoir été mené de {deficit_goals} buts, Wren.",
                               "Pas depuis {club_last_season}, Wren : {team} n'avait plus gagné un match en étant mené de {deficit_goals} buts.",
                               "Jamais, Wren. En {history_seasons} saisons, {team} n'avait jamais gagné après avoir été mené de {deficit_goals} buts.{club_first}",
                               "Une première pour {team} en {history_seasons} saisons, Wren : gagner après {deficit_goals} buts de retard.{club_first}",
                               "Mené {trailing_score}, Wren, et vainqueur {score}.{plain}"],
                       "any": ["{club_seasons_since} saisons, Wren. Voilà combien de temps {team} attendait ça.", "Jamais, Wren. En {history_seasons} saisons, {team} ne l'avait jamais fait.{club_first}",
                               "Mené {trailing_score}, Wren, et regarde le score maintenant : {score}.{plain}"]}},
        "es": {"headlines": {"level": ["El {team} empata tras ir {trailing_score}", "El {team} iguala un {trailing_score}"],
                             "ahead": ["El {team} le da la vuelta al {trailing_score}", "El {team} ya gana tras ir {trailing_score}"],
                             "won": ["El {team} gana tras ir perdiendo {trailing_score}", "El {team} completa la remontada del {trailing_score}"], "any": ["El {team} remonta un {trailing_score}"]},
               "whys": ["El {team} no había {done} tras ir perdiendo por {deficit_goals} goles desde {club_last_season}, hace {club_seasons_since} temporadas.",
                        "Una primera vez para el {team} en {history_seasons} temporadas: nunca había {done} tras ir perdiendo por {deficit_goals} goles.{club_first}",
                        "El {team} no había {done} tras ir perdiendo por {deficit_goals} goles desde {club_last_season}.{plain}", "El {team} perdía {trailing_score} y ha remontado hasta el {score}.{plain}"],
               "heart": {"level": ["¡Perdían {trailing_score} y mira al {team} ahora!", "¡Remontan desde el {trailing_score}! ¡El {team} empata!"],
                         "ahead": ["¿Quién lo veía venir con el {trailing_score}?", "¡Perdía {trailing_score} y ahora el {team} va por delante!"],
                         "won": ["¡Lo ha hecho el {team}! ¡Del {trailing_score} a la victoria!", "¡Perdía {trailing_score} y el {team} se lleva el partido!"],
                         "any": ["¡Perdían {trailing_score} y mira al {team} ahora!"]},
               "eye": {"level": ["{club_seasons_since} temporadas llevaba el {team} sin empatar tras ir perdiendo por {deficit_goals} goles, Wren.",
                                 "No pasaba desde {club_last_season}, Wren: el {team} no empataba tras ir perdiendo por {deficit_goals} goles.",
                                 "Nunca, Wren. En {history_seasons} temporadas, el {team} no había empatado tras ir perdiendo por {deficit_goals} goles.{club_first}",
                                 "Una primera vez para el {team} en {history_seasons} temporadas, Wren: empatar tras ir {deficit_goals} goles abajo.{club_first}",
                                 "Perdían {trailing_score}, Wren, y volvieron al {score}.{plain}"],
                       "ahead": ["{club_seasons_since} temporadas llevaba el {team} sin adelantarse tras ir perdiendo por {deficit_goals} goles, Wren.",
                                 "No pasaba desde {club_last_season}, Wren: el {team} no se ponía por delante tras ir perdiendo por {deficit_goals} goles.",
                                 "Nunca, Wren. En {history_seasons} temporadas, el {team} no se había adelantado tras ir perdiendo por {deficit_goals} goles.{club_first}",
                                 "Una primera vez para el {team} en {history_seasons} temporadas, Wren: por delante tras ir {deficit_goals} goles abajo.{club_first}",
                                 "Perdían {trailing_score}, Wren, y se pusieron por delante con el {score}.{plain}"],
                       "won": ["{club_seasons_since} temporadas llevaba el {team} sin ganar tras ir perdiendo por {deficit_goals} goles, Wren.",
                               "No pasaba desde {club_last_season}, Wren: el {team} no ganaba tras ir perdiendo por {deficit_goals} goles.",
                               "Nunca, Wren. En {history_seasons} temporadas, el {team} no había ganado tras ir perdiendo por {deficit_goals} goles.{club_first}",
                               "Una primera vez para el {team} en {history_seasons} temporadas, Wren: ganar tras ir {deficit_goals} goles abajo.{club_first}",
                               "Perdían {trailing_score}, Wren, y ganaron {score}.{plain}"],
                       "any": ["{club_seasons_since} temporadas, Wren. Eso es lo que esperaba el {team}.", "Nunca, Wren. En {history_seasons} temporadas, el {team} no lo había hecho.{club_first}",
                               "Perdían {trailing_score}, Wren, y mira ahora: {score}.{plain}"]}},
    },
    "ht-lead-lost": {
        "en": {"headlines": {"behind": ["{team} led {half_time_score} at the break, now trail", "{team} let a {half_time_score} half-time lead slip"],
                             "lost": ["{team} led {half_time_score} at the break, and lost", "{team} lose after leading {half_time_score} at half-time"],
                             "any": ["{team} let a {half_time_score} half-time lead slip"]},
               "whys": {"lost": ["{team}'s first {venue} defeat after leading at half-time since {club_last_season}, {club_seasons_since} seasons ago.",
                                 "A first in {history_seasons} seasons: {team} had never lost {venue_adv} after leading at half-time.{club_first}",
                                 "{team} led {half_time_score} at half-time and lost {score}.{plain}"],
                        "behind": ["If it ends like this, it will be {team}'s first {venue} defeat after leading at half-time since {club_last_season}.",
                                   "If it ends like this, a first in {history_seasons} seasons: {team} have never lost {venue_adv} after leading at half-time.{club_first}",
                                   "{team} led {half_time_score} at half-time and fell behind at {score}.{plain}"]},
               "heart": {"behind": ["{half_time_score} at the break, and look at {team} now.", "How has this got away from {team}?"],
                         "lost": ["{half_time_score} at the break, and {team} have lost it.", "How did {team} let a {half_time_score} half-time lead go?"],
                         "any": ["How has this got away from {team}?"]},
               "eye": {"lost": ["{club_seasons_since} seasons since that last happened to {team}, Wren.", "Not since {club_last_season} had {team} lost {venue_adv} after leading at the break, Wren.",
                                "Never in {history_seasons} seasons, Wren.{club_first}", "A first for {team} in {history_seasons} seasons, Wren: never had they lost {venue_adv} from ahead at the break.{club_first}",
                                "At the break it was {half_time_score}, Wren. At the end, {score}.{plain}"],
                       "behind": ["Not over yet, but {team} have not lost one like this since {club_last_season}.", "If it ends this way, a first for {team} since {club_last_season}, Wren.",
                                  "Not over yet, but {team} have never lost one like this.{club_first}", "If it ends this way, a first for {team}, Wren.{club_first}",
                                  "At the break it was {half_time_score}, Wren. Then {team} fell behind at {score}.{plain}"]}},
        "fr": {"headlines": {"behind": ["{team} menait {half_time_score} à la pause, le voilà mené", "{team} laisse filer son avance de la pause"],
                             "lost": ["{team} menait {half_time_score} à la pause, et a perdu", "{team} perd après avoir mené {half_time_score}"],
                             "any": ["{team} laisse filer son avance de la pause"]},
               "whys": {"lost": ["Première défaite de {team} {venue} après avoir mené à la pause depuis {club_last_season}, il y a {club_seasons_since} saisons.",
                                 "Une première en {history_seasons} saisons : jamais {team} n'avait perdu {venue} après avoir mené à la pause.{club_first}",
                                 "{team} menait {half_time_score} à la pause et a perdu {score}.{plain}"],
                        "behind": ["Si ça reste ainsi, ce sera la première défaite de {team} {venue} après avoir mené à la pause depuis {club_last_season}.",
                                   "Si ça reste ainsi, une première en {history_seasons} saisons : jamais {team} n'a perdu {venue} après avoir mené à la pause.{club_first}",
                                   "{team} menait {half_time_score} à la pause, puis s'est retrouvé mené {score}.{plain}"]},
               "heart": {"behind": ["{half_time_score} à la pause, et regarde où en est {team}.", "Comment {team} a pu laisser filer ça ?"],
                         "lost": ["{half_time_score} à la pause, et {team} a tout perdu.", "Comment {team} a-t-il pu laisser filer une avance de {half_time_score} à la pause ?"],
                         "any": ["Comment {team} a pu laisser filer ça ?"]},
               "eye": {"lost": ["{club_seasons_since} saisons que ça n'était plus arrivé à {team}, Wren.", "Pas depuis {club_last_season}, Wren : {team} n'avait plus perdu {venue} après avoir mené à la pause.",
                                "Jamais en {history_seasons} saisons, Wren.{club_first}", "Une première pour {team} en {history_seasons} saisons, Wren : jamais battu {venue} après avoir mené à la pause.{club_first}",
                                "À la pause, c'était {half_time_score}, Wren. À la fin, {score}.{plain}"],
                       "behind": ["Ce n'est pas fini, mais {team} n'a plus perdu un match comme ça depuis {club_last_season}.", "Si ça se termine ainsi, une première depuis {club_last_season} pour {team}, Wren.",
                                  "Ce n'est pas fini, mais {team} n'a jamais perdu un match comme ça.{club_first}", "Si ça se termine ainsi, une première pour {team}, Wren.{club_first}",
                                  "À la pause, c'était {half_time_score}, Wren. Puis {team} s'est retrouvé mené {score}.{plain}"]}},
        "es": {"headlines": {"behind": ["El {team} ganaba {half_time_score} al descanso y ahora pierde", "El {team} deja escapar su ventaja del descanso"],
                             "lost": ["El {team} ganaba {half_time_score} al descanso y perdió", "El {team} pierde tras ganar {half_time_score} al descanso"],
                             "any": ["El {team} deja escapar su ventaja del descanso"]},
               "whys": {"lost": ["Primera derrota del {team} {venue} tras ir ganando al descanso desde {club_last_season}, hace {club_seasons_since} temporadas.",
                                 "Una primera vez en {history_seasons} temporadas: el {team} nunca había perdido {venue} tras ir ganando al descanso.{club_first}",
                                 "El {team} ganaba {half_time_score} al descanso y perdió {score}.{plain}"],
                        "behind": ["Si termina así, será la primera derrota del {team} {venue} tras ir ganando al descanso desde {club_last_season}.",
                                   "Si termina así, una primera vez en {history_seasons} temporadas: el {team} nunca ha perdido {venue} tras ir ganando al descanso.{club_first}",
                                   "El {team} ganaba {half_time_score} al descanso y se vio por detrás con el {score}.{plain}"]},
               "heart": {"behind": ["{half_time_score} al descanso, y mira al {team} ahora.", "¿Cómo se le ha escapado esto al {team}?"],
                         "lost": ["{half_time_score} al descanso, y el {team} lo ha perdido todo.", "¿Cómo ha perdido el {team} un partido que ganaba {half_time_score} al descanso?"],
                         "any": ["¿Cómo se le ha escapado esto al {team}?"]},
               "eye": {"lost": ["Hacía {club_seasons_since} temporadas que no le pasaba al {team}, Wren.", "No pasaba desde {club_last_season}, Wren: el {team} no perdía {venue} tras ir ganando al descanso.",
                                "Nunca en {history_seasons} temporadas, Wren.{club_first}", "Una primera vez para el {team} en {history_seasons} temporadas, Wren: nunca había perdido {venue} tras ir ganando al descanso.{club_first}",
                                "Al descanso, {half_time_score}, Wren. Al final, {score}.{plain}"],
                       "behind": ["No ha terminado, pero el {team} no pierde uno así desde {club_last_season}.", "Si termina así, será la primera vez desde {club_last_season} para el {team}, Wren.",
                                  "No ha terminado, pero el {team} nunca ha perdido uno así.{club_first}", "Si termina así, será una primera vez para el {team}, Wren.{club_first}",
                                  "Al descanso, {half_time_score}, Wren. Luego, el {team} se vio por detrás con el {score}.{plain}"]}},
    },
    "storyline": {  # before kick-off, both clubs fighting relegation (the storyline's own verified texts are used first; these give the duo its numbers)
        "en": {"headlines": ["{home_club} v {away_club}: a relegation battle"],
               "whys": ["{away_club} are on {away_relegation_chance_pct}% to go down: {away_relegation_chance_if_win_pct}% with a win, {away_relegation_chance_if_loss_pct}% with a defeat."],
               "heart": ["{home_club} {home_rank_ord}, {away_club} {away_rank_ord}. Tonight is about staying up, Joss."],
               "eye": ["A win lifts {home_club} to {home_rank_if_win_ord}. A defeat drops them to {home_rank_if_loss_ord}.",
                       "{away_club} have a {away_relegation_chance_pct}% chance of going down. A win cuts it to {away_relegation_chance_if_win_pct}%."]},
        "fr": {"headlines": ["{home_club} - {away_club}, duel pour le maintien"],
               "whys": ["{away_club} a {away_relegation_chance_pct} % de risques de descendre : {away_relegation_chance_if_win_pct} % en cas de victoire, {away_relegation_chance_if_loss_pct} % en cas de défaite."],
               "heart": ["{home_club} {home_rank_ord}, {away_club} {away_rank_ord}. Ce soir, on joue le maintien, Joss."],
               "eye": ["Une victoire et {home_club} remonte {home_rank_if_win_ord}. Une défaite, et il passe {home_rank_if_loss_ord}.",
                       "{away_club} a {away_relegation_chance_pct} % de risques de descendre. Avec une victoire, ça tombe à {away_relegation_chance_if_win_pct} %."]},
        "es": {"headlines": ["{home_club}-{away_club}, duelo por la permanencia"],
               "whys": ["El {away_club} tiene un {away_relegation_chance_pct}% de opciones de descender: {away_relegation_chance_if_win_pct}% si gana, {away_relegation_chance_if_loss_pct}% si pierde."],
               "heart": ["El {home_club}, {home_rank_ord}; el {away_club}, {away_rank_ord}. Esta noche se juega la permanencia, Joss."],
               "eye": ["Si gana, el {home_club} sube al {home_rank_if_win_ord} puesto. Si pierde, cae al {home_rank_if_loss_ord}.",
                       "El {away_club} tiene un {away_relegation_chance_pct}% de opciones de descender. Si gana, baja al {away_relegation_chance_if_win_pct}%."]},
    },
    "xg": {  # goals against expected goals, at half-time or full time: the fact, never how many matches go that way
        "en": {"headlines": ["{team}: {goals} goals from {xg} xG", "{team} beat their {xg} xG by a mile"],
               "whys": ["{team} scored {goals} from chances worth {xg} expected goals.", "{goals} goals for {team}, from chances worth only {xg} expected goals."],
               "heart": ["Everything {team} hit went in tonight!", "{team} could not miss tonight!"],
               "eye": ["{goals} goals from chances worth {xg} expected goals.", "Chances worth {xg} expected goals, and {goals} goals for {team}."]},
        "fr": {"headlines": ["{team} : {goals} buts pour {xg} xG", "{team} pulvérise ses {xg} xG"],
               "whys": ["{team} a marqué {goals} buts sur des occasions qui en valaient {xg}.", "{goals} buts pour {team}, sur des occasions qui n'en valaient que {xg}."],
               "heart": ["Tout ce que {team} a tenté est rentré ce soir !", "{team} n'a rien raté ce soir !"],
               "eye": ["{goals} buts sur des occasions qui en valaient {xg}.", "{goals} buts, pour des occasions qui n'en valaient que {xg}."]},
        "es": {"headlines": ["El {team}: {goals} goles con {xg} xG", "El {team} pulveriza sus {xg} xG"],
               "whys": ["El {team} marcó {goals} con ocasiones que valían {xg} goles esperados.", "{goals} goles del {team}, con ocasiones que solo valían {xg} goles esperados."],
               "heart": ["¡Todo lo que tiró el {team} entró esta noche!", "¡El {team} no ha fallado nada esta noche!"],
               "eye": ["{goals} goles con ocasiones que valían {xg} goles esperados.", "Ocasiones de {xg} goles esperados, y {goals} goles del {team}."]},
    },
    "late": {  # an equaliser or a go-ahead goal from the 85th minute (goal_type): the league sees many, so the goal and its scorer are the story
        "en": {"headlines": {"equaliser": ["{player} levels late for {team}", "A late equaliser from {player}"],
                             "go-ahead goal": ["{player} puts {team} ahead late on", "A late go-ahead goal from {player}"], "any": ["A late goal from {player}"]},
               "whys": {"equaliser": ["At {minute}, {player} makes it {score_after}: a late equaliser for {team}.", "{player} levels it for {team} at {minute}: {score_after}, with the clock running down."],
                        "go-ahead goal": ["At {minute}, {player} makes it {score_after}: a late go-ahead goal for {team}.", "{player} puts {team} ahead at {minute}: {score_after}, with the clock running down."],
                        "any": ["At {minute}, {player} makes it {score_after} for {team}."]},
               "heart": {"equaliser": ["{player}! Level at {minute}, and {team} are not done!", "Late, late drama! {player} levels for {team}!"],
                         "go-ahead goal": ["{player}! {team} ahead at {minute}!", "Late, late drama! {player} puts {team} in front!"], "any": ["Late, late drama! {player} for {team}!"]},
               "eye": {"equaliser": ["Look at the clock, Wren: {minute}, and {team} are level.", "{player} at {minute}, Wren. {team} took it to the wire."],
                       "go-ahead goal": ["Look at the clock, Wren: {minute}, and {team} are in front.", "{player} at {minute}, Wren. {team} took it to the wire."],
                       "any": ["{player} at {minute}, Wren. {team} took it to the wire."]}},
        "fr": {"headlines": {"equaliser": ["{player} égalise sur le fil", "Égalisation tardive signée {player}"],
                             "go-ahead goal": ["{player} met {team} devant sur le fil", "But tardif de {player} pour {team}"], "any": ["But tardif signé {player}"]},
               "whys": {"equaliser": ["À la {minute}, {player} égalise pour {team} : {score_after}.", "{player} remet {team} à hauteur à la {minute} : {score_after}, dans les dernières minutes."],
                        "go-ahead goal": ["À la {minute}, {player} donne l'avantage à {team} : {score_after}.", "{player} fait passer {team} devant à la {minute} : {score_after}, dans les dernières minutes."],
                        "any": ["À la {minute}, {player} marque pour {team} : {score_after}."]},
               "heart": {"equaliser": ["{player} ! Égalisation à la {minute}, et {team} y croit encore !", "Quel final ! {player} égalise pour {team} !"],
                         "go-ahead goal": ["{player} ! {team} passe devant à la {minute} !", "Quel final ! {player} donne l'avantage à {team} !"], "any": ["Quel final ! {player} pour {team} !"]},
               "eye": {"equaliser": ["Regarde l'horloge, Wren : {minute}, et {team} revient à hauteur.", "{player} à la {minute}, Wren. {team} y a cru jusqu'au bout."],
                       "go-ahead goal": ["Regarde l'horloge, Wren : {minute}, et {team} passe devant.", "{player} à la {minute}, Wren. {team} y a cru jusqu'au bout."],
                       "any": ["{player} à la {minute}, Wren. {team} y a cru jusqu'au bout."]}},
        "es": {"headlines": {"equaliser": ["{player} empata al final para el {team}", "Empate tardío de {player}"],
                             "go-ahead goal": ["{player} adelanta al {team} al final", "Gol tardío de {player} para el {team}"], "any": ["Gol tardío de {player}"]},
               "whys": {"equaliser": ["En el {minute}, {player} empata para el {team}: {score_after}.", "{player} iguala para el {team} en el {minute}: {score_after}, en los últimos minutos."],
                        "go-ahead goal": ["En el {minute}, {player} adelanta al {team}: {score_after}.", "{player} pone por delante al {team} en el {minute}: {score_after}, en los últimos minutos."],
                        "any": ["En el {minute}, {player} marca para el {team}: {score_after}."]},
               "heart": {"equaliser": ["¡{player}! ¡Empate en el {minute}, y el {team} sigue vivo!", "¡Qué final! ¡{player} empata para el {team}!"],
                         "go-ahead goal": ["¡{player}! ¡El {team} se adelanta en el {minute}!", "¡Qué final! ¡{player} adelanta al {team}!"], "any": ["¡Qué final! ¡{player} para el {team}!"]},
               "eye": {"equaliser": ["Mira el reloj, Wren: el {minute}, y el {team} empata.", "{player} en el {minute}, Wren. El {team} creyó hasta el final."],
                       "go-ahead goal": ["Mira el reloj, Wren: el {minute}, y el {team} se pone por delante.", "{player} en el {minute}, Wren. El {team} creyó hasta el final."],
                       "any": ["{player} en el {minute}, Wren. El {team} creyó hasta el final."]}},
    },
    "ten-men": {  # a team down to ten that scores (stage scored) or wins (won)
        "en": {"headlines": {"scored": ["{team} score with ten men", "Ten men, and {team} still score"], "won": ["{team} win with ten men", "Ten men, and {team} still win"]},
               "whys": {"scored": ["Down to ten, {team} still found the net: {goals_after_red} since the red card.", "A man down, {team} have scored {goals_after_red} since the red card."],
                        "won": ["Down to ten, {team} still won it: {score} at the final whistle.", "A man down for the rest of the match, and {team} still won, {score}."]},
               "heart": {"scored": ["A man down, and {team} still find a way!", "Who said ten men cannot score? {team} just did!"],
                         "won": ["Ten men, and {team} have won it!", "{team} did it with ten men!"]},
               "eye": {"scored": ["{goals_after_red} since the red card, Wren, and with a man less.", "With ten men, Wren: {goals_after_red} for {team} since the red card."],
                       "won": ["{team} won it a man down, Wren: {score}.", "With ten men, Wren, and {team} still took the win."]}},
        "fr": {"headlines": {"scored": ["{team} marque à dix", "À dix, {team} trouve la faille"], "won": ["{team} gagne à dix", "À dix, {team} s'impose"]},
               "whys": {"scored": ["Réduit à dix, {team} a encore marqué : {goals_after_red} depuis le carton rouge.", "Avec un homme de moins, {team} a marqué {goals_after_red} fois depuis l'expulsion."],
                        "won": ["Réduit à dix, {team} l'a quand même emporté : {score} au coup de sifflet final.", "À dix contre onze jusqu'au bout, {team} s'impose {score}."]},
               "heart": {"scored": ["À dix, et {team} trouve quand même la faille !", "Qui a dit qu'à dix on ne marquait pas ? {team} vient de le faire !"],
                         "won": ["À dix, et {team} l'a fait !", "{team} a gagné à dix !"]},
               "eye": {"scored": ["{goals_after_red} depuis le carton rouge, Wren, et avec un homme de moins.", "À dix, Wren : {goals_after_red} pour {team} depuis l'expulsion."],
                       "won": ["{team} a gagné à dix, Wren : {score}.", "À dix contre onze, Wren, et {team} s'impose quand même."]}},
        "es": {"headlines": {"scored": ["El {team} marca con diez", "Con diez, el {team} sigue marcando"], "won": ["El {team} gana con diez", "Con diez, el {team} se lleva el partido"]},
               "whys": {"scored": ["Con diez, el {team} sigue marcando: {goals_after_red} desde la roja.", "Con un hombre menos, el {team} ha marcado {goals_after_red} desde la expulsión."],
                        "won": ["Con diez, el {team} se llevó el partido: {score} al final.", "Con uno menos desde la roja, el {team} ganó {score}."]},
               "heart": {"scored": ["¡Con diez, y el {team} encuentra el camino!", "¿Quién dijo que con diez no se marca? ¡El {team} lo acaba de hacer!"],
                         "won": ["¡Con diez, y el {team} lo ha conseguido!", "¡El {team} ganó con diez!"]},
               "eye": {"scored": ["{goals_after_red} desde la roja, Wren, y con un hombre menos.", "Con diez, Wren: {goals_after_red} del {team} desde la expulsión."],
                       "won": ["El {team} ganó con uno menos, Wren: {score}.", "Con diez contra once, Wren, y el {team} se lleva el partido."]}},
    },
    "h2h-margin": {  # at full time, the winner's biggest win over this opponent in the history (record new) or as big as it (equalled)
        "en": {"headlines": {"new": ["A record win for {team} over {opponent}", "{team} beat {opponent} by {margin}, a record"],
                             "equalled": ["{team} equal their record win over {opponent}", "{team} match their biggest win over {opponent}"]},
               "whys": {"new": ["{score}: never in {history_seasons} seasons had {team} beaten {opponent} by {margin} goals.",
                                "A win by {margin} goals, {score}: the biggest for {team} over {opponent} in {history_seasons} seasons."],
                        "equalled": ["{score}: as big a win over {opponent} as {team} have ever had, last done in {equalled_season}.",
                                     "A win by {margin} goals, {score}: {team} had never beaten {opponent} by more, the last time in {equalled_season}."]},
               "heart": {"new": ["{team} have never beaten {opponent} like this!", "What a day for {team}: a record win over {opponent}!"],
                         "equalled": ["As big as it gets for {team} against {opponent}!", "{team} fans will not forget this one against {opponent}!"]},
               "eye": {"new": ["Never by {margin} goals before, Wren, not in {history_seasons} seasons.", "A record, Wren: {team} had never won by {margin} against {opponent}."],
                       "equalled": ["As big as their win in {equalled_season}, Wren.", "{team} had done it before, Wren, in {equalled_season}, never by more."]}},
        "fr": {"headlines": {"new": ["Victoire record pour {team} face à {opponent}", "{team} bat {opponent} de {margin} buts, un record"],
                             "equalled": ["{team} égale son record face à {opponent}", "{team} bat de nouveau {opponent} de {margin} buts"]},
               "whys": {"new": ["{score} : jamais en {history_seasons} saisons {team} n'avait battu {opponent} de {margin} buts.",
                                "Une victoire de {margin} buts, {score} : la plus large de {team} face à {opponent} en {history_seasons} saisons."],
                        "equalled": ["{score} : {team} égale sa plus large victoire face à {opponent}, déjà vue en {equalled_season}.",
                                     "Une victoire de {margin} buts, {score} : jamais {team} n'avait battu {opponent} plus largement, la dernière fois en {equalled_season}."]},
               "heart": {"new": ["{team} n'avait jamais battu {opponent} comme ça !", "Quelle journée pour {team} : une victoire record face à {opponent} !"],
                         "equalled": ["Difficile de faire mieux pour {team} face à {opponent} !", "Les supporters de {team} n'oublieront pas celle-là face à {opponent} !"]},
               "eye": {"new": ["Jamais de {margin} buts auparavant, Wren, pas en {history_seasons} saisons.", "Un record, Wren : {team} n'avait jamais gagné de {margin} buts face à {opponent}."],
                       "equalled": ["Aussi large que la victoire de {equalled_season}, Wren.", "{team} l'avait déjà fait, Wren, en {equalled_season}, jamais plus largement."]}},
        "es": {"headlines": {"new": ["Victoria récord del {team} ante el {opponent}", "Récord: el {team} gana por {margin} al {opponent}"],
                             "equalled": ["El {team} iguala su récord ante el {opponent}", "El {team} iguala su victoria más amplia"]},
               "whys": {"new": ["{score}: en {history_seasons} temporadas, el {team} nunca había ganado al {opponent} por {margin} goles.",
                                "Una victoria por {margin} goles, {score}: la mayor del {team} ante el {opponent} en {history_seasons} temporadas."],
                        "equalled": ["{score}: el {team} iguala su mayor victoria ante el {opponent}, la última en {equalled_season}.",
                                     "Una victoria por {margin} goles, {score}: el {team} nunca había ganado al {opponent} por más, la última vez en {equalled_season}."]},
               "heart": {"new": ["¡El {team} nunca había ganado así al {opponent}!", "¡Qué día para el {team}: victoria récord ante el {opponent}!"],
                         "equalled": ["¡Difícil hacerlo mejor para el {team} ante el {opponent}!", "¡La afición del {team} no olvidará esta ante el {opponent}!"]},
               "eye": {"new": ["Nunca por {margin} goles antes, Wren, no en {history_seasons} temporadas.", "Un récord, Wren: el {team} nunca había ganado por {margin} al {opponent}."],
                       "equalled": ["Tan amplia como la victoria de {equalled_season}, Wren.", "El {team} ya lo había hecho, Wren, en {equalled_season}, nunca por más."]}},
    },
    "h2h-drought": {  # at full time, the winner's first win in this fixture at this ground for seasons_without_win seasons, or ever ({first_win}); by venue
        "en": {"headlines": {"home": ["{team} beat {opponent} at home at last", "{team} end their home wait against {opponent}"],
                             "away": ["{team} win away at {opponent} at last", "{team} end their wait away at {opponent}"]},
               "whys": {"home": ["The first home win for {team} over {opponent} since {last_win_season}, {seasons_without_win} seasons ago.",
                                 "{team} had not beaten {opponent} at home for {seasons_without_win} seasons, since {last_win_season}.",
                                 "{team} had never beaten {opponent} at home in the league before.{first_win}",
                                 "A first home league win over {opponent} for {team}, after {seasons_without_win} seasons of trying.{first_win}"],
                        "away": ["The first win away at {opponent} for {team} since {last_win_season}, {seasons_without_win} seasons ago.",
                                 "{team} had not won away at {opponent} for {seasons_without_win} seasons, since {last_win_season}.",
                                 "{team} had never won away at {opponent} in the league before.{first_win}",
                                 "A first league win away at {opponent} for {team}, after {seasons_without_win} seasons of trying.{first_win}"]},
               "heart": {"home": ["At last! {team} beat {opponent} at home!", "{team} fans have waited a long time for this one!"],
                         "away": ["At last! {team} win away at {opponent}!", "The away end has waited a long time for this one!"]},
               "eye": {"home": ["{seasons_without_win} seasons, Wren. That is how long {team} had waited at home against {opponent}.",
                                "Not since {last_win_season} had {team} beaten {opponent} at home, Wren.", "Never before, Wren: {team} had never beaten {opponent} at home in the league.{first_win}"],
                       "away": ["{seasons_without_win} seasons, Wren. That is how long {team} had waited for a win away at {opponent}.",
                                "Not since {last_win_season} had {team} won away at {opponent}, Wren.", "Never before, Wren: {team} had never won away at {opponent} in the league.{first_win}"]}},
        "fr": {"headlines": {"home": ["{team} bat enfin {opponent} à domicile", "Enfin une victoire à domicile pour {team}"],
                             "away": ["{team} gagne enfin chez {opponent}", "Enfin une victoire chez {opponent} pour {team}"]},
               "whys": {"home": ["Première victoire de {team} à domicile face à {opponent} depuis {last_win_season}, il y a {seasons_without_win} saisons.",
                                 "{team} n'avait plus battu {opponent} à domicile depuis {last_win_season}, soit {seasons_without_win} saisons.",
                                 "{team} n'avait jamais battu {opponent} à domicile en championnat.{first_win}",
                                 "Une première victoire à domicile face à {opponent} pour {team}, après {seasons_without_win} saisons d'attente.{first_win}"],
                        "away": ["Première victoire de {team} chez {opponent} depuis {last_win_season}, il y a {seasons_without_win} saisons.",
                                 "{team} n'avait plus gagné chez {opponent} depuis {last_win_season}, soit {seasons_without_win} saisons.",
                                 "{team} n'avait jamais gagné chez {opponent} en championnat.{first_win}",
                                 "Une première victoire chez {opponent} pour {team}, après {seasons_without_win} saisons d'attente.{first_win}"]},
               "heart": {"home": ["Enfin ! {team} bat {opponent} à domicile !", "Les supporters de {team} attendaient ça depuis longtemps !"],
                         "away": ["Enfin ! {team} gagne chez {opponent} !", "Le parcage visiteur attendait ça depuis longtemps !"]},
               "eye": {"home": ["{seasons_without_win} saisons d'attente à domicile face à {opponent}, Wren.", "Pas depuis {last_win_season}, Wren : {team} n'avait plus battu {opponent} à domicile.",
                                "Jamais, Wren : {team} n'avait jamais battu {opponent} à domicile en championnat.{first_win}"],
                       "away": ["{seasons_without_win} saisons d'attente chez {opponent} pour {team}, Wren.", "Pas depuis {last_win_season}, Wren : {team} n'avait plus gagné chez {opponent}.",
                                "Jamais, Wren : {team} n'avait jamais gagné chez {opponent} en championnat.{first_win}"]}},
        "es": {"headlines": {"home": ["El {team} vence por fin al {opponent}", "Por fin, el {team} gana en casa"],
                             "away": ["El {team} gana por fin fuera de casa", "Por fin, el {team} vence fuera"]},
               "whys": {"home": ["Primera victoria del {team} en casa ante el {opponent} desde {last_win_season}, hace {seasons_without_win} temporadas.",
                                 "El {team} no ganaba en casa al {opponent} desde {last_win_season}: {seasons_without_win} temporadas.",
                                 "El {team} no había ganado jamás en casa al {opponent} en liga.{first_win}",
                                 "Primera victoria en casa ante el {opponent} para el {team}, tras {seasons_without_win} temporadas de espera.{first_win}"],
                        "away": ["Primera victoria del {team} en el campo del {opponent} desde {last_win_season}, hace {seasons_without_win} temporadas.",
                                 "El {team} no ganaba en el campo del {opponent} desde {last_win_season}: {seasons_without_win} temporadas.",
                                 "El {team} no había ganado jamás en el campo del {opponent} en liga.{first_win}",
                                 "Primera victoria en el campo del {opponent} para el {team}, tras {seasons_without_win} temporadas de espera.{first_win}"]},
               "heart": {"home": ["¡Por fin! ¡El {team} vence al {opponent} en casa!", "¡La afición del {team} llevaba mucho tiempo esperando esto!"],
                         "away": ["¡Por fin! ¡El {team} gana en el campo del {opponent}!", "¡La afición visitante llevaba mucho tiempo esperando esto!"]},
               "eye": {"home": ["{seasons_without_win} temporadas de espera en casa ante el {opponent}, Wren.", "No pasaba desde {last_win_season}, Wren: el {team} no ganaba en casa al {opponent}.",
                                "Nunca, Wren: el {team} no había ganado jamás en casa al {opponent} en liga.{first_win}"],
                       "away": ["{seasons_without_win} temporadas de espera en el campo del {opponent}, Wren.", "No pasaba desde {last_win_season}, Wren: el {team} no ganaba en el campo del {opponent}.",
                                "Nunca, Wren: el {team} no había ganado jamás en el campo del {opponent} en liga.{first_win}"]}},
    },
    "shots": {  # a team with three shots in four or more, at half-time or full time (window): the shots, not the model's expected share
        "en": {"headlines": {"first half": ["{team}: {shots} shots to {opponent_shots} at the break", "{team} on top: {shots} first-half shots", "{team} fire {shots} shots before the break"],
                             "match": ["{team}: {shots} shots to {opponent_shots}", "{team} on top all match: {shots} shots", "{team} fire {shots} shots in the match"],
                             "any": ["{team}: {shots} shots to {opponent_shots}"]},
               "whys": {"first half": ["{team} had {shots} shots in the first half, {other_team} {opponent_shots}.", "In the first half, {team} took {shots} shots and allowed {other_team} {opponent_shots}."],
                        "match": ["{team} had {shots} shots in the match, {other_team} {opponent_shots}.", "Over the match, {team} took {shots} shots and allowed {other_team} {opponent_shots}."],
                        "any": ["{team} had {shots} shots, {other_team} {opponent_shots}."]},
               "heart": {"first half": ["It has been one-way traffic, Joss: {team} all over them!", "{team} have camped in that half, Joss!"],
                         "match": ["One-way traffic all match, Joss!", "{team} never let up, Joss!"], "any": ["{team} never let up, Joss!"]},
               "eye": {"first half": ["{shots} shots to {opponent_shots} before the break, Wren.", "{shots} for {team} in the first half, Wren, and {opponent_shots} for {other_team}."],
                       "match": ["{shots} shots to {opponent_shots} over the match, Wren.", "{team} took {shots} shots, Wren, {other_team} {opponent_shots}."],
                       "any": ["{shots} shots to {opponent_shots}, Wren."]}},
        "fr": {"headlines": {"first half": ["{team} : {shots} tirs contre {opponent_shots} à la pause", "{team} domine : {shots} tirs en première période", "{team} tente {shots} tirs avant la pause"],
                             "match": ["{team} : {shots} tirs contre {opponent_shots}", "{team} domine tout le match : {shots} tirs", "{team} tente {shots} tirs sur le match"],
                             "any": ["{team} : {shots} tirs contre {opponent_shots}"]},
               "whys": {"first half": ["{team} a tenté {shots} tirs en première période, {other_team} {opponent_shots}.", "En première période, {team} a tiré {shots} fois, {other_team} {opponent_shots}."],
                        "match": ["{team} a tenté {shots} tirs sur l'ensemble du match, {other_team} {opponent_shots}.", "Sur le match, {team} a tiré {shots} fois, {other_team} {opponent_shots}."],
                        "any": ["{team} a tenté {shots} tirs, {other_team} {opponent_shots}."]},
               "heart": {"first half": ["C'est à sens unique, Joss : {team} les étouffe !", "{team} s'est installé dans ce camp, Joss !"],
                         "match": ["À sens unique tout le match, Joss !", "{team} n'a jamais relâché la pression, Joss !"], "any": ["{team} n'a jamais relâché la pression, Joss !"]},
               "eye": {"first half": ["{shots} tirs contre {opponent_shots} avant la pause, Wren.", "{shots} tirs pour {team} en première période, Wren, {opponent_shots} pour {other_team}."],
                       "match": ["{shots} tirs contre {opponent_shots} sur le match, Wren.", "{team} a tenté {shots} tirs, Wren, {other_team} {opponent_shots}."],
                       "any": ["{shots} tirs contre {opponent_shots}, Wren."]}},
        "es": {"headlines": {"first half": ["El {team}: {shots} tiros contra {opponent_shots} al descanso", "El {team} domina: {shots} tiros antes del descanso", "El {team} suma {shots} tiros al descanso"],
                             "match": ["El {team}: {shots} tiros contra {opponent_shots}", "El {team} domina todo el partido: {shots} tiros", "El {team} suma {shots} tiros en el partido"],
                             "any": ["El {team}: {shots} tiros contra {opponent_shots}"]},
               "whys": {"first half": ["El {team} tiró {shots} veces en la primera parte, el {other_team} {opponent_shots}.", "En la primera parte, el {team} disparó {shots} veces y el {other_team}, {opponent_shots}."],
                        "match": ["El {team} tiró {shots} veces en el partido, el {other_team} {opponent_shots}.", "En todo el partido, el {team} disparó {shots} veces y el {other_team}, {opponent_shots}."],
                        "any": ["El {team} tiró {shots} veces, el {other_team} {opponent_shots}."]},
               "heart": {"first half": ["¡Esto es un monólogo, Joss: el {team} los tiene encerrados!", "¡El {team} se ha instalado en ese campo, Joss!"],
                         "match": ["¡Un monólogo todo el partido, Joss!", "¡El {team} nunca aflojó, Joss!"], "any": ["¡El {team} nunca aflojó, Joss!"]},
               "eye": {"first half": ["{shots} tiros contra {opponent_shots} antes del descanso, Wren.", "{shots} tiros del {team} en la primera parte, Wren, y {opponent_shots} del {other_team}."],
                       "match": ["{shots} tiros contra {opponent_shots} en el partido, Wren.", "El {team} tiró {shots} veces, Wren, el {other_team} {opponent_shots}."],
                       "any": ["{shots} tiros contra {opponent_shots}, Wren."]}},
    },
    "possession-swing": {  # at full time, a team whose share of the passes rose by 12 points or more after the break: more of the ball, not always most of it
        "en": {"headlines": ["{team} see more of the ball after half-time", "{team} pass more after the break"],
               "whys": ["{team} had {first_half_pass_share_pct}% of the passes in the first half, {second_half_pass_share_pct}% in the second.",
                        "From {first_half_pass_share_pct}% of the passes before the break to {second_half_pass_share_pct}% after it, for {team}."],
               "heart": ["{team} came out a different side after the break, Joss!", "A different {team} after the break, Joss!"],
               "eye": ["{first_half_pass_share_pct}% of the passes in the first half, {second_half_pass_share_pct}% in the second, Wren.",
                       "{team} went from {first_half_pass_share_pct}% to {second_half_pass_share_pct}% of the passes, Wren."]},
        "fr": {"headlines": ["{team} a plus le ballon après la pause", "Plus de ballon pour {team} après la pause"],
               "whys": ["{team} avait {first_half_pass_share_pct} % des passes en première période, {second_half_pass_share_pct} % en seconde.",
                        "De {first_half_pass_share_pct} % des passes avant la pause à {second_half_pass_share_pct} % après, pour {team}."],
               "heart": ["{team} a changé de visage après la pause, Joss !", "Un autre visage pour {team} après la pause, Joss !"],
               "eye": ["{first_half_pass_share_pct} % des passes en première période, {second_half_pass_share_pct} % en seconde, Wren.",
                       "{team} est passé de {first_half_pass_share_pct} % à {second_half_pass_share_pct} % des passes, Wren."]},
        "es": {"headlines": ["Más balón para el {team} tras el descanso", "El {team} tiene más balón tras el descanso"],
               "whys": ["El {team} tuvo el {first_half_pass_share_pct}% de los pases en la primera parte y el {second_half_pass_share_pct}% en la segunda.",
                        "Del {first_half_pass_share_pct}% de los pases antes del descanso al {second_half_pass_share_pct}% después, para el {team}."],
               "heart": ["¡El {team} cambió la cara tras el descanso, Joss!", "¡Otro {team} tras el descanso, Joss!"],
               "eye": ["El {first_half_pass_share_pct}% de los pases en la primera parte, el {second_half_pass_share_pct}% en la segunda, Wren.",
                       "El {team} pasó del {first_half_pass_share_pct}% al {second_half_pass_share_pct}% de los pases, Wren."]},
    },
    "goals-total": {  # six goals or more in the match, both teams: the clubs and the score, a landmark in seasons when it is one
        "en": {"headlines": ["Goal after goal for {home_team} and {away_team}", "{home_team} and {away_team} cannot stop scoring"],
               "whys": ["{home_team} {score} {away_team}: {goals} goals in one match.{often}", "{home_team} {score} {away_team}: not since {last_season} had a league match seen {goals} goals.{rare}",
                        "{home_team} {score} {away_team}: never in {history_seasons} seasons had a league match seen {goals} goals.{never}",
                        "{home_team} {score} {away_team}: {goals} goals in one match.{plain}"],
               "heart": ["Goals, goals, goals! {home_team} and {away_team} will not stop!", "What a match, Joss: {goals} goals and nobody wants it to end!"],
               "eye": ["{goals} goals, Wren. Not since {last_season} has a league match had as many.{rare}", "Never in {history_seasons} seasons, Wren: no league match had {goals} goals.{never}",
                       "{goals} goals between them, Wren, and every one counts.", "{score}, Wren: {goals} goals in one match."]},
        "fr": {"headlines": ["Avalanche de buts entre {home_team} et {away_team}", "{home_team} et {away_team} n'arrêtent plus de marquer"],
               "whys": ["{home_team} {score} {away_team} : {goals} buts dans le même match.{often}", "{home_team} {score} {away_team} : pas un match de championnat avec {goals} buts depuis {last_season}.{rare}",
                        "{home_team} {score} {away_team} : jamais en {history_seasons} saisons un match de championnat n'avait compté {goals} buts.{never}",
                        "{home_team} {score} {away_team} : {goals} buts dans le même match.{plain}"],
               "heart": ["Des buts, encore des buts ! {home_team} et {away_team} ne s'arrêtent plus !", "Quel match, Joss : {goals} buts, et personne ne veut que ça s'arrête !"],
               "eye": ["{goals} buts, Wren. Pas autant dans un match de championnat depuis {last_season}.{rare}", "Jamais en {history_seasons} saisons, Wren : aucun match de championnat n'avait compté {goals} buts.{never}",
                       "{goals} buts entre les deux, Wren, et tous comptent.", "{score}, Wren : {goals} buts dans le même match."]},
        "es": {"headlines": ["{home_team} {score} {away_team}, lluvia de goles", "Festival de goles: {home_team} {score} {away_team}"],
               "whys": ["{home_team} {score} {away_team}: {goals} goles en un mismo partido.{often}", "{home_team} {score} {away_team}: ningún partido de liga tenía {goals} goles desde {last_season}.{rare}",
                        "{home_team} {score} {away_team}: nunca en {history_seasons} temporadas un partido de liga había tenido {goals} goles.{never}",
                        "{home_team} {score} {away_team}: {goals} goles en un mismo partido.{plain}"],
               "heart": ["¡Goles, goles y más goles! ¡El {home_team} y el {away_team} no paran!", "¡Qué partido, Joss: {goals} goles y nadie quiere que acabe!"],
               "eye": ["{goals} goles, Wren. Ningún partido de liga tenía tantos desde {last_season}.{rare}", "Nunca en {history_seasons} temporadas, Wren: ningún partido de liga había tenido {goals} goles.{never}",
                       "{goals} goles entre los dos, Wren, y todos cuentan.", "{score}, Wren: {goals} goles en un mismo partido."]},
    },
    "club-goals": {  # a team's most goals in a league match (record new) or as many as its best (equalled)
        "en": {"headlines": {"new": ["A record {goals} goals for {team}", "{team} have never scored so many"], "equalled": ["{team} equal their record: {goals} goals", "{team} match their best with {goals} goals"]},
               "whys": {"new": ["{team} have scored {goals}: never before had they scored more than {club_record} in a league match.", "{goals} goals for {team}, their most in a league match."],
                        "equalled": ["{team} have scored {goals}, as many as their best in a league match.", "{goals} goals for {team}: their record in a league match, equalled."]},
               "heart": {"new": ["{goals} for {team}! They have never scored so many!", "Will {team} ever stop scoring, Joss?"],
                         "equalled": ["{goals} for {team}, Joss! Can they beat their record?", "{team} are on a record day, Joss!"]},
               "eye": {"new": ["Never this many, Wren: {team} had never scored more than {club_record} in a league match.", "A club record, Wren: {goals} goals in one match."],
                       "equalled": ["As many as their best ever, Wren: {club_record}.", "{team} have equalled their record, Wren: {goals} goals in one match."]}},
        "fr": {"headlines": {"new": ["Record de buts pour {team}", "{team} n'avait jamais autant marqué"], "equalled": ["{team} égale son record : {goals} buts", "{team} rejoint son record de buts"]},
               "whys": {"new": ["{team} a marqué {goals} buts : jamais le club n'avait dépassé {club_record} buts dans un match de championnat.", "{goals} buts pour {team}, son record dans un match de championnat."],
                        "equalled": ["{team} a marqué {goals} buts, autant que son record en championnat.", "{goals} buts pour {team} : son record dans un match de championnat, égalé."]},
               "heart": {"new": ["{goals} buts pour {team} ! Jamais le club n'en avait marqué autant !", "{team} va-t-il s'arrêter un jour, Joss ?"],
                         "equalled": ["{goals} buts pour {team}, Joss ! Le record peut-il tomber ?", "Journée record pour {team}, Joss !"]},
               "eye": {"new": ["Jamais autant, Wren : {team} n'avait jamais marqué plus de {club_record} buts dans un match de championnat.", "Un record pour le club, Wren : {goals} buts dans le même match."],
                       "equalled": ["Autant que son record, Wren : {club_record}.", "{team} égale son record, Wren : {goals} buts dans le même match."]}},
        "es": {"headlines": {"new": ["Récord de goles del {team}", "El {team} nunca había marcado tanto"], "equalled": ["El {team} iguala su récord: {goals} goles", "El {team} iguala su récord de goles"]},
               "whys": {"new": ["El {team} ha marcado {goals}: nunca había pasado de {club_record} en un partido de liga.", "{goals} goles del {team}, su récord en un partido de liga."],
                        "equalled": ["El {team} ha marcado {goals}, tantos como su récord en liga.", "{goals} goles del {team}: su récord en un partido de liga, igualado."]},
               "heart": {"new": ["¡{goals} goles del {team}! ¡Nunca había marcado tantos!", "¿Va a parar algún día el {team}, Joss?"],
                         "equalled": ["¡{goals} goles del {team}, Joss! ¿Caerá el récord?", "¡Día de récord para el {team}, Joss!"]},
               "eye": {"new": ["Nunca tantos, Wren: el {team} nunca había marcado más de {club_record} en un partido de liga.", "Récord del club, Wren: {goals} goles en un mismo partido."],
                       "equalled": ["Tantos como su mejor marca, Wren: {club_record}.", "El {team} iguala su récord, Wren: {goals} goles en un mismo partido."]}},
    },
    "hat-trick-speed": {  # minutes between a player's first and third goal, against every hat-trick in the history
        "en": {"headlines": ["A quick-fire hat-trick from {player}{rare}", "{player} and a hat-trick in a hurry{rare}", "{player} completes his hat-trick{often}"],
               "whys": ["{player} scored at {first_goal} and had a hat-trick by {third_goal}: the fastest in {history_seasons} seasons of this league.{never}",
                        "{player} completed a hat-trick in {hat_trick_minutes} minutes, from {first_goal} to {third_goal}: only the {history_rank_ord} this quick in {history_seasons} seasons.",
                        "{player} completed a hat-trick in {hat_trick_minutes} minutes, from {first_goal} to {third_goal}.{often}"],
               "heart": ["A hat-trick in {hat_trick_minutes} minutes for {player}!", "{player} again! That was quick, Joss!{rare}", "{player} has his hat-trick, Joss!{often}"],
               "eye": ["The fastest in {history_seasons} seasons, Wren.{never}", "Only the {history_rank_ord} this quick in {history_seasons} seasons, Wren.",
                       "{hat_trick_minutes} minutes, Wren, from {first_goal} to {third_goal}.", "From {first_goal} to {third_goal}, Wren: {hat_trick_minutes} minutes for a hat-trick."]},
        "fr": {"headlines": ["Triplé éclair pour {player}{rare}", "{player}, un triplé express{rare}", "{player} signe son triplé{often}"],
               "whys": ["{player} a marqué à la {first_goal} et signé un triplé dès la {third_goal} : le plus rapide en {history_seasons} saisons de championnat.{never}",
                        "{player} a signé un triplé en {hat_trick_minutes} minutes, de la {first_goal} à la {third_goal} : seulement le {history_rank_ord} aussi rapide en {history_seasons} saisons.",
                        "{player} a signé un triplé en {hat_trick_minutes} minutes, de la {first_goal} à la {third_goal}.{often}"],
               "heart": ["Un triplé en {hat_trick_minutes} minutes pour {player} !", "Encore {player} ! Ça va vite, Joss !{rare}", "{player} tient son triplé, Joss !{often}"],
               "eye": ["Le plus rapide en {history_seasons} saisons, Wren.{never}", "Seulement le {history_rank_ord} aussi rapide en {history_seasons} saisons, Wren.",
                       "{hat_trick_minutes} minutes, Wren, de la {first_goal} à la {third_goal}.", "De la {first_goal} à la {third_goal}, Wren : {hat_trick_minutes} minutes pour un triplé."]},
        "es": {"headlines": ["Triplete relámpago de {player}{rare}", "{player}, triplete exprés{rare}", "{player} completa su triplete{often}"],
               "whys": ["{player} marcó en el {first_goal} y ya tenía un triplete en el {third_goal}: el más rápido en {history_seasons} temporadas de liga.{never}",
                        "{player} completó un triplete en {hat_trick_minutes} minutos, del {first_goal} al {third_goal}: solo el {history_rank_ord} tan rápido en {history_seasons} temporadas.",
                        "{player} completó un triplete en {hat_trick_minutes} minutos, del {first_goal} al {third_goal}.{often}"],
               "heart": ["¡Un triplete en {hat_trick_minutes} minutos para {player}!", "¡Otra vez {player}! ¡Qué rapidez, Joss!{rare}", "¡{player} ya tiene su triplete, Joss!{often}"],
               "eye": ["El más rápido en {history_seasons} temporadas, Wren.{never}", "Solo el {history_rank_ord} tan rápido en {history_seasons} temporadas, Wren.",
                       "{hat_trick_minutes} minutos, Wren, del {first_goal} al {third_goal}.", "Del {first_goal} al {third_goal}, Wren: {hat_trick_minutes} minutos para un triplete."]},
    },
    "run-unbeaten": {  # at full time, a club's unbeaten run extended by this result, or ended by a defeat (stage)
        "en": {"headlines": {"extended": ["{team} unbeaten in {run_matches}", "{team} keep the unbeaten run going"], "ended": ["The unbeaten run ends for {team}", "{team} lose after {run_matches} unbeaten"]},
               "whys": {"extended": ["{team} are now unbeaten in {run_matches} league matches.", "{run_matches} league matches without defeat for {team}, this one included."],
                        "ended": ["{team} had gone {run_matches} league matches unbeaten; this defeat ends the run.", "The run ends at {run_matches}: {team} had not lost in {run_matches} league matches."]},
               "heart": {"extended": ["{team} just keep going, Joss!", "Still unbeaten, {team}!"], "ended": ["It had to end some time, Joss. {team} are beaten.", "The run is over for {team}."]},
               "eye": {"extended": ["{run_matches} without defeat now, Wren.", "Unbeaten in {run_matches}, Wren, and counting."], "ended": ["{run_matches} unbeaten, Wren, and it ends here.", "It lasted {run_matches} matches, Wren."]}},
        "fr": {"headlines": {"extended": ["{team} invaincu depuis {run_matches} matchs", "{team} prolonge sa série d'invincibilité"], "ended": ["Fin de la série d'invincibilité pour {team}", "{team} chute après {run_matches} matchs sans défaite"]},
               "whys": {"extended": ["{team} reste invaincu depuis {run_matches} matchs de championnat.", "{run_matches} matchs de championnat sans défaite pour {team}, celui-ci compris."],
                        "ended": ["{team} restait sur {run_matches} matchs de championnat sans défaite ; cette défaite met fin à la série.", "La série s'arrête à {run_matches} : {team} n'avait plus perdu depuis {run_matches} matchs de championnat."]},
               "heart": {"extended": ["{team} ne s'arrête plus, Joss !", "Toujours invaincu, {team} !"], "ended": ["Ça devait s'arrêter un jour, Joss. {team} est battu.", "La série est finie pour {team}."]},
               "eye": {"extended": ["{run_matches} matchs sans défaite, Wren.", "Invaincu depuis {run_matches} matchs, Wren, et ça continue."], "ended": ["{run_matches} matchs sans défaite, Wren, et ça s'arrête ici.", "Elle aura duré {run_matches} matchs, Wren."]}},
        "es": {"headlines": {"extended": ["El {team}, invicto en {run_matches} partidos", "El {team} sigue invicto"], "ended": ["Se acaba la racha invicta del {team}", "El {team} cae tras {run_matches} partidos invicto"]},
               "whys": {"extended": ["El {team} lleva {run_matches} partidos de liga sin perder.", "{run_matches} partidos de liga sin derrota para el {team}, este incluido."],
                        "ended": ["El {team} llevaba {run_matches} partidos de liga sin perder; esta derrota acaba con la racha.", "La racha se queda en {run_matches}: el {team} no perdía desde hacía {run_matches} partidos de liga."]},
               "heart": {"extended": ["¡El {team} no se detiene, Joss!", "¡Sigue invicto el {team}!"], "ended": ["Tenía que acabar algún día, Joss. El {team} pierde.", "Se acabó la racha para el {team}."]},
               "eye": {"extended": ["{run_matches} sin perder ya, Wren.", "Invicto en {run_matches}, Wren, y sigue."], "ended": ["{run_matches} sin perder, Wren, y se acaba aquí.", "Duró {run_matches} partidos, Wren."]}},
    },
    "run-winless": {  # at full time, a club's run without a win extended by this result, or ended by a win (stage)
        "en": {"headlines": {"extended": ["{team} without a win in {run_matches}", "Still no win for {team}"], "ended": ["{team} win at last", "{team} win after {run_matches} matches without one"]},
               "whys": {"extended": ["{team} have now gone {run_matches} league matches without a win.", "{run_matches} league matches without a win for {team}, this one included."],
                        "ended": ["{team} had gone {run_matches} league matches without a win; this one ends the wait.", "The wait ends at {run_matches}: {team} had not won in {run_matches} league matches."]},
               "heart": {"extended": ["{team} are still waiting for a win, Joss.", "It is getting long for {team}, Joss."], "ended": ["At last! {team} have won!", "The wait is over for {team}!"]},
               "eye": {"extended": ["{run_matches} without a win now, Wren.", "No win in {run_matches}, Wren."], "ended": ["{run_matches} without a win, Wren, and it ends here.", "The wait lasted {run_matches} matches, Wren."]}},
        "fr": {"headlines": {"extended": ["{team} sans victoire depuis {run_matches} matchs", "Toujours pas de victoire pour {team}"], "ended": ["{team} gagne enfin", "{team} met fin à {run_matches} matchs sans victoire"]},
               "whys": {"extended": ["{team} reste sur {run_matches} matchs de championnat sans victoire.", "{run_matches} matchs de championnat sans victoire pour {team}, celui-ci compris."],
                        "ended": ["{team} restait sur {run_matches} matchs de championnat sans victoire ; celle-ci met fin à l'attente.", "L'attente s'arrête à {run_matches} : {team} n'avait plus gagné depuis {run_matches} matchs de championnat."]},
               "heart": {"extended": ["{team} attend toujours sa victoire, Joss.", "Ça devient long pour {team}, Joss."], "ended": ["Enfin ! {team} a gagné !", "L'attente est finie pour {team} !"]},
               "eye": {"extended": ["{run_matches} matchs sans victoire, Wren.", "Pas de victoire depuis {run_matches} matchs, Wren."], "ended": ["{run_matches} matchs sans victoire, Wren, et ça s'arrête ici.", "L'attente aura duré {run_matches} matchs, Wren."]}},
        "es": {"headlines": {"extended": ["El {team}, sin ganar en {run_matches} partidos", "Sigue sin ganar el {team}"], "ended": ["El {team} gana por fin", "El {team} acaba con {run_matches} partidos sin ganar"]},
               "whys": {"extended": ["El {team} lleva {run_matches} partidos de liga sin ganar.", "{run_matches} partidos de liga sin ganar para el {team}, este incluido."],
                        "ended": ["El {team} llevaba {run_matches} partidos de liga sin ganar; este acaba con la espera.", "La espera se queda en {run_matches}: el {team} no ganaba desde hacía {run_matches} partidos de liga."]},
               "heart": {"extended": ["El {team} sigue esperando su victoria, Joss.", "Se le hace largo al {team}, Joss."], "ended": ["¡Por fin! ¡El {team} ha ganado!", "¡Se acabó la espera para el {team}!"]},
               "eye": {"extended": ["{run_matches} sin ganar ya, Wren.", "Ninguna victoria en {run_matches}, Wren."], "ended": ["{run_matches} sin ganar, Wren, y se acaba aquí.", "La espera duró {run_matches} partidos, Wren."]}},
    },
}

BENCH_WORDS = ("{came_on}", "bench", "banc", "banquillo", "substitute", "remplaçant", "suplente", "cours de match")
MARKERS = ("{never}", "{club_first}", "{rare}", "{often}", "{starter}", "{plain}", "{first_win}")
STAGE_FIELDS = ("stage", "goal_type", "record", "venue", "window")  # the fact that says which templates of a part are true: how far it went, what kind of goal, a new or an equalled record, where, when

STAT_PREFIX = {"en": "Stat of the match: ", "fr": "Stat du match : ", "es": "El dato del partido: "}
STAT_WHY = {"en": " The standout fact of the match.", "fr": " Le fait marquant du match.", "es": " El dato más destacado del partido."}  # it ranks first: why, in bits and against the runner-up, stays in why_first
STAT_HEART = {  # Wren asks for the stat of the match, never a question the fact's own lines already asked; Joss answers with a line of the family he has not said yet
    "scorer": {"en": ["Joss, how will {player}'s night be remembered?", "Joss, put {player}'s night in context for us."],
               "fr": ["Joss, que restera-t-il de cette soirée pour {player} ?", "Joss, où se situe la soirée que vient de vivre {player} ?"],
               "es": ["Joss, ¿cómo se recordará esta noche de {player}?", "Joss, ¿dónde se sitúa lo de {player} esta noche?"]},
    "comeback": {"en": ["Joss, when did {team} last come back like this?", "Joss, how rare is a night like this for {team}?"],
                 "fr": ["Joss, quand {team} est-il revenu ainsi pour la dernière fois ?", "Joss, c'est rare pour {team}, une soirée pareille ?"],
                 "es": ["Joss, ¿cuándo fue la última remontada así del {team}?", "Joss, ¿qué tan rara es una noche así para el {team}?"]},
    "ht-lead-lost": {"en": ["Joss, when did this last happen to {team}?", "Joss, has this happened to {team} before?"],
                     "fr": ["Joss, à quand remonte la dernière fois pour {team} ?", "Joss, c'était déjà arrivé à {team} ?"],
                     "es": ["Joss, ¿cuándo le pasó esto por última vez al {team}?", "Joss, ¿le había pasado esto antes al {team}?"]},
    "xg": {"en": ["Joss, how did {team} score so many?", "Joss, what do the chances say about {team} tonight?"],
           "fr": ["Joss, comment {team} a pu en marquer autant ?", "Joss, que disent les occasions de {team} ce soir ?"],
           "es": ["Joss, ¿cómo ha marcado tanto el {team}?", "Joss, ¿qué dicen las ocasiones del {team} esta noche?"]},
    "any": {"en": ["Joss, your stat of the match?", "So, Joss, what is the stat of the match?"],
            "fr": ["Joss, ta stat du match ?", "Alors, Joss, la stat du match ?"],
            "es": ["Joss, ¿tu dato del partido?", "A ver, Joss, ¿el dato del partido?"]},
}

T = {family: {lang: fr_typography(v) if lang == "fr" else v for lang, v in by_lang.items()} for family, by_lang in T.items()}
STAT_PREFIX["fr"], STAT_WHY["fr"] = fr_typography(STAT_PREFIX["fr"]), fr_typography(STAT_WHY["fr"])
STAT_HEART = {family: {lang: fr_typography(v) if lang == "fr" else v for lang, v in by_lang.items()} for family, by_lang in STAT_HEART.items()}


def family(doc: dict) -> str | None:
    """The family of an insight, from its key (`scorer-home-20-4`, `comeback-home-2-3`); the stat of the match takes the family of its top fact; a storyline of two clubs fighting relegation has its own."""
    if doc.get("type") == "storyline":
        focus = doc.get("focus") or {}
        return "storyline" if focus and all(v == "relegation" for v in focus.values()) else None
    key = str((doc.get("headline_facts") or {}).get("top_fact") or doc.get("key") or "")
    return next((f for f in T if key.startswith(f + "-")), None)


def part(t: dict, name: str, facts: dict) -> list[str]:
    """The templates of one part, those of the stage the fact reached when they depend on it (a comeback that drew level is not yet a win, a late equaliser is not a go-ahead goal): the first of `STAGE_FIELDS` whose value keys the part."""
    p = t[name]
    if isinstance(p, list):
        return p
    stage = next((facts[f] for f in STAGE_FIELDS if isinstance(facts.get(f), str) and facts[f] in p), None)
    return p[stage] if stage is not None else p.get("any", [])


def values(facts: dict, lang: str) -> tuple[dict, dict]:
    """The placeholders for one language, and the fact field behind each numeric one."""
    vals, fields = {}, {}
    for k, v in facts.items():
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, int):
            vals[k] = Num(v, lang)
        elif isinstance(v, float):
            vals[k] = Dec(v, lang)
        else:
            vals[k] = v
        if isinstance(v, (int, float)) or any(c.isdigit() for c in str(v)):
            fields[k] = k
    for side in ("home", "away"):  # storyline facts are keyed by club slug (rovers_rank): give them a side alias, ranks as ordinals
        club = facts.get(f"{side}_club")
        if not isinstance(club, str):
            continue
        prefix = club.lower().replace(" ", "_") + "_"  # storyline.slug
        for k, v in facts.items():
            if k.startswith(prefix) and isinstance(v, (int, float)) and not isinstance(v, bool):
                alias = side + "_" + k[len(prefix):]
                vals[alias], fields[alias] = vals[k], k
                if "_rank" in alias and isinstance(v, int):
                    vals[alias + "_ord"] = Ord(v, lang)
                    fields[alias + "_ord"] = k
    if isinstance(facts.get("goals"), int) and facts["goals"] >= 2:
        vals["feat"] = FEAT[lang][min(facts["goals"], 5)]
    count, seasons = facts.get("history_count"), facts.get("history_seasons")
    if isinstance(facts.get("history_rank"), int) and isinstance(count, int) and isinstance(seasons, int) and 0 < count < seasons:
        vals["history_rank_ord"] = Ord(facts["history_rank"], lang)  # "the 6th": only while the feat is rarer than once a season
        fields["history_rank_ord"] = "history_rank"
    if facts.get("stage") in DONE[lang]:
        vals["done"] = DONE[lang][facts["stage"]]
    if facts.get("venue") in VENUE[lang]:
        vals["venue"] = VENUE[lang][facts["venue"]]
        vals["venue_adv"] = {"home": "at home", "away": "away"}[facts["venue"]] if lang == "en" else vals["venue"]
    if isinstance(facts.get("club_seasons_since"), int) and facts["club_seasons_since"] < 2:
        vals.pop("club_seasons_since")  # "1 seasons ago": the template without it is used
    team, home, away = facts.get("team"), facts.get("home_team"), facts.get("away_team")
    if isinstance(home, str) and isinstance(away, str) and team in (home, away):
        vals["other_team"] = away if team == home else home  # the shots of the other side, which a fact about one team does not name
    return vals, fields


def often(facts: dict) -> bool:
    """A feat the league sees at least once a season on average: told for who did it, never with the rate, and its count in the history stays with the producer (`writer.on_air`). Read from `history_per_season`, or from the count and the seasons when a store left the rate out."""
    per, count, seasons = facts.get("history_per_season"), facts.get("history_count"), facts.get("history_seasons")
    if isinstance(per, (int, float)) and not isinstance(per, bool):
        return True
    return isinstance(count, int) and not isinstance(count, bool) and isinstance(seasons, int) and 0 < seasons <= count


def usable(template: str, facts: dict) -> bool:
    """Whether a template is true for these facts; its markers are removed by `clean`. A `{plain}` template is true as well, but only told when no other template of its part can be (`pools`)."""
    if not facts.get("substitute") and any(w in template for w in BENCH_WORDS):
        return False
    if "{starter}" in template and facts.get("substitute") is not False:
        return False
    if "{never}" in template and facts.get("history_count") != 0:
        return False
    if "{club_first}" in template and facts.get("club_first_time") is not True:
        return False
    if "{first_win}" in template and facts.get("first_win_here") is not True:
        return False
    if "{often}" in template and not often(facts):
        return False
    if "{rare}" in template and (often(facts) or not isinstance(facts.get("history_count"), int)):
        return False
    return True


def clean(template: str) -> str:
    for m in MARKERS:
        template = template.replace(m, "")
    return template


def pools(templates: list[str], facts: dict) -> tuple[list[str], list[str]]:
    """The templates of a part that are true for these facts, without their markers: those told first, and the `{plain}` ones, told only when none of the first can be (a club the history does not know, a landmark too recent to count in seasons)."""
    true = [x for x in templates if usable(x, facts)]
    return [clean(x) for x in true if "{plain}" not in x], [clean(x) for x in true if "{plain}" in x]
