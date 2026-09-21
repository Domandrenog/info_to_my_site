from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from scripts.check_site_coin_differences import print_text_report


class CheckSiteCoinDifferencesOutputTests(unittest.TestCase):
    def render(self, report: dict[str, object], *, include_warnings: bool = False) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            print_text_report(report, include_warnings)
        return output.getvalue().strip()

    def test_text_report_is_aggregated_by_issue_type(self) -> None:
        report = {
            "country": "portugal",
            "country_name": "Portugal",
            "summary": {
                "total_issues": 60,
                "by_type": {
                    "missing_api_coin_record": 17,
                    "missing_image_url": 43,
                },
            },
            "coins_with_issues": [
                {
                    "denomination": "1 euro",
                    "ucoinUrl": "https://example.test/ucoin",
                    "siteUrl": "https://example.test/site",
                }
            ],
        }

        self.assertEqual(
            self.render(report),
            "Portugal: 60 issues\n- No photo: 43 issues\n- Not found: 17 issues",
        )

    def test_text_report_uses_singular_and_compact_warnings(self) -> None:
        report = {
            "country": "sri-lanka",
            "country_name": "Sri Lanka",
            "summary": {
                "total_issues": 1,
                "by_type": {"missing_notes": 1},
                "total_warnings": 2,
                "warnings_by_type": {"multiple_api_matches": 2},
            },
        }

        self.assertEqual(
            self.render(report, include_warnings=True),
            "Sri Lanka: 1 issue\n- Missing notes: 1 issue\nWarnings: 2\n- Multiple API matches: 2 warnings",
        )

    def test_text_report_shows_errors_without_coin_details(self) -> None:
        report = {
            "country": "pais-teste",
            "summary": {"total_issues": 0, "by_type": {}},
            "error": "Catalog not found",
        }

        self.assertEqual(self.render(report), "Pais Teste: error\n- Catalog not found")


if __name__ == "__main__":
    unittest.main()
