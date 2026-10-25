"""Fallback texts and duo lines of the insights the engine produces, by family of fact, with the numbers of `headline_facts`.

The generic lines of `writer.INSIGHT_TEMPLATES` stay for the kinds not covered here. Each number is a placeholder named after its fact field, so `duo.fill` claims it against that field and the Verifier checks it.

What a viewer can picture: the feat named for who did it ("the first four-goal haul by a substitute in 50 seasons"), and a team's own landmark in seasons ("Yarnford's first away defeat after leading at half-time since 2020/21"). League-wide rates and match counts ("once in 83", "0 in 19,000") stay in the console, in `rarity.basis`.
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

# Markers: {never} needs a zero history count, {club_first} a team that had never done it; a template with a placeholder the facts lack is skipped.
T = {  # family -> lang -> part -> templates, or {stage: templates, "any": templates} when the stage changes what is true
    "scorer": {
        "en": {"headlines": ["{player}: a {feat} off the bench", "On at {came_on}, {player} has a {feat}", "A {feat} for {player}"],
               "whys": ["On at {came_on}, {player} scored at {goal_minutes}. The first {feat} by a substitute in {history_seasons} seasons of this league.{never}",
                        "On at {came_on}, {player} scored at {goal_minutes}. Only the {history_rank_ord} {feat} by a substitute in {history_seasons} seasons, the first since {last_player} in {last_season}.",
                        "On at {came_on}, {player} scored at {goal_minutes}. A {feat} off the bench: this league sees about {history_per_season} a season."],
               "heart": ["{player} again! Joss, has a substitute ever done this?", "A {feat} for {player}! And {player} only came on at {came_on}!"],
               "eye": ["Never, Wren. In {history_seasons} seasons, no player coming off the bench had scored {goals}.{never}",
                       "The {history_rank_ord} in {history_seasons} seasons, Wren. The last one was {last_player}, in {last_season}.",
                       "About {history_per_season} a season from the bench in this league. This one will be remembered."]},
        "fr": {"headlines": ["{player}, un {feat} en sortie de banc", "Entré à la {came_on}, {player} signe un {feat}", "Un {feat} pour {player}"],
               "whys": ["Entré à la {came_on}, {player} a marqué : {goal_minutes}. Premier {feat} d'un joueur entré en cours de match en {history_seasons} saisons de championnat.{never}",
                        "Entré à la {came_on}, {player} a marqué : {goal_minutes}. Seulement le {history_rank_ord} {feat} d'un remplaçant en {history_seasons} saisons, le premier depuis {last_player} en {last_season}.",
                        "Entré à la {came_on}, {player} a marqué : {goal_minutes}. Un {feat} en sortie de banc : le championnat en voit environ {history_per_season} par saison."],
               "heart": ["Encore {player} ! Joss, un remplaçant a déjà fait ça ?", "Un {feat} pour {player} ! Et il n'est entré qu'à la {came_on} !"],
               "eye": ["Jamais, Wren. En {history_seasons} saisons, aucun joueur entré en cours de match n'avait marqué {goals} fois.{never}",
                       "Le {history_rank_ord} en {history_seasons} saisons, Wren. Le dernier : {last_player}, en {last_season}.",
                       "Le championnat en voit environ {history_per_season} par saison en sortie de banc. Celui-là, on s'en souviendra."]},
        "es": {"headlines": ["{player}, un {feat} desde el banquillo", "Entró en el {came_on} y {player} firma un {feat}", "Un {feat} para {player}"],
               "whys": ["Entró en el {came_on} y {player} marcó: {goal_minutes}. Primer {feat} de un suplente en {history_seasons} temporadas de liga.{never}",
                        "Entró en el {came_on} y {player} marcó: {goal_minutes}. Solo el {history_rank_ord} {feat} de un suplente en {history_seasons} temporadas, el primero desde {last_player} en {last_season}.",
                        "Entró en el {came_on} y {player} marcó: {goal_minutes}. Un {feat} desde el banquillo: la liga ve unos {history_per_season} por temporada."],
               "heart": ["¡Otra vez {player}! Joss, ¿un suplente había hecho esto?", "¡Un {feat} de {player}! ¡Y entró en el {came_on}!"],
               "eye": ["Nunca, Wren. En {history_seasons} temporadas, ningún jugador salido del banquillo había marcado {goals}.{never}",
                       "El {history_rank_ord} en {history_seasons} temporadas, Wren. El último: {last_player}, en {last_season}.",
                       "La liga ve unos {history_per_season} por temporada desde el banquillo. Este se recordará."]},
    },
    "comeback": {
        "en": {"headlines": {"level": ["{team} draw level from {trailing_score} down", "{team} back on terms after {trailing_score}"],
                             "ahead": ["{team} turn {trailing_score} around", "{team} now lead after trailing {trailing_score}"],
                             "won": ["{team} win from {trailing_score} down", "{team} complete the comeback from {trailing_score}"], "any": ["{team} come back from {trailing_score}"]},
               "whys": ["{team} had not {done} from {deficit_goals} goals down since {club_last_season}, {club_seasons_since} seasons ago.",
                        "A first for {team} in {history_seasons} seasons: the club had never {done} from {deficit_goals} goals down.{club_first}"],
               "heart": ["{trailing_score} down, and look at {team} now!", "Who saw this coming at {trailing_score}?"],
               "eye": ["{club_seasons_since} seasons, Wren. That is how long {team} had waited.", "Never, Wren. Not once in {history_seasons} seasons had {team} done it.{club_first}"]},
        "fr": {"headlines": {"level": ["{team} recolle après avoir été mené {trailing_score}", "{team} revient de {trailing_score}"],
                             "ahead": ["Mené {trailing_score}, {team} passe devant", "{team} renverse un {trailing_score}"],
                             "won": ["{team} gagne après avoir été mené {trailing_score}", "Mené {trailing_score}, {team} renverse tout"], "any": ["{team} remonte de {trailing_score}"]},
               "whys": ["{team} n'avait plus {done} après avoir été mené de {deficit_goals} buts depuis {club_last_season}, il y a {club_seasons_since} saisons.",
                        "Une première pour {team} en {history_seasons} saisons : jamais le club n'avait {done} après avoir été mené de {deficit_goals} buts.{club_first}"],
               "heart": ["Mené {trailing_score}, et regarde {team} maintenant !", "Qui y croyait à {trailing_score} ?"],
               "eye": ["{club_seasons_since} saisons, Wren. Voilà combien de temps {team} attendait ça.", "Jamais, Wren. En {history_seasons} saisons, {team} ne l'avait jamais fait.{club_first}"]},
        "es": {"headlines": {"level": ["El {team} empata tras ir {trailing_score}", "El {team} iguala un {trailing_score}"],
                             "ahead": ["El {team} le da la vuelta al {trailing_score}", "El {team} ya gana tras ir {trailing_score}"],
                             "won": ["El {team} gana tras ir perdiendo {trailing_score}", "El {team} completa la remontada del {trailing_score}"], "any": ["El {team} remonta un {trailing_score}"]},
               "whys": ["El {team} no había {done} tras ir perdiendo por {deficit_goals} goles desde {club_last_season}, hace {club_seasons_since} temporadas.",
                        "Una primera vez para el {team} en {history_seasons} temporadas: nunca había {done} tras ir perdiendo por {deficit_goals} goles.{club_first}"],
               "heart": ["¡Perdían {trailing_score} y mira al {team} ahora!", "¿Quién lo veía venir con el {trailing_score}?"],
               "eye": ["{club_seasons_since} temporadas, Wren. Eso es lo que esperaba el {team}.", "Nunca, Wren. En {history_seasons} temporadas, el {team} no lo había hecho.{club_first}"]},
    },
    "ht-lead-lost": {
        "en": {"headlines": {"behind": ["{team} led {half_time_score} at the break, now trail", "{team} let a {half_time_score} half-time lead slip"],
                             "lost": ["{team} led {half_time_score} at the break, and lost", "{team} lose after leading {half_time_score} at half-time"],
                             "any": ["{team} let a {half_time_score} half-time lead slip"]},
               "whys": {"lost": ["{team}'s first {venue} defeat after leading at half-time since {club_last_season}, {club_seasons_since} seasons ago.",
                                 "A first in {history_seasons} seasons: {team} had never lost {venue_adv} after leading at half-time.{club_first}"],
                        "behind": ["If it ends like this, it will be {team}'s first {venue} defeat after leading at half-time since {club_last_season}.",
                                   "If it ends like this, a first in {history_seasons} seasons: {team} have never lost {venue_adv} after leading at half-time.{club_first}"]},
               "heart": ["{half_time_score} at the break, and look at {team} now.", "How has this got away from {team}?"],
               "eye": {"lost": ["{club_seasons_since} seasons since that last happened to {team}, Wren.", "Never in {history_seasons} seasons, Wren.{club_first}"],
                       "behind": ["Not over yet, but {team} have not lost one like this since {club_last_season}.", "Not over yet, but {team} have never lost one like this.{club_first}"]}},
        "fr": {"headlines": {"behind": ["{team} menait {half_time_score} à la pause, le voilà mené", "{team} laisse filer son avance de la pause"],
                             "lost": ["{team} menait {half_time_score} à la pause, et a perdu", "{team} perd après avoir mené {half_time_score}"],
                             "any": ["{team} laisse filer son avance de la pause"]},
               "whys": {"lost": ["Première défaite de {team} {venue} après avoir mené à la pause depuis {club_last_season}, il y a {club_seasons_since} saisons.",
                                 "Une première en {history_seasons} saisons : jamais {team} n'avait perdu {venue} après avoir mené à la pause.{club_first}"],
                        "behind": ["Si ça reste ainsi, ce sera la première défaite de {team} {venue} après avoir mené à la pause depuis {club_last_season}.",
                                   "Si ça reste ainsi, une première en {history_seasons} saisons : jamais {team} n'a perdu {venue} après avoir mené à la pause.{club_first}"]},
               "heart": ["{half_time_score} à la pause, et regarde où en est {team}.", "Comment {team} a pu laisser filer ça ?"],
               "eye": {"lost": ["{club_seasons_since} saisons que ça n'était plus arrivé à {team}, Wren.", "Jamais en {history_seasons} saisons, Wren.{club_first}"],
                       "behind": ["Ce n'est pas fini, mais {team} n'a plus perdu un match comme ça depuis {club_last_season}.", "Ce n'est pas fini, mais {team} n'a jamais perdu un match comme ça.{club_first}"]}},
        "es": {"headlines": {"behind": ["El {team} ganaba {half_time_score} al descanso y ahora pierde", "El {team} deja escapar su ventaja del descanso"],
                             "lost": ["El {team} ganaba {half_time_score} al descanso y perdió", "El {team} pierde tras ganar {half_time_score} al descanso"],
                             "any": ["El {team} deja escapar su ventaja del descanso"]},
               "whys": {"lost": ["Primera derrota del {team} {venue} tras ir ganando al descanso desde {club_last_season}, hace {club_seasons_since} temporadas.",
                                 "Una primera vez en {history_seasons} temporadas: el {team} nunca había perdido {venue} tras ir ganando al descanso.{club_first}"],
                        "behind": ["Si termina así, será la primera derrota del {team} {venue} tras ir ganando al descanso desde {club_last_season}.",
                                   "Si termina así, una primera vez en {history_seasons} temporadas: el {team} nunca ha perdido {venue} tras ir ganando al descanso.{club_first}"]},
               "heart": ["{half_time_score} al descanso, y mira al {team} ahora.", "¿Cómo se le ha escapado esto al {team}?"],
               "eye": {"lost": ["Hacía {club_seasons_since} temporadas que no le pasaba al {team}, Wren.", "Nunca en {history_seasons} temporadas, Wren.{club_first}"],
                       "behind": ["No ha terminado, pero el {team} no pierde uno así desde {club_last_season}.", "No ha terminado, pero el {team} nunca ha perdido uno así.{club_first}"]}},
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
    "xg-ft": {
        "en": {"headlines": ["{team}: {goals} goals from {xg} xG", "{team} beat their {xg} xG by a mile"],
               "whys": ["{team} scored {goals} from chances worth {xg} expected goals: about one match in {one_in} goes that way."],
               "heart": ["Everything {team} hit went in tonight!"], "eye": ["{goals} goals from {xg} xG. About one match in {one_in} turns out like that."]},
        "fr": {"headlines": ["{team} : {goals} buts pour {xg} xG", "{team} pulvérise ses {xg} xG"],
               "whys": ["{team} a marqué {goals} buts sur des occasions qui en valaient {xg} : à peu près un match sur {one_in} tourne ainsi."],
               "heart": ["Tout ce que {team} a tenté est rentré ce soir !"], "eye": ["{goals} buts pour {xg} xG. À peu près un match sur {one_in} finit comme ça."]},
        "es": {"headlines": ["El {team}: {goals} goles con {xg} xG", "El {team} pulveriza sus {xg} xG"],
               "whys": ["El {team} marcó {goals} con ocasiones que valían {xg} goles esperados: más o menos un partido de cada {one_in} acaba así."],
               "heart": ["¡Todo lo que tiró el {team} entró esta noche!"], "eye": ["{goals} goles con {xg} xG. Más o menos un partido de cada {one_in} termina así."]},
    },
}

BENCH_WORDS = ("{came_on}", "bench", "banc", "banquillo", "substitute", "remplaçant", "suplente", "cours de match")
MARKERS = ("{never}", "{club_first}")

STAT_PREFIX = {"en": "Stat of the match: ", "fr": "Stat du match : ", "es": "El dato del partido: "}
STAT_WHY = {"en": " It ranks first, {times_rarer} times rarer than the next fact.", "fr": " Le fait le plus rare du match, {times_rarer} fois plus rare que le suivant.",
            "es": " Es el dato más raro del partido, {times_rarer} veces más raro que el siguiente."}

T = {family: {lang: fr_typography(v) if lang == "fr" else v for lang, v in by_lang.items()} for family, by_lang in T.items()}
STAT_PREFIX["fr"], STAT_WHY["fr"] = fr_typography(STAT_PREFIX["fr"]), fr_typography(STAT_WHY["fr"])


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
    if isinstance(facts.get("times_rarer_than_runner_up"), (int, float)):
        vals["times_rarer"] = Dec(facts["times_rarer_than_runner_up"], lang)
        fields["times_rarer"] = "times_rarer_than_runner_up"
    return vals, fields


def usable(template: str, facts: dict) -> bool:
    """Whether a template is true for these facts; its markers are removed by `clean`."""
    if not facts.get("substitute") and any(w in template for w in BENCH_WORDS):
        return False
    if "{never}" in template and facts.get("history_count") != 0:
        return False
    if "{club_first}" in template and facts.get("club_first_time") is not True:
        return False
    return True


def clean(template: str) -> str:
    for m in MARKERS:
        template = template.replace(m, "")
    return template
