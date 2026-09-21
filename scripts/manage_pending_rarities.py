#!/usr/bin/env python3
"""Prepare and apply rarity classifications for every pending country catalogue."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from scripts.catalog_paths import CATALOG_ROOT, iter_country_directories, slugify
from ucoin_to_mysite.generate_resume_json import (
    APP_CATALOG_FILENAME,
    AVAILABILITY_STATISTICS_FILENAME,
    COINS_AVAILABILITY_EXCEL_FILENAME,
    COIN_AVAILABILITIES,
    FINAL_APP_CATALOG_INPUT_FILENAME,
    PENDING_APP_CATALOG_FILENAME,
    PENDING_AVAILABILITY,
    cleanup_intermediate_files,
    statistics_document,
    validate_final_availability_catalogue,
    validate_resume_catalogue,
    without_statistics,
    write_coins_excel,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PENDING_OUTPUT = CATALOG_ROOT / "all-rarities-pending.json"
DEFAULT_FINAL_INPUT = CATALOG_ROOT / "all-rarities-final.json"
DEFAULT_PROMPT_OUTPUT = CATALOG_ROOT / "all-rarities-prompt.txt"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def count_catalogue_coins(catalogue: dict[str, Any]) -> int:
    return sum(
        len(period.get("coins", []))
        for period in catalogue.get("periods", [])
        if isinstance(period, dict) and isinstance(period.get("coins"), list)
    )


def discover_pending_catalogues(paises_dir: Path, country: str = "") -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    requested_country = slugify(country)
    for country_dir in iter_country_directories(paises_dir):
        pending_path = country_dir / PENDING_APP_CATALOG_FILENAME
        if not pending_path.is_file() or (country_dir / APP_CATALOG_FILENAME).is_file():
            continue
        catalogue = load_json(pending_path)
        if not isinstance(catalogue, dict):
            raise ValueError(f"Catálogo inválido: {pending_path}")
        country_keys = {slugify(country_dir.name), slugify(str(catalogue.get("country") or ""))}
        if requested_country and requested_country not in country_keys:
            continue

        missing_count = sum(
            1
            for period in catalogue.get("periods", [])
            if isinstance(period, dict)
            for coin in period.get("coins", [])
            if isinstance(coin, dict) and coin.get("availability") == PENDING_AVAILABILITY
        )
        if not missing_count:
            continue
        pending.append(
            {
                "path": pending_path,
                "catalog_key": pending_path.relative_to(paises_dir).as_posix(),
                "catalogue": catalogue,
                "source_sha256": sha256_path(pending_path),
                "missing_count": missing_count,
            }
        )
    return pending


def build_rarity_batch(entries: list[dict[str, Any]]) -> dict[str, Any]:
    countries: list[dict[str, Any]] = []
    for entry in entries:
        catalogue = entry["catalogue"]
        coins: list[dict[str, Any]] = []
        for period_index, period in enumerate(catalogue.get("periods", [])):
            if not isinstance(period, dict):
                continue
            for coin_index, coin in enumerate(period.get("coins", [])):
                if not isinstance(coin, dict) or coin.get("availability") != PENDING_AVAILABILITY:
                    continue
                coins.append(
                    {
                        "coinId": f"p{period_index + 1}-c{coin_index + 1}",
                        "historicalPeriod": period.get("title"),
                        "ruler": period.get("ruler"),
                        "denomination": coin.get("denomination"),
                        "issuePeriod": coin.get("issuePeriod"),
                        "startYear": coin.get("startYear"),
                        "endYear": coin.get("endYear"),
                        "detailUrl": coin.get("detailUrl"),
                        "availability": PENDING_AVAILABILITY,
                    }
                )
        countries.append(
            {
                "country": catalogue.get("country"),
                "catalogKey": entry["catalog_key"],
                "sourceSha256": entry["source_sha256"],
                "coins": coins,
            }
        )
    return {
        "instructions": {
            "task": "Replace only each coin availability value.",
            "allowedAvailability": ["circulating", "scarce", "withdrawn", "historical"],
            "preserve": "Keep every country, coin, coinId, catalogKey, sourceSha256 and all other values unchanged.",
        },
        "countries": countries,
    }


def rarity_prompt(pending_output: Path, final_input: Path) -> str:
    return f"""Vou fornecer o ficheiro `{pending_output}` com moedas de vários países.

Substitui apenas cada valor `availability` que está como `{PENDING_AVAILABILITY}` por exatamente um destes valores:
- `circulating`: ainda circula normalmente ou é facilmente encontrada em uso comum;
- `scarce`: válida ou recente, mas comprovadamente pouco comum em circulação;
- `withdrawn`: pertence ao sistema monetário moderno, mas foi retirada ou desmonetizada;
- `historical`: pertence a um país, território ou sistema monetário histórico que já não existe.

