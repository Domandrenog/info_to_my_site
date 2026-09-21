from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.check_site_coin_differences import (
    api_country_name,
    build_reverse_map,
    find_all_coins_country_folder,
    print_text_report,
)


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


class AllCoinsPathTests(unittest.TestCase):
    def test_api_country_name_resolves_base44_alias(self) -> None:
        self.assertEqual(api_country_name("mauricia", "Maurícias"), "Maurícia")
        self.assertEqual(api_country_name("sri-lanka", "Sri Lanka"), "Sri Lanka")

    def test_finds_normal_country_folder_below_continent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            expected = root / "fotos" / "paises" / "Asia" / "SriLanka" / "normal"
            expected.mkdir(parents=True)

            self.assertEqual(find_all_coins_country_folder(root, "sri-lanka"), expected)

    def test_resolves_historic_country_folder_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            expected = root / "fotos" / "paises" / "Europa" / "Croacia" / "normal"
            expected.mkdir(parents=True)

            self.assertEqual(find_all_coins_country_folder(root, "croatia"), expected)

    def test_missing_country_folder_is_an_explicit_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(FileNotFoundError, "Pasta All_Coins não encontrada"):
                find_all_coins_country_folder(Path(temp_dir), "sri-lanka")

    def test_build_reverse_map_reads_new_link_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            country_folder = Path(temp_dir)
            (country_folder / "links-internos.txt").write_text(
                "coin-slug:\n  frente: https://raw.example/coin-front.jpg\n  tras: https://raw.example/coin-back.jpg\n",
                encoding="utf-8",
            )
            (country_folder / "links-externos.txt").write_text(
                "coin-slug:\n  frente: https://i.ucoin.net/coin-front.jpg\n  tras: https://i.ucoin.net/coin-back.jpg\n",
                encoding="utf-8",
            )

            reverse = build_reverse_map(country_folder)

            self.assertEqual(
                reverse["https://raw.example/coin-front.jpg"],
                {
                    "slug": "coin-slug",
                    "side": "frente",
                    "ucoin_url": "https://i.ucoin.net/coin-front.jpg",
                },
            )


if __name__ == "__main__":
    unittest.main()
