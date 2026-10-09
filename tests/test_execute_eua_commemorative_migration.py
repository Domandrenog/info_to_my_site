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
        "created_special_coin_variants": {},
        "updated_sightings": {},
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
        record_id = url.rsplit("/", 1)[-1]
        if method == "GET":
            matches = [record for record in self.records if record.get("id") == record_id]
            if len(matches) != 1:
                raise AssertionError(f"Unknown record: {record_id}")
            return dict(matches[0])
        if method != "DELETE":
            raise AssertionError(f"Unexpected method: {method}")
        before = len(self.records)
        self.records = [record for record in self.records if record.get("id") != record_id]
        if len(self.records) == before:
            raise AssertionError(f"Unknown record: {record_id}")
        self.deleted += 1
        return None

    def update(self, record_id, patch):
        for index, record in enumerate(self.records):
            if record.get("id") == record_id:
                updated = {**record, **patch, "updated_date": "after-update"}
                self.records[index] = updated
                return updated
        raise AssertionError(f"Unknown record: {record_id}")


class FakeBaseClient:
    def __init__(self, records_by_entity):
        self.records_by_entity = records_by_entity

    def for_entity(self, entity):
        client = FakeSpecialCoinClient()
        client.records = [dict(record) for record in self.records_by_entity[entity]]
        return client


def make_relation_manifest_and_state():
    manifest = make_manifest()
    state = make_state()
    for index, item in enumerate(manifest["items"], start=1):
        target_id = f"special-parent-{index:03d}"
        state["created_special_coins"][item["source_coin_id"]] = {
            "target_special_coin_id": target_id
        }
        if index <= 137:
            item["source_record"]["has_variants"] = True
            for variant_index, tag in enumerate(("P", "D"), start=1):
                variant_id = f"variant-{index:03d}-{variant_index}"
                source_variant = {
                    "id": variant_id,
                    "coin_id": item["source_coin_id"],
                    "tag": tag,
                    "condition": "Tenho" if tag == "P" else "Não Tenho",
                    "adquirida_por": "",
                    "data_aquisicao": "",
                    "ordem": variant_index,
                }
                item["variants"].append(
                    {
                        "source_coin_variant_id": variant_id,
                        "source_record": source_variant,
                        "target_special_coin_variant_id": None,
                        "target_payload_without_parent_id": {
                            field: source_variant.get(field)
                            for field in (
                                "tag",
                                "condition",
                                "adquirida_por",
                                "data_aquisicao",
                                "ordem",
                            )
                        },
                    }
                )
        if index <= 112:
            sighting_id = f"sighting-{index:03d}"
            source_sighting = {
                "id": sighting_id,
                "coin_id": item["source_coin_id"],
                "item_id": item["source_coin_id"],
                "item_type": "normal_coin",
                "coin_name": item["source_record"]["name"],
                "years": item["source_record"]["years"],
                "variant_tag": None,
                "updated_date": "before-update",
            }
            item["sightings"].append(
                {
                    "source_coin_sighting_id": sighting_id,
                    "source_record": source_sighting,
                    "planned_patch_after_target_creation": {
                        "coin_id": None,
                        "item_id": None,
                        "item_type": "special_coin",
                        "coin_name": item["target_special_coin_payload"]["name"],
                        "years": item["target_special_coin_payload"]["year"],
                    },
                }
            )
    return manifest, state


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

    def test_variant_plans_map_274_old_variants_to_confirmed_new_parents(self):
        manifest, state = make_relation_manifest_and_state()

        plans = executor.special_variant_plans(manifest, state)

        self.assertEqual(len(plans), 274)
        self.assertEqual(plans[0]["source_coin_variant_id"], "variant-001-1")
        self.assertEqual(
            plans[0]["target_payload"]["special_coin_id"],
            "special-parent-001",
        )
        self.assertEqual(plans[0]["target_payload"]["tag"], "P")
        self.assertNotIn("coin_id", plans[0]["target_payload"])

    def test_variant_resume_adopts_existing_and_creates_only_missing_record(self):
        manifest, state = make_relation_manifest_and_state()
        plans = executor.special_variant_plans(manifest, state)
        client = FakeSpecialCoinClient()
        for index, plan in enumerate(plans[:-1], start=1):
            client.records.append(dict(plan["target_payload"], id=f"sv-{index:03d}"))

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            executor, "mark_numisvault_stats_stale", return_value=False
        ):
            created = executor.execute_variant_creations(
                artifact=Path(temp_dir),
                manifest=manifest,
                state=state,
                read_client=client,
                write_client=client,
                admin_client=object(),
            )
            saved = executor.read_json(Path(temp_dir) / executor.STATE_FILENAME)

        self.assertEqual(created, 1)
        self.assertEqual(client.created, 1)
        self.assertEqual(len(client.records), 274)
        self.assertEqual(len(saved["created_special_coin_variants"]), 274)
        self.assertEqual(saved["safety"]["source_variant_mutations"], 0)

    def test_sighting_resume_adopts_relinked_and_updates_only_pending_record(self):
        manifest, state = make_relation_manifest_and_state()
        plans = executor.sighting_relink_plans(manifest, state)
        client = FakeSpecialCoinClient()
        for plan in plans[:-1]:
            client.records.append(
                dict(
                    executor.expected_relinked_sighting(plan),
                    updated_date="already-relinked",
                )
            )
        client.records.append(dict(plans[-1]["source_record"]))

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            executor, "mark_numisvault_stats_stale", return_value=False
        ):
            updated = executor.execute_sighting_relinks(
                artifact=Path(temp_dir),
                manifest=manifest,
                state=state,
                read_client=client,
                write_client=client,
                admin_client=object(),
            )
            saved = executor.read_json(Path(temp_dir) / executor.STATE_FILENAME)

        self.assertEqual(updated, 1)
        self.assertEqual(len(saved["updated_sightings"]), 112)
        self.assertEqual(saved["safety"]["source_sighting_mutations"], 112)
        self.assertEqual(saved["safety"]["source_coin_deletions"], 0)
        self.assertEqual(saved["safety"]["source_variant_mutations"], 0)
        final = next(
            record
            for record in client.records
            if record["id"] == plans[-1]["source_coin_sighting_id"]
        )
        self.assertEqual(final["item_type"], "special_coin")
        self.assertEqual(final["item_id"], "special-parent-112")


if __name__ == "__main__":
    unittest.main()
