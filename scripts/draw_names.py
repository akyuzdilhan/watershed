"""Draw the fictional name pools once with Faker and freeze them in src/generator/names.py.

The generator does not depend on Faker: this script ran once, its output is committed, and it stays here so the draw can be traced and repeated. Same Faker version, same seed, same file.

    pip install faker==40.43.0
    python scripts/draw_names.py
"""

import importlib
import random
import unicodedata
from pathlib import Path

import faker

FAKER_VERSION = "40.43.0"
SEED = 2026
DRAWN_ON = "2026-10-25"
OUT = Path(__file__).resolve().parents[1] / "src" / "generator" / "names.py"

# Locale: (male first names, surnames) drawn from it. English and Irish names weigh more, as in an English league.
LOCALES: dict[str, tuple[int, int]] = {
    "en_GB": (110, 320), "en_IE": (70, 200),
    "fr_FR": (45, 110), "es_ES": (45, 110), "it_IT": (45, 110), "de_DE": (45, 110), "nl_NL": (40, 100), "pt_PT": (40, 80),
    "da_DK": (35, 70), "sv_SE": (35, 90), "no_NO": (35, 80), "pl_PL": (35, 100),
}

# Both names of the two fictional commentators of the demo, Wren Calder and Joss Ferrier: no player or referee may share them.
COMMENTATORS = {"Wren", "Calder", "Joss", "Ferrier"}
# Surnames of famous footballers, managers and broadcasters.
FOOTBALL = {
    "Alonso", "Alves", "Amorim", "Ancelotti", "Arteta", "Ballack", "Banks", "Baresi", "Barton", "Beckenbauer", "Beckham", "Bergkamp", "Best", "Bowen",
    "Brady", "Bravo", "Bruce", "Buffon", "Busquets", "Cantona", "Carragher", "Casillas", "Charlton", "Clough", "Conte", "Costa", "Courtois", "Cruyff",
    "Cuadrado", "Cunha", "Dalglish", "Davids", "Delap", "Dempsey", "Diallo", "Drury", "Eriksen", "Ferdinand", "Ferguson", "Fernandes", "Figo",
    "Gallagher", "Gerrard", "Giggs", "Gray", "Guardiola", "Gullit", "Haaland", "Hamann", "Hansen", "Henderson", "Henry", "Hierro", "Hoarau", "Hoddle",
    "Hurst", "Infantino", "Iniesta", "Jesus", "Kane", "Keane", "Keegan", "Keys", "Klinsmann", "Klopp", "Koeman", "Kroos", "Lahm", "Lampard",
    "Larsson", "Laudrup", "Law", "Lehmann", "Lewandowski", "Lineker", "Lucarelli", "Maldini", "Matthäus", "Merckx", "Messi", "Milla", "Modrić",
    "Moore", "Morata", "Motson", "Moura", "Mourinho", "Müller", "Navas", "Neuer", "Neville", "Owen", "Paisley", "Palmer", "Pirlo", "Platini", "Puyol",
    "Ramos", "Ranieri", "Raya", "Redknapp", "Redondo", "Rice", "Rijkaard", "Robben", "Robson", "Romagnoli", "Ronaldo", "Rooney", "Rossi", "Rush",
    "Salah", "Sarabia", "Schmeichel", "Scholes", "Seedorf", "Shankly", "Shearer", "Shilton", "Silva", "Simeone", "Souness", "Southgate", "Sterling",
    "Tassoni", "Terry", "Torres", "Totti", "Trubin", "Tyler", "Ugarte", "Valbuena", "Vardy", "Venables", "Vialli", "Walker", "Watkins", "Wenger",
    "Wright", "Xavi", "Zidane", "Zola", "Ødegaard", "Ødegård",
}
# Other famous people found in Faker's lists (its Italian list has many).
FAMOUS = {
    "Antonioni", "Badoglio", "Baglioni", "Bernini", "Boccioni", "Bocelli", "Borghese", "Borgia", "Buscetta", "Busoni", "Cabrini", "Cadorna",
    "Caetani", "Caine", "Canova", "Caracciolo", "Cardano", "Carosone", "Carpaccio", "Casellati", "Cavalcanti", "Chigi", "Clinton", "Connery",
    "Crispi", "Fermi", "Fogazzaro", "Franscini", "Frescobaldi", "Gabbana", "Gadda", "Gelli", "Gentileschi", "Guinizzelli", "Haughey", "Heaney",
    "Heydrich", "Juvara", "Kohl", "Leoncavallo", "Lombroso", "Loredan", "Malaparte", "Marconi", "Marinetti", "Mastroianni", "Matteotti", "Montessori",
    "Morpurgo", "Mozart", "Odescalchi", "Paruta", "Pasolini", "Pausini", "Pavarotti", "Pergolesi", "Prodi", "Quasimodo", "Ramazzotti", "Romiti",
    "Rosmini", "Rossellini", "Rossini", "Rubbia", "Segni", "Sgarbi", "Smirnoff", "Soffici", "Speer", "Tartini", "Tebaldi", "Vasari", "Vattimo",
    "Veltroni", "Vespucci", "Virgilio", "Volta", "Zeffirelli",
}
# Places with a football club, and entries that are not a man's surname.
OTHERS = {
    "Birmingham", "Blackburn", "Bolton", "Breugelensis", "Burton", "Chrodtrud", "Claesdr", "Ditschlerin", "Frances", "Hastings", "Jerusalem", "Marie",
    "Mary", "Preston", "Spain",
}
EXCLUDED_SURNAMES = COMMENTATORS | FOOTBALL | FAMOUS | OTHERS
# Footballers known by one name, famous first names, and entries that are not usable as a first name.
EXCLUDED_FIRST_NAMES = COMMENTATORS | {"Casemiro", "Kaká", "Pelé", "Rivaldo", "Romário", "Zico", "Xavi", "Michelangelo", "Ranieri", "Kévim", "Ørni", "Øvli"}


