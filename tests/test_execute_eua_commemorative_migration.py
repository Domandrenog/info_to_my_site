from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import execute_eua_commemorative_migration as executor


def make_item(index: int, *, condition: str = "Não Tenho"):
    return {
        "source_coin_id": f"coin-{index:03d}",
        "source_record": {
            "id": f"coin-{index:03d}",
            "country": "EUA",
            "continent": "América",
            "name": f"Quarter - Tema {index}",
            "years": str(2000 + index),
            "condition": condition,
            "has_variants": False,
            "notes": "Programa",
        },
        "target_special_coin_id": None,
        "source_coin_deletion_authorized": False,
        "target_special_coin_payload": {
            "country": "EUA",
            "continent": "América",
            "name": "25 cents",
            "commemorative_name": f"Tema {index}",
            "year": str(2000 + index),
            "condition": condition,
            "has_variants": False,
            "hidden": False,
            "notes": "Programa",
        },
        "variants": [],
        "sightings": [],
    }


def make_manifest(*, first_condition: str = "Não Tenho"):
    return {
        "format_version": 1,
        "kind": executor.EXPECTED_MANIFEST_KIND,
        "status": "planned_read_only",
        "items": [
            make_item(index, condition=first_condition if index == 1 else "Não Tenho")
            for index in range(1, executor.EXPECTED_MANIFEST_ITEMS + 1)
        ],
        "excluded_normal_coins": [
            {
                "record": {
                    "id": f"normal-{index:03d}",
                    "country": "EUA",
                    "continent": "América",
                    "name": f"Normal {index}",
                },
                "variants": [],
                "sightings": [],
            }
            for index in range(1, 13)
        ],
    }


def make_state():
    return {
        "format_version": 1,
        "manifest_sha256": "manifest",
        "created_at": "2026-10-09T00:00:00+00:00",
        "updated_at": "2026-10-09T00:00:00+00:00",
        "capabilities": {},
        "created_special_coins": {},
        "safety": {
            "source_coin_deletions": 0,
            "source_variant_mutations": 0,
            "source_sighting_mutations": 0,
        },
    }


class FakeSpecialCoinClient:
    def __init__(self):
        self.records = []
        self.next_id = 1
        self.base_url = "https://example.test/SpecialCoin"
        self.created = 0
        self.deleted = 0

    def filter(self, query, limit=1, skip=0):
        matches = [
            record
            for record in self.records
            if all(record.get(field) == value for field, value in query.items())
        ]
        return matches[skip : skip + limit]

    def bulk_create(self, records):
        created = []
        for payload in records:
            record = dict(payload)
            record["id"] = f"special-{self.next_id}"
            self.next_id += 1
            self.records.append(record)
            created.append(record)
            self.created += 1
        return created

    def request(self, method, url, payload=None):
        if method != "DELETE":
            raise AssertionError(f"Unexpected method: {method}")
        record_id = url.rsplit("/", 1)[-1]
        before = len(self.records)
        self.records = [record for record in self.records if record.get("id") != record_id]
        if len(self.records) == before:
            raise AssertionError(f"Unknown record: {record_id}")
        self.deleted += 1
        return None


class FakeBaseClient:
    def __init__(self, records_by_entity):
        self.records_by_entity = records_by_entity

    def for_entity(self, entity):
        client = FakeSpecialCoinClient()
        client.records = [dict(record) for record in self.records_by_entity[entity]]
        return client


