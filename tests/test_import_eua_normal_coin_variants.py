from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from scripts import import_eua_normal_coin_variants as importer
from scripts.admin_stats_sync import MutationRun


class ImportEuaNormalCoinVariantsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan = importer.load_plan(importer.DEFAULT_PLAN)

    def parents(self) -> list[dict[str, object]]:
        return [
            {
                "id": item["coin_id"],
                "country": "EUA",
                "name": item["name"],
                "years": item["years"],
                "url_ucoin": item["url_ucoin"],
                "has_variants": False,
                "condition": "Não Tenho",
            }
            for item in self.plan["items"]
        ]

    def test_real_plan_is_create_only_and_has_exact_counts(self) -> None:
        plan = self.plan

        self.assertEqual(len(plan["items"]), importer.EXPECTED_PARENTS)
        self.assertEqual(
            sum(len(item["variants"]) for item in plan["items"]),
            importer.EXPECTED_VARIANTS,
        )
        self.assertEqual(plan["safety"]["coin_variant_updates"], 0)
        self.assertEqual(plan["safety"]["coin_variant_deletes"], 0)
        self.assertEqual(plan["safety"]["coin_creates"], 0)
        self.assertEqual(plan["safety"]["coin_deletes"], 0)

    def test_reconcile_accepts_existing_variants_without_modifying_them(self) -> None:
        first = self.plan["items"][0]
        variants = [
            {
                "id": f"variant-{index}",
                "coin_id": first["coin_id"],
                "tag": tag,
                "condition": "Tenho" if index == 0 else "Por Entregar",
                "ordem": 999 - index,
            }
            for index, tag in enumerate(first["variants"][:2])
        ]

        state = importer.reconcile_live(self.plan, self.parents(), variants)

        self.assertEqual(state["existing"], 2)
        self.assertEqual(state["pending"], importer.EXPECTED_VARIANTS - 2)
        self.assertEqual(variants[0]["condition"], "Tenho")
        self.assertEqual(variants[0]["ordem"], 999)

    def test_reconcile_rejects_an_unapproved_variant(self) -> None:
        first = self.plan["items"][0]
        variants = [
            {
                "id": "unexpected",
                "coin_id": first["coin_id"],
                "tag": "1900 PROOF",
                "condition": "Não Tenho",
            }
        ]

        with self.assertRaisesRegex(importer.NormalCoinVariantError, "inesperadas"):
            importer.reconcile_live(self.plan, self.parents(), variants)

    def test_new_payload_uses_not_owned_and_stable_order(self) -> None:
        item = self.plan["items"][0]
        tag = item["variants"][3]

        payload = importer.variant_payload(item, tag)

        self.assertEqual(
            payload,
            {
                "coin_id": item["coin_id"],
                "tag": tag,
                "condition": "Não Tenho",
                "ordem": 3,
            },
        )

    def test_only_has_variants_and_updated_date_are_unprotected(self) -> None:
        before = self.parents()[0]
        after = {**before, "has_variants": True, "updated_date": "later"}

        self.assertEqual(
            importer.protected_parent_fields(before),
            importer.protected_parent_fields(after),
        )
        changed = copy.deepcopy(after)
        changed["condition"] = "Tenho"
        self.assertNotEqual(
            importer.protected_parent_fields(before),
            importer.protected_parent_fields(changed),
        )

    @patch.object(importer, "VERIFY_PAUSE_SECONDS", 0)
    def test_create_waits_for_eventual_read_consistency(self) -> None:
        item = {
            "coin_id": "coin-1",
            "name": "1 cêntimo",
            "years": "1982",
            "variants": ["P", "D"],
        }

        class ReadClient:
            calls = 0

            def filter(self, query, limit=1000, skip=0):
                self.calls += 1
                if self.calls < 3:
                    return []
                return [
                    {**importer.variant_payload(item, tag), "id": f"variant-{tag}"}
                    for tag in item["variants"]
                ]

        class WriteClient:
            payloads = None

            def bulk_create(self, payloads):
                self.payloads = copy.deepcopy(payloads)

        mutations = MutationRun(lambda: False)
        write_client = WriteClient()

        created = importer.create_batch_and_verify(
            item=item,
            tags=item["variants"],
            read_client=ReadClient(),
            write_client=write_client,
            mutations=mutations,
        )

        self.assertEqual(created, 2)
        self.assertEqual(len(write_client.payloads), 2)
        self.assertEqual(mutations.mutation_count, 2)

    def test_apply_requires_explicit_yes_before_live_access(self) -> None:
        with self.assertRaisesRegex(importer.NormalCoinVariantError, "exige --yes"):
            importer.run(["--apply"])


if __name__ == "__main__":
    unittest.main()
