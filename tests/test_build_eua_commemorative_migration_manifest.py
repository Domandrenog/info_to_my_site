from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts import build_eua_commemorative_migration_manifest as migration


def json_content(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def make_entries():
    programmes = [
        programme
        for programme, count in migration.EXPECTED_GROUP_COUNTS.items()
        for _ in range(count)
    ]
    denominations = [
        denomination
        for denomination, count in migration.EXPECTED_DENOMINATION_COUNTS.items()
        for _ in range(count)
    ]
    entries = []
    for index, (programme, denomination) in enumerate(
        zip(programmes, denominations, strict=True), start=1
    ):
        coin_id = f"coin-{index:03d}"
        variants = []
        sightings = []
        if index == 1:
            variants = [
                {
                    "id": "variant-001",
                    "coin_id": coin_id,
                    "tag": "P",
                    "condition": "Tenho",
                    "adquirida_por": "Pessoa",
                    "data_aquisicao": "2026-01-01",
                    "ordem": 1,
                    "created_date": "original",
                }
            ]
            sightings = [
                {
                    "id": "sighting-001",
                    "coin_id": coin_id,
                    "item_id": coin_id,
                    "item_type": "normal_coin",
                    "coin_name": f"{denomination} - Tema {index}",
                    "years": "2000",
                    "username": "Pessoa",
                }
            ]
        entries.append(
            {
                "entity": "Coin",
                "record": {
                    "id": coin_id,
                    "country": "EUA",
                    "continent": "América",
                    "name": f"{denomination} - Tema {index}",
                    "years": "2000",
                    "condition": "Incompleto" if variants else "Não Tenho",
                    "rarity": "Circulante",
                    "has_variants": bool(variants),
                    "image_frente": "front",
                    "image_verso": "back",
                    "adquirida_por": "",
                    "data_aquisicao": "",
                    "local_compra": None,
                    "valor_pago": None,
                    "moeda_valor": "EUR",
                    "notes": programme,
                    "url_numista": None,
                    "url_ucoin": "https://example.test/coin",
                    "ordem": index,
                    "hidden": False,
                    "created_date": "preserve-me",
                },
                "variants": variants,
                "sightings": sightings,
            }
        )

    for index in range(migration.EXPECTED_NORMAL_COINS):
        coin_id = f"normal-{index:03d}"
        entries.append(
            {
                "entity": "Coin",
                "record": {
                    "id": coin_id,
                    "country": "EUA",
                    "continent": "América",
                    "name": f"Penny - Normal {index}",
                    "years": "1900-2000",
                    "condition": "Tenho",
                    "rarity": "Circulante",
                    "has_variants": False,
                    "notes": "Programa normal",
                },
                "variants": [],
                "sightings": [],
            }
        )
    return entries


def make_backup(root: Path, entries):
    backup = root / "base44-test"
    source_path = backup / migration.SOURCE_EXPORT
    source_path.parent.mkdir(parents=True)
    source_bytes = json_content(entries)
    source_path.write_bytes(source_bytes)
    source_checksum = hashlib.sha256(source_bytes).hexdigest()
    special_coin_path = backup / "entities" / "SpecialCoin.json"
    special_coin_path.parent.mkdir(parents=True)
    special_coin_bytes = json_content([])
    special_coin_path.write_bytes(special_coin_bytes)
    special_coin_checksum = hashlib.sha256(special_coin_bytes).hexdigest()
    manifest = {
        "format_version": 3,
        "status": "complete",
        "created_at": "2026-10-09T10:00:00+00:00",
        "scope": {
            "entities": sorted(migration.REQUIRED_BACKUP_ENTITIES),
            "all_api_fields_preserved": True,
            "complete_item_views": True,
        },
        "entities": [
            {
                "entity": "SpecialCoin",
                "file": "entities/SpecialCoin.json",
                "records": 0,
                "sha256": special_coin_checksum,
            }
        ],
        "country_exports": [
            {
                "country": "EUA",
                "continent": "América",
                "kind": "normal",
                "file": migration.SOURCE_EXPORT.as_posix(),
                "records": len(entries),
                "sha256": source_checksum,
            }
        ],
    }
    manifest_bytes = json_content(manifest)
    (backup / "manifest.json").write_bytes(manifest_bytes)
    manifest_checksum = hashlib.sha256(manifest_bytes).hexdigest()
    (backup / "SHA256SUMS").write_text(
        f"{source_checksum}  {migration.SOURCE_EXPORT.as_posix()}\n"
        f"{special_coin_checksum}  entities/SpecialCoin.json\n"
        f"{manifest_checksum}  manifest.json\n",
        encoding="utf-8",
    )
    return backup


class UsaCommemorativeMigrationManifestTests(unittest.TestCase):
    def test_builds_exact_read_only_plan_and_preserves_original_records(self):
        entries = make_entries()
        manifest, summary = migration.build_migration_manifest(
            entries,
            source_backup={"backup_directory": "base44-test"},
            created_at=datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(len(manifest["items"]), 238)
        self.assertEqual(len(manifest["excluded_normal_coins"]), 12)
        self.assertEqual(manifest["safety"]["base44_writes"], 0)
        self.assertEqual(manifest["safety"]["source_coin_deletions_authorized"], 0)
        first = manifest["items"][0]
        self.assertEqual(first["source_record"]["created_date"], "preserve-me")
        self.assertEqual(first["target_special_coin_payload"]["name"], "1 cent")
        self.assertEqual(first["target_special_coin_payload"]["commemorative_name"], "Tema 1")
        self.assertEqual(first["target_special_coin_payload"]["year"], "2000")
        self.assertTrue(first["target_special_coin_payload"]["has_variants"])
        self.assertNotIn("id", first["target_special_coin_payload"])
        self.assertNotIn("rarity", first["target_special_coin_payload"])
        self.assertEqual(first["source_only_fields"]["rarity"], "Circulante")
        self.assertEqual(first["variants"][0]["source_record"]["created_date"], "original")
        self.assertIsNone(first["variants"][0]["target_special_coin_variant_id"])
        self.assertEqual(
            first["sightings"][0]["planned_patch_after_target_creation"]["item_type"],
            "special_coin",
        )
        self.assertEqual(summary["counts"]["source_variants"], 1)
        self.assertEqual(summary["counts"]["source_sightings"], 1)
        self.assertEqual(
            summary["review_before_execution"]["undocumented_target_conditions"],
            {"Incompleto": 1},
        )

    def test_rejects_changed_programme_count(self):
        entries = make_entries()
        entries[0]["record"]["notes"] = "Programa desconhecido"

        with self.assertRaisesRegex(
            migration.MigrationManifestError, "seleção não corresponde"
        ):
            migration.build_migration_manifest(
                entries,
                source_backup={},
                created_at=datetime.now(timezone.utc),
            )

    def test_rejects_variant_associated_with_another_coin(self):
        entries = make_entries()
        entries[0]["variants"][0]["coin_id"] = "coin-999"

        with self.assertRaisesRegex(
            migration.MigrationManifestError, "coin_id não corresponde"
        ):
            migration.build_migration_manifest(
                entries,
                source_backup={},
                created_at=datetime.now(timezone.utc),
            )

    def test_creates_atomic_private_artifact_with_verified_checksums(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backup = make_backup(root, make_entries())
            output_root = root / "migrations"
            destination, manifest, summary = migration.create_manifest_artifact(
                backup_directory=backup,
                output_root=output_root,
                created_at=datetime(2026, 10, 9, 12, 30, tzinfo=timezone.utc),
            )

            self.assertEqual(destination.name, "eua-commemorativas-20261009T123000Z")
            self.assertEqual((output_root / "LATEST").read_text().strip(), destination.name)
            self.assertEqual((output_root / "LATEST").stat().st_mode & 0o777, 0o600)
            self.assertEqual(manifest["summary"]["commemoratives_selected"], 238)
            self.assertEqual(summary["counts"]["base44_writes"], 0)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
            checksums = migration.parse_checksum_file(destination / "SHA256SUMS")
            for filename in ("manifest.json", "summary.json"):
                path = destination / filename
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(checksums[filename], migration.sha256_file(path))

    def test_rejects_tampered_source_export(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backup = make_backup(root, make_entries())
            source_path = backup / migration.SOURCE_EXPORT
            source_path.write_bytes(source_path.read_bytes() + b" ")

            with self.assertRaisesRegex(
                migration.MigrationManifestError, "checksum do export"
            ):
                migration.create_manifest_artifact(
                    backup_directory=backup,
                    output_root=root / "migrations",
                )


if __name__ == "__main__":
    unittest.main()
