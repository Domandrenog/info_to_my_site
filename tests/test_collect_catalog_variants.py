from __future__ import annotations

import copy
import unittest

from scripts import collect_catalog_variants as collector
from scripts.import_catalog_variants import CatalogVariantError


class CollectCatalogVariantsTests(unittest.TestCase):
    def test_identical_duplicate_urls_are_collected_once(self) -> None:
        coin = {
            "denomination": "5 escudos",
            "issuePeriod": "1963 - 1986",
            "detailUrl": "https://ucoin.test/five",
        }
        catalogue = {
            "country": "Portugal",
            "periods": [{"coins": [coin]}, {"coins": [copy.deepcopy(coin)]}],
        }

        self.assertEqual(len(collector.catalog_coins(catalogue, "normal")), 1)

    def test_conflicting_duplicate_urls_are_rejected(self) -> None:
        catalogue = {
            "country": "Portugal",
            "periods": [
                {
                    "coins": [
                        {
                            "denomination": "5 escudos",
                            "issuePeriod": "1963 - 1986",
                            "detailUrl": "https://ucoin.test/five",
                        },
                        {
                            "denomination": "10 escudos",
                            "issuePeriod": "1963 - 1986",
                            "detailUrl": "https://ucoin.test/five",
                        },
                    ]
                }
            ],
        }

        with self.assertRaisesRegex(CatalogVariantError, "contraditórias"):
            collector.catalog_coins(catalogue, "normal")

    def test_variant_metadata_is_propagated_to_duplicate_url(self) -> None:
        source = {
            "denomination": "5 escudos",
            "issuePeriod": "1963 - 1986",
            "detailUrl": "https://ucoin.test/five",
            "variants": [{"tag": "1963", "order": 0}],
            "hasVariants": True,
            "variantCollectionStatus": "collected",
        }
        duplicate = {
            "denomination": "5 escudos",
            "issuePeriod": "1963 - 1986",
            "detailUrl": "https://ucoin.test/five",
        }
        catalogue = {
            "country": "Portugal",
            "periods": [{"coins": [source]}, {"coins": [duplicate]}],
        }

        collector.propagate_variant_metadata(catalogue, "normal", source)

        self.assertEqual(duplicate["variants"], source["variants"])
        self.assertIsNot(duplicate["variants"], source["variants"])
        self.assertTrue(duplicate["hasVariants"])

    def test_select_catalog_coins_limits_exact_urls(self) -> None:
        coins = [
            {"detailUrl": "https://ucoin.test/one"},
            {"detailUrl": "https://ucoin.test/two"},
        ]

        selected = collector.select_catalog_coins(
            coins, ["https://ucoin.test/two"]
        )

        self.assertEqual(selected, [coins[1]])

    def test_select_catalog_coins_rejects_unknown_url(self) -> None:
        with self.assertRaisesRegex(CatalogVariantError, "não existem no catálogo"):
            collector.select_catalog_coins(
                [{"detailUrl": "https://ucoin.test/one"}],
                ["https://ucoin.test/missing"],
            )


if __name__ == "__main__":
    unittest.main()
