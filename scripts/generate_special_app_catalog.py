#!/usr/bin/env python3
"""Generate the public catalogue of USA collection coins from a verified backup.

The generated catalogue is the ``SpecialCoin`` equivalent of the normal
``app-catalog.json``.  It deliberately contains only descriptive catalogue
data.  API identifiers, ownership state, acquisition details, users and
sightings remain exclusively in the private Base44 backup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BACKUP_ROOT = PROJECT_ROOT / "backups"
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "info/paises/america/eua/special-app-catalog.json"
)
SOURCE_EXPORT = Path("america/eua/collection.json")
COUNTRY_NAME = "Estados Unidos da América"
SITE_COUNTRY = "EUA"
CONTINENT = "América"
CATALOG_FORMAT_VERSION = 1

REQUIRED_COIN_FIELDS = {
    "name",
    "commemorative_name",
    "year",
    "ordem",
    "url_ucoin",
    "image_frente",
    "image_verso",
    "notes",
    "hidden",
    "has_variants",
}


class SpecialCatalogError(RuntimeError):
    """Raised when a safe, complete catalogue cannot be generated."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Gera special-app-catalog.json a partir do último backup completo "
            "do Site Base44, sem incluir dados pessoais da coleção."
        )
    )
    parser.add_argument(
        "--backup",
        type=Path,
        help="Pasta de backup; por omissão usa backups/LATEST.",
    )
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Confirma que o catálogo versionado está atualizado sem o alterar.",
    )
    return parser.parse_args(argv)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpecialCatalogError(f"JSON inválido ou ilegível: {path}") from exc


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_backup_directory(
    *, backup: Path | None, backup_root: Path
) -> Path:
    if backup is not None:
        destination = backup.resolve()
    else:
        pointer = backup_root / "LATEST"
        try:
            backup_name = pointer.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SpecialCatalogError(
                f"Não foi possível ler o ponteiro do backup: {pointer}"
            ) from exc
        if not backup_name or Path(backup_name).name != backup_name:
            raise SpecialCatalogError(f"Ponteiro de backup inválido: {pointer}")
        destination = (backup_root / backup_name).resolve()

    if not destination.is_dir():
        raise SpecialCatalogError(f"A pasta de backup não existe: {destination}")
    return destination


