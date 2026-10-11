from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts import fix_eua_special_coin_ucoin_links as fixer


PLAN_PATH = Path(
    "info/paises/america/eua/special-coin-ucoin-corrections-approved.json"
)
BACKUP_PATH = Path(
    "backups/base44-20261011T111936Z/entities/SpecialCoin.json"
)


class UsaSpecialCoinUcoinLinkFixTests(unittest.TestCase):
    def test_real_plan_has_only_the_approved_update_scope(self) -> None:
        plan, digest = fixer.load_plan(PLAN_PATH)

        self.assertEqual(len(digest), 64)
        self.assertEqual(len(plan["updates"]), 238)
        self.assertEqual(plan["summary"]["url_ucoin_fields"], 238)
        self.assertEqual(plan["summary"]["image_verso_fields"], 157)
        self.assertEqual(plan["summary"]["image_frente_fields"], 1)
        self.assertEqual(plan["safety"]["creates"], 0)
        self.assertEqual(plan["safety"]["deletions"], 0)
        self.assertEqual(plan["safety"]["front_image_updates"], 1)
        self.assertEqual(
            sum("image_frente" in item["set"] for item in plan["updates"]), 1
        )

    def test_every_corrected_back_is_the_other_side_of_the_existing_front(self) -> None:
        plan, _digest = fixer.load_plan(PLAN_PATH)
        corrected = [
            item for item in plan["updates"] if "image_verso" in item["set"]
        ]

        self.assertEqual(len(corrected), 157)
        for item in corrected:
            if "image_frente" in item["set"]:
                self.assertEqual(
                    item["identity"]["commemorative_name"],
                    "California. Steve Jobs",
                )
                self.assertNotEqual(
                    fixer.image_asset_id(item["match"]["source_external_front"]),
                    fixer.image_asset_id(item["set"]["image_frente"]),
                )
                continue
            source_front = item["match"]["source_external_front"]
            target_back = item["set"]["image_verso"]
            self.assertEqual(
                fixer.image_asset_id(source_front),
                fixer.image_asset_id(target_back),
            )
            self.assertNotEqual(
                fixer.image_side(source_front), fixer.image_side(target_back)
            )
            self.assertEqual(fixer.counterpart_image_url(source_front), target_back)

    def test_backup_reconciles_to_exactly_the_expected_pending_changes(self) -> None:
        plan, _digest = fixer.load_plan(PLAN_PATH)
        backup = json.loads(BACKUP_PATH.read_text(encoding="utf-8"))
        live = [record for record in backup if record.get("country") == "EUA"]

        pending, complete = fixer.reconcile_live(plan, live)

        self.assertEqual(len(pending), 238)
        self.assertEqual(complete, [])
        self.assertEqual(sum("url_ucoin" in x["patch"] for x in pending), 238)
        self.assertEqual(sum("image_verso" in x["patch"] for x in pending), 157)
        self.assertEqual(sum("image_frente" in x["patch"] for x in pending), 1)

    def test_fully_applied_state_is_idempotent_and_has_245_unique_urls(self) -> None:
        plan, _digest = fixer.load_plan(PLAN_PATH)
        backup = json.loads(BACKUP_PATH.read_text(encoding="utf-8"))
        live = [copy.deepcopy(record) for record in backup if record.get("country") == "EUA"]
        by_id = {record["id"]: record for record in live}
        for item in plan["updates"]:
            by_id[item["id"]].update(item["set"])

        pending, complete = fixer.reconcile_live(plan, live)

        self.assertEqual(pending, [])
        self.assertEqual(len(complete), 238)
        urls = [record["url_ucoin"] for record in live]
        self.assertEqual(len(urls), 245)
        self.assertEqual(len(set(urls)), 245)

    def test_unexpected_live_field_value_blocks_the_whole_operation(self) -> None:
        plan, _digest = fixer.load_plan(PLAN_PATH)
        backup = json.loads(BACKUP_PATH.read_text(encoding="utf-8"))
        live = [copy.deepcopy(record) for record in backup if record.get("country") == "EUA"]
        live[0]["url_ucoin"] = "https://example.test/unreviewed"

        with self.assertRaisesRegex(fixer.MigrationExecutionError, "Conflito"):
            fixer.reconcile_live(plan, live)

    def test_plan_validation_rejects_an_unapproved_field_update(self) -> None:
        plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
        plan["updates"][0]["current"]["name"] = "old"
        plan["updates"][0]["set"]["name"] = "new"

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "plan.json"
            path.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaisesRegex(
                fixer.MigrationExecutionError, "Campos não permitidos"
            ):
                fixer.load_plan(path)

    def test_write_mode_requires_explicit_confirmation_before_reading_plan(self) -> None:
        with self.assertRaisesRegex(fixer.MigrationExecutionError, "exige --yes"):
            fixer.main(["--apply", "--plan", "does-not-exist.json"])


if __name__ == "__main__":
    unittest.main()
