from __future__ import annotations

import copy
import unittest

from scripts import finalize_portugal_normal_catalog as finalizer
from scripts.migrate_portugal_commemoratives import SELECTION


def coin(url: str, denomination: str = "1 escudo") -> dict:
    return {
        "denomination": denomination,
        "value": 1.0,
        "unit": "escudo",
        "issuePeriod": "1986 - 2001",
        "startYear": 1986,
        "endYear": 2001,
        "availability": "historical",
        "detailUrl": url,
        "obverseImage": "https://example.test/front.jpg",
        "reverseImage": "https://example.test/back.jpg",
        "hasVariants": False,
        "variants": [],
        "excludedProofOnlyVariants": [],
    }


class FinalizePortugalNormalCatalogTests(unittest.TestCase):
    def legacy_catalogue(self) -> dict:
        normal_one = coin("https://pt.ucoin.net/coin/normal-1/?tid=1")
        normal_two = coin("https://pt.ucoin.net/coin/normal-2/?tid=2", "2 escudos")
        commemoratives = [
            coin(f"https://pt.ucoin.net/coin/special-{tid}/?tid={tid}")
            for tid, _subject, _programme in SELECTION
        ]
        return {
            "country": "Portugal",
            "periods": [
                {
                    "title": "Portugal › República Portuguesa",
                    "ruler": "República Portuguesa",
                    "coins": [normal_one, normal_two, copy.deepcopy(normal_two)],
                },
                {
                    "title": "Portugal › ECU",
                    "ruler": "ECU",
                    "coins": commemoratives,
                },
            ],
        }

    def test_finalizes_exact_audited_scope_and_is_idempotent(self) -> None:
        result, summary = finalizer.finalize_catalogue(
            self.legacy_catalogue(), expected_normal_coins=2
        )

        self.assertEqual(
            summary,
            {
                "normal_coins": 2,
                "commemoratives_removed": 19,
                "duplicates_removed": 1,
            },
        )
        self.assertEqual(result["catalogType"], "normal")
        self.assertEqual(result["ucoinCoinType"], "normal")
        self.assertEqual(result["coinOrder"], "value_then_issue")
        self.assertEqual(len(result["periods"]), 1)

        repeated, repeated_summary = finalizer.finalize_catalogue(
            result, expected_normal_coins=2
        )
        self.assertEqual(repeated, result)
        self.assertEqual(repeated_summary["commemoratives_removed"], 0)
        self.assertEqual(repeated_summary["duplicates_removed"], 0)

    def test_rejects_a_partial_commemorative_set(self) -> None:
        catalogue = self.legacy_catalogue()
        catalogue["periods"][1]["coins"].pop()

        with self.assertRaisesRegex(finalizer.PortugalCatalogError, "parcial"):
            finalizer.finalize_catalogue(catalogue, expected_normal_coins=2)

    def test_rejects_conflicting_duplicate_urls(self) -> None:
        catalogue = self.legacy_catalogue()
        catalogue["periods"][0]["coins"][2]["issuePeriod"] = "outro"

        with self.assertRaisesRegex(finalizer.PortugalCatalogError, "dados diferentes"):
            finalizer.finalize_catalogue(catalogue, expected_normal_coins=2)


if __name__ == "__main__":
    unittest.main()
