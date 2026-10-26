"""Fallback texts and duo lines of the insights the engine produces, by family of fact, with the numbers of `headline_facts`.

The generic lines of `writer.INSIGHT_TEMPLATES` stay for the kinds not covered here. Each number is a placeholder named after its fact field, so `duo.fill` claims it against that field and the Verifier checks it.

What a viewer can picture: the feat named for who did it ("the first four-goal haul by a substitute in 50 seasons"), and a team's own landmark in seasons ("Yarnford's first away defeat after leading at half-time since 2020/21"). League-wide rates, match counts, ratios and bits ("once in 83", "about 3 a season", "228 times rarer", "0 in 19,000") never go on air or into a duo line: they stay for the producer in the console, in `rarity.basis` and `why_first`, and `writer.on_air` keeps them out of what the model sees.

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
# {often} a feat the league sees at least once a season (told without the rate); a template with a placeholder the facts lack is skipped.
T = {  # family -> lang -> part -> templates, or {stage: templates, "any": templates} when the stage changes what is true
    "scorer": {
        "en": {"headlines": ["{player}: a {feat} off the bench", "On at {came_on}, {player} has a {feat}", "A {feat} for {player}"],
               "whys": ["On at {came_on}, {player} scored at {goal_minutes}. The first {feat} by a substitute in {history_seasons} seasons of this league.{never}",
                        "On at {came_on}, {player} scored at {goal_minutes}. Only the {history_rank_ord} {feat} by a substitute in {history_seasons} seasons, the first since {last_player} in {last_season}.",
                        "On at {came_on}, {player} scored at {goal_minutes}: a {feat} off the bench.{often}"],
               "heart": ["{player} again! Joss, has a substitute ever done this?{rare}", "A {feat} for {player}! And he only came on at {came_on}!",
                         "{player} again, and off the bench too!{often}"],
               "eye": ["Never, Wren. In {history_seasons} seasons, no player coming off the bench had scored {goals}.{never}",
                       "A first in {history_seasons} seasons, Wren: no substitute had ever scored {goals} in this league.{never}",
                       "The {history_rank_ord} in {history_seasons} seasons, Wren. The last one was {last_player}, in {last_season}.",
                       "Not since {last_player}, in {last_season}, Wren.{rare}",
                       "{goals} goals off the bench, Wren. Not a bad way to come on.{often}",
                       "That is what a substitute is for, Wren: {goals} goals since coming on.{often}"]},
        "fr": {"headlines": ["{player}, un {feat} en sortie de banc", "Entré à la {came_on}, {player} signe un {feat}", "Un {feat} pour {player}"],
               "whys": ["Entré à la {came_on}, {player} a marqué : {goal_minutes}. Premier {feat} d'un joueur entré en cours de match en {history_seasons} saisons de championnat.{never}",
                        "Entré à la {came_on}, {player} a marqué : {goal_minutes}. Seulement le {history_rank_ord} {feat} d'un remplaçant en {history_seasons} saisons, le premier depuis {last_player} en {last_season}.",
                        "Entré à la {came_on}, {player} a marqué : {goal_minutes}. Un {feat} en sortie de banc.{often}"],
               "heart": ["Encore {player} ! Joss, un remplaçant a déjà fait ça ?{rare}", "Un {feat} pour {player} ! Et il n'est entré qu'à la {came_on} !",
                         "Encore {player}, et en sortie de banc !{often}"],
               "eye": ["Jamais, Wren. En {history_seasons} saisons, aucun joueur entré en cours de match n'avait marqué {goals} fois.{never}",
                       "Une première en {history_seasons} saisons, Wren : aucun remplaçant n'avait jamais marqué {goals} buts dans ce championnat.{never}",
                       "Le {history_rank_ord} en {history_seasons} saisons, Wren. Le dernier : {last_player}, en {last_season}.",
                       "Pas depuis {last_player}, en {last_season}, Wren.{rare}",
                       "{goals} buts depuis son entrée en jeu, Wren. Pas mal pour un remplaçant.{often}",
                       "Voilà à quoi sert un remplaçant, Wren : {goals} buts depuis son entrée.{often}"]},
        "es": {"headlines": ["{player}, un {feat} desde el banquillo", "Entró en el {came_on} y {player} firma un {feat}", "Un {feat} para {player}"],
               "whys": ["Entró en el {came_on} y {player} marcó: {goal_minutes}. Primer {feat} de un suplente en {history_seasons} temporadas de liga.{never}",
                        "Entró en el {came_on} y {player} marcó: {goal_minutes}. Solo el {history_rank_ord} {feat} de un suplente en {history_seasons} temporadas, el primero desde {last_player} en {last_season}.",
                        "Entró en el {came_on} y {player} marcó: {goal_minutes}. Un {feat} desde el banquillo.{often}"],
               "heart": ["¡Otra vez {player}! Joss, ¿un suplente había hecho esto?{rare}", "¡Un {feat} de {player}! ¡Y entró en el {came_on}!",
                         "¡Otra vez {player}, y desde el banquillo!{often}"],
               "eye": ["Nunca, Wren. En {history_seasons} temporadas, ningún jugador salido del banquillo había marcado {goals}.{never}",
                       "Una primera vez en {history_seasons} temporadas, Wren: ningún suplente había marcado {goals} goles en esta liga.{never}",
                       "El {history_rank_ord} en {history_seasons} temporadas, Wren. El último: {last_player}, en {last_season}.",
                       "No pasaba desde {last_player}, en {last_season}, Wren.{rare}",
                       "{goals} goles desde que salió del banquillo, Wren. No está mal para un suplente.{often}",
                       "Para eso está un suplente, Wren: {goals} goles desde que entró.{often}"]},
    },
    "comeback": {
        "en": {"headlines": {"level": ["{team} draw level from {trailing_score} down", "{team} back on terms after {trailing_score}"],
                             "ahead": ["{team} turn {trailing_score} around", "{team} now lead after trailing {trailing_score}"],
                             "won": ["{team} win from {trailing_score} down", "{team} complete the comeback from {trailing_score}"], "any": ["{team} come back from {trailing_score}"]},
               "whys": ["{team} had not {done} from {deficit_goals} goals down since {club_last_season}, {club_seasons_since} seasons ago.",
                        "A first for {team} in {history_seasons} seasons: the club had never {done} from {deficit_goals} goals down.{club_first}"],
               "heart": {"level": ["{trailing_score} down, and look at {team} now!", "Back from {trailing_score} down! {team} are level!"],
                         "ahead": ["Who saw this coming at {trailing_score}?", "{team} were {trailing_score} down, and now they lead!"],
                         "won": ["{team} have done it! From {trailing_score} down to a win!", "{trailing_score} down, and {team} have won it!"],
                         "any": ["{trailing_score} down, and look at {team} now!"]},
               "eye": {"level": ["{club_seasons_since} seasons since {team} last drew level from {deficit_goals} goals down, Wren.",
                                 "Not since {club_last_season} had {team} drawn level from {deficit_goals} goals down, Wren.",
                                 "Never, Wren. Not once in {history_seasons} seasons had {team} drawn level from {deficit_goals} goals down.{club_first}",
                                 "A first for {team} in {history_seasons} seasons, Wren: level after {deficit_goals} goals down.{club_first}"],
                       "ahead": ["{club_seasons_since} seasons since {team} last went ahead from {deficit_goals} goals down, Wren.",
                                 "Not since {club_last_season} had {team} turned a {deficit_goals}-goal deficit into a lead, Wren.",
                                 "Never, Wren. Not once in {history_seasons} seasons had {team} gone ahead from {deficit_goals} goals down.{club_first}",
                                 "A first for {team} in {history_seasons} seasons, Wren: ahead after {deficit_goals} goals down.{club_first}"],
                       "won": ["{club_seasons_since} seasons since {team} last won from {deficit_goals} goals down, Wren. That is how long they had waited.",
                               "Not since {club_last_season} had {team} won from {deficit_goals} goals down, Wren.",
                               "Never, Wren. Not once in {history_seasons} seasons had {team} won from {deficit_goals} goals down.{club_first}",
                               "A first for {team} in {history_seasons} seasons, Wren: a win after {deficit_goals} goals down.{club_first}"],
                       "any": ["{club_seasons_since} seasons, Wren. That is how long {team} had waited.", "Never, Wren. Not once in {history_seasons} seasons had {team} done it.{club_first}"]}},
        "fr": {"headlines": {"level": ["{team} recolle après avoir été mené {trailing_score}", "{team} revient de {trailing_score}"],
                             "ahead": ["Mené {trailing_score}, {team} passe devant", "{team} renverse un {trailing_score}"],
                             "won": ["{team} gagne après avoir été mené {trailing_score}", "Mené {trailing_score}, {team} renverse tout"], "any": ["{team} remonte de {trailing_score}"]},
               "whys": ["{team} n'avait plus {done} après avoir été mené de {deficit_goals} buts depuis {club_last_season}, il y a {club_seasons_since} saisons.",
                        "Une première pour {team} en {history_seasons} saisons : jamais le club n'avait {done} après avoir été mené de {deficit_goals} buts.{club_first}"],
               "heart": {"level": ["Mené {trailing_score}, et regarde {team} maintenant !", "Revenu de {trailing_score} ! {team} recolle au score !"],
                         "ahead": ["Qui y croyait à {trailing_score} ?", "Mené {trailing_score}, et {team} passe devant !"],
                         "won": ["{team} l'a fait ! Mené {trailing_score}, et vainqueur !", "Mené {trailing_score}, et {team} s'impose !"],
                         "any": ["Mené {trailing_score}, et regarde {team} maintenant !"]},
               "eye": {"level": ["{club_seasons_since} saisons que {team} n'était plus revenu à hauteur après avoir été mené de {deficit_goals} buts, Wren.",
                                 "Pas depuis {club_last_season}, Wren : {team} n'avait plus égalisé après avoir été mené de {deficit_goals} buts.",
                                 "Jamais, Wren. En {history_seasons} saisons, {team} n'était jamais revenu à hauteur après avoir été mené de {deficit_goals} buts.{club_first}",
                                 "Une première pour {team} en {history_seasons} saisons, Wren : revenir après {deficit_goals} buts de retard.{club_first}"],
                       "ahead": ["{club_seasons_since} saisons que {team} n'avait plus pris l'avantage après avoir été mené de {deficit_goals} buts, Wren.",
                                 "Pas depuis {club_last_season}, Wren : {team} n'avait plus renversé un retard de {deficit_goals} buts.",
                                 "Jamais, Wren. En {history_seasons} saisons, {team} n'avait jamais pris l'avantage après avoir été mené de {deficit_goals} buts.{club_first}",
                                 "Une première pour {team} en {history_seasons} saisons, Wren : passer devant après {deficit_goals} buts de retard.{club_first}"],
                       "won": ["{club_seasons_since} saisons que {team} n'avait plus gagné après avoir été mené de {deficit_goals} buts, Wren.",
                               "Pas depuis {club_last_season}, Wren : {team} n'avait plus gagné un match en étant mené de {deficit_goals} buts.",
                               "Jamais, Wren. En {history_seasons} saisons, {team} n'avait jamais gagné après avoir été mené de {deficit_goals} buts.{club_first}",
                               "Une première pour {team} en {history_seasons} saisons, Wren : gagner après {deficit_goals} buts de retard.{club_first}"],
                       "any": ["{club_seasons_since} saisons, Wren. Voilà combien de temps {team} attendait ça.", "Jamais, Wren. En {history_seasons} saisons, {team} ne l'avait jamais fait.{club_first}"]}},
        "es": {"headlines": {"level": ["El {team} empata tras ir {trailing_score}", "El {team} iguala un {trailing_score}"],
                             "ahead": ["El {team} le da la vuelta al {trailing_score}", "El {team} ya gana tras ir {trailing_score}"],
                             "won": ["El {team} gana tras ir perdiendo {trailing_score}", "El {team} completa la remontada del {trailing_score}"], "any": ["El {team} remonta un {trailing_score}"]},
               "whys": ["El {team} no había {done} tras ir perdiendo por {deficit_goals} goles desde {club_last_season}, hace {club_seasons_since} temporadas.",
                        "Una primera vez para el {team} en {history_seasons} temporadas: nunca había {done} tras ir perdiendo por {deficit_goals} goles.{club_first}"],
               "heart": {"level": ["¡Perdían {trailing_score} y mira al {team} ahora!", "¡Remontan desde el {trailing_score}! ¡El {team} empata!"],
                         "ahead": ["¿Quién lo veía venir con el {trailing_score}?", "¡Perdía {trailing_score} y ahora el {team} va por delante!"],
                         "won": ["¡Lo ha hecho el {team}! ¡Del {trailing_score} a la victoria!", "¡Perdía {trailing_score} y el {team} se lleva el partido!"],
                         "any": ["¡Perdían {trailing_score} y mira al {team} ahora!"]},
               "eye": {"level": ["{club_seasons_since} temporadas llevaba el {team} sin empatar tras ir perdiendo por {deficit_goals} goles, Wren.",
                                 "No pasaba desde {club_last_season}, Wren: el {team} no empataba tras ir perdiendo por {deficit_goals} goles.",
                                 "Nunca, Wren. En {history_seasons} temporadas, el {team} no había empatado tras ir perdiendo por {deficit_goals} goles.{club_first}",
                                 "Una primera vez para el {team} en {history_seasons} temporadas, Wren: empatar tras ir {deficit_goals} goles abajo.{club_first}"],
                       "ahead": ["{club_seasons_since} temporadas llevaba el {team} sin adelantarse tras ir perdiendo por {deficit_goals} goles, Wren.",
                                 "No pasaba desde {club_last_season}, Wren: el {team} no se ponía por delante tras ir perdiendo por {deficit_goals} goles.",
                                 "Nunca, Wren. En {history_seasons} temporadas, el {team} no se había adelantado tras ir perdiendo por {deficit_goals} goles.{club_first}",
                                 "Una primera vez para el {team} en {history_seasons} temporadas, Wren: por delante tras ir {deficit_goals} goles abajo.{club_first}"],
                       "won": ["{club_seasons_since} temporadas llevaba el {team} sin ganar tras ir perdiendo por {deficit_goals} goles, Wren.",
                               "No pasaba desde {club_last_season}, Wren: el {team} no ganaba tras ir perdiendo por {deficit_goals} goles.",
                               "Nunca, Wren. En {history_seasons} temporadas, el {team} no había ganado tras ir perdiendo por {deficit_goals} goles.{club_first}",
                               "Una primera vez para el {team} en {history_seasons} temporadas, Wren: ganar tras ir {deficit_goals} goles abajo.{club_first}"],
                       "any": ["{club_seasons_since} temporadas, Wren. Eso es lo que esperaba el {team}.", "Nunca, Wren. En {history_seasons} temporadas, el {team} no lo había hecho.{club_first}"]}},
    },
    "ht-lead-lost": {
        "en": {"headlines": {"behind": ["{team} led {half_time_score} at the break, now trail", "{team} let a {half_time_score} half-time lead slip"],
                             "lost": ["{team} led {half_time_score} at the break, and lost", "{team} lose after leading {half_time_score} at half-time"],
                             "any": ["{team} let a {half_time_score} half-time lead slip"]},
               "whys": {"lost": ["{team}'s first {venue} defeat after leading at half-time since {club_last_season}, {club_seasons_since} seasons ago.",
                                 "A first in {history_seasons} seasons: {team} had never lost {venue_adv} after leading at half-time.{club_first}"],
                        "behind": ["If it ends like this, it will be {team}'s first {venue} defeat after leading at half-time since {club_last_season}.",
                                   "If it ends like this, a first in {history_seasons} seasons: {team} have never lost {venue_adv} after leading at half-time.{club_first}"]},
               "heart": {"behind": ["{half_time_score} at the break, and look at {team} now.", "How has this got away from {team}?"],
                         "lost": ["{half_time_score} at the break, and {team} have lost it.", "How did {team} let a {half_time_score} half-time lead go?"],
                         "any": ["How has this got away from {team}?"]},
               "eye": {"lost": ["{club_seasons_since} seasons since that last happened to {team}, Wren.", "Not since {club_last_season} had {team} lost {venue_adv} after leading at the break, Wren.",
                                "Never in {history_seasons} seasons, Wren.{club_first}", "A first for {team} in {history_seasons} seasons, Wren: never had they lost {venue_adv} from ahead at the break.{club_first}"],
                       "behind": ["Not over yet, but {team} have not lost one like this since {club_last_season}.", "If it ends this way, a first for {team} since {club_last_season}, Wren.",
                                  "Not over yet, but {team} have never lost one like this.{club_first}", "If it ends this way, a first for {team}, Wren.{club_first}"]}},
        "fr": {"headlines": {"behind": ["{team} menait {half_time_score} à la pause, le voilà mené", "{team} laisse filer son avance de la pause"],
                             "lost": ["{team} menait {half_time_score} à la pause, et a perdu", "{team} perd après avoir mené {half_time_score}"],
                             "any": ["{team} laisse filer son avance de la pause"]},
               "whys": {"lost": ["Première défaite de {team} {venue} après avoir mené à la pause depuis {club_last_season}, il y a {club_seasons_since} saisons.",
                                 "Une première en {history_seasons} saisons : jamais {team} n'avait perdu {venue} après avoir mené à la pause.{club_first}"],
                        "behind": ["Si ça reste ainsi, ce sera la première défaite de {team} {venue} après avoir mené à la pause depuis {club_last_season}.",
                                   "Si ça reste ainsi, une première en {history_seasons} saisons : jamais {team} n'a perdu {venue} après avoir mené à la pause.{club_first}"]},
               "heart": {"behind": ["{half_time_score} à la pause, et regarde où en est {team}.", "Comment {team} a pu laisser filer ça ?"],
                         "lost": ["{half_time_score} à la pause, et {team} a tout perdu.", "Comment {team} a-t-il pu laisser filer une avance de {half_time_score} à la pause ?"],
                         "any": ["Comment {team} a pu laisser filer ça ?"]},
               "eye": {"lost": ["{club_seasons_since} saisons que ça n'était plus arrivé à {team}, Wren.", "Pas depuis {club_last_season}, Wren : {team} n'avait plus perdu {venue} après avoir mené à la pause.",
                                "Jamais en {history_seasons} saisons, Wren.{club_first}", "Une première pour {team} en {history_seasons} saisons, Wren : jamais battu {venue} après avoir mené à la pause.{club_first}"],
                       "behind": ["Ce n'est pas fini, mais {team} n'a plus perdu un match comme ça depuis {club_last_season}.", "Si ça se termine ainsi, une première depuis {club_last_season} pour {team}, Wren.",
                                  "Ce n'est pas fini, mais {team} n'a jamais perdu un match comme ça.{club_first}", "Si ça se termine ainsi, une première pour {team}, Wren.{club_first}"]}},
        "es": {"headlines": {"behind": ["El {team} ganaba {half_time_score} al descanso y ahora pierde", "El {team} deja escapar su ventaja del descanso"],
                             "lost": ["El {team} ganaba {half_time_score} al descanso y perdió", "El {team} pierde tras ganar {half_time_score} al descanso"],
                             "any": ["El {team} deja escapar su ventaja del descanso"]},
               "whys": {"lost": ["Primera derrota del {team} {venue} tras ir ganando al descanso desde {club_last_season}, hace {club_seasons_since} temporadas.",
                                 "Una primera vez en {history_seasons} temporadas: el {team} nunca había perdido {venue} tras ir ganando al descanso.{club_first}"],
                        "behind": ["Si termina así, será la primera derrota del {team} {venue} tras ir ganando al descanso desde {club_last_season}.",
                                   "Si termina así, una primera vez en {history_seasons} temporadas: el {team} nunca ha perdido {venue} tras ir ganando al descanso.{club_first}"]},
               "heart": {"behind": ["{half_time_score} al descanso, y mira al {team} ahora.", "¿Cómo se le ha escapado esto al {team}?"],
                         "lost": ["{half_time_score} al descanso, y el {team} lo ha perdido todo.", "¿Cómo ha perdido el {team} un partido que ganaba {half_time_score} al descanso?"],
                         "any": ["¿Cómo se le ha escapado esto al {team}?"]},
               "eye": {"lost": ["Hacía {club_seasons_since} temporadas que no le pasaba al {team}, Wren.", "No pasaba desde {club_last_season}, Wren: el {team} no perdía {venue} tras ir ganando al descanso.",
                                "Nunca en {history_seasons} temporadas, Wren.{club_first}", "Una primera vez para el {team} en {history_seasons} temporadas, Wren: nunca había perdido {venue} tras ir ganando al descanso.{club_first}"],
                       "behind": ["No ha terminado, pero el {team} no pierde uno así desde {club_last_season}.", "Si termina así, será la primera vez desde {club_last_season} para el {team}, Wren.",
                                  "No ha terminado, pero el {team} nunca ha perdido uno así.{club_first}", "Si termina así, será una primera vez para el {team}, Wren.{club_first}"]}},
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
    "xg-ft": {  # goals against expected goals: the fact, never how many matches go that way
        "en": {"headlines": ["{team}: {goals} goals from {xg} xG", "{team} beat their {xg} xG by a mile"],
               "whys": ["{team} scored {goals} from chances worth {xg} expected goals."],
               "heart": ["Everything {team} hit went in tonight!", "{team} could not miss tonight!"],
               "eye": ["{goals} goals from chances worth {xg} expected goals.", "Chances worth {xg} expected goals, and {goals} goals for {team}."]},
        "fr": {"headlines": ["{team} : {goals} buts pour {xg} xG", "{team} pulvérise ses {xg} xG"],
               "whys": ["{team} a marqué {goals} buts sur des occasions qui en valaient {xg}."],
               "heart": ["Tout ce que {team} a tenté est rentré ce soir !", "{team} n'a rien raté ce soir !"],
               "eye": ["{goals} buts sur des occasions qui en valaient {xg}.", "{goals} buts, pour des occasions qui n'en valaient que {xg}."]},
        "es": {"headlines": ["El {team}: {goals} goles con {xg} xG", "El {team} pulveriza sus {xg} xG"],
               "whys": ["El {team} marcó {goals} con ocasiones que valían {xg} goles esperados."],
               "heart": ["¡Todo lo que tiró el {team} entró esta noche!", "¡El {team} no ha fallado nada esta noche!"],
               "eye": ["{goals} goles con ocasiones que valían {xg} goles esperados.", "Ocasiones de {xg} goles esperados, y {goals} goles del {team}."]},
    },
}

BENCH_WORDS = ("{came_on}", "bench", "banc", "banquillo", "substitute", "remplaçant", "suplente", "cours de match")
MARKERS = ("{never}", "{club_first}", "{rare}", "{often}")

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
    "xg-ft": {"en": ["Joss, how did {team} score so many?", "Joss, what do the chances say about {team} tonight?"],
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
    """The templates of one part, those of the stage the fact reached when they depend on it (a comeback that drew level is not yet a win)."""
    p = t[name]
    return p if isinstance(p, list) else p.get(facts.get("stage"), p.get("any", []))


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
    for side in ("home", "away"):  # storyline facts are keyed by club slug (varleigh_rank): give them a side alias, ranks as ordinals
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
    return vals, fields


def often(facts: dict) -> bool:
    """A feat the league sees at least once a season on average: told for who did it, never with the rate, and its count in the history stays with the producer (`writer.on_air`). Read from `history_per_season`, or from the count and the seasons when a store left the rate out."""
    per, count, seasons = facts.get("history_per_season"), facts.get("history_count"), facts.get("history_seasons")
    if isinstance(per, (int, float)) and not isinstance(per, bool):
        return True
    return isinstance(count, int) and not isinstance(count, bool) and isinstance(seasons, int) and 0 < seasons <= count


def usable(template: str, facts: dict) -> bool:
    """Whether a template is true for these facts; its markers are removed by `clean`."""
    if not facts.get("substitute") and any(w in template for w in BENCH_WORDS):
        return False
    if "{never}" in template and facts.get("history_count") != 0:
        return False
    if "{club_first}" in template and facts.get("club_first_time") is not True:
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
