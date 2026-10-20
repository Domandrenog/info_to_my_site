#!/usr/bin/env python3
"""Safely create missing Base44 SpecialCoin parents from an approved catalogue."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.admin_stats_sync import MutationRun, mark_numisvault_stats_stale
from scripts.execute_eua_commemorative_migration import (
    MigrationExecutionError,
    create_and_verify_one,
    fetch_all,
    no_retry_entity_client,
    special_coin_identity,
)
from scripts.import_base44_coins import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_RATE_LIMIT_DELAY_SECONDS,
    DEFAULT_REQUEST_DELAY_SECONDS,
    create_client,
)
from scripts.prepare_special_app_catalog import (
    SpecialCatalogPreparationError,
    iter_special_coins,
    read_json,
    validate_special_catalog,
)


CATALOG_FIELDS = (
    "country",
    "continent",
    "name",
    "commemorative_name",
    "year",
    "notes",
    "ordem",
    "url_ucoin",
    "url_numista",
    "image_frente",
    "image_verso",
    "hidden",
)


@dataclass
class SpecialCoinPlan:
    country: str
    expected: int
    existing: list[tuple[dict[str, Any], dict[str, Any]]]
    pending: list[dict[str, Any]]
    untracked_live: int


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verifica ou cria apenas SpecialCoin em falta a partir de um "
            "special-app-catalog.json aprovado."
        )
    )
    parser.add_argument("--catalog", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-live", action="store_true")
    modes.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    parser.add_argument(
        "--rate-limit-delay", type=float, default=DEFAULT_RATE_LIMIT_DELAY_SECONDS
    )
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    return parser.parse_args(argv)


def normalized_catalog_field(record: dict[str, Any], field: str) -> Any:
    value = record.get(field)
    if field == "hidden":
        return bool(value)
    if field == "url_numista":
        return str(value or "").strip()
    return value


def payload_from_coin(
    catalogue: dict[str, Any], programme: str, coin: dict[str, Any]
) -> dict[str, Any]:
    return {
        "country": str(catalogue["siteCountry"]),
        "continent": str(catalogue["continent"]),
        "condition": "Não Tenho",
        "name": str(coin["denomination"]),
        "commemorative_name": str(coin["commemorativeName"]),
        "year": str(coin["year"]),
        "notes": programme,
        "ordem": int(coin["order"]),
        "url_ucoin": str(coin["detailUrl"]),
        "url_numista": str(coin.get("numistaUrl") or ""),
        "image_frente": str(coin.get("obverseImage") or ""),
        "image_verso": str(coin.get("reverseImage") or ""),
        "hidden": bool(coin.get("hidden")),
        # The variant importer enables this only after every requested child
        # has been confirmed, avoiding an empty selector after parent creation.
        "has_variants": False,
        "adquirida_por": "",
        "data_aquisicao": "",
        "local_compra": None,
        "valor_pago": None,
        "moeda_valor": "EUR",
    }


def catalogue_payloads(catalogue: dict[str, Any]) -> list[dict[str, Any]]:
    validate_special_catalog(catalogue, require_approved=True)
    return [
        payload_from_coin(catalogue, programme, coin)
        for programme, coin in iter_special_coins(catalogue)
    ]


def catalog_differences(
    expected: dict[str, Any], actual: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    return {
        field: {
            "expected": normalized_catalog_field(expected, field),
            "actual": normalized_catalog_field(actual, field),
        }
        for field in CATALOG_FIELDS
        if normalized_catalog_field(expected, field)
        != normalized_catalog_field(actual, field)
    }


def build_plan(
    catalogue: dict[str, Any], live_records: list[dict[str, Any]]
) -> SpecialCoinPlan:
    payloads = catalogue_payloads(catalogue)
    country = str(catalogue["siteCountry"])
    by_url: dict[str, list[dict[str, Any]]] = {}
    by_identity: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for record in live_records:
        url = str(record.get("url_ucoin") or "").strip()
        if url:
            by_url.setdefault(url, []).append(record)
        try:
            identity = special_coin_identity(record)
        except MigrationExecutionError:
            continue
        by_identity.setdefault(identity, []).append(record)

    existing: list[tuple[dict[str, Any], dict[str, Any]]] = []
    pending: list[dict[str, Any]] = []
    tracked_ids: set[str] = set()
    for payload in payloads:
        url = payload["url_ucoin"]
        url_matches = by_url.get(url, [])
        identity = special_coin_identity(payload)
        identity_matches = by_identity.get(identity, [])
        if len(url_matches) > 1 or len(identity_matches) > 1:
            raise MigrationExecutionError(
                f"Registo ambíguo no Site para {payload['name']} — "
                f"{payload['commemorative_name']} ({payload['year']})."
            )
        if url_matches:
            actual = url_matches[0]
            if actual.get("country") != country:
                raise MigrationExecutionError(
                    f"O URL {url} já pertence ao país {actual.get('country')}."
                )
            if identity_matches != url_matches:
                raise MigrationExecutionError(
                    f"O URL {url} está associado a outra identidade no Site."
                )
            differences = catalog_differences(payload, actual)
            if differences:
                raise MigrationExecutionError(
                    f"A moeda existente {url} diverge do catálogo: "
                    + json.dumps(differences, ensure_ascii=False, sort_keys=True)
                )
            existing.append((payload, actual))
            tracked_ids.add(str(actual.get("id") or ""))
            continue
        if identity_matches:
            raise MigrationExecutionError(
                f"A identidade {identity} já existe com outro URL uCoin."
            )
        pending.append(payload)

    untracked_live = sum(
        1
        for record in live_records
        if record.get("country") == country
        and str(record.get("id") or "") not in tracked_ids
    )
    return SpecialCoinPlan(country, len(payloads), existing, pending, untracked_live)


def print_plan(plan: SpecialCoinPlan) -> None:
    print("\n" + "=" * 72)
    print(f"MOEDAS DE COLEÇÃO — {plan.country} — PLANO CREATE-ONLY")
    print("=" * 72)
    print(f"Catálogo aprovado: {plan.expected} moedas")
    print(f"Já existentes e preservadas: {len(plan.existing)}")
    print(f"Novas a criar: {len(plan.pending)}")
    print(f"Outras moedas live fora deste catálogo: {plan.untracked_live}")
    print("Atualizações: 0")
    print("Eliminações: 0")
    if plan.pending:
        grouped: dict[str, int] = {}
        for payload in plan.pending:
            grouped[payload["notes"]] = grouped.get(payload["notes"], 0) + 1
        print("\nNovas moedas por programa/época:")
        for programme, count in grouped.items():
            print(f"- {programme}: {count}")


def format_duration(seconds: float) -> str:
    rounded = max(0, round(seconds))
    minutes, remainder = divmod(rounded, 60)
    return f"{minutes}m {remainder:02d}s" if minutes else f"{remainder}s"


def progress_line(payload: dict[str, Any], position: int, total: int, started: float) -> str:
    elapsed = time.monotonic() - started
    remaining = total - position
    eta = elapsed / position * remaining if position else 0.0
    return (
        f"Criado: {position}/{total} ({position / total * 100:.1f}%) — "
        f"{payload['country']} — {payload['name']} — "
        f"{payload['commemorative_name']} ({payload['year']}) "
        f"[faltam: {remaining} | decorrido: {format_duration(elapsed)} | "
        f"restante: {'concluído' if not remaining else '~' + format_duration(eta)}]"
    )


def apply_plan(base_client: Any, plan: SpecialCoinPlan) -> int:
    if not plan.pending:
        print("Nenhuma SpecialCoin por criar.")
        return 0
    read_client = base_client.for_entity("SpecialCoin")
    write_client = no_retry_entity_client(base_client, "SpecialCoin")
    started = time.monotonic()
    created = 0
    with MutationRun(
        lambda: mark_numisvault_stats_stale(base_client),
        mutation_label="SpecialCoin record",
    ) as mutations:
        for position, payload in enumerate(plan.pending, start=1):
            create_and_verify_one(
                read_client=read_client,
                write_client=write_client,
                payload=payload,
                mutation_success_fn=mutations.record_success,
            )
            created += 1
            print(progress_line(payload, position, len(plan.pending), started), flush=True)
    return created


def run(args: argparse.Namespace) -> int:
    if args.apply and not args.yes:
        raise MigrationExecutionError(
            "--apply exige --yes; nenhuma alteração foi efetuada."
        )
    catalogue = read_json(args.catalog)
    if not isinstance(catalogue, dict):
        raise SpecialCatalogPreparationError("Catálogo aprovado inválido.")
    validate_special_catalog(catalogue, require_approved=True)
    if not (args.check_live or args.apply):
        payloads = catalogue_payloads(catalogue)
        print(f"País: {catalogue['siteCountry']}")
        print(f"Moedas no catálogo aprovado: {len(payloads)}")
        print("Modo local: nenhuma chamada ao Site Base44.")
        return 0

    base_client = create_client(args)
    live = fetch_all(base_client.for_entity("SpecialCoin"), {})
    plan = build_plan(catalogue, live)
    print_plan(plan)
    if args.check_live:
        print("Site Base44: nenhuma alteração efetuada.")
        return 0

    created = apply_plan(base_client, plan)
    verified = build_plan(
        catalogue,
        fetch_all(base_client.for_entity("SpecialCoin"), {}),
    )
    if verified.pending:
        raise MigrationExecutionError(
            f"A verificação final encontrou {len(verified.pending)} moedas em falta."
        )
    print(f"Concluído: {created} SpecialCoin criadas e verificadas.")
    print("SpecialCoin atualizadas ou apagadas: 0")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except (
        MigrationExecutionError,
        SpecialCatalogPreparationError,
        OSError,
        RuntimeError,
    ) as exc:
        print(f"Erro: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
