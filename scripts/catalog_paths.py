from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Iterator


CATALOG_ROOT = Path("info") / "paises"

CONTINENT_LABELS = {
    "africa": "África",
    "america": "América",
    "asia": "Ásia",
    "europa": "Europa",
    "oceania": "Oceânia",
}

# This mapping defines the default storage location. Callers can still pass an
# explicit continent for a new country that has not been registered here yet.
COUNTRY_CONTINENTS = {
    "africa-do-sul": "africa",
    "australia": "oceania",
    "bahamas": "america",
    "bielorrussia": "europa",
    "brasil": "america",
    "canada": "america",
    "cazaquistao": "asia",
    "china": "asia",
    "coreia-do-sul": "asia",
    "croacia": "europa",
    "croatia": "europa",
    "dinamarca": "europa",
    "egipto": "africa",
    "egito": "africa",
    "emirados-arabes-unidos": "asia",
    "eua": "america",
    "filipinas": "asia",
    "hong-kong": "asia",
    "hungria": "europa",
    "india": "asia",
    "indonesia": "asia",
    "japao": "asia",
    "macau": "asia",
    "malasia": "asia",
    "mauricia": "africa",
    "mauricias": "africa",
    "nova-zelandia": "oceania",
    "polonia": "europa",
    "reino-unido": "europa",
    "republica-checa": "europa",
    "romenia": "europa",
    "russia": "europa",
    "seychelles": "africa",
    "singapura": "asia",
    "sri-lanka": "asia",
    "sudao": "africa",
    "suecia": "europa",
    "suica": "europa",
    "tailandia": "asia",
    "taiwan": "asia",
    "tunisia": "africa",
    "turquia": "asia",
}

# Some existing catalogue folders use a historic or English spelling. Keep
# those stable while accepting the Portuguese country name shown in the JSON.
COUNTRY_DIRECTORY_ALIASES = {
    "croacia": "croatia",
    "egito": "egipto",
    "mauricias": "mauricia",
}


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")


def normalize_continent(continent: str) -> str:
    continent_slug = slugify(continent)
    if continent_slug not in CONTINENT_LABELS:
        choices = "|".join(CONTINENT_LABELS.values())
        raise ValueError(f"Continente inválido ou em falta. Indica {choices}.")
    return continent_slug


def country_slug(country: str) -> str:
    slug = slugify(country)
    return COUNTRY_DIRECTORY_ALIASES.get(slug, slug)


def continent_for_country(country: str) -> str:
    return COUNTRY_CONTINENTS.get(country_slug(country), "")


def continent_label_for_country(country: str) -> str:
    continent = continent_for_country(country)
    return CONTINENT_LABELS.get(continent, "")


def country_directory(
    country: str,
    continent: str = "",
    *,
    root: Path = CATALOG_ROOT,
) -> Path:
    canonical_country_slug = country_slug(country)
    continent_slug = normalize_continent(continent) if continent else continent_for_country(canonical_country_slug)
    if not continent_slug:
        raise ValueError(
            f"Continente desconhecido para {country}. Indica explicitamente "
            f"{'|'.join(CONTINENT_LABELS.values())}."
        )
    return root / continent_slug / canonical_country_slug


def find_country_directory(root: Path, country: str) -> Path:
    canonical_country_slug = country_slug(country)
    mapped_continent = continent_for_country(canonical_country_slug)
    if mapped_continent:
        mapped_path = root / mapped_continent / canonical_country_slug
        if mapped_path.exists():
            return mapped_path

    matches = sorted(path for path in root.glob(f"*/{canonical_country_slug}") if path.is_dir())
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"País duplicado em mais de um continente: {canonical_country_slug}")

    if mapped_continent:
        return root / mapped_continent / canonical_country_slug
    raise ValueError(f"País sem continente conhecido: {country}. Indica o continente primeiro.")


def iter_country_directories(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    for continent_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        yield from sorted(path for path in continent_dir.iterdir() if path.is_dir())
