#!/usr/bin/env python3
"""Preview and create missing Base44 Souvenir records from a pending catalogue."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from scripts.import_base44_coins import (
    DEFAULT_BASE44_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RATE_LIMIT_DELAY_SECONDS,
    DEFAULT_REQUEST_DELAY_SECONDS,
    Base44Client,
    format_duration,
    load_dotenv,
)


REQUIRED_FIELDS = {"name", "continent", "country", "city", "type", "condition"}
VALID_CONTINENTS = {"Europa", "América", "Ásia", "África", "Oceânia"}
VALID_TYPES = {"pressed", "coin", "card", "other"}
VALID_CONDITIONS = {"Tenho", "Não Tenho"}
VALID_SHAPES = {"", "oval", "circle", "card_wide", "square"}
SOUVENIR_FIELDS = {
    "name",
    "continent",
    "country",
    "city",
    "type",
    "condition",
    "location_name",
    "description",
    "display_shape",
    "image_front",
    "image_back",
    "acquisition_date",
    "notes",
    "reference_url",
    "ordem",
    "hidden",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verifica e cria apenas souvenirs ainda em falta no Site Base44."
    )
    parser.add_argument("--input", type=Path, required=True, help="presscoins-catalog.json revisto.")
    parser.add_argument("--apply", action="store_true", help="Permitir criação após confirmação explícita.")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--limit", type=int, default=0, help="Considerar apenas os primeiros N souvenirs.")
    parser.add_argument(
        "--catalog-number",
        action="append",
        default=[],
        help="Importar apenas este número de catálogo Presscoins; pode repetir a opção.",
    )
    parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    parser.add_argument("--rate-limit-delay", type=float, default=DEFAULT_RATE_LIMIT_DELAY_SECONDS)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    return parser.parse_args(argv)


def read_catalog(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("O catálogo deve conter um objeto JSON.")
    if payload.get("import_ready") is False:
        raise ValueError(
            "Este catálogo está marcado como não aprovado para importação. "
            "Revê e aprova primeiro os registos pendentes."
        )
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("O catálogo não contém uma lista items.")
    return payload


def is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def normalized_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_value.casefold().split())


def validate_record(record: dict[str, Any], index: int) -> dict[str, Any]:
    missing = sorted(field for field in REQUIRED_FIELDS if not record.get(field))
    if missing:
        raise ValueError(f"Souvenir {index}: campos obrigatórios em falta: {', '.join(missing)}")
    if record["continent"] not in VALID_CONTINENTS:
        raise ValueError(f"Souvenir {index}: continente inválido: {record['continent']}")
    if record["type"] not in VALID_TYPES:
        raise ValueError(f"Souvenir {index}: tipo inválido: {record['type']}")
    if record["condition"] not in VALID_CONDITIONS:
        raise ValueError(f"Souvenir {index}: condição inválida: {record['condition']}")
    if str(record.get("display_shape") or "") not in VALID_SHAPES:
        raise ValueError(f"Souvenir {index}: display_shape inválido: {record.get('display_shape')}")
    for field in ("image_front", "image_back", "reference_url"):
        value = record.get(field)
        if value and (not isinstance(value, str) or not is_http_url(value)):
            raise ValueError(f"Souvenir {index}: URL inválido em {field}: {value}")
    if "ordem" in record and not isinstance(record["ordem"], int):
        raise ValueError(f"Souvenir {index}: ordem deve ser um número inteiro.")
    if "hidden" in record and not isinstance(record["hidden"], bool):
        raise ValueError(f"Souvenir {index}: hidden deve ser booleano.")
    return {field: record[field] for field in SOUVENIR_FIELDS if field in record}


def catalogue_records(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, item in enumerate(catalog["items"], start=1):
        if not isinstance(item, dict) or not isinstance(item.get("souvenir"), dict):
            raise ValueError(f"Item {index}: objeto souvenir em falta.")
        review_status = str(item.get("review_status") or "").casefold()
        if review_status in {"skipped", "rejected"}:
            continue
        if review_status and review_status != "approved":
            raise ValueError(
                f"Item {index}: decisão de revisão ainda pendente: {review_status}."
            )
        records.append(validate_record(item["souvenir"], index))
    duplicate_keys: set[tuple[str, ...]] = set()
    seen: set[tuple[str, ...]] = set()
    for record in records:
        key = preferred_identity(record)
        if key in seen:
            duplicate_keys.add(key)
        seen.add(key)
    if duplicate_keys:
        raise ValueError(f"O catálogo contém {len(duplicate_keys)} identidades duplicadas.")
    return records


def filter_records_by_catalog_numbers(
    records: list[dict[str, Any]], requested_values: list[str]
) -> list[dict[str, Any]]:
    requested: list[str] = []
    seen: set[str] = set()
    for value in requested_values:
        catalog_number = str(value or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]+", catalog_number):
            raise ValueError(f"Número de catálogo inválido: {value!r}")
        if catalog_number not in seen:
            seen.add(catalog_number)
            requested.append(catalog_number)
    if not requested:
        return records
    records_by_catalog_number = {
        presscoins_catalog_number(record): record for record in records
    }
    missing = [code for code in requested if code not in records_by_catalog_number]
    if missing:
        raise ValueError(
            "Números de catálogo não encontrados no ficheiro: " + ", ".join(missing)
        )
    return [records_by_catalog_number[code] for code in requested]


def presscoins_catalog_number(record: dict[str, Any]) -> str:
    reference = str(record.get("reference_url") or "")
    if reference:
        values = parse_qs(urlparse(reference).query).get("search", [])
        if values and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]+", values[0]):
            return values[0].upper()
    notes = str(record.get("notes") or "")
    match = re.search(r"Catálogo Presscoins:\s*([A-Za-z0-9-]+)", notes, flags=re.IGNORECASE)
    return match.group(1).upper() if match else ""


def pennycollector_identity(record: dict[str, Any]) -> tuple[str, ...] | None:
    reference = str(record.get("reference_url") or "")
    parsed = urlparse(reference)
    if not parsed.netloc.casefold().endswith("pennycollector.com"):
        return None
    locations = parse_qs(parsed.query).get("location", [])
    location_id = locations[0].strip() if locations else ""
    machine = ""
    position = ""
    fragment_match = re.fullmatch(
        r"machine-(\d+)-position-(\d+)", parsed.fragment, flags=re.IGNORECASE
    )
    if fragment_match:
        machine, position = fragment_match.groups()
    else:
        notes_match = re.search(
            r"Machine\s+(\d+).*?Posição\s+(\d+)",
            str(record.get("notes") or ""),
            flags=re.IGNORECASE,
        )
        if notes_match:
            machine, position = notes_match.groups()
    if location_id and machine and position:
        return ("pennycollector", location_id, machine, position)
    return None


def fallback_identity(record: dict[str, Any]) -> tuple[str, ...]:
    return (
        "fields",
        normalized_text(record.get("country")),
        normalized_text(record.get("city")),
        normalized_text(record.get("location_name")),
        normalized_text(record.get("name")),
    )


def preferred_identity(record: dict[str, Any]) -> tuple[str, ...]:
    pennycollector = pennycollector_identity(record)
    if pennycollector is not None:
        return pennycollector
    reference_url = str(record.get("reference_url") or "").strip()
    if urlparse(reference_url).netloc.casefold().endswith("pennycollector.com"):
        return fallback_identity(record)
    catalog_number = presscoins_catalog_number(record)
    if catalog_number:
        return ("presscoins", catalog_number.casefold())
    reference_url = str(record.get("reference_url") or "").strip()
    if reference_url:
        return ("reference", reference_url)
    return fallback_identity(record)


def strong_identity(record: dict[str, Any]) -> tuple[str, ...] | None:
    identity = preferred_identity(record)
    return identity if identity[0] != "fields" else None


def create_client(args: argparse.Namespace) -> Base44Client:
    load_dotenv()
    app_id = os.environ.get("BASE44_APP_ID", "")
    api_key = os.environ.get("BASE44_API_KEY", "")
    if not app_id or not api_key:
        raise ValueError("Falta BASE44_APP_ID ou BASE44_API_KEY no environment/.env.")
    return Base44Client(
        app_id=app_id,
        api_key=api_key,
        server_url=os.environ.get("BASE44_SERVER_URL", DEFAULT_BASE44_URL),
        request_delay_seconds=args.request_delay,
        rate_limit_delay_seconds=args.rate_limit_delay,
        max_retries=args.max_retries,
        entity_name="Souvenir",
    )


def existing_records_for_country(client: Base44Client, country: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    page_size = 1000
    skip = 0
    while True:
        result = client.filter({"country": country}, limit=page_size, skip=skip)
        if not isinstance(result, list):
            raise RuntimeError(f"Resposta inesperada do Site Base44: {result!r}")
        records.extend(record for record in result if isinstance(record, dict))
        if len(result) < page_size:
            return records
        skip += page_size


def partition_missing(
    records: list[dict[str, Any]], existing: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    existing_strong = {
        identity
        for record in existing
        if (identity := strong_identity(record)) is not None
    }
    existing_all_fallback = {fallback_identity(record) for record in existing}
    existing_legacy_fallback = {
        fallback_identity(record)
        for record in existing
        if strong_identity(record) is None
    }
    missing: list[dict[str, Any]] = []
    already_present: list[dict[str, Any]] = []
    for record in records:
        identity = strong_identity(record)
        fallback = fallback_identity(record)
        is_present = (
            identity in existing_strong or fallback in existing_legacy_fallback
            if identity is not None
            else fallback in existing_all_fallback
        )
        if is_present:
            already_present.append(record)
        else:
            missing.append(record)
    return missing, already_present


def load_existing(client: Base44Client, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing: list[dict[str, Any]] = []
    for country in sorted({str(record["country"]) for record in records}):
        existing.extend(existing_records_for_country(client, country))
    return existing


def print_plan(
    records: list[dict[str, Any]],
    missing: list[dict[str, Any]],
    already_present: list[dict[str, Any]],
) -> None:
    with_photos = sum(bool(record.get("image_front")) for record in missing)
    print("\nPlano de importação de souvenirs:")
    print(f"- Catálogo revisto: {len(records)} souvenirs")
    print(f"- Já existentes no Site Base44: {len(already_present)}")
    print(f"- Novos a criar: {len(missing)}")
    print(f"- Novos com fotografia: {with_photos}")
    print(f"- Novos sem fotografia: {len(missing) - with_photos}")
    if missing:
        print("\nPrimeiros registos a criar:")
        for record in missing[:10]:
            catalog_number = presscoins_catalog_number(record)
            suffix = f" [{catalog_number}]" if catalog_number else ""
            print(f"- {record['name']} — {record.get('location_name', '')}{suffix}")
        if len(missing) > 10:
            print(f"- ... e mais {len(missing) - 10}")


def ask_confirmation(count: int) -> bool:
    answer = input(f"\nCriar exatamente {count} souvenirs no Site Base44? [s/N]: ").strip().casefold()
    return answer in {"s", "sim", "y", "yes"}


def chunks(records: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [records[index : index + size] for index in range(0, len(records), size)]


def create_missing_records(
    client: Base44Client,
    records: list[dict[str, Any]],
    *,
    batch_size: int,
) -> int:
    if not records:
        return 0
    created = 0
    started_at = time.monotonic()
    batches = chunks(records, max(1, batch_size))
    for batch_index, batch in enumerate(batches, start=1):
        batch_started_at = time.monotonic()
        client.bulk_create(batch)
        created += len(batch)
        elapsed = time.monotonic() - started_at
        average_batch = elapsed / batch_index
        remaining_seconds = average_batch * (len(batches) - batch_index)
        percent = created / len(records) * 100
        remaining = len(records) - created
        first_name = batch[0]["name"]
        last_name = batch[-1]["name"]
        batch_label = first_name if len(batch) == 1 else f"{first_name} → {last_name}"
        print(
            f"Criados: {created}/{len(records)} ({percent:.1f}%) — {batch_label} "
            f"[faltam: {remaining} | lote: {format_duration(time.monotonic() - batch_started_at, precise=True)} "
            f"| decorrido: {format_duration(elapsed)} | restante: ~{format_duration(remaining_seconds)}]"
        )
    return created


def verify_created(client: Base44Client, records: list[dict[str, Any]]) -> None:
    existing = load_existing(client, records)
    missing, _ = partition_missing(records, existing)
    if missing:
        raise RuntimeError(
            f"A verificação final não encontrou {len(missing)} souvenirs que deveriam ter sido criados."
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.batch_size < 1:
        raise ValueError("--batch-size tem de ser pelo menos 1.")
    catalog = read_catalog(args.input)
    records = catalogue_records(catalog)
    records = filter_records_by_catalog_numbers(records, args.catalog_number)
    if args.limit > 0:
        records = records[: args.limit]
    if not records:
        print("O catálogo não contém souvenirs para importar.")
        return 0

    client = create_client(args)
    existing = load_existing(client, records)
    missing, already_present = partition_missing(records, existing)
    print_plan(records, missing, already_present)

    if not args.apply:
        print("\nPré-visualização concluída. O Site Base44 não foi alterado.")
        return 0
    if not missing:
        print("\nNão há souvenirs novos para criar.")
        return 0
    if not ask_confirmation(len(missing)):
        print("Operação cancelada. O Site Base44 não foi alterado.")
        return 0

    created = create_missing_records(client, missing, batch_size=args.batch_size)
    verify_created(client, missing)
    print(f"\nImportação concluída e verificada: {created} souvenirs criados.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        raise SystemExit(1)