def parse_checksums(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SpecialCatalogError(f"Falta o ficheiro de checksums: {path}") from exc
    for line in lines:
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2 or len(parts[0]) != 64 or not parts[1]:
            raise SpecialCatalogError(f"Checksum inválido em {path}: {line!r}")
        digest, relative_path = parts
        if relative_path in checksums:
            raise SpecialCatalogError(
                f"Caminho repetido no ficheiro de checksums: {relative_path}"
            )
        checksums[relative_path] = digest
    return checksums


def load_verified_export(backup_directory: Path) -> list[dict[str, Any]]:
    manifest_path = backup_directory / "manifest.json"
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("status") != "complete":
        raise SpecialCatalogError("O backup não está marcado como completo.")

    scope = manifest.get("scope")
    if not isinstance(scope, dict) or not scope.get("complete_item_views"):
        raise SpecialCatalogError("O backup não garante relações completas.")

    exports = manifest.get("country_exports")
    if not isinstance(exports, list):
        raise SpecialCatalogError("O backup não contém country_exports.")
    matches = [
        item
        for item in exports
        if isinstance(item, dict) and item.get("file") == SOURCE_EXPORT.as_posix()
    ]
    if len(matches) != 1:
        raise SpecialCatalogError(
            f"Era esperado exatamente um export {SOURCE_EXPORT}; encontrados: {len(matches)}."
        )
    metadata = matches[0]
    if (
        metadata.get("country") != SITE_COUNTRY
        or metadata.get("continent") != CONTINENT
        or metadata.get("kind") != "collection"
    ):
        raise SpecialCatalogError("Os metadados do catálogo dos EUA não são canónicos.")

    source_path = backup_directory / SOURCE_EXPORT
    if not source_path.is_file():
        raise SpecialCatalogError(f"Falta o export de coleção dos EUA: {source_path}")
    digest = sha256_file(source_path)
    if metadata.get("sha256") != digest:
        raise SpecialCatalogError("O checksum do export não corresponde ao manifest.")
    checksums = parse_checksums(backup_directory / "SHA256SUMS")
    if checksums.get(SOURCE_EXPORT.as_posix()) != digest:
        raise SpecialCatalogError("O checksum do export não corresponde a SHA256SUMS.")

    entries = read_json(source_path)
    if not isinstance(entries, list) or not entries:
        raise SpecialCatalogError("O export de coleção dos EUA está vazio ou é inválido.")
    if metadata.get("records") != len(entries):
        raise SpecialCatalogError("A contagem do export não corresponde ao manifest.")
    return entries


def _required_text(record: dict[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SpecialCatalogError(
            f"Campo obrigatório ausente em SpecialCoin: {field}={value!r}"
        )
    return value.strip()


def _coin_sort_key(coin: dict[str, Any]) -> tuple[Any, ...]:
    return (
        coin["order"],
        coin["year"],
        coin["commemorativeName"].casefold(),
        coin["denomination"].casefold(),
    )


def build_catalog(entries: list[dict[str, Any]]) -> dict[str, Any]:
    programmes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    identities: set[tuple[str, str, str]] = set()
    urls: set[str] = set()
    parent_ids: set[str] = set()
    variant_count = 0

    for entry in entries:
        if not isinstance(entry, dict) or entry.get("entity") != "SpecialCoin":
            raise SpecialCatalogError("O export contém uma entrada que não é SpecialCoin.")
        record = entry.get("record")
        variants = entry.get("variants")
        if not isinstance(record, dict) or not isinstance(variants, list):
            raise SpecialCatalogError("SpecialCoin sem record ou variants válidos.")
        missing_fields = sorted(REQUIRED_COIN_FIELDS - record.keys())
        if missing_fields:
            raise SpecialCatalogError(
                "SpecialCoin sem campos obrigatórios: " + ", ".join(missing_fields)
            )
        if record.get("country") != SITE_COUNTRY or record.get("continent") != CONTINENT:
            raise SpecialCatalogError("O export contém uma SpecialCoin de outro país.")

        parent_id = _required_text(record, "id")
        if parent_id in parent_ids:
            raise SpecialCatalogError(f"ID de SpecialCoin repetido: {parent_id}")
        parent_ids.add(parent_id)

        denomination = _required_text(record, "name")
        commemorative_name = _required_text(record, "commemorative_name")
        year = _required_text(record, "year")
        programme = _required_text(record, "notes")
        detail_url = _required_text(record, "url_ucoin")
        obverse_image = _required_text(record, "image_frente")
        reverse_image = _required_text(record, "image_verso")
        order = record.get("ordem")
        if not isinstance(order, int):
            raise SpecialCatalogError(f"Ordem inválida em {denomination}: {order!r}")

        identity = (denomination, commemorative_name, year)
        if identity in identities:
            raise SpecialCatalogError(f"Moeda especial repetida: {identity}")
        identities.add(identity)
        if detail_url in urls:
            raise SpecialCatalogError(f"URL uCoin repetido: {detail_url}")
        urls.add(detail_url)

        public_variants: list[dict[str, Any]] = []
        tags: set[str] = set()
        for variant in variants:
            if not isinstance(variant, dict):
                raise SpecialCatalogError(f"Variante inválida em {identity}.")
            if variant.get("special_coin_id") != parent_id:
                raise SpecialCatalogError(
                    f"Variante associada ao pai errado em {identity}."
                )
            tag = _required_text(variant, "tag")
            variant_order = variant.get("ordem")
            if tag in tags:
                raise SpecialCatalogError(f"Variante repetida em {identity}: {tag}")
            if not isinstance(variant_order, int):
                raise SpecialCatalogError(
                    f"Ordem de variante inválida em {identity} / {tag}."
                )
            tags.add(tag)
            public_variants.append({"tag": tag, "order": variant_order})
        public_variants.sort(key=lambda item: (item["order"], item["tag"].casefold()))
        if bool(record.get("has_variants")) != bool(public_variants):
            raise SpecialCatalogError(
                f"has_variants não corresponde às variantes em {identity}."
            )
        variant_count += len(public_variants)

        programmes[programme].append(
            {
                "denomination": denomination,
                "commemorativeName": commemorative_name,
                "year": year,
                "order": order,
                "detailUrl": detail_url,
                "numistaUrl": record.get("url_numista"),
                "obverseImage": obverse_image,
                "reverseImage": reverse_image,
                "hidden": bool(record.get("hidden")),
                "hasVariants": bool(public_variants),
                "variants": public_variants,
            }
        )

    public_programmes: list[dict[str, Any]] = []
    for title, coins in programmes.items():
        coins.sort(key=_coin_sort_key)
        public_programmes.append({"title": title, "coins": coins})
    public_programmes.sort(
        key=lambda item: (
            min(coin["order"] for coin in item["coins"]),
            item["title"].casefold(),
        )
    )

    return {
        "formatVersion": CATALOG_FORMAT_VERSION,
        "catalogType": "special",
        "country": COUNTRY_NAME,
        "siteCountry": SITE_COUNTRY,
        "continent": CONTINENT,
        "programmes": public_programmes,
        "summary": {
            "programmes": len(public_programmes),
            "coins": len(identities),
            "variants": variant_count,
        },
    }


def write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    backup_directory = resolve_backup_directory(
        backup=args.backup, backup_root=args.backup_root
    )
    catalog = build_catalog(load_verified_export(backup_directory))
    content = json_bytes(catalog)

    if args.check:
        if not args.output.is_file() or args.output.read_bytes() != content:
            print(f"Desatualizado: {args.output}")
            return 1
        print(
            f"Atualizado: {args.output} — {catalog['summary']['coins']} moedas, "
            f"{catalog['summary']['variants']} variantes"
        )
        return 0

    write_atomic(args.output, content)
    print(
        f"Criado: {args.output} — {catalog['summary']['coins']} moedas, "
        f"{catalog['summary']['variants']} variantes, "
        f"{catalog['summary']['programmes']} programas"
    )
    print("Dados pessoais, estados de posse e sinalizações: não incluídos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