Prioridade: historical > withdrawn > scarce > circulating.

Regras obrigatórias:
- não alteres, removas, acrescentes ou reordenes países ou moedas;
- preserva exatamente `coinId`, `catalogKey`, `sourceSha256` e todos os restantes valores;
- altera apenas `availability`;
- não deixes nenhum valor como `{PENDING_AVAILABILITY}`;
- devolve apenas JSON válido, sem markdown nem explicações.

Guarda a resposta completa em `{final_input}`.
"""


def _batch_classifications(batch: Any) -> dict[tuple[str, str], str]:
    if not isinstance(batch, dict) or not isinstance(batch.get("countries"), list):
        raise ValueError("O ficheiro final deve conter countries[].")
    classifications: dict[tuple[str, str], str] = {}
    for country in batch["countries"]:
        if not isinstance(country, dict):
            raise ValueError("Cada país do ficheiro final deve ser um objeto.")
        catalog_key = country.get("catalogKey")
        if not isinstance(catalog_key, str) or not isinstance(country.get("coins"), list):
            raise ValueError("Cada país deve preservar catalogKey e coins[].")
        for coin in country["coins"]:
            if not isinstance(coin, dict):
                raise ValueError(f"Moeda inválida em {catalog_key}.")
            coin_id = coin.get("coinId")
            availability = coin.get("availability")
            if not isinstance(coin_id, str):
                raise ValueError(f"coinId em falta em {catalog_key}.")
            if availability not in COIN_AVAILABILITIES:
                raise ValueError(f"Raridade inválida em {catalog_key}/{coin_id}: {availability}")
            key = (catalog_key, coin_id)
            if key in classifications:
                raise ValueError(f"Moeda repetida no ficheiro final: {catalog_key}/{coin_id}")
            classifications[key] = availability
    return classifications


def apply_rarity_batch(batch: Any, entries: list[dict[str, Any]]) -> list[tuple[Path, dict[str, Any]]]:
    classifications = _batch_classifications(batch)
    expected: set[tuple[str, str]] = set()
    expected_catalog_keys = {str(entry["catalog_key"]) for entry in entries}
    expected_hashes = {
        country.get("catalogKey"): country.get("sourceSha256")
        for country in batch.get("countries", [])
        if isinstance(country, dict)
    }
    actual_catalog_keys = set(expected_hashes)
    if actual_catalog_keys != expected_catalog_keys:
        missing = sorted(expected_catalog_keys - actual_catalog_keys)
        unexpected = sorted(actual_catalog_keys - expected_catalog_keys)
        details = []
        if missing:
            details.append(f"em falta: {', '.join(missing)}")
        if unexpected:
            details.append(f"inesperados: {', '.join(unexpected)}")
        raise ValueError(f"Países do lote não correspondem aos pendentes ({'; '.join(details)}).")
    results: list[tuple[Path, dict[str, Any]]] = []

    for entry in entries:
        catalog_key = entry["catalog_key"]
        if expected_hashes.get(catalog_key) != entry["source_sha256"]:
            raise ValueError(f"O catálogo mudou depois da criação do lote: {catalog_key}")
        final_catalogue = copy.deepcopy(entry["catalogue"])
        for period_index, period in enumerate(final_catalogue.get("periods", [])):
            if not isinstance(period, dict):
                continue
            for coin_index, coin in enumerate(period.get("coins", [])):
                if not isinstance(coin, dict) or coin.get("availability") != PENDING_AVAILABILITY:
                    continue
                coin_id = f"p{period_index + 1}-c{coin_index + 1}"
                key = (catalog_key, coin_id)
                expected.add(key)
                if key not in classifications:
                    raise ValueError(f"Falta classificar: {catalog_key}/{coin_id}")
                coin["availability"] = classifications[key]

        coin_count = count_catalogue_coins(final_catalogue)
        validate_resume_catalogue(final_catalogue, coin_count)
        validate_final_availability_catalogue(final_catalogue)
        results.append((entry["path"].parent, final_catalogue))

    unexpected = sorted(set(classifications) - expected)
    if unexpected:
        catalog_key, coin_id = unexpected[0]
        raise ValueError(f"Classificação inesperada: {catalog_key}/{coin_id}")
    return results


def write_country_outputs(
    results: list[tuple[Path, dict[str, Any]]],
    *,
    cleanup_intermediate: bool = False,
) -> None:
    for country_dir, final_catalogue in results:
        final_input_path = country_dir / FINAL_APP_CATALOG_INPUT_FILENAME
        app_catalogue_path = country_dir / APP_CATALOG_FILENAME
        statistics_path = country_dir / AVAILABILITY_STATISTICS_FILENAME
        excel_path = country_dir / COINS_AVAILABILITY_EXCEL_FILENAME
        app_catalogue = without_statistics(final_catalogue)

        write_json_atomic(final_input_path, final_catalogue)
        write_json_atomic(app_catalogue_path, app_catalogue)
        write_json_atomic(statistics_path, statistics_document(app_catalogue))
        write_coins_excel(str(excel_path), app_catalogue)
        print(f"Concluído: {final_catalogue.get('country')} -> {app_catalogue_path}")
    if cleanup_intermediate:
        for country_dir, _ in results:
            cleanup_intermediate_files(str(country_dir / FINAL_APP_CATALOG_INPUT_FILENAME))


def write_final_catalogues(results: list[tuple[Path, dict[str, Any]]]) -> None:
    for country_dir, final_catalogue in results:
        final_input_path = country_dir / FINAL_APP_CATALOG_INPUT_FILENAME
        write_json_atomic(final_input_path, final_catalogue)
        print(f"Raridades definidas: {final_catalogue.get('country')} -> {final_input_path}")


def cleanup_batch_files(*paths: Path) -> None:
    for path in paths:
        if path.exists():
            path.unlink()
            print(f"Deleted {path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepara e aplica raridades para todos os catálogos pendentes.")
    parser.add_argument("--paises-dir", default=str(PROJECT_ROOT / CATALOG_ROOT))
    parser.add_argument("--country", default="", help="Processa apenas o slug deste país.")
    parser.add_argument("--output", default=str(PROJECT_ROOT / DEFAULT_PENDING_OUTPUT))
    parser.add_argument("--prompt-output", default=str(PROJECT_ROOT / DEFAULT_PROMPT_OUTPUT))
    parser.add_argument("--final-input", default="")
    parser.add_argument("--wait-for-final", action="store_true")
    parser.add_argument(
        "--rarities-only",
        action="store_true",
        help="Guarda app-catalog-final.json sem gerar os outputs finais nem limpar os ficheiros do país.",
    )
    parser.add_argument(
        "--cleanup-intermediate",
        action="store_true",
        help="Depois de gerar todos os outputs finais, apaga os catálogos e ficheiros de lote intermédios.",
    )
    return parser.parse_args()


def run(args: argparse.Namespace) -> int:
    paises_dir = Path(args.paises_dir)
    entries = discover_pending_catalogues(paises_dir, args.country)
    if not entries:
        print("Não existem catálogos pendentes sem raridade.")
        return 0

    output_path = Path(args.output)
    prompt_path = Path(args.prompt_output)
    final_input_path = Path(args.final_input) if args.final_input else PROJECT_ROOT / DEFAULT_FINAL_INPUT
    batch = build_rarity_batch(entries)
    total_coins = sum(int(entry["missing_count"]) for entry in entries)

    if args.final_input:
        final_batch = load_json(final_input_path)
        results = apply_rarity_batch(final_batch, entries)
        if args.rarities_only:
            write_final_catalogues(results)
            cleanup_batch_files(output_path, prompt_path, final_input_path)
        else:
            write_country_outputs(results, cleanup_intermediate=args.cleanup_intermediate)
        if args.cleanup_intermediate and not args.rarities_only:
            cleanup_batch_files(output_path, prompt_path, final_input_path)
        print(f"Raridades aplicadas: {total_coins} tipos em {len(results)} países.")
        return 0

    write_json_atomic(output_path, batch)
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(rarity_prompt(output_path, final_input_path), encoding="utf-8")
    print(f"Gerado: {output_path}")
    print(f"Prompt: {prompt_path}")
    print(f"Pendentes: {total_coins} tipos em {len(entries)} países.")

    if not args.wait_for_final:
        print(f"Depois guarda a resposta em {final_input_path} e executa:")
        print(f"python3 -m scripts.manage_pending_rarities --final-input {final_input_path}")
        return 0

    if not final_input_path.exists():
        final_input_path.touch()
    print(f"Preenche {final_input_path} usando o prompt e o JSON gerados.")
    input("Carrega Enter quando o ficheiro final estiver pronto...")
    if not final_input_path.read_text(encoding="utf-8").strip():
        raise ValueError(f"O ficheiro final continua vazio: {final_input_path}")
    final_batch = load_json(final_input_path)
    results = apply_rarity_batch(final_batch, entries)
    if args.rarities_only:
        write_final_catalogues(results)
        cleanup_batch_files(output_path, prompt_path, final_input_path)
    else:
        write_country_outputs(results, cleanup_intermediate=args.cleanup_intermediate)
    if args.cleanup_intermediate and not args.rarities_only:
        cleanup_batch_files(output_path, prompt_path, final_input_path)
    print(f"Raridades aplicadas: {total_coins} tipos em {len(results)} países.")
    return 0


def main() -> int:
    args = parse_args()
    try:
        return run(args)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Erro: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
