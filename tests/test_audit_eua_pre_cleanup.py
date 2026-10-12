from __future__ import annotations

import unittest

from scripts import audit_eua_pre_cleanup as audit


class AuditEuaPreCleanupTests(unittest.TestCase):
    def test_committed_normal_catalog_has_thirty_unique_types(self) -> None:
        catalog = audit.load_json(audit.DEFAULT_NORMAL_CATALOG)

        records, duplicates = audit.unique_normal_records(catalog)

        self.assertEqual(len(records), 30)
        self.assertEqual(duplicates, 11)
        self.assertEqual(len({record["url_ucoin"] for record in records}), 30)
        self.assertTrue(all(record["country"] == "EUA" for record in records))

    def test_approved_updates_cover_three_links_and_three_periods(self) -> None:
        updates = audit.approved_normal_updates()

        self.assertEqual(len(updates), 5)
        self.assertEqual(
            updates["69fbb07731ca7cea1ab5d213"]["url_ucoin"],
            "https://pt.ucoin.net/coin/usa-1-cent-1959-1982/?tid=11496",
        )
        self.assertEqual(
            updates["69fbb07f14a0b335bc86fedb"],
            {
                "url_ucoin": "https://pt.ucoin.net/coin/usa-5-cents-1946-2003/?tid=11530",
                "years": "1946 - 2003",
            },
        )
        self.assertEqual(
            updates["69fbb08559a47e0846e2f9b1"]["years"],
            "1965 - 2026",
        )
        self.assertEqual(
            updates["69fbb0f39d04e1015dea7f3c"]["years"],
            "1977 - 2026",
        )

    def test_meaningful_differences_ignores_server_and_selected_fields(self) -> None:
        expected = {
            "id": "old-id",
            "updated_date": "before",
            "condition": "Tenho",
            "image_frente": "old-front",
        }
        actual = {
            "id": "new-id",
            "updated_date": "after",
            "condition": "Não Tenho",
            "image_frente": "new-front",
        }

        differences = audit.meaningful_differences(
            expected,
            actual,
            excluded={"image_frente"},
        )

        self.assertEqual(
            differences,
            {"condition": {"expected": "Tenho", "actual": "Não Tenho"}},
        )


if __name__ == "__main__":
    unittest.main()
