#!/usr/bin/env python3
"""Build a private, read-only plan for moving USA commemoratives to SpecialCoin.

This module deliberately has no Base44 client and performs no network requests.
It only consumes a complete backup produced by ``backup.py`` and creates an
auditable migration manifest.  Applying that plan is a separate phase.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MANIFEST_FORMAT_VERSION = 1
MINIMUM_BACKUP_FORMAT_VERSION = 3
DEFAULT_BACKUP_ROOT = Path("backups")
DEFAULT_OUTPUT_ROOT = DEFAULT_BACKUP_ROOT / "migrations"
SOURCE_EXPORT = Path("america/eua/normal.json")
EXPECTED_USA_COINS = 250
EXPECTED_COMMEMORATIVES = 238
EXPECTED_NORMAL_COINS = 12
EXPECTED_EXISTING_USA_SPECIAL_COINS = 0

# These exact programme labels are the immutable selection contract.  A new or
# renamed group makes the script stop instead of silently moving the wrong coin.
EXPECTED_GROUP_COUNTS = {
    "America the Beautiful": 56,
    "50 State Quarters": 50,
    "Presidential Dollars": 40,
    "Inovação americana": 31,
    "American Women": 20,
    "Nativos Americanos": 18,
    "Independência dos EUA": 9,
    "DC and Territories": 6,
    "Nasc. Abraham Lincoln": 4,
    "Westward Journey": 4,
}

# The current Coin.name consists of denomination + commemorative subject.  The
# target SpecialCoin model stores those two concepts in separate fields.
TARGET_DENOMINATION_BY_SOURCE_PREFIX = {
    "Penny": "1 cent",
    "Nickel": "5 cents",
    "Dime": "10 cents",
    "Quarter": "25 cents",
    "Half Dollar": "50 cents",
    "Dollar": "1 dólar",
}

EXPECTED_DENOMINATION_COUNTS = {
    "Penny": 5,
    "Nickel": 5,
    "Dime": 1,
    "Quarter": 137,
    "Half Dollar": 1,
    "Dollar": 89,
}

REQUIRED_BACKUP_ENTITIES = {
    "Coin",
    "CoinVariant",
    "SpecialCoin",
    "SpecialCoinVariant",
    "CoinSighting",
}

SPECIAL_COIN_COPY_FIELDS = (
    "country",
    "continent",
    "condition",
    "image_frente",
    "image_verso",
    "adquirida_por",
    "data_aquisicao",
    "local_compra",
    "valor_pago",
    "moeda_valor",
    "notes",
    "url_numista",
    "url_ucoin",
    "ordem",
    "hidden",
)

SPECIAL_VARIANT_COPY_FIELDS = (
    "tag",
    "condition",
    "adquirida_por",
    "data_aquisicao",
    "ordem",
)

DOCUMENTED_SPECIAL_COIN_CONDITIONS = {
    "Tenho",
    "Má Qualidade",
    "Por Entregar",
    "Não Tenho",
}


class MigrationManifestError(RuntimeError):
    """Raised when the source cannot be proven safe enough to plan."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Cria, sem escrever no Base44, o manifesto exato das 238 moedas "
            "comemorativas dos EUA que estão atualmente em Coin."
        )
    )
    parser.add_argument(
        "--backup",
        type=Path,
        help="Pasta de backup a usar. Por omissão lê backups/LATEST.",
    )
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser.parse_args(argv)


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationManifestError(f"JSON inválido ou ilegível: {path}") from exc


def resolve_backup_directory(
    *, backup: Path | None, backup_root: Path = DEFAULT_BACKUP_ROOT
) -> Path:
    if backup is not None:
        destination = backup
    else:
        latest_path = backup_root / "LATEST"
        try:
            backup_name = latest_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise MigrationManifestError(
                f"Não foi possível ler o ponteiro do backup: {latest_path}"
            ) from exc
        if not backup_name or Path(backup_name).name != backup_name:
            raise MigrationManifestError(f"Ponteiro de backup inválido: {latest_path}")
        destination = backup_root / backup_name

    if not destination.is_dir():
        raise MigrationManifestError(f"A pasta de backup não existe: {destination}")
    return destination


