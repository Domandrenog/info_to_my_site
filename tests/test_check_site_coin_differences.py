from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.check_site_coin_differences import (
    api_country_name,
    api_tracking_report,
    build_reverse_map,
    country_slugs_with_catalog,
    detail_stem,
    find_all_coins_country_folder,
    group_api_records_by_country,
    pending_catalogue_api_countries,
    pending_rarity_report,
    print_text_report,
    report_for_output,
    untracked_api_countries,
    write_output_report,
)


class CheckSiteCoinDifferencesOutputTests(unittest.TestCase):
    def render(self, report: dict[str, object], *, include_warnings: bool = False) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            print_text_report(report, include_warnings)
        return output.getvalue().strip()

    def test_text_report_separates_analyzed_coins_from_issue_occurrences(self) -> None:
        coins = [
            {
                "denomination": f"moeda {index}",
                "issuePeriod": "2000",
                "issues": [
                    {
                        "type": "missing_api_coin_record",
                        "field": "api",
                        "value": "",
                        "missing_value": "Not found",
                    }
                ],
            }
            for index in range(1, 21)
        ]
        coins.append(
            {
                "denomination": "10 piso",
                "issuePeriod": "2025",
                "issues": [
                    {
                        "type": "missing_api_coin_record",
                        "field": "api",
                        "value": "",
                        "missing_value": "Not found",
                    },
                    {
                        "type": "missing_image_url",
                        "field": "obverseImage",
                        "value": "",
                        "missing_value": "image_url",
                    },
                    {
                        "type": "missing_image_url",
                        "field": "reverseImage",
                        "value": "",
                        "missing_value": "image_url",
                    },
                ],
            }
        )
        report = {
            "country": "filipinas",
            "country_name": "Filipinas",
            "summary": {
                "analyzed_coins": 21,
                "api_coin_count": 19,
                "total_issues": 23,
                "by_type": {
                    "missing_api_coin_record": 21,
                    "missing_image_url": 2,
                },
            },
            "coins_with_issues": coins,
        }

        self.assertEqual(
            self.render(report),
            "Filipinas: 21 moedas analisadas\n"
            "\n"
            "- Sem associação confirmada: 21 moedas\n"
            "- Possivelmente em falta na API: 2 moedas\n"
            "- Sem fotografia: 1 moeda\n"
            "  - 10 piso (2025): frente e verso em falta\n"
            "\n"
            "API Base44: 19 moedas\n"
            "Catálogo local: 21 moedas",
        )

    def test_text_report_uses_singular_and_compact_warnings(self) -> None:
        report = {
            "country": "sri-lanka",
            "country_name": "Sri Lanka",
            "summary": {
                "analyzed_coins": 1,
                "api_coin_count": 1,
                "total_issues": 1,
                "by_type": {"missing_notes": 1},
                "total_warnings": 2,
                "warnings_by_type": {"multiple_api_matches": 2},
            },
            "coins_with_issues": [
                {
                    "denomination": "1 cêntimo",
                    "issues": [
                        {
                            "type": "missing_notes",
                            "field": "notes",
                            "value": "",
                            "missing_value": "República",
                        }
                    ],
                }
            ],
        }

        self.assertEqual(
            self.render(report, include_warnings=True),
            "Sri Lanka: 1 moeda analisada\n"
            "\n"
            "- Sem notes: 1 moeda\n"
            "\n"
            "API Base44 e catálogo local: 1 moeda\n"
            "Warnings: 2\n"
            "- Multiple API matches: 2 warnings",
        )

    def test_text_report_combines_equal_api_and_catalog_counts(self) -> None:
        report = {
            "country": "seychelles",
            "country_name": "Seicheles",
            "summary": {
                "analyzed_coins": 28,
                "api_coin_count": 28,
                "total_issues": 1,
                "by_type": {"missing_api_coin_record": 1},
            },
            "coins_with_issues": [
                {
                    "denomination": "50 cêntimos",
                    "issuePeriod": "1977",
                    "issues": [
                        {
                            "type": "missing_api_coin_record",
                            "field": "api",
                            "value": "",
                            "missing_value": "Not found",
                        }
                    ],
                }
            ],
        }

        self.assertEqual(
            self.render(report),
            "Seicheles: 28 moedas analisadas\n"
            "\n"
            "- Sem associação confirmada: 1 moeda\n"
            "\n"
            "API Base44 e catálogo local: 28 moedas",
        )

    def test_text_report_shows_errors_without_coin_details(self) -> None:
        report = {
            "country": "pais-teste",
            "summary": {"total_issues": 0, "by_type": {}},
            "error": "Catalog not found",
        }

        self.assertEqual(self.render(report), "Pais Teste: error\n- Catalog not found")

    def test_text_report_hides_countries_without_issues(self) -> None:
        report = {
            "country": "sri-lanka",
            "country_name": "Sri Lanka",
            "summary": {"total_issues": 0, "by_type": {}},
        }

        self.assertEqual(self.render(report), "")

    def test_text_report_does_not_print_zero_issues_when_only_warnings_exist(self) -> None:
        report = {
            "country": "sri-lanka",
            "country_name": "Sri Lanka",
            "summary": {
                "total_issues": 0,
                "by_type": {},
                "total_warnings": 1,
                "warnings_by_type": {"multiple_api_matches": 1},
            },
        }

        self.assertEqual(
            self.render(report, include_warnings=True),
            "Sri Lanka: 1 warning\n- Multiple API matches: 1 warning",
        )

    def test_text_report_lists_api_countries_without_local_tracking(self) -> None:
        report = api_tracking_report(
            [
                {"country": "Portugal", "coin_count": 60},
                {"country": "Alemanha", "coin_count": 1},
            ]
        )

        self.assertEqual(
            self.render(report),
            "Países na API sem tracking local: 2\n- Portugal: 60 moedas\n- Alemanha: 1 moeda",
        )

    def test_text_report_lists_pending_catalogues_as_missing_rarity(self) -> None:
        report = pending_rarity_report(
            [
                {
                    "country": "Bahamas",
                    "coin_count": 24,
                    "catalog_coin_type_count": 3,
                    "missing_rarity_count": 3,
                }
            ]
        )

        self.assertEqual(
            self.render(report),
            "Países recolhidos ainda sem raridade: 1\n"
            "- Bahamas: 3/3 tipos sem raridade (24 moedas na API)",
        )


