"""Fallback texts and duo lines of the insights the engine produces, by family of fact, with the numbers of `headline_facts`.

The generic lines of `writer.INSIGHT_TEMPLATES` stay for the kinds not covered here. Each number is a placeholder named after its fact field, so `duo.fill` claims it against that field and the Verifier checks it.
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


STAGE = {  # what a comeback or a lost lead reached, as a verb phrase
    "en": {"level": "drew level", "ahead": "went ahead", "won": "won", "behind": "fell behind", "lost": "lost", "drawn": "only drew"},
    "fr": {"level": "ont égalisé", "ahead": "sont passées devant", "won": "ont gagné", "behind": "sont passées derrière", "lost": "ont perdu", "drawn": "ont été rejointes"},
    "es": {"level": "empataron", "ahead": "se pusieron por delante", "won": "ganaron", "behind": "se vieron por detrás", "lost": "perdieron", "drawn": "acabaron empatando"},
}

T = {  # family -> lang -> {headlines, whys, heart, eye}; a template whose placeholder is missing from the facts is skipped
    "scorer": {
        "en": {"headlines": ["{player}: {goals} goals off the bench", "On at {came_on}, {player} now has {goals}", "{player} reaches {goals}"],
               "whys": ["On at {came_on}, {player} has scored: {goal_minutes}. No substitute had done it in {history_opportunities} league matches since {history_since}.{never}",
                        "{player} has scored: {goal_minutes}. A substitute had done it in {history_count} of {history_opportunities} league matches since {history_since}.{seen}"],
               "heart": ["{player} again! Joss, has anyone ever done this?", "{goals} for {player}! {player} only came on at {came_on}!"],
               "eye": ["Not once, Wren: {history_opportunities} matches since {history_since}, and no substitute had scored {goals}.{never}",
                       "{history_count} times in {history_opportunities} matches since {history_since}. Rare company.{seen}"]},
        "fr": {"headlines": ["{player}, {goals} buts en sortie de banc", "Entré à la {came_on}, {player} en met {goals}", "{player} atteint les {goals} buts"],
               "whys": ["Entré à la {came_on}, {player} a marqué : {goal_minutes}. Aucun remplaçant ne l'avait fait en {history_opportunities} matchs de championnat depuis {history_since}.{never}",
                        "{player} a marqué : {goal_minutes}. Un remplaçant l'avait fait dans {history_count} matchs sur {history_opportunities} depuis {history_since}.{seen}"],
               "heart": ["Encore {player} ! Joss, quelqu'un a déjà fait ça ?", "{goals} buts pour {player} ! Et il n'est entré qu'à la {came_on} !"],
               "eye": ["Jamais, Wren : {history_opportunities} matchs depuis {history_since}, et aucun remplaçant n'avait marqué {goals} fois.{never}",
                       "{history_count} fois en {history_opportunities} matchs depuis {history_since}. Il entre dans un club très fermé.{seen}"]},
        "es": {"headlines": ["{player}, {goals} goles desde el banquillo", "Entró en el {came_on} y {player} ya lleva {goals}", "{player} llega a {goals}"],
               "whys": ["Entró en el {came_on} y marcó: {goal_minutes}. Ningún suplente lo había hecho en {history_opportunities} partidos de liga desde {history_since}.{never}",
                        "{player} marcó: {goal_minutes}. Un suplente lo había hecho en {history_count} de {history_opportunities} partidos desde {history_since}.{seen}"],
               "heart": ["¡Otra vez {player}! Joss, ¿alguien había hecho esto?", "¡{goals} goles de {player}! ¡Y entró en el {came_on}!"],
               "eye": ["Nunca, Wren: {history_opportunities} partidos desde {history_since} y ningún suplente había marcado {goals}.{never}",
                       "{history_count} veces en {history_opportunities} partidos desde {history_since}. Un club muy pequeño.{seen}"]},
    },
    "comeback": {
        "en": {"headlines": ["{team} come back from {trailing_score}", "{team} turn {trailing_score} around", "From {trailing_score} down, {team} {stage}"],
               "whys": ["Of {history_opportunities} teams {deficit_goals} goals down since {history_since}, {history_count} {stage}.{club}"],
               "heart": ["{trailing_score} down, and look at {team} now!", "Who saw this coming at {trailing_score}?"],
               "eye": ["Only {history_count} of {history_opportunities} teams {deficit_goals} goals down {stage}. That is the size of it.{club}"]},
        "fr": {"headlines": ["{team} remonte de {trailing_score}", "{team} renverse un {trailing_score}", "Mené {trailing_score}, {team} renverse tout"],
               "whys": ["Sur {history_opportunities} équipes menées de {deficit_goals} buts depuis {history_since}, {history_count} {stage}.{club}"],
               "heart": ["Mené {trailing_score}, et regarde {team} maintenant !", "Qui y croyait à {trailing_score} ?"],
               "eye": ["Seulement {history_count} équipes sur {history_opportunities} menées de {deficit_goals} buts {stage}. Voilà la taille de l'exploit.{club}"]},
        "es": {"headlines": ["El {team} remonta un {trailing_score}", "El {team} le da la vuelta al {trailing_score}", "Del {trailing_score} a la remontada del {team}"],
               "whys": ["De {history_opportunities} equipos que perdían por {deficit_goals} goles desde {history_since}, {history_count} {stage}.{club}"],
               "heart": ["¡Perdían {trailing_score} y mira al {team} ahora!", "¿Quién lo veía venir con el {trailing_score}?"],
               "eye": ["Solo {history_count} de {history_opportunities} equipos con {deficit_goals} goles en contra {stage}. Ese es el tamaño de la gesta.{club}"]},
    },
    "ht-lead-lost": {
        "en": {"headlines": ["{team} let a {half_time_score} half-time lead slip", "{team} led {half_time_score} at the break, and lost it"],
               "whys": ["Of {history_opportunities} teams leading by two or more at half-time since {history_since}, {history_count} {stage}.{club_lead}"],
               "heart": ["{half_time_score} at the break, and {team} have nothing to show for it.", "How did {team} lose this from {half_time_score}?"],
               "eye": ["{history_count} of {history_opportunities} half-time leads like that one were lost. {team} join a short list.{club_lead}"]},
        "fr": {"headlines": ["{team} laisse filer son avance de la pause", "{team} menait {half_time_score} à la pause, et a tout perdu"],
               "whys": ["Sur {history_opportunities} équipes qui menaient de deux buts ou plus à la pause depuis {history_since}, {history_count} {stage}.{club_lead}"],
               "heart": ["{half_time_score} à la pause, et {team} repart les mains vides.", "Comment {team} a pu perdre ça en menant {half_time_score} ?"],
               "eye": ["{history_count} avances de ce genre sur {history_opportunities} ont été perdues. {team} entre dans une liste courte.{club_lead}"]},
        "es": {"headlines": ["El {team} deja escapar su ventaja del descanso", "El {team} ganaba {half_time_score} al descanso y lo perdió"],
               "whys": ["De {history_opportunities} equipos que ganaban por dos o más al descanso desde {history_since}, {history_count} {stage}.{club_lead}"],
               "heart": ["{half_time_score} al descanso, y el {team} se va con las manos vacías.", "¿Cómo perdió esto el {team} ganando {half_time_score}?"],
               "eye": ["{history_count} ventajas así de {history_opportunities} se perdieron. El {team} entra en una lista corta.{club_lead}"]},
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


def with_extras(template: str, vals: dict, lang: str, facts: dict) -> str:
    """Puts the optional sentences in place: the 'never' or 'seen' one depending on the history count, the club one when the facts have it."""
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
    """A 'never before' variant needs a zero count; a 'seen' variant needs a positive one."""
    count = facts.get("history_count")
    if not facts.get("substitute") and any(w in template for w in BENCH_WORDS):
        return False
    if "{never}" in template:
        return count == 0
    if "{seen}" in template or "{history_count}" in template:
        return isinstance(count, int) and count > 0
    return True
