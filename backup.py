#!/usr/bin/env python3
"""Create a complete, read-only backup of the known Base44 entities."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from scripts.import_base44_coins import (
    DEFAULT_BASE44_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RATE_LIMIT_DELAY_SECONDS,
    DEFAULT_REQUEST_DELAY_SECONDS,
    Base44Client,
    load_dotenv,
)


BACKUP_FORMAT_VERSION = 2
DEFAULT_OUTPUT_ROOT = Path("backups")
DEFAULT_PAGE_SIZE = 1000
DEFAULT_ENTITIES = (
    "CoinVariant",
    "SpecialCoin",
    "CountryNote",
    "CountrySettings",
    "Coin",
    "CoinSighting",
    "Souvenir",
    "User",
)
COLLECTIBLE_ENTITIES = ("Coin", "SpecialCoin", "CountryNote", "Souvenir")
ENTITY_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Guarda todos os registos das entidades Base44 conhecidas numa pasta "
            "datada. Esta operação é exclusivamente de leitura."
        )
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--entity",
        action="append",
        default=[],
        help="Incluir apenas esta entidade; pode repetir a opção.",
    )
    parser.add_argument(
        "--exclude-entity",
        action="append",
        default=[],
        help="Excluir uma entidade do backup; pode repetir a opção.",
    )
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    parser.add_argument(
        "--rate-limit-delay",
        type=float,
        default=DEFAULT_RATE_LIMIT_DELAY_SECONDS,
    )
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    return parser.parse_args(argv)


def selected_entities(requested: list[str], excluded: list[str]) -> list[str]:
    entities = requested or list(DEFAULT_ENTITIES)
    invalid = sorted(
        {name for name in [*entities, *excluded] if not ENTITY_NAME_RE.fullmatch(name)}
    )
    if invalid:
        raise ValueError("Nome de entidade inválido: " + ", ".join(invalid))
    excluded_set = set(excluded)
    selected: list[str] = []
    seen: set[str] = set()
    for entity in entities:
        if entity not in excluded_set and entity not in seen:
            selected.append(entity)
            seen.add(entity)
    if not selected:
        raise ValueError("O backup tem de incluir pelo menos uma entidade.")
    return selected


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch_entity_records(
    client: Base44Client,
    entity: str,
    *,
    page_size: int,
    output_fn: Callable[[str], None] = print,
) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    page = 0
    skip = 0
    while True:
        page += 1
        result = client.filter({}, limit=page_size, skip=skip)
        if not isinstance(result, list):
            raise RuntimeError(f"{entity}: resposta inesperada do Site Base44: {result!r}")
        page_records = [record for record in result if isinstance(record, dict)]
        if len(page_records) != len(result):
            raise RuntimeError(f"{entity}: a resposta contém registos inválidos.")
        for record in page_records:
            record_id = str(record.get("id") or "").strip()
            if not record_id:
                raise RuntimeError(f"{entity}: foi devolvido um registo sem ID.")
            if record_id in seen_ids:
                raise RuntimeError(
                    f"{entity}: o registo {record_id} apareceu em mais de uma página."
                )
            seen_ids.add(record_id)
            records.append(record)
        output_fn(
            f"{entity}: página {page} — {len(page_records)} registos "
            f"[{len(records)} acumulados]"
        )
        if len(result) < page_size:
            break
        skip += page_size
    records.sort(
        key=lambda record: (
            str(record.get("id") or ""),
            json.dumps(record, ensure_ascii=False, sort_keys=True),
        )
    )
    return records, page


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_private_file(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    path.chmod(0o600)


def verified_file(path: Path, *, expected_sha256: str, expected_count: int) -> None:
    content = path.read_bytes()
    actual_sha256 = sha256_bytes(content)
    if actual_sha256 != expected_sha256:
        raise RuntimeError(f"Checksum inválido depois de escrever {path}.")
    payload = json.loads(content.decode("utf-8"))
    if not isinstance(payload, list) or len(payload) != expected_count:
        raise RuntimeError(f"Contagem inválida depois de escrever {path}.")


def unique_destination(output_root: Path, timestamp: str) -> Path:
    base_name = f"base44-{timestamp}"
    destination = output_root / base_name
    suffix = 2
    while destination.exists():
        destination = output_root / f"{base_name}-{suffix}"
        suffix += 1
    return destination


def previous_backup_directory(output_root: Path) -> Path | None:
    latest_path = output_root / "LATEST"
    if not latest_path.exists():
        return None
    backup_name = latest_path.read_text(encoding="utf-8").strip()
    if not backup_name or Path(backup_name).name != backup_name:
        raise RuntimeError(f"Ponteiro de backup inválido em {latest_path}.")
    previous = output_root / backup_name
    if not previous.is_dir():
        raise RuntimeError(f"O backup anterior indicado em {latest_path} não existe.")
    return previous


def build_complete_item_views(
    records_by_entity: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    views: dict[str, list[dict[str, Any]]] = {}
    item_index: dict[str, tuple[str, dict[str, Any]]] = {}
    for entity in COLLECTIBLE_ENTITIES:
        if entity not in records_by_entity:
            continue
        entries: list[dict[str, Any]] = []
        for record in records_by_entity[entity]:
            record_id = str(record["id"])
            if record_id in item_index:
                previous_entity = item_index[record_id][0]
                raise RuntimeError(
                    f"ID {record_id} repetido entre {previous_entity} e {entity}."
                )
            entry = {
                "entity": entity,
                "record": record,
                "variants": [],
                "sightings": [],
            }
            item_index[record_id] = (entity, entry)
            entries.append(entry)
        views[entity] = entries

    unmatched_relations: list[dict[str, Any]] = []
    variant_records = records_by_entity.get("CoinVariant", [])
    linked_variants = 0
    for variant in variant_records:
        target = item_index.get(str(variant.get("coin_id") or ""))
        if target is not None and target[0] == "Coin":
            target[1]["variants"].append(variant)
            linked_variants += 1
        else:
            unmatched_relations.append(
                {
                    "relation_entity": "CoinVariant",
                    "reason": "coin_id sem moeda correspondente no âmbito deste backup",
                    "record": variant,
                }
            )

    sighting_records = records_by_entity.get("CoinSighting", [])
    linked_sightings = 0
    for sighting in sighting_records:
        item_target = item_index.get(str(sighting.get("item_id") or ""))
        coin_target = item_index.get(str(sighting.get("coin_id") or ""))
        if item_target is not None and coin_target is not None and item_target is not coin_target:
            unmatched_relations.append(
                {
                    "relation_entity": "CoinSighting",
                    "reason": "item_id e coin_id apontam para itens diferentes",
                    "record": sighting,
                }
            )
            continue
        target = item_target or coin_target
        if target is not None:
            target[1]["sightings"].append(sighting)
            linked_sightings += 1
        else:
            unmatched_relations.append(
                {
                    "relation_entity": "CoinSighting",
                    "reason": "item_id/coin_id sem item correspondente no âmbito deste backup",
                    "record": sighting,
                }
            )

    views["unmatched-relations"] = unmatched_relations
    summary = {
        "items": sum(len(views.get(entity, [])) for entity in COLLECTIBLE_ENTITIES),
        "coin_variants": {
            "total": len(variant_records),
            "linked": linked_variants,
            "unmatched": len(variant_records) - linked_variants,
        },
        "coin_sightings": {
            "total": len(sighting_records),
            "linked": linked_sightings,
            "unmatched": len(sighting_records) - linked_sightings,
        },
        "unmatched_relations": len(unmatched_relations),
    }
    return views, summary


def load_previous_entity_records(
    previous_backup: Path,
    entity: str,
) -> list[dict[str, Any]] | None:
    path = previous_backup / "entities" / f"{entity}.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or any(not isinstance(record, dict) for record in payload):
        raise RuntimeError(f"Backup anterior inválido: {path}.")
    return payload


def changed_record_fields(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> list[str]:
    keys = previous.keys() | current.keys()
    return sorted(
        key
        for key in keys
        if (key in previous) != (key in current) or previous.get(key) != current.get(key)
    )


def build_change_report(
    previous_backup: Path | None,
    records_by_entity: dict[str, list[dict[str, Any]]],
    *,
    created_at: str,
) -> dict[str, Any]:
    entity_reports: list[dict[str, Any]] = []
    total_created = 0
    total_modified = 0
    total_deleted = 0
    total_unchanged = 0
    for entity, current_records in records_by_entity.items():
        previous_records = (
            load_previous_entity_records(previous_backup, entity)
            if previous_backup is not None
            else None
        )
        if previous_records is None:
            entity_reports.append(
                {
                    "entity": entity,
                    "status": "baseline",
                    "current_records": len(current_records),
                    "created": [],
                    "modified": [],
                    "deleted": [],
                    "unchanged": 0,
                }
            )
            continue

        previous_by_id = {str(record["id"]): record for record in previous_records}
        current_by_id = {str(record["id"]): record for record in current_records}
        created = [
            current_by_id[record_id]
            for record_id in sorted(current_by_id.keys() - previous_by_id.keys())
        ]
        deleted = [
            previous_by_id[record_id]
            for record_id in sorted(previous_by_id.keys() - current_by_id.keys())
        ]
        modified = [
            {
                "id": record_id,
                "changed_fields": changed_record_fields(
                    previous_by_id[record_id], current_by_id[record_id]
                ),
                "previous": previous_by_id[record_id],
                "current": current_by_id[record_id],
            }
            for record_id in sorted(current_by_id.keys() & previous_by_id.keys())
            if current_by_id[record_id] != previous_by_id[record_id]
        ]
        unchanged = len(current_by_id.keys() & previous_by_id.keys()) - len(modified)
        total_created += len(created)
        total_modified += len(modified)
        total_deleted += len(deleted)
        total_unchanged += unchanged
        entity_reports.append(
            {
                "entity": entity,
                "status": "compared",
                "previous_records": len(previous_records),
                "current_records": len(current_records),
                "created": created,
                "modified": modified,
                "deleted": deleted,
                "unchanged": unchanged,
            }
        )

    return {
        "created_at": created_at,
        "previous_backup": previous_backup.name if previous_backup is not None else None,
        "baseline": previous_backup is None,
        "totals": {
            "created": total_created,
            "modified": total_modified,
            "deleted": total_deleted,
            "unchanged": total_unchanged,
        },
        "entities": entity_reports,
        "note": (
            "Cada alteração inclui o registo completo anterior e atual. "
            "O primeiro backup é a base; alterações anteriores a essa base não estão disponíveis."
        ),
    }


def create_backup(
    *,
    output_root: Path,
    entities: list[str],
    client_factory: Callable[[str], Base44Client],
    app_id: str,
    server_url: str,
    page_size: int,
    created_at: datetime | None = None,
    output_fn: Callable[[str], None] = print,
) -> tuple[Path, dict[str, Any]]:
    started = created_at or datetime.now(timezone.utc)
    timestamp = started.strftime("%Y%m%dT%H%M%SZ")
    output_root.mkdir(parents=True, exist_ok=True)
    output_root.chmod(0o700)
    previous_backup = previous_backup_directory(output_root)
    destination = unique_destination(output_root, timestamp)

    with tempfile.TemporaryDirectory(
        prefix=f".{destination.name}.partial-", dir=output_root
    ) as temporary_name:
        temporary = Path(temporary_name)
        temporary.chmod(0o700)
        entity_directory = temporary / "entities"
        entity_directory.mkdir()
        entity_directory.chmod(0o700)

        manifest_entities: list[dict[str, Any]] = []
        records_by_entity: dict[str, list[dict[str, Any]]] = {}
        total_records = 0
        output_fn(f"Backup Base44: {len(entities)} entidades")
        for index, entity in enumerate(entities, start=1):
            output_fn("\n" + "#" * 72)
            output_fn(f"ENTIDADE {index}/{len(entities)} — {entity}")
            output_fn("#" * 72)
            entity_started_at = iso_now()
            records, pages = fetch_entity_records(
                client_factory(entity),
                entity,
                page_size=page_size,
                output_fn=output_fn,
            )
            records_by_entity[entity] = records
            content = json_bytes(records)
            relative_path = Path("entities") / f"{entity}.json"
            target = temporary / relative_path
            write_private_file(target, content)
            checksum = sha256_bytes(content)
            verified_file(
                target,
                expected_sha256=checksum,
                expected_count=len(records),
            )
            total_records += len(records)
            manifest_entities.append(
                {
                    "entity": entity,
                    "file": relative_path.as_posix(),
                    "records": len(records),
                    "pages": pages,
                    "bytes": len(content),
                    "sha256": checksum,
                    "started_at": entity_started_at,
                    "completed_at": iso_now(),
                }
            )
            output_fn(f"{entity}: guardado e verificado — {len(records)} registos")

        completed_at = iso_now()
        view_directory = temporary / "views"
        view_directory.mkdir()
        view_directory.chmod(0o700)
        complete_views, relationship_summary = build_complete_item_views(records_by_entity)
        manifest_views: list[dict[str, Any]] = []
        for view_name, records in complete_views.items():
            relative_path = Path("views") / f"{view_name}-complete.json"
            content = json_bytes(records)
            target = temporary / relative_path
            write_private_file(target, content)
            checksum = sha256_bytes(content)
            verified_file(target, expected_sha256=checksum, expected_count=len(records))
            manifest_views.append(
                {
                    "view": view_name,
                    "file": relative_path.as_posix(),
                    "records": len(records),
                    "bytes": len(content),
                    "sha256": checksum,
                }
            )

        change_report = build_change_report(
            previous_backup,
            records_by_entity,
            created_at=completed_at,
        )
        change_report_relative_path = Path("changes-since-previous.json")
        change_report_content = json_bytes(change_report)
        change_report_checksum = sha256_bytes(change_report_content)
        change_report_path = temporary / change_report_relative_path
        write_private_file(change_report_path, change_report_content)
        loaded_change_report = json.loads(change_report_path.read_text(encoding="utf-8"))
        if loaded_change_report != change_report:
            raise RuntimeError("O relatório de alterações não ficou íntegro.")

        manifest = {
            "format_version": BACKUP_FORMAT_VERSION,
            "status": "complete",
            "created_at": started.isoformat(timespec="seconds"),
            "completed_at": completed_at,
            "source": {
                "service": "Base44",
                "server_url": server_url,
                "app_id": app_id,
            },
            "scope": {
                "entities": entities,
                "binary_assets_downloaded": False,
                "all_api_fields_preserved": True,
                "complete_item_views": True,
                "change_tracking_from_previous_backup": True,
                "note": (
                    "Todos os campos devolvidos pela API são preservados. Os URLs das "
                    "imagens são guardados; os ficheiros binários não são descarregados."
                ),
            },
            "totals": {
                "entities": len(manifest_entities),
                "records": total_records,
            },
            "entities": manifest_entities,
            "complete_item_views": manifest_views,
            "relationships": relationship_summary,
            "changes_since_previous": {
                "file": change_report_relative_path.as_posix(),
                "previous_backup": change_report["previous_backup"],
                "totals": change_report["totals"],
                "bytes": len(change_report_content),
                "sha256": change_report_checksum,
            },
        }
        manifest_content = json_bytes(manifest)
        manifest_path = temporary / "manifest.json"
        write_private_file(manifest_path, manifest_content)
        json.loads(manifest_path.read_text(encoding="utf-8"))

        checksum_lines = [
            f"{entry['sha256']}  {entry['file']}" for entry in manifest_entities
        ]
        checksum_lines.extend(
            f"{entry['sha256']}  {entry['file']}" for entry in manifest_views
        )
        checksum_lines.append(
            f"{change_report_checksum}  {change_report_relative_path.as_posix()}"
        )
        checksum_lines.append(
            f"{sha256_bytes(manifest_content)}  manifest.json"
        )
        write_private_file(
            temporary / "SHA256SUMS",
            ("\n".join(checksum_lines) + "\n").encode("utf-8"),
        )

        os.replace(temporary, destination)

    latest_path = output_root / "LATEST"
    write_private_file(latest_path, (destination.name + "\n").encode("utf-8"))
    output_fn("\n" + "=" * 72)
    output_fn("BACKUP CONCLUÍDO E VERIFICADO")
    output_fn("=" * 72)
    output_fn(f"Pasta: {destination}")
    output_fn(f"Entidades: {len(manifest_entities)}")
    output_fn(f"Registos: {total_records}")
    output_fn(f"Itens completos: {relationship_summary['items']}")
    output_fn(
        "Variantes associadas: "
        f"{relationship_summary['coin_variants']['linked']}/"
        f"{relationship_summary['coin_variants']['total']}"
    )
    output_fn(
        "Descobertas associadas: "
        f"{relationship_summary['coin_sightings']['linked']}/"
        f"{relationship_summary['coin_sightings']['total']}"
    )
    output_fn(
        "Alterações desde o backup anterior: "
        f"{change_report['totals']['created']} criadas · "
        f"{change_report['totals']['modified']} modificadas · "
        f"{change_report['totals']['deleted']} apagadas"
    )
    output_fn("Site Base44: nenhuma alteração efetuada.")
    return destination, manifest


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.page_size < 1 or args.page_size > 1000:
        raise ValueError("--page-size tem de estar entre 1 e 1000.")
    entities = selected_entities(args.entity, args.exclude_entity)
    load_dotenv()
    app_id = os.environ.get("BASE44_APP_ID", "")
    api_key = os.environ.get("BASE44_API_KEY", "")
    server_url = os.environ.get("BASE44_SERVER_URL", DEFAULT_BASE44_URL)
    if not app_id or not api_key:
        raise ValueError("Falta BASE44_APP_ID ou BASE44_API_KEY no environment/.env.")

    def client_factory(entity: str) -> Base44Client:
        return Base44Client(
            app_id=app_id,
            api_key=api_key,
            server_url=server_url,
            request_delay_seconds=args.request_delay,
            rate_limit_delay_seconds=args.rate_limit_delay,
            max_retries=args.max_retries,
            entity_name=entity,
        )

    create_backup(
        output_root=args.output_root,
        entities=entities,
        client_factory=client_factory,
        app_id=app_id,
        server_url=server_url,
        page_size=args.page_size,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        raise SystemExit(1)
