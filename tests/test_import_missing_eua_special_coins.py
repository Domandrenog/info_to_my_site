from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts import import_missing_eua_special_coins as importer


CATALOG_PATH = Path(
    "info/paises/america/eua/special-coins-missing-approved.json"
)


def empty_state(catalog_sha256: str = "sha") -> dict:
    return {
        "format_version": 1,
        "catalog_sha256": catalog_sha256,
        "created_at": "2026-10-10T00:00:00+00:00",
        "updated_at": "2026-10-10T00:00:00+00:00",
        "special_coins": {},
        "special_coin_variants": {},
        "safety": {"updates": 0, "deletions": 0},
    }


def baseline_record(index: int) -> dict:
    return {
        "id": f"old-{index:03d}",
        "country": "EUA",
        "name": f"Moeda {index}",
        "commemorative_name": f"Tema antigo {index}",
        "year": str(1800 + index),
        "ordem": index,
        "url_ucoin": None,
    }


class MissingUsaSpecialCoinsTests(unittest.TestCase):
    def test_real_catalog_contains_exact_approved_scope_without_s_variants(self) -> None:
        catalog, digest = importer.load_approved_catalog(CATALOG_PATH)

        self.assertEqual(len(digest), 64)
        self.assertEqual(len(catalog["items"]), 7)
        variants = [
            variant
            for item in catalog["items"]
            for variant in item["variants"]
        ]
        self.assertEqual(len(variants), 24)
        self.assertFalse(any(importer.is_s_variant(item["tag"]) for item in variants))
        booker = next(
            item
            for item in catalog["items"]
            if item["key"] == "1946-1951-booker-t-washington"
        )
        self.assertEqual(booker["source"]["composition"], "Silver 0.900")
        self.assertEqual(len(booker["variants"]), 12)

    def test_catalog_validation_rejects_a_historical_s_variant(self) -> None:
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        catalog["items"][-1]["variants"][0]["tag"] = "1946 S"

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "catalog.json"
            path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaisesRegex(
                importer.MigrationExecutionError, "variante S está proibida"
            ):
                importer.load_approved_catalog(path)

    def test_initial_reconciliation_finds_only_the_seven_pending_parents(self) -> None:
        catalog, _digest = importer.load_approved_catalog(CATALOG_PATH)
        live = [
            baseline_record(index)
            for index in range(1, importer.BASELINE_USA_SPECIAL_COINS + 1)
        ]

        pending, existing = importer.reconcile_parents(
            catalog, empty_state(), live
        )

        self.assertEqual(len(pending), 7)
        self.assertEqual(existing, [])

    def test_exact_existing_parent_is_recovered_but_changed_parent_is_blocked(self) -> None:
        catalog, _digest = importer.load_approved_catalog(CATALOG_PATH)
        first = catalog["items"][0]
        live = [
            baseline_record(index)
            for index in range(1, importer.BASELINE_USA_SPECIAL_COINS + 1)
        ]
        live.append({**first["payload"], "id": "new-1"})

        pending, existing = importer.reconcile_parents(
            catalog, empty_state(), live
        )

        self.assertEqual(len(pending), 6)
        self.assertEqual(existing[0][1]["id"], "new-1")

        changed = copy.deepcopy(live)
        changed[-1]["notes"] = "Alterado"
        with self.assertRaisesRegex(importer.MigrationExecutionError, "diverge"):
            importer.reconcile_parents(catalog, empty_state(), changed)

    def test_reconciliation_stops_if_the_238_coin_baseline_changed(self) -> None:
        catalog, _digest = importer.load_approved_catalog(CATALOG_PATH)
        live = [
            baseline_record(index)
            for index in range(1, importer.BASELINE_USA_SPECIAL_COINS)
        ]

        with self.assertRaisesRegex(importer.MigrationExecutionError, "base live"):
            importer.reconcile_parents(catalog, empty_state(), live)

    def test_variant_reconciliation_refuses_an_unapproved_s_record(self) -> None:
        catalog, _digest = importer.load_approved_catalog(CATALOG_PATH)
        parent_by_key = {
            item["key"]: {**item["payload"], "id": f"parent-{index}"}
            for index, item in enumerate(catalog["items"], start=1)
        }
        plans = importer.build_variant_plans(catalog, parent_by_key)
        unwanted = {
            "id": "variant-s",
            "special_coin_id": parent_by_key[catalog["items"][0]["key"]]["id"],
            "tag": "S",
            "condition": "Não Tenho",
        }

        with self.assertRaisesRegex(importer.MigrationExecutionError, "não aprovadas"):
            importer.reconcile_variants(plans, empty_state(), [unwanted])

    def test_write_mode_requires_explicit_yes_before_any_live_access(self) -> None:
        with self.assertRaisesRegex(importer.MigrationExecutionError, "exige --yes"):
            importer.main(["--apply-all-create-only"])


if __name__ == "__main__":
    unittest.main()