class AllCoinsPathTests(unittest.TestCase):
    def test_api_country_name_resolves_base44_alias(self) -> None:
        self.assertEqual(api_country_name("mauricia", "Maurícias"), "Maurícia")
        self.assertEqual(api_country_name("eua", "Estados Unidos da América"), "EUA")
        self.assertEqual(api_country_name("seychelles", "Seicheles"), "Seychelles")
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

    def test_detail_stem_reads_coin_from_login_ref(self) -> None:
        detail_url = "https://pt.ucoin.net/login/?ref=/coin/greenland-10-kroner-1932/?tid=183830"

        self.assertEqual(detail_stem(detail_url), "greenland-10-kroner-1932")

    def test_detail_stem_keeps_regular_coin_url(self) -> None:
        detail_url = "https://pt.ucoin.net/coin/denmark-10-kroner-1932/?tid=123"

        self.assertEqual(detail_stem(detail_url), "denmark-10-kroner-1932")


class ApiCountryTrackingTests(unittest.TestCase):
    def test_untracked_api_countries_are_grouped_and_counted(self) -> None:
        records = [
            {"country": "Maurícia", "id": "1"},
            {"country": "Portugal", "id": "2"},
            {"country": "Portugal", "id": "3"},
            {"country": "", "id": "4"},
        ]

        grouped = group_api_records_by_country(records)
        missing = untracked_api_countries(grouped, {"mauricia"})

        self.assertEqual(missing, [{"country": "Portugal", "coin_count": 2}])

    def test_pending_catalogue_is_reported_separately_from_untracked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paises_dir = Path(temp_dir)
            country_dir = paises_dir / "america" / "bahamas"
            country_dir.mkdir(parents=True)
            (country_dir / "app-catalog-pending.json").write_text(
                json.dumps(
                    {
                        "country": "Bahamas",
                        "periods": [
                            {
                                "coins": [
                                    {"availability": "still needed to calculate"},
                                    {"availability": "historical"},
                                ]
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            grouped = {"bahamas": [{"country": "Bahamas"}, {"country": "Bahamas"}]}

            pending, pending_keys = pending_catalogue_api_countries(grouped, paises_dir, set())

            self.assertEqual(pending_keys, {"bahamas"})
            self.assertEqual(
                pending,
                [
                    {
                        "country": "Bahamas",
                        "coin_count": 2,
                        "catalog_coin_type_count": 2,
                        "missing_rarity_count": 1,
                    }
                ],
            )
            self.assertEqual(untracked_api_countries(grouped, pending_keys), [])

    def test_report_output_hides_internal_image_match_urls(self) -> None:
        report = {
            "country": "portugal",
            "summary": {"total_issues": 1},
            "image_matched_ucoin_urls": ["https://pt.ucoin.net/coin/example"],
        }

        public_report = report_for_output(report)

        self.assertNotIn("image_matched_ucoin_urls", public_report)
        self.assertIn("image_matched_ucoin_urls", report)

    def test_global_country_list_ignores_pending_only_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paises_dir = Path(temp_dir)
            tracked = paises_dir / "america" / "canada"
            pending = paises_dir / "america" / "bahamas"
            tracked.mkdir(parents=True)
            pending.mkdir(parents=True)
            (tracked / "app-catalog.json").write_text("{}", encoding="utf-8")
            (pending / "app-catalog-pending.json").write_text("{}", encoding="utf-8")

            self.assertEqual(country_slugs_with_catalog(paises_dir, "app-catalog.json"), ["canada"])

    def test_output_report_preserves_existing_file_when_errors_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "all-differences.json"
            output_path.write_text("previous report\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                write_output_report(output_path, [], total_issues=5, has_errors=True)

            self.assertEqual(output_path.read_text(encoding="utf-8"), "previous report\n")


if __name__ == "__main__":
    unittest.main()
