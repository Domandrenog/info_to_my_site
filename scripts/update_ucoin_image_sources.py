#!/usr/bin/env python3
"""Review and replace non-uCoin image sources in All_Coins mappings."""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from scripts import check_site_coin_differences as checker


SOURCE_ISSUE_TYPE = "non_ucoin_external_image"
SIDE_LABELS = {"frente": "Frente", "tras": "Verso"}


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Replace confirmed Base44 image sources with their uCoin URLs in All_Coins."
    )
    parser.add_argument("--input", required=True, help="Difference report JSON.")
    parser.add_argument(
        "--all-coins-dir",
        default=str(project_root.parent / "All_Coins"),
        help="Path to the All_Coins repository.",
    )
    parser.add_argument("--apply", action="store_true", help="Ask for confirmation and write the replacements.")
    return parser.parse_args()


def image_source_replacements(payload: object) -> list[dict[str, str]]:
    reports = payload if isinstance(payload, list) else [payload]
    replacements: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()

    for report in reports:
        if not isinstance(report, dict) or report.get("error"):
            continue
        country = str(report.get("country") or "")
        country_name = str(report.get("country_name") or country)
        for coin in report.get("coins_with_issues", []):
            if not isinstance(coin, dict):
                continue
            for issue in coin.get("issues", []):
                if not isinstance(issue, dict) or issue.get("type") != SOURCE_ISSUE_TYPE:
                    continue
                field = str(issue.get("field") or "")
                side = field.removeprefix("links-externos.")
                current = checker.normalize_url(str(issue.get("value") or ""))[0]
                proposed = checker.normalize_url(str(issue.get("missing_value") or ""))[0]
                if side not in SIDE_LABELS or not current or not proposed:
                    continue
                if checker.is_ucoin_image_url(current) or not checker.is_ucoin_image_url(proposed):
                    continue
                key = (country, side, current, proposed)
                if key in seen:
                    continue
                seen.add(key)
                replacements.append(
                    {
                        "country": country,
                        "country_name": country_name,
                        "denomination": str(coin.get("denomination") or ""),
                        "issuePeriod": str(coin.get("issuePeriod") or ""),
                        "side": side,
                        "current": current,
                        "proposed": proposed,
                    }
                )

    return replacements


def load_image_source_replacements(path: Path) -> list[dict[str, str]]:
    return image_source_replacements(json.loads(path.read_text(encoding="utf-8")))


def replacement_counts(replacements: list[dict[str, str]]) -> tuple[int, int]:
    coins = {
        (
            item.get("country", ""),
            item.get("denomination", ""),
            item.get("issuePeriod", ""),
        )
        for item in replacements
    }
    return len(coins), len(replacements)


def print_replacements(replacements: list[dict[str, str]]) -> None:
    current_country = ""
    current_coin: tuple[str, str, str] | None = None
    for item in replacements:
        country = item.get("country", "")
        country_name = item.get("country_name", "") or country
        coin_key = (country, item.get("denomination", ""), item.get("issuePeriod", ""))
        if country != current_country:
            print("\n" + "#" * 72)
            print(f"REVER ORIGENS DAS FOTOGRAFIAS — {country_name.upper()}")
            print("#" * 72)
            current_country = country
            current_coin = None
        if coin_key != current_coin:
            years = item.get("issuePeriod", "")
            years_text = f" ({years})" if years else ""
            print(f"\n- {item.get('denomination', '')}{years_text}")
            current_coin = coin_key
        print(f"  - {SIDE_LABELS[item['side']]}: Base44 interno → uCoin")
        print(f"    Atual: {item.get('current', '')}")
        print(f"    Novo: {item.get('proposed', '')}")


def prepare_file_updates(
    replacements: list[dict[str, str]],
    all_coins_dir: Path,
) -> tuple[dict[Path, str], list[str]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in replacements:
        grouped[item.get("country", "")].append(item)

    prepared: dict[Path, str] = {}
    errors: list[str] = []
    link_pattern = re.compile(r"^(\s*)(frente|tras):(\s+)(https?://\S+)(\s*)(\r?\n)?$")

    for country, country_replacements in grouped.items():
        try:
            country_folder = checker.find_all_coins_country_folder(all_coins_dir, country)
        except (FileNotFoundError, ValueError) as exc:
            errors.append(str(exc))
            continue
        path = country_folder / "links-externos.txt"
        if not path.is_file():
            errors.append(f"Mapa externo não encontrado: {path}")
            continue

        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        for item in country_replacements:
            matches: list[int] = []
            for index, line in enumerate(lines):
                match = link_pattern.match(line)
                if not match or match.group(2) != item.get("side"):
                    continue
                current = checker.normalize_url(match.group(4))[0]
                if current == item.get("current"):
                    matches.append(index)

            label = f"{item.get('denomination', '')} ({item.get('issuePeriod', '')}) — {SIDE_LABELS[item['side']]}"
            if len(matches) != 1:
                errors.append(
                    f"{country}: {label}: esperado 1 link atual no mapa, encontrados {len(matches)}"
                )
                continue

            index = matches[0]
            match = link_pattern.match(lines[index])
            if match is None:
                errors.append(f"{country}: {label}: linha deixou de ser válida")
                continue
            newline = match.group(6) or ""
            lines[index] = (
                f"{match.group(1)}{match.group(2)}:{match.group(3)}"
                f"{item.get('proposed', '')}{match.group(5)}{newline}"
            )

        prepared[path] = "".join(lines)

    if errors:
        return {}, errors
    return prepared, []


def apply_file_updates(prepared: dict[Path, str]) -> None:
    for path, content in prepared.items():
        path.write_text(content, encoding="utf-8")


def main() -> int:
    args = parse_args()
    try:
        replacements = load_image_source_replacements(Path(args.input))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Não foi possível ler as substituições: {exc}")
        return 1

    if not replacements:
        print("Não existem origens de fotografias para substituir.")
        return 0

    coin_count, link_count = replacement_counts(replacements)
    print(f"Rever origens das fotografias — {coin_count} moedas / {link_count} links")
    print_replacements(replacements)

    prepared, errors = prepare_file_updates(replacements, Path(args.all_coins_dir))
    if errors:
        print("\nOperação bloqueada; nenhum ficheiro foi alterado:")
        for error in errors:
            print(f"- {error}")
        return 1

    if not args.apply:
        print("\nPré-visualização concluída; nenhuma alteração foi aplicada.")
        return 0

    try:
        choice = input(
            f"\nAplicar exatamente {link_count} substituições em {coin_count} moedas no All_Coins? [s/N]: "
        ).strip().lower()
    except EOFError:
        choice = ""
    if choice not in {"y", "yes", "s", "sim"}:
        print("Operação cancelada; nenhum ficheiro foi alterado.")
        return 0

    apply_file_updates(prepared)
    file_label = "ficheiro" if len(prepared) == 1 else "ficheiros"
    print(
        f"Concluído: {link_count} links substituídos em {coin_count} moedas "
        f"({len(prepared)} {file_label})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