def parse_checksum_file(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise MigrationManifestError(f"Falta o ficheiro de checksums: {path}") from exc
    for line in lines:
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2 or len(parts[0]) != 64 or not parts[1]:
            raise MigrationManifestError(f"Linha de checksum inválida em {path}: {line!r}")
        checksum, relative_path = parts
        if relative_path in checksums:
            raise MigrationManifestError(
                f"Caminho repetido no ficheiro de checksums: {relative_path}"
            )
        checksums[relative_path] = checksum
    return checksums


def source_export_metadata(
    backup_directory: Path,
) -> tuple[dict[str, Any], dict[str, Any], Path, int]:
    manifest_path = backup_directory / "manifest.json"
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise MigrationManifestError("O manifest.json do backup não é um objeto.")
    if manifest.get("status") != "complete":
        raise MigrationManifestError("O backup não está marcado como completo.")
    if int(manifest.get("format_version") or 0) < MINIMUM_BACKUP_FORMAT_VERSION:
        raise MigrationManifestError(
            f"É necessário um backup de formato {MINIMUM_BACKUP_FORMAT_VERSION} ou superior."
        )

    scope = manifest.get("scope")
    if not isinstance(scope, dict):
        raise MigrationManifestError("O backup não contém informação de âmbito.")
    if not scope.get("all_api_fields_preserved") or not scope.get(
        "complete_item_views"
    ):
        raise MigrationManifestError(
            "O backup não garante todos os campos e as relações completas."
        )
    entities = set(scope.get("entities") or [])
    missing_entities = sorted(REQUIRED_BACKUP_ENTITIES - entities)
    if missing_entities:
        raise MigrationManifestError(
            "O backup não contém as entidades necessárias: "
            + ", ".join(missing_entities)
        )

    exports = manifest.get("country_exports")
    if not isinstance(exports, list):
        raise MigrationManifestError("O backup não contém country_exports.")
    matches = [entry for entry in exports if entry.get("file") == SOURCE_EXPORT.as_posix()]
    if len(matches) != 1:
        raise MigrationManifestError(
            f"Era esperado exatamente um export {SOURCE_EXPORT}; encontrados: {len(matches)}."
        )
    export = matches[0]
    if (
        export.get("country") != "EUA"
        or export.get("continent") != "América"
        or export.get("kind") != "normal"
    ):
        raise MigrationManifestError("Os metadados do export dos EUA não são canónicos.")

    source_path = backup_directory / SOURCE_EXPORT
    if not source_path.is_file():
        raise MigrationManifestError(f"Falta o export dos EUA: {source_path}")
    actual_export_checksum = sha256_file(source_path)
    if export.get("sha256") != actual_export_checksum:
        raise MigrationManifestError("O checksum do export dos EUA não corresponde ao manifest.")

    checksums = parse_checksum_file(backup_directory / "SHA256SUMS")
    manifest_entities = manifest.get("entities")
    if not isinstance(manifest_entities, list):
        raise MigrationManifestError("O backup não contém o inventário das entidades.")
    special_coin_entries = [
        entry for entry in manifest_entities if entry.get("entity") == "SpecialCoin"
    ]
    if len(special_coin_entries) != 1:
        raise MigrationManifestError(
            "Era esperado exatamente um ficheiro da entidade SpecialCoin no backup."
        )
    special_coin_entry = special_coin_entries[0]
    special_coin_relative_path = str(special_coin_entry.get("file") or "")
    if not special_coin_relative_path:
        raise MigrationManifestError("O caminho da entidade SpecialCoin está em falta.")
    special_coin_path = backup_directory / special_coin_relative_path
    if not special_coin_path.is_file():
        raise MigrationManifestError(f"Falta a entidade SpecialCoin: {special_coin_path}")
    special_coin_checksum = sha256_file(special_coin_path)
    if special_coin_entry.get("sha256") != special_coin_checksum:
        raise MigrationManifestError(
            "O checksum da entidade SpecialCoin não corresponde ao manifest."
        )

    for relative_path, actual_checksum in (
        ("manifest.json", sha256_file(manifest_path)),
        (SOURCE_EXPORT.as_posix(), actual_export_checksum),
        (special_coin_relative_path, special_coin_checksum),
    ):
        if checksums.get(relative_path) != actual_checksum:
            raise MigrationManifestError(
                f"O checksum de {relative_path} não corresponde a SHA256SUMS."
            )

    special_coins = read_json(special_coin_path)
    if not isinstance(special_coins, list) or any(
        not isinstance(record, dict) for record in special_coins
    ):
        raise MigrationManifestError("A entidade SpecialCoin do backup é inválida.")
    existing_usa_special_coins = sum(
        record.get("country") == "EUA" for record in special_coins
    )
    if existing_usa_special_coins != EXPECTED_EXISTING_USA_SPECIAL_COINS:
        raise MigrationManifestError(
            "O plano pressupõe que ainda não existem SpecialCoin dos EUA; "
            f"encontradas: {existing_usa_special_coins}."
        )
    return manifest, export, source_path, existing_usa_special_coins


def split_source_name(name: Any) -> tuple[str, str]:
    if not isinstance(name, str) or name.count(" - ") < 1:
        raise MigrationManifestError(f"Nome comemorativo inesperado: {name!r}")
    denomination, commemorative_name = name.split(" - ", 1)
    if denomination not in TARGET_DENOMINATION_BY_SOURCE_PREFIX:
        raise MigrationManifestError(f"Denominação dos EUA desconhecida: {denomination!r}")
    if not commemorative_name.strip():
        raise MigrationManifestError(f"Nome comemorativo vazio: {name!r}")
    return denomination, commemorative_name


def build_special_coin_payload(
    record: dict[str, Any], variants: list[dict[str, Any]]
) -> tuple[dict[str, Any], str]:
    source_denomination, commemorative_name = split_source_name(record.get("name"))
    year = record.get("years")
    if not isinstance(year, str) or not year.strip():
        raise MigrationManifestError(
            f"Coin {record.get('id')} não tem um período de emissão válido."
        )
    payload = {field: deepcopy(record.get(field)) for field in SPECIAL_COIN_COPY_FIELDS}
    payload.update(
        {
            "name": TARGET_DENOMINATION_BY_SOURCE_PREFIX[source_denomination],
            "commemorative_name": commemorative_name,
            "year": year,
            "has_variants": bool(variants),
        }
    )
    return payload, source_denomination


def build_variant_plan(variant: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_coin_variant_id": variant["id"],
        "source_record": deepcopy(variant),
        "target_special_coin_variant_id": None,
        "target_payload_without_parent_id": {
            field: deepcopy(variant.get(field)) for field in SPECIAL_VARIANT_COPY_FIELDS
        },
        "requires_new_special_coin_id": True,
        "status": "planned",
    }


def build_sighting_plan(
    sighting: dict[str, Any], target_payload: dict[str, Any]
) -> dict[str, Any]:
    return {
        "source_coin_sighting_id": sighting["id"],
        "source_record": deepcopy(sighting),
        "planned_patch_after_target_creation": {
            "coin_id": None,
            "item_id": None,
            "item_type": "special_coin",
            "coin_name": target_payload["name"],
            "years": target_payload["year"],
        },
        "requires_new_special_coin_id": True,
        "status": "planned",
    }


def _ensure_unique_id(value: Any, *, kind: str, seen: set[str]) -> str:
    record_id = str(value or "").strip()
    if not record_id:
        raise MigrationManifestError(f"Foi encontrado um {kind} sem ID.")
    if record_id in seen:
        raise MigrationManifestError(f"ID repetido em {kind}: {record_id}")
    seen.add(record_id)
    return record_id


def validate_source_entries(entries: Any) -> list[dict[str, Any]]:
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise MigrationManifestError("O export normal dos EUA não é uma lista válida.")
    if len(entries) != EXPECTED_USA_COINS:
        raise MigrationManifestError(
            f"Eram esperadas {EXPECTED_USA_COINS} moedas dos EUA; encontradas: {len(entries)}."
        )

    coin_ids: set[str] = set()
    variant_ids: set[str] = set()
    sighting_ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("entity") != "Coin":
            raise MigrationManifestError("O export normal dos EUA contém uma entidade não-Coin.")
        record = entry.get("record")
        variants = entry.get("variants")
        sightings = entry.get("sightings")
        if not isinstance(record, dict):
            raise MigrationManifestError("Foi encontrada uma entrada Coin sem registo completo.")
        if not isinstance(variants, list) or any(not isinstance(item, dict) for item in variants):
            raise MigrationManifestError(f"Coin {record.get('id')}: variantes inválidas.")
        if not isinstance(sightings, list) or any(
            not isinstance(item, dict) for item in sightings
        ):
            raise MigrationManifestError(f"Coin {record.get('id')}: descobertas inválidas.")
        coin_id = _ensure_unique_id(record.get("id"), kind="Coin", seen=coin_ids)
        if record.get("country") != "EUA" or record.get("continent") != "América":
            raise MigrationManifestError(
                f"Coin {coin_id}: país/continente diferente de EUA/América."
            )
        if bool(record.get("has_variants")) != bool(variants):
            raise MigrationManifestError(
                f"Coin {coin_id}: has_variants não corresponde às variantes associadas."
            )
        for variant in variants:
            variant_id = _ensure_unique_id(
                variant.get("id"), kind="CoinVariant", seen=variant_ids
            )
            if str(variant.get("coin_id") or "") != coin_id:
                raise MigrationManifestError(
                    f"CoinVariant {variant_id}: coin_id não corresponde ao pai {coin_id}."
                )
        for sighting in sightings:
            sighting_id = _ensure_unique_id(
                sighting.get("id"), kind="CoinSighting", seen=sighting_ids
            )
            for field in ("coin_id", "item_id"):
                relation_id = str(sighting.get(field) or "").strip()
                if relation_id and relation_id != coin_id:
                    raise MigrationManifestError(
                        f"CoinSighting {sighting_id}: {field} não corresponde ao pai {coin_id}."
                    )
        validated.append(entry)
    return validated


def counted(values: list[Any]) -> dict[str, int]:
    return dict(sorted(Counter(str(value) for value in values).items()))


def build_migration_manifest(
    entries: Any,
    *,
    source_backup: dict[str, Any],
    created_at: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validated = validate_source_entries(entries)
    selected = [
        entry
        for entry in validated
        if entry["record"].get("notes") in EXPECTED_GROUP_COUNTS
    ]
    excluded = [entry for entry in validated if entry not in selected]
    if len(selected) != EXPECTED_COMMEMORATIVES or len(excluded) != EXPECTED_NORMAL_COINS:
        raise MigrationManifestError(
            "A seleção não corresponde ao contrato: "
            f"{len(selected)} comemorativas e {len(excluded)} normais."
        )

    actual_group_counts = Counter(entry["record"].get("notes") for entry in selected)
    if actual_group_counts != Counter(EXPECTED_GROUP_COUNTS):
        raise MigrationManifestError(
            "As contagens por programa comemorativo mudaram: "
            f"{dict(sorted(actual_group_counts.items()))}"
        )

    migration_items: list[dict[str, Any]] = []
    source_denominations: list[str] = []
    for sequence, entry in enumerate(selected, start=1):
        record = entry["record"]
        variants = entry["variants"]
        sightings = entry["sightings"]
        target_payload, source_denomination = build_special_coin_payload(record, variants)
        source_denominations.append(source_denomination)
        migration_items.append(
            {
                "sequence": sequence,
                "programme": record["notes"],
                "source_coin_id": record["id"],
                "source_record": deepcopy(record),
                "source_record_sha256": sha256_bytes(json_bytes(record)),
                "target_special_coin_id": None,
                "target_special_coin_payload": target_payload,
                "source_only_fields": {
                    "rarity": deepcopy(record.get("rarity")),
                    "note": (
                        "SpecialCoin não documenta o campo rarity; o valor original "
                        "fica preservado neste manifesto e no backup."
                    ),
                },
                "variants": [build_variant_plan(variant) for variant in variants],
                "sightings": [
                    build_sighting_plan(sighting, target_payload) for sighting in sightings
                ],
                "source_coin_deletion_authorized": False,
                "status": "planned",
            }
        )

    actual_denomination_counts = Counter(source_denominations)
    if actual_denomination_counts != Counter(EXPECTED_DENOMINATION_COUNTS):
        raise MigrationManifestError(
            "As contagens por denominação mudaram: "
            f"{dict(sorted(actual_denomination_counts.items()))}"
        )

    variant_count = sum(len(item["variants"]) for item in migration_items)
    variant_parent_count = sum(bool(item["variants"]) for item in migration_items)
    sighting_count = sum(len(item["sightings"]) for item in migration_items)
    sighting_parent_count = sum(bool(item["sightings"]) for item in migration_items)
    conditions = counted([item["source_record"].get("condition") for item in migration_items])
    undocumented_conditions = {
        condition: count
        for condition, count in conditions.items()
        if condition not in DOCUMENTED_SPECIAL_COIN_CONDITIONS
    }
    summary = {
        "status": "planned_read_only",
        "created_at": created_at.isoformat(timespec="seconds"),
        "source_backup": source_backup,
        "counts": {
            "usa_coins_in_source": len(validated),
            "commemoratives_selected": len(migration_items),
            "normal_coins_excluded": len(excluded),
            "existing_usa_special_coins_in_backup": int(
                source_backup.get("existing_usa_special_coins") or 0
            ),
            "source_variants": variant_count,
            "parents_with_variants": variant_parent_count,
            "source_sightings": sighting_count,
            "parents_with_sightings": sighting_parent_count,
            "base44_writes": 0,
        },
        "programme_counts": dict(sorted(actual_group_counts.items())),
        "source_denomination_counts": dict(sorted(actual_denomination_counts.items())),
        "target_denomination_counts": counted(
            [item["target_special_coin_payload"]["name"] for item in migration_items]
        ),
        "condition_counts": conditions,
        "source_rarity_counts": counted(
            [item["source_record"].get("rarity") for item in migration_items]
        ),
        "data_preservation": {
            "full_source_records_in_manifest": True,
            "full_source_variants_in_manifest": True,
            "full_source_sightings_in_manifest": True,
            "source_rarity_preserved_outside_target_payload": True,
            "source_coin_deletions_authorized": 0,
        },
        "review_before_execution": {
            "undocumented_target_conditions": undocumented_conditions,
            "source_only_field": "rarity",
            "target_ids_resolved": 0,
        },
    }
    manifest = {
        "format_version": MANIFEST_FORMAT_VERSION,
        "kind": "eua_coin_to_special_coin_migration",
        "status": "planned_read_only",
        "created_at": created_at.isoformat(timespec="seconds"),
        "source": source_backup,
        "selection_contract": {
            "country": "EUA",
            "continent": "América",
            "source_entity": "Coin",
            "target_entity": "SpecialCoin",
            "source_export": SOURCE_EXPORT.as_posix(),
            "expected_usa_coins": EXPECTED_USA_COINS,
            "expected_commemoratives": EXPECTED_COMMEMORATIVES,
            "expected_normal_coins": EXPECTED_NORMAL_COINS,
            "programme_counts": EXPECTED_GROUP_COUNTS,
            "source_denomination_counts": EXPECTED_DENOMINATION_COUNTS,
            "target_denomination_mapping": TARGET_DENOMINATION_BY_SOURCE_PREFIX,
        },
        "safety": {
            "base44_reads": 0,
            "base44_writes": 0,
            "network_requests": 0,
            "source_coin_deletions_authorized": 0,
            "note": (
                "Este ficheiro é apenas um plano. Criar SpecialCoin, migrar variantes/"
                "descobertas e apagar Coin exigem uma fase separada e confirmação explícita."
            ),
        },
        "summary": summary["counts"],
        "items": migration_items,
        "excluded_normal_coins": [deepcopy(entry) for entry in excluded],
    }
    return manifest, summary


def unique_destination(output_root: Path, timestamp: str) -> Path:
    base_name = f"eua-commemorativas-{timestamp}"
    destination = output_root / base_name
    suffix = 2
    while destination.exists():
        destination = output_root / f"{base_name}-{suffix}"
        suffix += 1
    return destination


def write_private_file(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    path.chmod(0o600)


def create_manifest_artifact(
    *,
    backup_directory: Path,
    output_root: Path,
    created_at: datetime | None = None,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    (
        backup_manifest,
        source_export,
        source_path,
        existing_usa_special_coins,
    ) = source_export_metadata(backup_directory)
    entries = read_json(source_path)
    timestamp_value = created_at or datetime.now(timezone.utc)
    if timestamp_value.tzinfo is None:
        raise ValueError("created_at tem de incluir timezone.")
    timestamp = timestamp_value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    source_metadata = {
        "backup_directory": backup_directory.name,
        "backup_created_at": backup_manifest.get("created_at"),
        "backup_manifest_sha256": sha256_file(backup_directory / "manifest.json"),
        "source_export": SOURCE_EXPORT.as_posix(),
        "source_export_records": source_export.get("records"),
        "source_export_sha256": source_export.get("sha256"),
        "existing_usa_special_coins": existing_usa_special_coins,
    }
    migration_manifest, summary = build_migration_manifest(
        entries,
        source_backup=source_metadata,
        created_at=timestamp_value,
    )

    output_root.mkdir(parents=True, exist_ok=True)
    output_root.chmod(0o700)
    destination = unique_destination(output_root, timestamp)
    with tempfile.TemporaryDirectory(
        prefix=f".{destination.name}.partial-", dir=output_root
    ) as temporary_name:
        temporary = Path(temporary_name)
        temporary.chmod(0o700)
        manifest_content = json_bytes(migration_manifest)
        summary_content = json_bytes(summary)
        write_private_file(temporary / "manifest.json", manifest_content)
        write_private_file(temporary / "summary.json", summary_content)
        checksums = (
            f"{sha256_bytes(manifest_content)}  manifest.json\n"
            f"{sha256_bytes(summary_content)}  summary.json\n"
        ).encode("utf-8")
        write_private_file(temporary / "SHA256SUMS", checksums)

        # Re-read every generated file before publishing the complete directory.
        if read_json(temporary / "manifest.json") != migration_manifest:
            raise MigrationManifestError("O manifesto escrito não ficou íntegro.")
        if read_json(temporary / "summary.json") != summary:
            raise MigrationManifestError("O resumo escrito não ficou íntegro.")
        generated_checksums = parse_checksum_file(temporary / "SHA256SUMS")
        for relative_path in ("manifest.json", "summary.json"):
            if generated_checksums[relative_path] != sha256_file(temporary / relative_path):
                raise MigrationManifestError(
                    f"Falhou a verificação final de {relative_path}."
                )
        os.replace(temporary, destination)

    write_private_file(output_root / "LATEST", (destination.name + "\n").encode("utf-8"))
    return destination, migration_manifest, summary


def print_summary(destination: Path, summary: dict[str, Any]) -> None:
    counts = summary["counts"]
    print("\n" + "=" * 72)
    print("MANIFESTO DOS EUA CONCLUÍDO E VERIFICADO")
    print("=" * 72)
    print(f"Pasta privada: {destination}")
    print(f"Moedas dos EUA analisadas: {counts['usa_coins_in_source']}")
    print(f"Comemorativas selecionadas: {counts['commemoratives_selected']}")
    print(f"Moedas normais mantidas fora do plano: {counts['normal_coins_excluded']}")
    print(
        "Variantes preservadas: "
        f"{counts['source_variants']} em {counts['parents_with_variants']} moedas"
    )
    print(
        "Descobertas preservadas: "
        f"{counts['source_sightings']} em {counts['parents_with_sightings']} moedas"
    )
    undocumented = summary["review_before_execution"]["undocumented_target_conditions"]
    if undocumented:
        print("Revisão obrigatória antes de executar: " + json.dumps(undocumented, ensure_ascii=False))
    print("Site Base44: nenhuma leitura ou alteração efetuada.")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    backup_directory = resolve_backup_directory(
        backup=args.backup, backup_root=args.backup_root
    )
    destination, _manifest, summary = create_manifest_artifact(
        backup_directory=backup_directory,
        output_root=args.output_root,
    )
    print_summary(destination, summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
