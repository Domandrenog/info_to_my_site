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


BACKUP_FORMAT_VERSION = 1
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
            if record_id and record_id in seen_ids:
                raise RuntimeError(
                    f"{entity}: o registo {record_id} apareceu em mais de uma página."
                )
            if record_id:
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
                "note": "Os URLs das imagens são preservados; os ficheiros binários não são descarregados.",
            },
            "totals": {
                "entities": len(manifest_entities),
                "records": total_records,
            },
            "entities": manifest_entities,
        }
        manifest_content = json_bytes(manifest)
        manifest_path = temporary / "manifest.json"
        write_private_file(manifest_path, manifest_content)
        json.loads(manifest_path.read_text(encoding="utf-8"))

        checksum_lines = [
            f"{entry['sha256']}  {entry['file']}" for entry in manifest_entities
        ]
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