def keep(name: str) -> bool:
    """Letters only (accents allowed), no space, hyphen or apostrophe, 3 to 12 letters."""
    return name.isalpha() and 3 <= len(name) <= 12


def sort_key(name: str) -> tuple[str, str]:
    plain = "".join(c for c in unicodedata.normalize("NFKD", name.casefold()) if not unicodedata.combining(c))
    return plain, name


def pool(values) -> list[str]:
    return sorted({v for v in values if keep(v)}, key=sort_key)


def draw() -> tuple[list[str], list[str]]:
    assert faker.VERSION == FAKER_VERSION, f"Faker {FAKER_VERSION} expected, {faker.VERSION} found"
    firsts: set[str] = set()
    lasts: set[str] = set()
    for i, (locale, (n_first, n_last)) in enumerate(LOCALES.items()):
        provider = importlib.import_module(f"faker.providers.person.{locale}").Provider
        surnames = list(provider.last_names) if locale != "pl_PL" else list(provider.male_last_names) + list(provider.unisex_last_names)
        first_pool, last_pool = pool(provider.first_names_male), pool(surnames)
        r = random.Random(SEED * 100 + i)
        firsts.update(r.sample(first_pool, min(n_first, len(first_pool))))
        lasts.update(r.sample(last_pool, min(n_last, len(last_pool))))
    # Exclusions are applied after the draw, so adding one removes that name and leaves the rest of the draw unchanged. A surname that is also one of
    # the first names goes too: no "Thomas Thomas", and each name reads one way.
    firsts -= EXCLUDED_FIRST_NAMES
    lasts -= EXCLUDED_SURNAMES | firsts
    return sorted(firsts, key=sort_key), sorted(lasts, key=sort_key)


def as_tuple(name: str, values: list[str], width: int = 150) -> str:
    lines, line = [], "   "
    for v in values:
        item = f' "{v}",'
        if len(line) + len(item) > width:
            lines.append(line)
            line = "   "
        line += item
    lines.append(line)
    return f"{name}: tuple[str, ...] = (\n" + "\n".join(lines) + "\n)\n"


def main() -> None:
    firsts, lasts = draw()
    header = (
        '"""Fictional name pools of the generator and the league history.\n\n'
        f"Drawn once on {DRAWN_ON} with Faker {FAKER_VERSION} (MIT, https://github.com/joke2k/faker) by scripts/draw_names.py, seed {SEED}: male first names and surnames"
        f" of the locales {', '.join(LOCALES)}, letters only (accents allowed), 3 to 12 letters, without the surnames of famous footballers, managers and"
        " broadcasters, other famous people found in Faker's lists, place names with football clubs, the names of the two fictional commentators"
        " and the surnames that are also first names of the pool, deduplicated and sorted.\n\n"
        "Frozen here so the generator has no dependency on Faker and stays reproducible: a match seed always gives the same names, whatever Faker version is"
        " installed, if any.\n"
        '"""\n\n'
    )
    OUT.write_text(header + as_tuple("FIRST_NAMES", firsts) + "\n" + as_tuple("SURNAMES", lasts), encoding="utf-8", newline="\n")
    print(f"{OUT}: {len(firsts)} first names, {len(lasts)} surnames")


if __name__ == "__main__":
    main()
