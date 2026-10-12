from __future__ import annotations

import unittest

from scripts import collect_eua_normal_coin_variants as collector


class CollectEuaNormalCoinVariantsTests(unittest.TestCase):
    def test_real_plan_has_the_complete_approved_scope(self) -> None:
        plan = collector.read_json(collector.DEFAULT_OUTPUT)

        self.assertEqual(plan["summary"]["parents"], collector.EXPECTED_PARENTS)
        self.assertEqual(plan["summary"]["variants"], collector.EXPECTED_VARIANTS)
        self.assertEqual(
            plan["summary"]["proof_only_excluded"],
            collector.EXPECTED_PROOF_EXCLUSIONS,
        )
        self.assertEqual(len({item["coin_id"] for item in plan["items"]}), 30)
        self.assertEqual(len({item["url_ucoin"] for item in plan["items"]}), 30)
        self.assertTrue(all(len(item["variants"]) >= 2 for item in plan["items"]))
        self.assertTrue(
            all(
                not (set(item["variants"]) & set(item["excluded_proof_only"]))
                for item in plan["items"]
            )
        )

    def test_finalize_includes_unc_and_bu_but_excludes_proof_only(self) -> None:
        item = collector.finalize_scan_item(
            {
                "denomination": "1 cêntimo",
                "issuePeriod": "2025",
                "detailUrl": "https://example.test/coin",
                "rows": [
                    {
                        "year": "2025",
                        "mint": "P",
                        "unc_mintage": "10 000",
                        "bu_mintage": "-",
                        "proof_mintage": "-",
                    },
                    {
                        "year": "2025",
                        "mint": "Ω",
                        "unc_mintage": "-",
                        "bu_mintage": "25 000",
                        "proof_mintage": "-",
                    },
                    {
                        "year": "2025",
                        "mint": "S",
                        "unc_mintage": "-",
                        "bu_mintage": "-",
                        "proof_mintage": "1 000",
                    },
                ],
            }
        )

        self.assertEqual(item["variants"], ["P", "Ω"])
        self.assertEqual(item["excluded_proof_only"], ["S"])

    def test_finalize_preserves_meaningful_subtypes(self) -> None:
        item = collector.finalize_scan_item(
            {
                "denomination": "1 cêntimo",
                "issuePeriod": "1909 - 1910",
                "detailUrl": "https://example.test/coin",
                "rows": [
                    {
                        "year": "1909",
                        "mint": "D_",
                        "unc_mintage": "1",
                        "bu_mintage": "-",
                        "proof_mintage": "-",
                    },
                    {
                        "year": "1909",
                        "mint": "D_FG",
                        "unc_mintage": "1",
                        "bu_mintage": "-",
                        "proof_mintage": "-",
                    },
                ],
            }
        )

        self.assertEqual(item["variants"], ["1909 D (FG)", "1909 D (sem FG)"])


if __name__ == "__main__":
    unittest.main()
