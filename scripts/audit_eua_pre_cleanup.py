#!/usr/bin/env python3
"""Audit the complete USA Coin -> SpecialCoin migration before cleanup.

The audit is intentionally offline: it reads a verified ``backup.py`` snapshot,
the approved catalogues and the migration checkpoints.  It never loads API
credentials and has no mutation or deletion mode.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.import_base44_coins import flatten_coins, to_coin_record


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BACKUP_ROOT = PROJECT_ROOT / "backups"
DEFAULT_MIGRATION_ROOT = DEFAULT_BACKUP_ROOT / "migrations"
DEFAULT_NORMAL_CATALOG = (
    PROJECT_ROOT / "info/paises/america/eua/app-catalog.json"
)
DEFAULT_MISSING_CATALOG = (
    PROJECT_ROOT / "info/paises/america/eua/special-coins-missing-approved.json"
)
DEFAULT_ASSOCIATIONS = (
    PROJECT_ROOT
    / "info/paises/america/eua/normal-coin-associations-approved.json"
)
DEFAULT_PERIOD_CORRECTIONS = (
    PROJECT_ROOT
    / "info/paises/america/eua/normal-coin-period-corrections-approved.json"
)
DEFAULT_MISSING_STATE = (
    DEFAULT_MIGRATION_ROOT / "eua-missing-special-coins-execution-state.json"
)
SERVER_FIELDS = {
    "id",
    "created_date",
    "updated_date",
    "created_by_id",
    "created_by",
    "is_sample",
}
SIGHTING_RELATIONAL_FIELDS = {
    "coin_id",
    "item_id",
    "item_type",
    "coin_name",
    "years",
}


class AuditError(RuntimeError):
    """Raised when audit inputs are missing or structurally invalid."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audita offline as moedas normais, coleção, variantes e "
            "sinalizações dos EUA antes de qualquer limpeza."
        )
    )
    parser.add_argument(
        "--backup",
        type=Path,
        default=None,
        help="Pasta de backup; por omissão usa backups/LATEST.",
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        default=None,
        help="Pasta da migração original; por omissão usa backups/migrations/LATEST.",
    )
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args(argv)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AuditError(f"Ficheiro em falta: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AuditError(f"JSON inválido: {path}: {exc}") from exc


def resolve_pointer(root: Path, pointer_name: str = "LATEST") -> Path:
    pointer = root / pointer_name
    try:
        name = pointer.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise AuditError(f"Ponteiro em falta: {pointer}") from exc
    if not name or Path(name).name != name:
        raise AuditError(f"Ponteiro inválido: {pointer}")
    destination = root / name
    if not destination.is_dir():
        raise AuditError(f"A pasta indicada por {pointer} não existe: {destination}")
    return destination


def entity_records(backup: Path, entity: str) -> list[dict[str, Any]]:
    payload = load_json(backup / "entities" / f"{entity}.json")
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise AuditError(f"Entidade inválida no backup: {entity}")
    return payload


def record_id(record: dict[str, Any]) -> str:
    return str(record.get("id") or "").strip()


def index_by_id(records: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        key = record_id(record)
        if not key:
            raise AuditError(f"{label}: registo sem ID")
        if key in result:
            raise AuditError(f"{label}: ID repetido: {key}")
        result[key] = record
    return result


def meaningful_differences(
    expected: dict[str, Any],
    actual: dict[str, Any],
    *,
    excluded: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    ignored = SERVER_FIELDS | (excluded or set())
    return {
        field: {"expected": value, "actual": actual.get(field)}
        for field, value in expected.items()
        if field not in ignored and actual.get(field) != value
    }


def unique_normal_records(catalog: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    options = {"country": "EUA", "continent": "América", "condition": "Não Tenho"}
    records = [
        to_coin_record(entry, options, position)
        for position, entry in enumerate(flatten_coins(catalog), start=1)
    ]
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        url = str(record.get("url_ucoin") or "").strip()
        if not url:
            raise AuditError("O catálogo normal dos EUA contém um URL uCoin vazio.")
        if url in seen:
            continue
        seen.add(url)
        unique.append(record)
    return unique, len(records) - len(unique)


def approved_normal_updates() -> dict[str, dict[str, Any]]:
    updates: dict[str, dict[str, Any]] = defaultdict(dict)
    for path in (DEFAULT_ASSOCIATIONS, DEFAULT_PERIOD_CORRECTIONS):
        payload = load_json(path)
        for item in payload.get("updates", []):
            updates[str(item["record_id"])].update(item.get("set", {}))
    return dict(updates)


def add_check(
    checks: list[dict[str, Any]],
    blockers: list[str],
    *,
    name: str,
    ok: bool,
    expected: Any,
    actual: Any,
    detail: Any = None,
) -> None:
    entry = {
        "name": name,
        "status": "ok" if ok else "blocked",
        "expected": expected,
        "actual": actual,
    }
    if detail not in (None, [], {}):
        entry["detail"] = detail
    checks.append(entry)
    if not ok:
        blockers.append(name)


def audit(
    *,
    backup: Path,
    artifact: Path,
) -> dict[str, Any]:
    migration_manifest = load_json(artifact / "manifest.json")
    migration_state = load_json(artifact / "execution-state.json")
    missing_catalog = load_json(DEFAULT_MISSING_CATALOG)
    missing_state = load_json(DEFAULT_MISSING_STATE)
    normal_catalog = load_json(DEFAULT_NORMAL_CATALOG)

    coins = entity_records(backup, "Coin")
    coin_variants = entity_records(backup, "CoinVariant")
    special_coins = entity_records(backup, "SpecialCoin")
    special_variants = entity_records(backup, "SpecialCoinVariant")
    sightings = entity_records(backup, "CoinSighting")
    coin_by_id = index_by_id(coins, "Coin")
    variant_by_id = index_by_id(coin_variants, "CoinVariant")
    special_by_id = index_by_id(special_coins, "SpecialCoin")
    special_variant_by_id = index_by_id(special_variants, "SpecialCoinVariant")
    sighting_by_id = index_by_id(sightings, "CoinSighting")

    checks: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []

    migration_items = migration_manifest.get("items", [])
    source_coin_ids = {str(item["source_coin_id"]) for item in migration_items}
    source_live_ids = source_coin_ids & coin_by_id.keys()
    missing_source_ids = sorted(source_coin_ids - coin_by_id.keys())
    add_check(
        checks,
        blockers,
        name="238 Coin comemorativas originais preservadas",
        ok=len(source_coin_ids) == 238 and not missing_source_ids,
        expected=238,
        actual=len(source_live_ids),
        detail={"missing_ids": missing_source_ids},
    )

    source_coin_differences: dict[str, Any] = {}
    allowed_source_cache_changes: dict[str, Any] = {}
    for item in migration_items:
        source_id = str(item["source_coin_id"])
        actual = coin_by_id.get(source_id)
        if actual is None:
            continue
        differences = meaningful_differences(item["source_record"], actual)
        checkpoint = migration_state.get("created_special_coins", {}).get(source_id, {})
        target_id = str(checkpoint.get("target_special_coin_id") or "")
        target = special_by_id.get(target_id, {})
        has_migrated_sighting = any(
            str(sighting_checkpoint.get("source_coin_id") or "") == source_id
            for sighting_checkpoint in migration_state.get("updated_sightings", {}).values()
        )
        safe_cache_change = (
            set(differences) == {"condition"}
            and differences["condition"] == {
                "expected": "Por Entregar",
                "actual": "Não Tenho",
            }
            and target.get("condition") == "Por Entregar"
            and has_migrated_sighting
        )
        if safe_cache_change:
            allowed_source_cache_changes[source_id] = differences
            continue
        if differences:
            source_coin_differences[source_id] = differences
    add_check(
        checks,
        blockers,
        name="dados das Coin antigas intactos salvo cache esperado",
        ok=not source_coin_differences,
        expected=0,
        actual=len(source_coin_differences),
        detail={
            "unexpected_differences": source_coin_differences,
            "allowed_cache_changes": allowed_source_cache_changes,
        },
    )

    normal_records, duplicate_catalog_entries = unique_normal_records(normal_catalog)
    normal_urls = {str(record["url_ucoin"]): record for record in normal_records}
    live_coin_by_url: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in coins:
        if str(record.get("country") or "") != "EUA":
            continue
        url = str(record.get("url_ucoin") or "").strip()
        if url:
            live_coin_by_url[url].append(record)
    normal_matches = {
        url: live_coin_by_url.get(url, [])
        for url in normal_urls
    }
    missing_normal_urls = sorted(url for url, matches in normal_matches.items() if not matches)
    duplicate_normal_urls = {
        url: [record_id(record) for record in matches]
        for url, matches in normal_matches.items()
        if len(matches) > 1
    }
    normal_coin_ids = {
        record_id(matches[0])
        for matches in normal_matches.values()
        if len(matches) == 1
    }
    add_check(
        checks,
        blockers,
        name="30 tipos normais uCoin associados uma única vez",
        ok=(
            len(normal_records) == 30
            and not missing_normal_urls
            and not duplicate_normal_urls
            and len(normal_coin_ids) == 30
        ),
        expected=30,
        actual=len(normal_coin_ids),
        detail={
            "missing_urls": missing_normal_urls,
            "duplicate_urls": duplicate_normal_urls,
            "repeated_catalog_entries_ignored": duplicate_catalog_entries,
        },
    )

    excluded_normals = migration_manifest.get("excluded_normal_coins", [])
    old_normal_by_id = {
        record_id(item["record"]): item for item in excluded_normals
    }
    approved_updates = approved_normal_updates()
    old_normal_differences: dict[str, Any] = {}
    for old_id, item in old_normal_by_id.items():
        actual = coin_by_id.get(old_id)
        if actual is None:
            old_normal_differences[old_id] = {"record": "missing"}
            continue
        expected = dict(item["record"])
        expected.update(approved_updates.get(old_id, {}))
        differences = meaningful_differences(expected, actual)
        if differences:
            old_normal_differences[old_id] = differences
    add_check(
        checks,
        blockers,
        name="12 Coin normais antigas preservadas com correções aprovadas",
        ok=len(old_normal_by_id) == 12 and not old_normal_differences,
        expected=12,
        actual=sum(1 for old_id in old_normal_by_id if old_id in coin_by_id),
        detail=old_normal_differences,
    )

    old_normal_ids = set(old_normal_by_id)
    new_normal_ids = normal_coin_ids - old_normal_ids
    new_normal_differences: dict[str, Any] = {}
    for url, expected in normal_urls.items():
        matches = normal_matches[url]
        if len(matches) != 1:
            continue
        actual = matches[0]
        if record_id(actual) not in new_normal_ids:
            continue
        differences = meaningful_differences(expected, actual)
        if differences:
            new_normal_differences[record_id(actual)] = differences
    add_check(
        checks,
        blockers,
        name="18 Coin normais novas correspondem exatamente ao catálogo",
        ok=len(new_normal_ids) == 18 and not new_normal_differences,
        expected=18,
        actual=len(new_normal_ids),
        detail=new_normal_differences,
    )

    usa_coin_ids = {
        record_id(record)
        for record in coins
        if str(record.get("country") or "") == "EUA"
    }
    expected_usa_coin_ids = source_coin_ids | normal_coin_ids
    unexpected_usa_coin_ids = sorted(usa_coin_ids - expected_usa_coin_ids)
    missing_usa_coin_ids = sorted(expected_usa_coin_ids - usa_coin_ids)
    add_check(
        checks,
        blockers,
        name="conjunto Coin EUA sem registos extra ou em falta",
        ok=(
            len(usa_coin_ids) == 268
            and not unexpected_usa_coin_ids
            and not missing_usa_coin_ids
        ),
        expected=268,
        actual=len(usa_coin_ids),
        detail={
            "unexpected_ids": unexpected_usa_coin_ids,
            "missing_ids": missing_usa_coin_ids,
        },
    )

    migrated_parent_map = {
        str(source_id): str(checkpoint.get("target_special_coin_id") or "")
        for source_id, checkpoint in migration_state.get(
            "created_special_coins", {}
        ).items()
    }
    migrated_target_ids = set(migrated_parent_map.values()) - {""}
    missing_parent_map = {
        str(key): str(checkpoint.get("id") or "")
        for key, checkpoint in missing_state.get("special_coins", {}).items()
    }
    additional_target_ids = set(missing_parent_map.values()) - {""}
    expected_special_ids = migrated_target_ids | additional_target_ids
    usa_special_ids = {
        record_id(record)
        for record in special_coins
        if str(record.get("country") or "") == "EUA"
    }
    add_check(
        checks,
        blockers,
        name="245 SpecialCoin EUA exatas",
        ok=(
            len(migrated_target_ids) == 238
            and len(additional_target_ids) == 7
            and usa_special_ids == expected_special_ids
        ),
        expected={"migrated": 238, "additional": 7, "total": 245},
        actual={
            "migrated": len(migrated_target_ids & usa_special_ids),
            "additional": len(additional_target_ids & usa_special_ids),
            "total": len(usa_special_ids),
        },
        detail={
            "unexpected_ids": sorted(usa_special_ids - expected_special_ids),
            "missing_ids": sorted(expected_special_ids - usa_special_ids),
        },
    )

    migrated_parent_differences: dict[str, Any] = {}
    for item in migration_items:
        target_id = migrated_parent_map.get(str(item["source_coin_id"]), "")
        actual = special_by_id.get(target_id)
        if actual is None:
            continue
        differences = meaningful_differences(
            item["target_special_coin_payload"],
            actual,
            excluded={"url_ucoin", "image_frente", "image_verso"},
        )
        if differences:
            migrated_parent_differences[target_id] = differences
    add_check(
        checks,
        blockers,
        name="estado e dados pessoais dos 238 SpecialCoin preservados",
        ok=not migrated_parent_differences,
        expected=0,
        actual=len(migrated_parent_differences),
        detail=migrated_parent_differences,
    )

    missing_items_by_key = {
        str(item["key"]): item for item in missing_catalog.get("items", [])
    }
    additional_parent_differences: dict[str, Any] = {}
    for key, target_id in missing_parent_map.items():
        item = missing_items_by_key.get(key)
        actual = special_by_id.get(target_id)
        if item is None or actual is None:
            continue
        differences = meaningful_differences(
            item["payload"],
            actual,
            excluded={"image_frente", "image_verso"},
        )
        if differences:
            additional_parent_differences[target_id] = differences
    add_check(
        checks,
        blockers,
        name="7 SpecialCoin adicionais correspondem ao catálogo aprovado",
        ok=len(missing_items_by_key) == 7 and not additional_parent_differences,
        expected=7,
        actual=len(additional_target_ids & usa_special_ids),
        detail=additional_parent_differences,
    )

    special_urls: dict[str, list[str]] = defaultdict(list)
    special_image_issues: dict[str, Any] = {}
    for target_id in sorted(usa_special_ids):
        record = special_by_id[target_id]
        url = str(record.get("url_ucoin") or "").strip()
        if url:
            special_urls[url].append(target_id)
        front = str(record.get("image_frente") or "")
        back = str(record.get("image_verso") or "")
        issues = []
        if "/fotos/paises/America/EUA/collection/frente/" not in front:
            issues.append("image_frente não aponta para collection/frente")
        if "/fotos/paises/America/EUA/collection/tras/" not in back:
            issues.append("image_verso não aponta para collection/tras")
        if issues:
            special_image_issues[target_id] = issues
    empty_special_urls = sorted(
        target_id
        for target_id in usa_special_ids
        if not str(special_by_id[target_id].get("url_ucoin") or "").strip()
    )
    duplicate_special_urls = {
        url: ids for url, ids in special_urls.items() if len(ids) > 1
    }
    add_check(
        checks,
        blockers,
        name="URLs uCoin e fotografias das 245 SpecialCoin completos",
        ok=(
            len(special_urls) == 245
            and not empty_special_urls
            and not duplicate_special_urls
            and not special_image_issues
        ),
        expected={"unique_ucoin_urls": 245, "photo_pairs": 245},
        actual={
            "unique_ucoin_urls": len(special_urls),
            "photo_pairs_without_issues": 245 - len(special_image_issues),
        },
        detail={
            "empty_url_ids": empty_special_urls,
            "duplicate_urls": duplicate_special_urls,
            "image_issues": special_image_issues,
        },
    )

    source_variants = [
        variant_plan["source_record"]
        for item in migration_items
        for variant_plan in item.get("variants", [])
    ]
    source_variant_ids = {record_id(variant) for variant in source_variants}
    source_variant_differences: dict[str, Any] = {}
    allowed_source_variant_cache_changes: dict[str, Any] = {}
    for expected in source_variants:
        variant_id = record_id(expected)
        actual = variant_by_id.get(variant_id)
        if actual is None:
            source_variant_differences[variant_id] = {"record": "missing"}
            continue
        differences = meaningful_differences(expected, actual)
        source_parent_id = str(expected.get("coin_id") or "")
        target_parent_id = migrated_parent_map.get(source_parent_id, "")
        target_parent = special_by_id.get(target_parent_id, {})
        matching_pending_sighting = any(
            (
                str(checkpoint.get("source_coin_id") or "") == source_parent_id
                and str(checkpoint.get("source_record", {}).get("variant_tag") or "")
                == str(expected.get("tag") or "")
                and str(checkpoint.get("source_record", {}).get("status") or "")
                == "por_entregar"
            )
            for checkpoint in migration_state.get("updated_sightings", {}).values()
        )
        safe_cache_change = (
            set(differences) == {"condition"}
            and differences["condition"]
            == {"expected": "Tenho", "actual": "Não Tenho"}
            and target_parent.get("condition") == "Por Entregar"
            and matching_pending_sighting
        )
        if safe_cache_change:
            allowed_source_variant_cache_changes[variant_id] = differences
            continue
        if differences:
            source_variant_differences[variant_id] = differences
    add_check(
        checks,
        blockers,
        name="274 CoinVariant comemorativas originais intactas",
        ok=len(source_variant_ids) == 274 and not source_variant_differences,
        expected=274,
        actual=sum(1 for variant_id in source_variant_ids if variant_id in variant_by_id),
        detail={
            "unexpected_differences": source_variant_differences,
            "allowed_cache_changes": allowed_source_variant_cache_changes,
        },
    )

    normal_variants = [
        variant for item in excluded_normals for variant in item.get("variants", [])
    ]
    normal_variant_ids = {record_id(variant) for variant in normal_variants}
    normal_variant_differences: dict[str, Any] = {}
    for expected in normal_variants:
        variant_id = record_id(expected)
        actual = variant_by_id.get(variant_id)
        if actual is None:
            normal_variant_differences[variant_id] = {"record": "missing"}
            continue
        differences = meaningful_differences(expected, actual)
        if differences:
            normal_variant_differences[variant_id] = differences
    usa_coin_variant_ids = {
        record_id(variant)
        for variant in coin_variants
        if str(variant.get("coin_id") or "") in usa_coin_ids
    }
    expected_usa_coin_variant_ids = source_variant_ids | normal_variant_ids
    add_check(
        checks,
        blockers,
        name="CoinVariant normais preservadas e conjunto EUA fechado",
        ok=(
            not normal_variant_differences
            and usa_coin_variant_ids == expected_usa_coin_variant_ids
        ),
        expected=len(expected_usa_coin_variant_ids),
        actual=len(usa_coin_variant_ids),
        detail={
            "normal_variant_differences": normal_variant_differences,
            "unexpected_ids": sorted(
                usa_coin_variant_ids - expected_usa_coin_variant_ids
            ),
            "missing_ids": sorted(
                expected_usa_coin_variant_ids - usa_coin_variant_ids
            ),
        },
    )

    migrated_variant_state = migration_state.get("created_special_coin_variants", {})
    migrated_special_variant_ids = {
        str(checkpoint.get("target_special_coin_variant_id") or "")
        for checkpoint in migrated_variant_state.values()
    } - {""}
    additional_variant_state = missing_state.get("special_coin_variants", {})
    additional_special_variant_ids = {
        str(checkpoint.get("id") or "")
        for checkpoint in additional_variant_state.values()
    } - {""}
    expected_special_variant_ids = (
        migrated_special_variant_ids | additional_special_variant_ids
    )
    usa_special_variant_ids = {
        record_id(variant)
        for variant in special_variants
        if str(variant.get("special_coin_id") or "") in usa_special_ids
    }
    special_variant_differences: dict[str, Any] = {}
    allowed_special_variant_cache_changes: dict[str, Any] = {}
    for checkpoint in [
        *migrated_variant_state.values(),
        *additional_variant_state.values(),
    ]:
        variant_id = str(
            checkpoint.get("target_special_coin_variant_id")
            or checkpoint.get("id")
            or ""
        )
        actual = special_variant_by_id.get(variant_id)
        expected = checkpoint.get("verified_record", {})
        if actual is None:
            special_variant_differences[variant_id] = {"record": "missing"}
            continue
        differences = meaningful_differences(expected, actual)
        parent_id = str(actual.get("special_coin_id") or "")
        parent = special_by_id.get(parent_id, {})
        matching_pending_sighting = any(
            (
                str(sighting.get("item_id") or sighting.get("coin_id") or "")
                == parent_id
                and str(sighting.get("variant_tag") or "")
                == str(actual.get("tag") or "")
                and str(sighting.get("status") or "") == "por_entregar"
            )
            for sighting in sightings
        )
        safe_cache_change = (
            set(differences) == {"condition"}
            and differences["condition"]
            == {"expected": "Tenho", "actual": "Não Tenho"}
            and parent.get("condition") == "Por Entregar"
            and matching_pending_sighting
        )
        if safe_cache_change:
            allowed_special_variant_cache_changes[variant_id] = differences
            continue
        if differences:
            special_variant_differences[variant_id] = differences
    add_check(
        checks,
        blockers,
        name="298 SpecialCoinVariant exatas e ligadas aos pais EUA",
        ok=(
            len(migrated_special_variant_ids) == 274
            and len(additional_special_variant_ids) == 24
            and usa_special_variant_ids == expected_special_variant_ids
            and not special_variant_differences
        ),
        expected={"migrated": 274, "additional": 24, "total": 298},
        actual={
            "migrated": len(migrated_special_variant_ids & usa_special_variant_ids),
            "additional": len(additional_special_variant_ids & usa_special_variant_ids),
            "total": len(usa_special_variant_ids),
        },
        detail={
            "differences": special_variant_differences,
            "allowed_cache_changes": allowed_special_variant_cache_changes,
            "unexpected_ids": sorted(
                usa_special_variant_ids - expected_special_variant_ids
            ),
            "missing_ids": sorted(
                expected_special_variant_ids - usa_special_variant_ids
            ),
        },
    )

    migrated_sighting_state = migration_state.get("updated_sightings", {})
    migrated_sighting_differences: dict[str, Any] = {}
    for sighting_id, checkpoint in migrated_sighting_state.items():
        actual = sighting_by_id.get(str(sighting_id))
        if actual is None:
            migrated_sighting_differences[str(sighting_id)] = {"record": "missing"}
            continue
        differences: dict[str, Any] = {}
        for field, expected in checkpoint.get("target_patch", {}).items():
            if actual.get(field) != expected:
                differences[field] = {"expected": expected, "actual": actual.get(field)}
        source = checkpoint.get("source_record", {})
        preserved = meaningful_differences(
            source,
            actual,
            excluded=SIGHTING_RELATIONAL_FIELDS | {"updated_date"},
        )
        differences.update(preserved)
        if differences:
            migrated_sighting_differences[str(sighting_id)] = differences
    add_check(
        checks,
        blockers,
        name="112 CoinSighting comemorativas religadas com estado preservado",
        ok=len(migrated_sighting_state) == 112 and not migrated_sighting_differences,
        expected=112,
        actual=sum(
            1 for sighting_id in migrated_sighting_state if sighting_id in sighting_by_id
        ),
        detail=migrated_sighting_differences,
    )

    normal_sightings = [
        sighting for item in excluded_normals for sighting in item.get("sightings", [])
    ]
    normal_sighting_ids = {record_id(sighting) for sighting in normal_sightings}
    normal_sighting_differences: dict[str, Any] = {}
    for expected in normal_sightings:
        sighting_id = record_id(expected)
        actual = sighting_by_id.get(sighting_id)
        if actual is None:
            normal_sighting_differences[sighting_id] = {"record": "missing"}
            continue
        differences = meaningful_differences(expected, actual)
        if differences:
            normal_sighting_differences[sighting_id] = differences
    add_check(
        checks,
        blockers,
        name="CoinSighting das moedas normais antigas intactas",
        ok=not normal_sighting_differences,
        expected=len(normal_sighting_ids),
        actual=sum(1 for sighting_id in normal_sighting_ids if sighting_id in sighting_by_id),
        detail=normal_sighting_differences,
    )

    source_sighting_references = sorted(
        record_id(sighting)
        for sighting in sightings
        if str(sighting.get("coin_id") or "") in source_coin_ids
        or str(sighting.get("item_id") or "") in source_coin_ids
    )
    add_check(
        checks,
        blockers,
        name="nenhuma CoinSighting ainda aponta para as 238 Coin antigas",
        ok=not source_sighting_references,
        expected=0,
        actual=len(source_sighting_references),
        detail={"sighting_ids": source_sighting_references},
    )

    unmatched_path = backup / "views/unmatched-relations-complete.json"
    unmatched = load_json(unmatched_path) if unmatched_path.exists() else []
    if unmatched:
        warnings.append(
            f"O backup contém {len(unmatched)} relação(ões) órfã(s) global(is); "
            "não pertencem automaticamente ao lote de limpeza dos EUA."
        )
    if allowed_source_cache_changes:
        warnings.append(
            f"{len(allowed_source_cache_changes)} Coin antiga(s) passaram de "
            "Por Entregar para Não Tenho depois de as sinalizações serem "
            "religadas; os SpecialCoin correspondentes continuam Por Entregar."
        )
    if allowed_special_variant_cache_changes:
        warnings.append(
            "A variante Yosemite P está Não Tenho, enquanto o pai e a "
            "sinalização preservam Por Entregar; não foi restaurado o antigo "
            "valor incorreto Tenho."
        )
    if allowed_source_variant_cache_changes:
        warnings.append(
            "A CoinVariant antiga Yosemite P também ficou Não Tenho depois da "
            "religação; o estado Por Entregar permanece no SpecialCoin e na "
            "sinalização correspondente."
        )

    cleanup_candidates = {
        "coin_ids": sorted(source_coin_ids),
        "coin_variant_ids": sorted(source_variant_ids),
    }
    return {
        "format_version": 1,
        "kind": "eua_pre_cleanup_audit",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "ready_for_cleanup_review" if not blockers else "blocked",
        "backup": str(backup.relative_to(PROJECT_ROOT)),
        "migration_artifact": str(artifact.relative_to(PROJECT_ROOT)),
        "deletion_performed": False,
        "deletion_authorized": False,
        "summary": {
            "usa_coin_records": len(usa_coin_ids),
            "normal_coin_types": len(normal_coin_ids),
            "legacy_commemorative_coin_records": len(source_live_ids),
            "usa_special_coin_records": len(usa_special_ids),
            "legacy_commemorative_coin_variants": len(source_variant_ids),
            "usa_special_coin_variants": len(usa_special_variant_ids),
            "migrated_sightings": len(migrated_sighting_state),
            "normal_sightings": len(normal_sighting_ids),
            "cleanup_candidate_coins": len(source_coin_ids),
            "cleanup_candidate_coin_variants": len(source_variant_ids),
            "blockers": len(blockers),
        },
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "cleanup_candidates": cleanup_candidates,
        "note": (
            "Este relatório não autoriza nem executa DELETE. Os IDs só podem ser "
            "usados depois de revisão humana e de um backup final verificado."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    backup = args.backup or resolve_pointer(DEFAULT_BACKUP_ROOT)
    artifact = args.artifact or resolve_pointer(DEFAULT_MIGRATION_ROOT)
    if not backup.is_absolute():
        backup = PROJECT_ROOT / backup
    if not artifact.is_absolute():
        artifact = PROJECT_ROOT / artifact
    report = audit(backup=backup.resolve(), artifact=artifact.resolve())
    output = args.output or (
        DEFAULT_BACKUP_ROOT
        / "audits"
        / f"eua-pre-cleanup-{backup.name}.json"
    )
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    output.chmod(0o600)
    print("AUDITORIA PRÉ-LIMPEZA DOS EUA")
    print(f"Backup: {report['backup']}")
    print(f"Estado: {report['status']}")
    for check in report["checks"]:
        marker = "OK" if check["status"] == "ok" else "BLOQUEADO"
        print(f"- [{marker}] {check['name']}: {check['actual']}")
    print(f"Bloqueios: {len(report['blockers'])}")
    print(f"Relatório: {output.relative_to(PROJECT_ROOT)}")
    print("DELETE executado: não")
    return 0 if not report["blockers"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AuditError as exc:
        print(f"Erro: {exc}")
        raise SystemExit(1)
