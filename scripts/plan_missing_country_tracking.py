#!/usr/bin/env python3
"""Plan uCoin collection for Base44 countries without a local catalogue."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from scripts.catalog_paths import CATALOG_ROOT, country_directory, country_slug, iter_country_directories, slugify
from scripts.check_site_coin_differences import (
    api_records_for_all_countries,
    group_api_records_by_country,
    tracked_api_country_keys,
    untracked_api_countries,
)


UCOIN_COUNTRY_LINK_NAMES = {
    "brasil": "brazil",
    "coreia-do-sul": "south_korea",
    "emirados-arabes-unidos": "united_arab_emirates",
    "eua": "usa",
    "filipinas": "philippines",
    "hong-kong": "hong_kong",
    "japao": "japan",
    "malasia": "malaysia",
    "polonia": "poland",
    "singapura": "singapore",
    "tailandia": "thailand",
}


def record_year_bounds(record: dict[str, object]) -> tuple[int, int] | None:
    years = [int(value) for value in re.findall(r"(?<!\d)(\d{4})(?!\d)", str(record.get("years") or ""))]
    if not years:
        return None
    return min(years), max(years)


def year_coverage(records: list[dict[str, object]]) -> dict[str, object]:
    dated_records = [(record, bounds) for record in records if (bounds := record_year_bounds(record)) is not None]
    if not dated_records:
        return {
            "first_year": None,
            "last_year": None,
            "latest_coins": [],
            "records_without_years": len(records),
        }

    first_year = min(bounds[0] for _, bounds in dated_records)
    last_year = max(bounds[1] for _, bounds in dated_records)
    latest_coins = sorted(
        {
            f"{str(record.get('name') or 'Moeda').strip()} ({str(record.get('years') or '').strip()})"
            for record, bounds in dated_records
            if bounds[1] == last_year
        }
    )
    return {
        "first_year": first_year,
        "last_year": last_year,
        "latest_coins": latest_coins,
        "records_without_years": len(records) - len(dated_records),
    }


def most_common_continent(records: list[dict[str, object]]) -> str:
    continents = [str(record.get("continent") or "").strip() for record in records]
    counts = Counter(continent for continent in continents if continent)
    return counts.most_common(1)[0][0] if counts else ""


def build_tracking_plans(
    grouped_api_records: dict[str, list[dict[str, object]]],
    tracked_country_keys: set[str],
) -> list[dict[str, object]]:
    plans: list[dict[str, object]] = []
    for missing in untracked_api_countries(grouped_api_records, tracked_country_keys):
        country = str(missing["country"])
        country_key = slugify(country)
        records = grouped_api_records[country_key]
        local_slug = country_slug(country)
        continent = most_common_continent(records)
        coverage = year_coverage(records)
        plans.append(
            {
                "country": country,
                "country_slug": local_slug,
                "continent": continent,
                "api_coin_count": len(records),
                "first_year": coverage["first_year"],
                "last_year": coverage["last_year"],
                "latest_coins": coverage["latest_coins"],
                "records_without_years": coverage["records_without_years"],
                "ucoin_country_link_name": UCOIN_COUNTRY_LINK_NAMES.get(local_slug, local_slug.replace("-", "_")),
                "output_dir": str(country_directory(country, continent)) if continent else "",
            }
        )
    return plans


def select_tracking_plans(plans: list[dict[str, object]], selection: str) -> list[dict[str, object]]:
    normalized = selection.strip().lower()
    if normalized in {"", "todos", "todas", "all"}:
        return list(plans)

    selected_indexes: list[int] = []
    for raw_value in normalized.split(","):
        value = raw_value.strip()
        if not value.isdigit():
            raise ValueError("Escolhe 'todos' ou números separados por vírgulas, por exemplo: 1,3,5.")
        index = int(value)
        if index < 1 or index > len(plans):
            raise ValueError(f"Índice fora da lista: {index}.")
        if index not in selected_indexes:
            selected_indexes.append(index)
    return [plans[index - 1] for index in selected_indexes]


def plan_start_year(plan: dict[str, object]) -> int | None:
    value = plan.get("first_year")
    return int(value) if isinstance(value, int) else None


def build_collection_command(
    plan: dict[str, object],
    *,
    browser_args: list[str],
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "scripts.ucoin_pipeline",
        str(plan["country"]),
        "--continent",
        str(plan["continent"]),
        "--country-link-name",
        str(plan["ucoin_country_link_name"]),
        "--no-wait-for-final",
    ]
    start_year = plan_start_year(plan)
    if start_year is not None:
        command.extend(["--start-year", str(start_year)])
    command.extend(browser_args)
    return command


def print_tracking_plans(plans: list[dict[str, object]]) -> None:
    for index, plan in enumerate(plans, start=1):
        first_year = plan.get("first_year")
        last_year = plan.get("last_year")
        coverage = f"{first_year}–{last_year}" if first_year is not None and last_year is not None else "sem anos válidos"
        latest_coins = plan.get("latest_coins", [])
        latest = ", ".join(str(value) for value in latest_coins[:3]) if isinstance(latest_coins, list) else ""
        if isinstance(latest_coins, list) and len(latest_coins) > 3:
            latest += f" (+{len(latest_coins) - 3})"
        print(f"{index}) {plan['country']} ({plan['continent']})")
        print(f"   API: {plan['api_coin_count']} moedas | cobertura: {coverage}")
        if latest:
            print(f"   Última(s): {latest}")
        print(f"   Tracking completo sugerido: desde {first_year if first_year is not None else 'o início'}")
        print(f"   uCoin: country={plan['ucoin_country_link_name']}")
        print(f"   Destino: {plan['output_dir']}")


def load_tracking_plans(paises_dir: Path, catalog_filename: str) -> tuple[list[dict[str, object]] | None, str | None]:
    records, error = api_records_for_all_countries()
    if records is None:
        return None, error
    country_slugs = sorted(path.name for path in iter_country_directories(paises_dir))
    tracked = tracked_api_country_keys(paises_dir, country_slugs, catalog_filename)
    already_collected = tracked | tracked_api_country_keys(
        paises_dir,
        country_slugs,
        "app-catalog-pending.json",
    )
    return build_tracking_plans(group_api_records_by_country(records), already_collected), None


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Planeia a recolha de países da Base44 ainda sem catálogo local.")
    parser.add_argument("--paises-dir", default=str(project_root / CATALOG_ROOT))
    parser.add_argument("--catalog-file", default="app-catalog.json")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plans, error = load_tracking_plans(Path(args.paises_dir), args.catalog_file)
    if plans is None:
        print(f"Erro ao consultar Base44: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(plans, ensure_ascii=False, indent=2))
    elif plans:
        print_tracking_plans(plans)
    else:
        print("Todos os países da API já foram recolhidos ou têm tracking final.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