class UsaCommemorativeMigrationExecutorTests(unittest.TestCase):
    def test_reconcile_accepts_only_an_exact_unique_target(self):
        manifest = make_manifest()
        state = make_state()
        first = manifest["items"][0]
        actual = dict(first["target_special_coin_payload"], id="special-1")

        pending, existing = executor.reconcile_items(manifest, state, [actual])

        self.assertEqual(len(existing), 1)
        self.assertEqual(len(pending), 237)
        self.assertEqual(existing[0][1]["id"], "special-1")

        changed = dict(actual, notes="Alterado")
        with self.assertRaisesRegex(executor.MigrationExecutionError, "diverge"):
            executor.reconcile_items(manifest, state, [changed])

    def test_canary_creates_exactly_one_and_checkpoints_it_without_source_writes(self):
        manifest = make_manifest()
        state = make_state()
        client = FakeSpecialCoinClient()
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            executor, "mark_numisvault_stats_stale", return_value=False
        ):
            artifact = Path(temp_dir)
            created = executor.execute_creations(
                artifact=artifact,
                manifest=manifest,
                state=state,
                read_client=client,
                write_client=client,
                admin_client=object(),
                canary_only=True,
            )

            saved = executor.read_json(artifact / executor.STATE_FILENAME)

        self.assertEqual(created, 1)
        self.assertEqual(client.created, 1)
        self.assertEqual(client.deleted, 0)
        self.assertEqual(len(client.records), 1)
        self.assertEqual(len(saved["created_special_coins"]), 1)
        self.assertEqual(saved["safety"]["source_coin_deletions"], 0)
        self.assertEqual(saved["safety"]["source_variant_mutations"], 0)
        self.assertEqual(saved["safety"]["source_sighting_mutations"], 0)

    def test_full_parent_creation_requires_successful_incomplete_probe(self):
        manifest = make_manifest(first_condition="Incompleto")
        state = make_state()
        client = FakeSpecialCoinClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(
                executor.MigrationExecutionError, "--probe-incomplete"
            ):
                executor.execute_creations(
                    artifact=Path(temp_dir),
                    manifest=manifest,
                    state=state,
                    read_client=client,
                    write_client=client,
                    admin_client=object(),
                    canary_only=False,
                )

        self.assertEqual(client.created, 0)

    def test_incomplete_probe_is_verified_deleted_and_saved(self):
        state = make_state()
        client = FakeSpecialCoinClient()
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            executor, "mark_numisvault_stats_stale", return_value=False
        ):
            artifact = Path(temp_dir)
            capability = executor.run_incomplete_probe(
                artifact=artifact,
                state=state,
                read_client=client,
                write_client=client,
                admin_client=object(),
            )
            saved = executor.read_json(artifact / executor.STATE_FILENAME)

        self.assertTrue(capability["verified"])
        self.assertTrue(capability["cleanup_verified"])
        self.assertEqual(capability["verified_condition"], "Incompleto")
        self.assertEqual(client.created, 1)
        self.assertEqual(client.deleted, 1)
        self.assertEqual(client.records, [])
        self.assertTrue(saved["capabilities"]["incomplete_condition"]["verified"])

    def test_checkpoint_for_missing_live_record_blocks_recreation(self):
        manifest = make_manifest()
        state = make_state()
        state["created_special_coins"]["coin-001"] = {
            "target_special_coin_id": "special-missing"
        }

        with self.assertRaisesRegex(executor.MigrationExecutionError, "já não existe"):
            executor.reconcile_items(manifest, state, [])

    def test_live_source_snapshot_requires_all_250_original_records_unchanged(self):
        manifest = make_manifest()
        entries = executor.source_entries(manifest)
        records = {
            "Coin": [entry["record"] for entry in entries],
            "CoinVariant": [],
            "CoinSighting": [],
        }
        counts = executor.verify_live_source_snapshot(manifest, FakeBaseClient(records))
        self.assertEqual(counts, {"coins": 250, "variants": 0, "sightings": 0})

        changed_records = {entity: list(values) for entity, values in records.items()}
        changed_records["Coin"][0] = dict(changed_records["Coin"][0], notes="Mudou")
        with self.assertRaisesRegex(executor.MigrationExecutionError, "mudou desde o backup"):
            executor.verify_live_source_snapshot(
                manifest, FakeBaseClient(changed_records)
            )


if __name__ == "__main__":
    unittest.main()
