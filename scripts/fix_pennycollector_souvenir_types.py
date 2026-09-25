#!/usr/bin/env python3
"""Repair PennyCollector token/medallion types locally and in Base44."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from scripts import import_base44_souvenirs
from scripts.import_base44_coins import format_duration
from scripts.pennycollector_souvenirs import (
    normalize_pennycollector_catalog_types,
    souvenir_type_for_machine_details,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Corrige tokens e medalhões PennyCollector marcados como pressed. "
            "Sem --apply apenas mostra o plano."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=import_base44_souvenirs.DEFAULT_CATALOG_ROOT,
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Usar apenas após uma autorização explícita já recebida.",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=import_base44_souvenirs.DEFAULT_REQUEST_DELAY_SECONDS,
    )
    parser.add_argument(
        "--rate-limit-delay",
        type=float,
        default=import_base44_souvenirs.DEFAULT_RATE_LIMIT_DELAY_SECONDS,
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=import_base44_souvenirs.DEFAULT_MAX_RETRIES,
    )
    return parser.parse_args(argv)


def discover_catalog_paths(root: Path) -> list[Path]:
    return sorted(
        [
            *root.rglob("pennycollector-catalog.json"),
            *root.rglob("pennycollector-catalog-final.json"),
        ]
    )


def read_catalog(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError(f"Catálogo inválido: {path}")
    return payload


def prepare_catalog_repairs(
    paths: list[Path],
) -> list[tuple[Path, dict[str, Any], int]]:
    repairs: list[tuple[Path, dict[str, Any], int]] = []
    for path in paths:
        payload = read_catalog(path)
        changed = normalize_pennycollector_catalog_types(payload)
        if changed:
            repairs.append((path, payload, changed))
    return repairs


def desired_coin_records(
    paths: list[Path],
) -> list[dict[str, Any]]:
    records_by_identity: dict[tuple[str, ...], dict[str, Any]] = {}
    for path in paths:
        if path.name != "pennycollector-catalog-final.json":
            continue
        payload = read_catalog(path)
        if payload.get("import_ready") is not True:
            continue
        normalize_pennycollector_catalog_types(payload)
        for index, item in enumerate(payload["items"], start=1):
            if str(item.get("review_status") or "approved") != "approved":
                continue
            source = item.get("source", {})
            souvenir = item.get("souvenir", {})
            if not isinstance(source, dict) or not isinstance(souvenir, dict):
                continue
            if souvenir_type_for_machine_details(
                str(source.get("machine_details") or "")
            ) != "coin":
                continue
            record = import_base44_souvenirs.validate_record(souvenir, index)
            identity = import_base44_souvenirs.preferred_identity(record)
            previous = records_by_identity.get(identity)
            if previous is not None and previous != record:
                raise ValueError(
                    "O mesmo token/medalhão tem dados diferentes em catálogos finais."
                )
            records_by_identity[identity] = record
    return sorted(
        records_by_identity.values(),
        key=lambda record: (
            str(record["country"]),
            str(record.get("location_name") or ""),
            str(record["name"]),
        ),
    )


def matching_existing_record(
    desired: dict[str, Any], existing: list[dict[str, Any]]
) -> dict[str, Any] | None:
    identity = import_base44_souvenirs.strong_identity(desired)
    fallback = import_base44_souvenirs.fallback_identity(desired)
    if identity is not None:
        for record in existing:
            if import_base44_souvenirs.strong_identity(record) == identity:
                return record
        for record in existing:
            if (
                import_base44_souvenirs.strong_identity(record) is None
                and import_base44_souvenirs.fallback_identity(record) == fallback
            ):
                return record
        return None
    for record in existing:
        if import_base44_souvenirs.fallback_identity(record) == fallback:
            return record
    return None


def build_corrections(
    desired: list[dict[str, Any]], existing: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    corrections: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for record in desired:
        current = matching_existing_record(record, existing)
        if current is None:
            missing.append(record)
            continue
        if current.get("type") == "coin":
            continue
        record_id = str(current.get("id") or "").strip()
        if not record_id:
            raise ValueError(f"Registo sem id no Site Base44: {record['name']}")
        corrections.append(
            {
                "id": record_id,
                "country": record["country"],
                "name": record["name"],
                "location_name": record.get("location_name") or "",
                "current_type": current.get("type") or "",
                "desired_type": "coin",
            }
        )
    return corrections, missing


def print_plan(
    repairs: list[tuple[Path, dict[str, Any], int]],
    desired: list[dict[str, Any]],
    corrections: list[dict[str, Any]],
    missing: list[dict[str, Any]],
) -> None:
    print("\nPlano de correção de tipos PennyCollector:")
    print(f"- Tokens/medalhões confirmados nos catálogos finais: {len(desired)}")
    print(f"- Ficheiros locais a corrigir: {len(repairs)}")
    print(f"- Campos type locais a corrigir: {sum(item[2] for item in repairs)}")
    print(f"- Registos Base44 a corrigir: {len(corrections)}")
    print(f"- Registos não encontrados no Base44: {len(missing)}")
    if corrections:
        print("\nAlterações exatas no Site Base44:")
        for index, correction in enumerate(corrections, start=1):
            print(
                f"{index}/{len(corrections)} — {correction['country']} — "
                f"{correction['location_name']} — {correction['name']}: "
                f"{correction['current_type']} → {correction['desired_type']}"
            )


def ask_confirmation(correction_count: int, local_count: int) -> bool:
    answer = input(
        f"\nCorrigir {local_count} ficheiros locais e exatamente "
        f"{correction_count} registos no Site Base44? [s/N]: "
    ).strip().casefold()
    return answer in {"s", "sim", "y", "yes"}


def write_catalog_repairs(
    repairs: list[tuple[Path, dict[str, Any], int]]
) -> None:
    for path, payload, _changed in repairs:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def apply_corrections(client: Any, corrections: list[dict[str, Any]]) -> None:
    started_at = time.monotonic()
    for index, correction in enumerate(corrections, start=1):
        item_started_at = time.monotonic()
        client.update(correction["id"], {"type": correction["desired_type"]})
        elapsed = time.monotonic() - started_at
        remaining_count = len(corrections) - index
        estimated = (elapsed / index) * remaining_count
        print(
            f"Atualizado: {index}/{len(corrections)} ({index / len(corrections) * 100:.1f}%) "
            f"— {correction['country']} — {correction['location_name']} — "
            f"{correction['name']} [faltam: {remaining_count} | "
            f"demorou nesta: {format_duration(time.monotonic() - item_started_at, precise=True)} "
            f"| decorrido: {format_duration(elapsed)} | restante: ~{format_duration(estimated)}] "
            f"— type: {correction['current_type']} → {correction['desired_type']}"
        )


def load_existing(client: Any, desired: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return import_base44_souvenirs.load_existing(
        client,
        desired,
        show_progress=True,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = discover_catalog_paths(args.root)
    if not paths:
        print(f"Nenhum catálogo PennyCollector encontrado em {args.root}.")
        return 0
    repairs = prepare_catalog_repairs(paths)
    desired = desired_coin_records(paths)
    if not desired:
        print("Nenhum token ou medalhão confirmado nos catálogos finais.")
        return 0

    client = import_base44_souvenirs.create_client(args)
    existing = load_existing(client, desired)
    corrections, missing = build_corrections(desired, existing)
    print_plan(repairs, desired, corrections, missing)

    if not args.apply:
        print("\nPré-visualização concluída. Nenhum ficheiro ou registo foi alterado.")
        return 0
    if not args.yes and not ask_confirmation(len(corrections), len(repairs)):
        print("Operação cancelada. Nenhum ficheiro ou registo foi alterado.")
        return 0

    write_catalog_repairs(repairs)
    print(f"\nCatálogos locais corrigidos: {len(repairs)}")
    apply_corrections(client, corrections)

    verified_existing = load_existing(client, desired)
    remaining, still_missing = build_corrections(desired, verified_existing)
    if remaining or still_missing:
        raise RuntimeError(
            "A verificação final falhou: "
            f"{len(remaining)} tipos ainda incorretos e "
            f"{len(still_missing)} registos não encontrados."
        )
    remaining_local = prepare_catalog_repairs(paths)
    if remaining_local:
        raise RuntimeError(
            f"A verificação final encontrou {len(remaining_local)} catálogos locais por corrigir."
        )
    print(
        f"\nCorreção concluída e verificada: {len(corrections)} registos Base44 "
        f"e {len(repairs)} ficheiros locais corrigidos."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        raise SystemExit(1)
