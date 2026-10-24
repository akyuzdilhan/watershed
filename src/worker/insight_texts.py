"""Fallback texts and duo lines of the insights the engine produces, by family of fact, with the numbers of `headline_facts`.

The generic lines of `writer.INSIGHT_TEMPLATES` stay for the kinds not covered here. Each number is a placeholder named after its fact field, so `duo.fill` claims it against that field and the Verifier checks it. Rarity is told in seasons, a unit a viewer can picture ("never in 50 seasons", "about twice a season"), not in matches ("0 in 19,000").
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


class Dec(float):
    def __new__(cls, value: float, lang: str):
        obj = super().__new__(cls, value)
        obj.lang = lang
        return obj

    def __format__(self, spec: str) -> str:
        return f"{float(self):g}".replace(".", DEC[self.lang])


STAGE = {  # what one team reached after a comeback or a lost lead, as a verb in the singular
    "en": {"level": "draws level", "ahead": "goes ahead", "won": "wins", "behind": "falls behind", "lost": "loses", "drawn": "is pegged back"},
    "fr": {"level": "égalise", "ahead": "passe devant", "won": "gagne", "behind": "passe derrière", "lost": "perd", "drawn": "se fait rejoindre"},
    "es": {"level": "empata", "ahead": "se pone por delante", "won": "gana", "behind": "se ve por detrás", "lost": "pierde", "drawn": "se deja empatar"},
}

T = {  # family -> lang -> {headlines, whys, heart, eye}; a template whose placeholder is missing from the facts is skipped
    "scorer": {
        "en": {"headlines": ["{player}: {goals} goals off the bench", "On at {came_on}, {player} now has {goals}", "{player} reaches {goals}"],
               "whys": ["On at {came_on}, {player} has scored: {goal_minutes}. No substitute had done it in {history_seasons} seasons of this league.{never}",
                        "{player} has scored: {goal_minutes}. A substitute does it {history_per_season} times a season in this league.{seen}",
                        "{player} has scored: {goal_minutes}. A substitute had done it {history_count} times in {history_seasons} seasons.{seen}"],
               "heart": ["{player} again! Joss, has anyone ever done this?", "{goals} for {player}! And {player} only came on at {came_on}!"],
               "eye": ["Never, Wren. Not once in {history_seasons} seasons had a substitute scored {goals}.{never}",
                       "{history_count} times in {history_seasons} seasons. Rare company.{seen}", "It happens {history_per_season} times a season. This one will be remembered.{seen}"]},
        "fr": {"headlines": ["{player}, {goals} buts en sortie de banc", "Entré à la {came_on}, {player} en met {goals}", "{player} atteint les {goals} buts"],
               "whys": ["Entré à la {came_on}, {player} a marqué : {goal_minutes}. Aucun remplaçant ne l'avait fait en {history_seasons} saisons de championnat.{never}",
                        "{player} a marqué : {goal_minutes}. Un remplaçant le fait {history_per_season} fois par saison dans ce championnat.{seen}",
                        "{player} a marqué : {goal_minutes}. Un remplaçant l'avait fait {history_count} fois en {history_seasons} saisons.{seen}"],
               "heart": ["Encore {player} ! Joss, quelqu'un a déjà fait ça ?", "{goals} buts pour {player} ! Et il n'est entré qu'à la {came_on} !"],
               "eye": ["Jamais, Wren. En {history_seasons} saisons, aucun remplaçant n'avait marqué {goals} fois.{never}",
                       "{history_count} fois en {history_seasons} saisons. Un club très fermé.{seen}", "Ça arrive {history_per_season} fois par saison. Celui-là, on s'en souviendra.{seen}"]},
        "es": {"headlines": ["{player}, {goals} goles desde el banquillo", "Entró en el {came_on} y {player} ya lleva {goals}", "{player} llega a {goals}"],
               "whys": ["Entró en el {came_on} y marcó: {goal_minutes}. Ningún suplente lo había hecho en {history_seasons} temporadas de liga.{never}",
                        "{player} marcó: {goal_minutes}. Un suplente lo hace {history_per_season} veces por temporada en esta liga.{seen}",
                        "{player} marcó: {goal_minutes}. Un suplente lo había hecho {history_count} veces en {history_seasons} temporadas.{seen}"],
               "heart": ["¡Otra vez {player}! Joss, ¿alguien había hecho esto?", "¡{goals} goles de {player}! ¡Y entró en el {came_on}!"],
               "eye": ["Nunca, Wren. En {history_seasons} temporadas, ningún suplente había marcado {goals}.{never}",
                       "{history_count} veces en {history_seasons} temporadas. Un club muy pequeño.{seen}", "Pasa {history_per_season} veces por temporada. Este se recordará.{seen}"]},
    },
    "comeback": {
        "en": {"headlines": {"level": ["{team} draw level from {trailing_score} down", "{team} back on terms after {trailing_score}"],
                             "ahead": ["{team} turn {trailing_score} around", "{team} now lead after trailing {trailing_score}"],
                             "won": ["{team} win from {trailing_score} down", "{team} complete the comeback from {trailing_score}"], "any": ["{team} come back from {trailing_score}"]},
               "whys": ["A team {deficit_goals} goals down {stage} about once in {one_in}: {history_per_season} times a season in this league.{club}",
                        "A team {deficit_goals} goals down {stage} about once in {one_in}: {history_count} times in {history_seasons} seasons.{club}"],
               "heart": ["{trailing_score} down, and look at {team} now!", "Who saw this coming at {trailing_score}?"],
               "eye": ["Once in {one_in}, Wren. That is the size of it.{club}"]},
        "fr": {"headlines": {"level": ["{team} recolle après avoir été mené {trailing_score}", "{team} revient de {trailing_score}"],
                             "ahead": ["Mené {trailing_score}, {team} passe devant", "{team} renverse un {trailing_score}"],
                             "won": ["{team} gagne après avoir été mené {trailing_score}", "Mené {trailing_score}, {team} renverse tout"], "any": ["{team} remonte de {trailing_score}"]},
               "whys": ["Une équipe menée de {deficit_goals} buts {stage} environ une fois sur {one_in}, soit {history_per_season} fois par saison dans ce championnat.{club}",
                        "Une équipe menée de {deficit_goals} buts {stage} environ une fois sur {one_in} : {history_count} fois en {history_seasons} saisons.{club}"],
               "heart": ["Mené {trailing_score}, et regarde {team} maintenant !", "Qui y croyait à {trailing_score} ?"],
               "eye": ["Une fois sur {one_in}, Wren. Voilà la taille de l'exploit.{club}"]},
        "es": {"headlines": {"level": ["El {team} empata tras ir {trailing_score}", "El {team} iguala un {trailing_score}"],
                             "ahead": ["El {team} le da la vuelta al {trailing_score}", "El {team} ya gana tras ir {trailing_score}"],
                             "won": ["El {team} gana tras ir perdiendo {trailing_score}", "El {team} completa la remontada del {trailing_score}"], "any": ["El {team} remonta un {trailing_score}"]},
               "whys": ["Un equipo que pierde por {deficit_goals} goles {stage} más o menos una vez de cada {one_in}: {history_per_season} veces por temporada en esta liga.{club}",
                        "Un equipo que pierde por {deficit_goals} goles {stage} más o menos una vez de cada {one_in}: {history_count} veces en {history_seasons} temporadas.{club}"],
               "heart": ["¡Perdían {trailing_score} y mira al {team} ahora!", "¿Quién lo veía venir con el {trailing_score}?"],
               "eye": ["Una vez de cada {one_in}, Wren. Ese es el tamaño de la gesta.{club}"]},
    },
    "ht-lead-lost": {
        "en": {"headlines": {"behind": ["{team} led {half_time_score} at the break, now trail", "{team} let a {half_time_score} half-time lead slip"],
                             "lost": ["{team} led {half_time_score} at the break, and lost", "{team} lose after leading {half_time_score} at half-time"],
                             "any": ["{team} let a {half_time_score} half-time lead slip"]},
               "whys": ["A team leading by two or more at half-time {stage} about once in {one_in}: {history_per_season} times a season.{club_lead}",
                        "A team leading by two or more at half-time {stage} about once in {one_in}: {history_count} times in {history_seasons} seasons.{club_lead}"],
               "heart": ["{half_time_score} at the break, and look at {team} now.", "How has this got away from {team}?"],
               "eye": ["Once in {one_in}. {team} join a short list.{club_lead}"]},
        "fr": {"headlines": {"behind": ["{team} menait {half_time_score} à la pause, le voilà mené", "{team} laisse filer son avance de la pause"],
                             "lost": ["{team} menait {half_time_score} à la pause, et a perdu", "{team} perd après avoir mené {half_time_score}"],
                             "any": ["{team} laisse filer son avance de la pause"]},
               "whys": ["Une équipe qui mène de deux buts ou plus à la pause {stage} environ une fois sur {one_in}, soit {history_per_season} fois par saison.{club_lead}",
                        "Une équipe qui mène de deux buts ou plus à la pause {stage} environ une fois sur {one_in} : {history_count} fois en {history_seasons} saisons.{club_lead}"],
               "heart": ["{half_time_score} à la pause, et regarde où en est {team}.", "Comment {team} a pu laisser filer ça ?"],
               "eye": ["Une fois sur {one_in}. {team} entre dans une liste courte.{club_lead}"]},
        "es": {"headlines": {"behind": ["El {team} ganaba {half_time_score} al descanso y ahora pierde", "El {team} deja escapar su ventaja del descanso"],
                             "lost": ["El {team} ganaba {half_time_score} al descanso y perdió", "El {team} pierde tras ganar {half_time_score} al descanso"],
                             "any": ["El {team} deja escapar su ventaja del descanso"]},
               "whys": ["Un equipo que gana por dos o más al descanso {stage} más o menos una vez de cada {one_in}: {history_per_season} veces por temporada.{club_lead}",
                        "Un equipo que gana por dos o más al descanso {stage} más o menos una vez de cada {one_in}: {history_count} veces en {history_seasons} temporadas.{club_lead}"],
               "heart": ["{half_time_score} al descanso, y mira al {team} ahora.", "¿Cómo se le ha escapado esto al {team}?"],
               "eye": ["Una vez de cada {one_in}. El {team} entra en una lista corta.{club_lead}"]},
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

EXTRA = {  # optional sentences appended to a why or a line, filled from the facts when present
    "never": {"en": "", "fr": "", "es": ""},
    "seen": {"en": " Last time: {last_season}, {last_match}.", "fr": " La dernière fois : {last_season}, {last_match}.", "es": " La última vez: {last_season}, {last_match}."},
    "club": {"en": " {team} had last done it in {club_last_season}.", "fr": " La dernière fois pour {team} : {club_last_season}.", "es": " La última vez del {team}: {club_last_season}."},
    "club_lead": {"en": " {team} had last lost one like this in {club_last_season}.", "fr": " La dernière fois pour {team} : {club_last_season}.", "es": " La última vez del {team}: {club_last_season}."},
}

BENCH_WORDS = ("{came_on}", "bench", "banc", "banquillo", "substitute", "remplaçant", "suplente")

STAT_PREFIX = {"en": "Stat of the match: ", "fr": "Stat du match : ", "es": "El dato del partido: "}
STAT_WHY = {"en": " It ranks first, {times_rarer} times rarer than the next fact.", "fr": " Le fait le plus rare du match, {times_rarer} fois plus rare que le suivant.",
            "es": " Es el dato más raro del partido, {times_rarer} veces más raro que el siguiente."}

T = {family: {lang: fr_typography(v) if lang == "fr" else v for lang, v in by_lang.items()} for family, by_lang in T.items()}
EXTRA = {k: {lang: fr_typography(v) if lang == "fr" else v for lang, v in by_lang.items()} for k, by_lang in EXTRA.items()}
STAT_PREFIX["fr"], STAT_WHY["fr"] = fr_typography(STAT_PREFIX["fr"]), fr_typography(STAT_WHY["fr"])


def family(doc: dict) -> str | None:
    """The family of an insight, from its key (`scorer-home-20-4`, `comeback-home-2-3`); the stat of the match takes the family of its top fact."""
    key = str((doc.get("headline_facts") or {}).get("top_fact") or doc.get("key") or "")
    return next((f for f in T if key.startswith(f + "-")), None)


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
    if "stage" in facts:
        vals["stage"] = STAGE[lang].get(facts["stage"], facts["stage"])
        fields.pop("stage", None)
    if isinstance(facts.get("times_rarer_than_runner_up"), (int, float)):
        vals["times_rarer"] = Dec(facts["times_rarer_than_runner_up"], lang)
        fields["times_rarer"] = "times_rarer_than_runner_up"
    return vals, fields


def headlines(t: dict, facts: dict) -> list[str]:
    """The headline templates of a family, those of the stage the fact reached when they depend on it (a comeback that drew level is not yet a win)."""
    h = t["headlines"]
    return h if isinstance(h, list) else h.get(facts.get("stage"), h["any"])


def with_extras(template: str, vals: dict, lang: str, facts: dict) -> str:
    """Puts the optional sentences in place: the 'seen' one when the history has a last time, the club one when the facts have it."""
    out = template
    for name, by_lang in EXTRA.items():
        tag = "{" + name + "}"
        if tag not in out:
            continue
        text = by_lang[lang]
        needed = [n for n in ("last_season", "last_match", "club_last_season") if "{" + n + "}" in text]
        if any(n not in vals for n in needed):
            text = ""
        out = out.replace(tag, text)
    return out


def usable(template: str, facts: dict) -> bool:
    """A 'never before' variant needs a zero count, a 'seen' one a positive count, a count over seasons fewer occurrences than seasons (from one a season on, the rate per season says it better)."""
    count, seasons = facts.get("history_count"), facts.get("history_seasons")
    if not facts.get("substitute") and any(w in template for w in BENCH_WORDS):
        return False
    if "{never}" in template and count != 0:
        return False
    if "{seen}" in template and not (isinstance(count, int) and count > 0):
        return False
    if "{history_count}" in template and not (isinstance(count, int) and isinstance(seasons, int) and 0 < count < seasons):
        return False
    return True
