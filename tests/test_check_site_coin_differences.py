from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.check_site_coin_differences import (
    api_country_name,
    api_tracking_report,
    build_reverse_map,
    compare_country,
    country_slugs_with_catalog,
    detail_stem,
    external_image_source_suggestions,
    find_all_coins_country_folder,
    group_api_records_by_country,
    normalize_key,
    pending_catalogue_api_countries,
    pending_rarity_report,
    print_text_report,
    print_text_reports,
    report_for_output,
    untracked_api_countries,
    write_output_report,
)


class CheckSiteCoinDifferencesOutputTests(unittest.TestCase):
    def test_match_key_ignores_spacing_around_year_ranges(self) -> None:
        self.assertEqual(normalize_key("1966 - 1970"), normalize_key("1966-1970"))

    def render(self, report: dict[str, object], *, include_warnings: bool = False) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            print_text_report(report, include_warnings)
        return output.getvalue().strip()

    def test_multiple_countries_are_separated_visually(self) -> None:
        reports = [
            {
                "country_name": country,
                "summary": {
                    "analyzed_coins": 1,
                    "api_coin_count": 1,
                    "site_missing_fields": {"url_ucoin": 0, "notes": 0},
                    "total_issues": 1,
                    "by_type": {"missing_api_coin_record": 1},
                },
                "coins_with_issues": [
                    {
                        "denomination": "1 moeda",
                        "issues": [
                            {
                                "type": "missing_api_coin_record",
                                "field": "api",
                                "missing_value": "Not found",
                            }
                        ],
                    }
                ],
            }
            for country in ("Seicheles", "Singapura")
        ]

        with redirect_stdout(io.StringIO()) as output:
            print_text_reports(reports, include_warnings=False)

        self.assertTrue(
            output.getvalue().startswith(
                "#" * 72
                + "\nPAÍSES COM PROBLEMAS A REVER\n"
                + "#" * 72
                + "\n\nSeicheles: 1 moeda analisada"
            )
        )
        self.assertIn(
            "Catálogo local: 1 moeda\n\n"
            + "-" * 72
            + "\n\nSingapura: 1 moeda analisada",
            output.getvalue(),
        )

    def test_photo_only_countries_are_grouped_before_other_problems(self) -> None:
        problem_report = {
            "country_name": "Bahamas",
            "summary": {
                "analyzed_coins": 1,
                "api_coin_count": 0,
                "site_missing_fields": {"url_ucoin": 0, "notes": 0},
                "total_issues": 1,
                "by_type": {"missing_api_coin_record": 1},
            },
            "coins_with_issues": [
                {
                    "denomination": "1 dólar",
                    "issues": [
                        {
                            "type": "missing_api_coin_record",
                            "field": "api",
                            "missing_value": "Not found",
                        }
                    ],
                }
            ],
        }
        photo_only_report = {
            "country_name": "África do Sul",
            "summary": {
                "analyzed_coins": 1,
                "api_coin_count": 1,
                "site_missing_fields": {"url_ucoin": 0, "notes": 0},
                "total_issues": 2,
                "by_type": {"missing_image_url": 2},
            },
            "coins_with_issues": [
                {
                    "denomination": "10 cêntimos",
                    "issuePeriod": "2026",
                    "issues": [
                        {
                            "type": "missing_image_url",
                            "field": "obverseImage",
                            "missing_value": "image_url",
                        },
                        {
                            "type": "missing_image_url",
                            "field": "reverseImage",
                            "missing_value": "image_url",
                        },
                    ],
                }
            ],
        }

        with redirect_stdout(io.StringIO()) as output:
            print_text_reports(
                [problem_report, photo_only_report], include_warnings=False
            )

        rendered = output.getvalue()
        photo_heading = "PAÍSES APENAS COM FOTOGRAFIAS INDISPONÍVEIS NO uCoin"
        problem_heading = "PAÍSES COM PROBLEMAS A REVER"
        self.assertLess(rendered.index(photo_heading), rendered.index("África do Sul"))
        self.assertLess(rendered.index("África do Sul"), rendered.index(problem_heading))
        self.assertLess(rendered.index(problem_heading), rendered.index("Bahamas"))
        self.assertIn(
            "Fonte uCoin:\n"
            "- Sem fotografia disponível: 1 moeda\n"
            "  - 10 cêntimos (2026): frente e verso não disponíveis",
            rendered,
        )

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
                "site_missing_fields": {"url_ucoin": 19, "notes": 19},
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
            "Associação:\n"
            "- Sem associação confirmada: 21 moedas\n"
            "  - Possivelmente em falta no Site Base44: 2 moedas\n"
            "  - Possivelmente existentes, mas sem correspondência: 19 moedas\n"
            "\n"
            "Site Base44: 19 moedas\n"
            "- Sem URL do uCoin: 19 moedas\n"
            "- Sem notes: 19 moedas\n"
            "\n"
            "Catálogo local: 21 moedas\n"
            "\n"
            "Fonte uCoin:\n"
            "- Sem fotografia disponível: 1 moeda\n"
            "  - 10 piso (2025): frente e verso não disponíveis",
        )

    def test_text_report_uses_singular_and_compact_warnings(self) -> None:
        report = {
            "country": "sri-lanka",
            "country_name": "Sri Lanka",
            "summary": {
                "analyzed_coins": 1,
                "api_coin_count": 1,
                "site_missing_fields": {"url_ucoin": 0, "notes": 1},
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
            "Site Base44: 1 moeda\n"
            "- Sem notes: 1 moeda\n"
            "\n"
            "Catálogo local: 1 moeda\n"
            "Warnings: 2\n"
            "- Multiple API matches: 2 warnings",
        )

    def test_text_report_suggests_ucoin_for_non_ucoin_external_images(self) -> None:
        report = {
            "country": "filipinas",
            "country_name": "Filipinas",
            "summary": {
                "analyzed_coins": 1,
                "api_coin_count": 1,
                "site_missing_fields": {"url_ucoin": 0, "notes": 0},
                "total_issues": 2,
                "by_type": {"non_ucoin_external_image": 2},
            },
            "coins_with_issues": [
                {
                    "denomination": "1 cêntimo",
                    "issuePeriod": "1995 - 2016",
                    "issues": [
                        {
                            "type": "non_ucoin_external_image",
                            "field": "links-externos.frente",
                            "value": "https://base44.test/front.jpg",
                            "missing_value": "https://i.ucoin.net/front.jpg",
                        },
                        {
                            "type": "non_ucoin_external_image",
                            "field": "links-externos.tras",
                            "value": "https://base44.test/back.jpg",
                            "missing_value": "https://i.ucoin.net/back.jpg",
                        },
                    ],
                }
            ],
        }

        self.assertEqual(
            self.render(report),
            "Filipinas: 1 moeda analisada\n"
            "\n"
            "Site Base44: 1 moeda\n"
            "\n"
            "Catálogo local: 1 moeda\n"
            "\n"
            "Mapeamento All_Coins:\n"
            "- Link externo não é do uCoin: 1 moeda\n"
            "  - 1 cêntimo (1995 - 2016): substituir frente e verso pelos links do uCoin",
        )

    def test_text_report_combines_equal_api_and_catalog_counts(self) -> None:
        report = {
            "country": "seychelles",
            "country_name": "Seicheles",
            "summary": {
                "analyzed_coins": 28,
                "api_coin_count": 28,
                "site_missing_fields": {"url_ucoin": 0, "notes": 0},
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
            "Associação:\n"
            "- Sem associação confirmada: 1 moeda\n"
            "  - Possivelmente existentes, mas sem correspondência: 1 moeda\n"
            "\n"
            "Site Base44: 28 moedas\n"
            "\n"
            "Catálogo local: 28 moedas",
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
            "Países no Site Base44 sem tracking local: 2\n- Portugal: 60 moedas\n- Alemanha: 1 moeda",
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
            "- Bahamas: 3/3 tipos sem raridade (24 moedas no Site Base44)",
        )


class AllCoinsPathTests(unittest.TestCase):
    def test_compare_country_suggests_replacing_external_sources_with_ucoin(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paises_dir = root / "paises"
            country_dir = paises_dir / "asia" / "filipinas"
            country_dir.mkdir(parents=True)
            detail_url = "https://pt.ucoin.net/coin/philippines-1-sentimo-1995-2016"
            ucoin_front = "https://i.ucoin.net/coin/philippines-front.jpg"
            ucoin_back = "https://i.ucoin.net/coin/philippines-back.jpg"
            (country_dir / "app-catalog.json").write_text(
                json.dumps(
                    {
                        "country": "Filipinas",
                        "periods": [
                            {
                                "title": "Filipinas › República › 1995-2016",
                                "coins": [
                                    {
                                        "denomination": "1 cêntimo",
                                        "issuePeriod": "1995 - 2016",
                                        "detailUrl": detail_url,
                                        "obverseImage": ucoin_front,
                                        "reverseImage": ucoin_back,
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            all_coins_dir = root / "All_Coins"
            image_dir = all_coins_dir / "fotos" / "paises" / "Asia" / "Filipinas" / "normal"
            image_dir.mkdir(parents=True)
            raw_front = "https://raw.example/front.jpg"
            raw_back = "https://raw.example/back.jpg"
            external_front = "https://base44.test/front.jpg"
            external_back = "https://base44.test/back.jpg"
            (image_dir / "links-internos.txt").write_text(
                "coin-slug:\n"
                f"  frente: {raw_front}\n"
                f"  tras: {raw_back}\n",
                encoding="utf-8",
            )
            (image_dir / "links-externos.txt").write_text(
                "coin-slug:\n"
                f"  frente: {external_front}\n"
                f"  tras: {external_back}\n",
                encoding="utf-8",
            )
            api_record = {
                "id": "record-1",
                "country": "Filipinas",
                "name": "1 cêntimo",
                "years": "1995-2016",
                "notes": "República",
                "url_ucoin": detail_url,
                "image_frente": external_front,
                "image_verso": external_back,
            }

            with patch.dict("os.environ", {"BASE44_APP_ID": "app-id"}):
                report = compare_country(
                    paises_dir,
                    all_coins_dir,
                    "filipinas",
                    "app-catalog.json",
                    include_markdown_as_issue=False,
                    check_api=True,
                    include_warnings=False,
                    api_records_by_country={"filipinas": [api_record]},
                )

        coin = report["coins_with_issues"][0]
        suggestions = [
            issue
            for issue in coin["issues"]
            if issue["type"] == "non_ucoin_external_image"
        ]
        self.assertEqual(len(suggestions), 2)
        self.assertEqual(suggestions[0]["value"], external_front)
        self.assertEqual(suggestions[0]["missing_value"], ucoin_front)
        self.assertEqual(suggestions[1]["value"], external_back)
        self.assertEqual(suggestions[1]["missing_value"], ucoin_back)

    def test_confirmed_name_association_exposes_missing_site_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paises_dir = root / "paises"
            country_dir = paises_dir / "asia" / "filipinas"
            country_dir.mkdir(parents=True)
            detail_url = "https://pt.ucoin.net/coin/philippines-10-piso-2025"
            (country_dir / "app-catalog.json").write_text(
                json.dumps(
                    {
                        "country": "Filipinas",
                        "periods": [
                            {
                                "title": "Filipinas › República › 2025",
                                "coins": [
                                    {
                                        "denomination": "10 piso",
                                        "issuePeriod": "2025",
                                        "detailUrl": detail_url,
                                        "obverseImage": "",
                                        "reverseImage": "",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (country_dir / "filipinas-missing-found.json").write_text(
                json.dumps(
                    {
                        "missing": [
                            {
                                "status": "connected_pending_name_update",
                                "ucoinUrl": detail_url,
                                "apiUrl": "https://base44.test/entities/Coin/record-1",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            all_coins_dir = root / "All_Coins"
            image_dir = all_coins_dir / "fotos" / "paises" / "Asia" / "Filipinas" / "normal"
            image_dir.mkdir(parents=True)
            (image_dir / "links-internos.txt").write_text("", encoding="utf-8")
            (image_dir / "links-externos.txt").write_text("", encoding="utf-8")
            api_record = {
                "id": "record-1",
                "country": "Filipinas",
                "name": "10 pesos",
                "years": "2025",
                "notes": "",
                "url_ucoin": "",
                "image_frente": "",
                "image_verso": "",
            }

            with patch.dict("os.environ", {"BASE44_APP_ID": "app-id"}):
                report = compare_country(
                    paises_dir,
                    all_coins_dir,
                    "filipinas",
                    "app-catalog.json",
                    include_markdown_as_issue=False,
                    check_api=True,
                    include_warnings=False,
                    api_records_by_country={"filipinas": [api_record]},
                )

        coin = report["coins_with_issues"][0]
        self.assertTrue(coin["siteUrl"].endswith("/record-1"))
        issue_types = [issue["type"] for issue in coin["issues"]]
        self.assertNotIn("missing_api_coin_record", issue_types)
        self.assertIn("missing_notes", issue_types)
        self.assertIn("missing_url_ucoin", issue_types)

    def test_unique_name_and_year_associate_coin_without_source_photos(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paises_dir = root / "paises"
            country_dir = paises_dir / "africa" / "africa-do-sul"
            country_dir.mkdir(parents=True)
            (country_dir / "app-catalog.json").write_text(
                json.dumps(
                    {
                        "country": "África do Sul",
                        "periods": [
                            {
                                "title": "África do Sul › República da África do Sul › 2026",
                                "coins": [
                                    {
                                        "denomination": "10 cêntimos",
                                        "issuePeriod": "2026",
                                        "detailUrl": "https://pt.ucoin.net/coin/south-africa-10-cents-2026",
                                        "obverseImage": "",
                                        "reverseImage": "",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            all_coins_dir = root / "All_Coins"
            image_dir = all_coins_dir / "fotos" / "paises" / "Africa" / "AfricaDoSul" / "normal"
            image_dir.mkdir(parents=True)
            (image_dir / "links-internos.txt").write_text("", encoding="utf-8")
            (image_dir / "links-externos.txt").write_text("", encoding="utf-8")
            api_record = {
                "id": "record-1",
                "country": "África do Sul",
                "name": "10 cêntimos",
                "years": "2026",
                "notes": "República da África do Sul",
                "url_ucoin": "https://pt.ucoin.net/coin/south-africa-10-cents-2026",
                "image_frente": "",
                "image_verso": "",
            }

            with patch.dict("os.environ", {"BASE44_APP_ID": "app-id"}):
                report = compare_country(
                    paises_dir,
                    all_coins_dir,
                    "africa-do-sul",
                    "app-catalog.json",
                    include_markdown_as_issue=False,
                    check_api=True,
                    include_warnings=False,
                    api_records_by_country={"africa-do-sul": [api_record]},
                )

        coin = report["coins_with_issues"][0]
        self.assertTrue(coin["siteUrl"].endswith("/record-1"))
        self.assertEqual(
            [issue["type"] for issue in coin["issues"]],
            ["missing_image_url", "missing_image_url"],
        )

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
            self.assertEqual(
                reverse["https://i.ucoin.net/coin-front.jpg"],
                reverse["https://raw.example/coin-front.jpg"],
            )

    def test_non_ucoin_external_images_receive_exact_ucoin_suggestions(self) -> None:
        reverse = {
            "https://raw.example/front.jpg": {
                "slug": "coin-slug",
                "side": "frente",
                "ucoin_url": "https://base44.test/front.jpg",
            },
            "https://raw.example/back.jpg": {
                "slug": "coin-slug",
                "side": "tras",
                "ucoin_url": "https://numista.test/back.jpg",
            },
        }
        record = {
            "image_frente": "https://raw.example/front.jpg",
            "image_verso": "https://raw.example/back.jpg",
        }

        suggestions = external_image_source_suggestions(
            record,
            {
                "frente": "https://i.ucoin.net/coin/front.jpg",
                "tras": "https://i.ucoin.net/coin/back.jpg",
            },
            reverse,
        )

        self.assertEqual(
            suggestions,
            [
                {
                    "type": "non_ucoin_external_image",
                    "field": "links-externos.frente",
                    "value": "https://base44.test/front.jpg",
                    "missing_value": "https://i.ucoin.net/coin/front.jpg",
                },
                {
                    "type": "non_ucoin_external_image",
                    "field": "links-externos.tras",
                    "value": "https://numista.test/back.jpg",
                    "missing_value": "https://i.ucoin.net/coin/back.jpg",
                },
            ],
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
