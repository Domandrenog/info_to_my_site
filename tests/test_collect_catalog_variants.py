from __future__ import annotations

import copy
import unittest

from scripts import collect_catalog_variants as collector
from scripts.import_catalog_variants import CatalogVariantError


class CollectCatalogVariantsTests(unittest.TestCase):
    def test_safe_request_pacing_defaults_are_conservative(self) -> None:
        args = collector.parse_args(
            ["--catalog", "catalog.json", "--attach-cdp"]
        )

        self.assertEqual(args.delay, 20.0)
        self.assertEqual(args.batch_size, 3)
        self.assertEqual(args.batch_pause, 120.0)

    def test_detail_page_identity_rejects_wrong_country_and_ip_block(self) -> None:
        common = {
            "detail_url": "https://pt.ucoin.net/coin/portugal-1-escudo/?tid=1",
            "final_url": "https://pt.ucoin.net/coin/portugal-1-escudo/?tid=1",
            "h1": "Portugal 1 escudo, 1927 - 1968",
            "body_text": "Portugal",
            "country": "Portugal",
        }
        self.assertEqual(
            collector.detail_page_identity_error(
                **common,
                document_title="1 escudo 1927-1968, Portugal - uCoin.net",
            ),
            "",
        )
        self.assertIn(
            "outro país",
            collector.detail_page_identity_error(
                **common,
                document_title="10 cents 2007-2026, Irlanda - uCoin.net",
            ),
        )
        self.assertIn(
            "00:09:20",
            collector.detail_page_identity_error(
                **{**common, "body_text": "Your IP is blocked. Wait 00:09:20"},
                document_title="uCoin.net",
            ),
        )

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

    def test_catalog_coins_can_limit_refresh_to_exact_period_titles(self) -> None:
        catalogue = {
            "periods": [
                {
                    "title": "Portugal › República Portuguesa › 1910 - 1969",
                    "coins": [
                        {
                            "denomination": "1 escudo",
                            "issuePeriod": "1927 - 1968",
                            "detailUrl": "https://ucoin.test/escudo",
                        }
                    ],
                },
                {
                    "title": "Portugal › Reino de Portugal › 1836 - 1910",
                    "coins": [
                        {
                            "denomination": "100 reis",
                            "issuePeriod": "1838 - 1843",
                            "detailUrl": "https://ucoin.test/reis",
                        }
                    ],
                },
            ]
        }

        selected = collector.catalog_coins(
            catalogue,
            "normal",
            ["Portugal › República Portuguesa › 1910 - 1969"],
        )

        self.assertEqual([coin["denomination"] for coin in selected], ["1 escudo"])

    def test_catalog_coins_rejects_unknown_period_title(self) -> None:
        with self.assertRaisesRegex(
            CatalogVariantError, "períodos/programas não existem"
        ):
            collector.catalog_coins(
                {"periods": [{"title": "Período conhecido", "coins": []}]},
                "normal",
                ["Período errado"],
            )


if __name__ == "__main__":
    unittest.main()
