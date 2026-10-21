from __future__ import annotations

import argparse
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from tests.test_ucoin_catalog_parser import coin
from scripts.ucoin_catalog import (
    DETAIL_PAGE_LOADED,
    VARIANT_STATUS_PRO_REQUIRED,
    build_country_url,
    collect_catalogue_variants,
    filter_periods_by_start_year,
    finalize_variant_rows,
    is_ucoin_pro_paywall_text,
    launch_incognito_chromium,
    login_redirect_references_coin,
    open_ucoin_detail_page,
    parse_variant_table_cells,
    remove_stale_scrape_error,
    variant_issue_period,
)
from ucoin_to_mysite.catalog_parser import crawl_ucoin_catalogue, parse_catalogue_page


BASE = "https://pt.ucoin.net/catalog/?country=canada"
PAGE2 = "https://pt.ucoin.net/catalog/?country=canada&page=2"


def header(title: str) -> str:
    return f"<header><h2><span>{title}</span></h2></header>"


def hint(value: str = "1 dólar = 100 cêntimos") -> str:
    return f'<div class="period-hint">{value}</div>'


def page(*children: str, pagination: str = "") -> str:
    return """
    <html>
      <head><link rel="canonical" href="https://pt.ucoin.net/catalog/?country=canada"></head>
      <body>
        <h1>Canadá</h1>
        <div id="catalog-list">
          %s
          %s
        </div>
      </body>
    </html>
    """ % ("\n".join(children), pagination)


def pages(*hrefs: str) -> str:
    return '<div class="pages">%s</div>' % "".join(f'<a href="{href}">{index}</a>' for index, href in enumerate(hrefs, 1))


def grouped_coin(tid: int, pid: str = "24", value: str | None = None) -> str:
    return coin(
        value=value or f"{tid} cêntimos, 1982-1989",
        tid=str(tid),
        pid=pid,
        detail=f"/coin/canada-{tid}-cents-1982-1989/?tid={tid}",
        info=f"Níquel, {tid}.0g, ø 20mm<br>UC# {tid} · Circulação normal",
        obverse=f"https://i.ucoin.net/coin/{tid}-1s/canada-{tid}-cents-1984.jpg",
        reverse=f"https://i.ucoin.net/coin/{tid}-2s/canada-{tid}-cents-1984.jpg",
    )


class UCoinCatalogueGroupingTests(unittest.TestCase):
    class DetailPage:
        def __init__(
            self,
            urls: list[str],
            statuses: list[int],
            body_texts: list[str] | None = None,
        ) -> None:
            self.urls = iter(urls)
            self.statuses = iter(statuses)
            self.body_texts = iter(body_texts or [""] * len(urls))
            self.url = ""
            self.body_text = ""
            self.goto_calls: list[str] = []
            self.bring_to_front_calls = 0

        def goto(self, url: str, **_kwargs):
            self.goto_calls.append(url)
            self.url = next(self.urls)
            self.body_text = next(self.body_texts)
            return argparse.Namespace(status=next(self.statuses))

        def bring_to_front(self) -> None:
            self.bring_to_front_calls += 1

        def locator(self, selector: str):
            if selector != "body":
                raise AssertionError(selector)
            return argparse.Namespace(inner_text=lambda **_kwargs: self.body_text)

    def test_detects_portuguese_ucoin_pro_paywall(self) -> None:
        self.assertTrue(
            is_ucoin_pro_paywall_text(
                "Desbloqueie o acesso completo com uma subscrição de membro PRO."
            )
        )

    def test_detects_coin_specific_login_redirect_as_restricted(self) -> None:
        self.assertTrue(
            login_redirect_references_coin(
                "https://pt.ucoin.net/login/?ref=/coin/portugal-100-reis-1838-1843/?tid=63603"
            )
        )
        self.assertFalse(login_redirect_references_coin("https://pt.ucoin.net/login/"))

    @patch("builtins.input")
    def test_variant_page_skips_coin_login_redirect_even_without_paywall_text(
        self, user_input
    ) -> None:
        detail_url = "https://pt.ucoin.net/coin/portugal-100-reis-1838-1843/?tid=63603"
        page = self.DetailPage(
            ["https://pt.ucoin.net/login/?ref=/coin/portugal-100-reis-1838-1843/?tid=63603"],
            [200],
            [""],
        )

        status = open_ucoin_detail_page(
            page,
            detail_url,
            label="100 reis",
            issue_period="1838 - 1843",
            timeout_seconds=60,
            login_retries=2,
        )

        self.assertEqual(status, VARIANT_STATUS_PRO_REQUIRED)
        user_input.assert_not_called()

    @patch("builtins.input")
    def test_variant_page_skips_pro_content_without_asking_for_login(self, user_input) -> None:
        detail_url = "https://pt.ucoin.net/coin/portugal-100-reis-1838-1843/?tid=63603"
        page = self.DetailPage(
            ["https://pt.ucoin.net/login/?ref=/coin/portugal-100-reis-1838-1843/"],
            [200],
            ["Desbloqueie o acesso completo com uma subscrição de membro PRO."],
        )

        status = open_ucoin_detail_page(
            page,
            detail_url,
            label="100 reis",
            issue_period="1838 - 1843",
            timeout_seconds=60,
            login_retries=2,
        )

        self.assertEqual(status, VARIANT_STATUS_PRO_REQUIRED)
        self.assertEqual(page.bring_to_front_calls, 0)
        user_input.assert_not_called()

    def test_pro_promotion_does_not_hide_an_accessible_coin_page(self) -> None:
        detail_url = "https://pt.ucoin.net/coin/portugal-100-reis-1838-1843/?tid=63603"
        page = self.DetailPage(
            [detail_url],
            [200],
            ["Desbloqueie o acesso completo com uma subscrição de membro PRO."],
        )

        status = open_ucoin_detail_page(
            page,
            detail_url,
            label="100 reis",
            issue_period="1838 - 1843",
            timeout_seconds=60,
            login_retries=2,
        )

        self.assertEqual(status, DETAIL_PAGE_LOADED)

    @patch("builtins.input", return_value="")
    def test_variant_page_retries_same_coin_after_manual_login(self, _input) -> None:
        detail_url = "https://pt.ucoin.net/coin/portugal-100-reis-1838-1843/?tid=63603"
        page = self.DetailPage(
            [
                "https://pt.ucoin.net/login/",
                detail_url,
            ],
            [200, 200],
        )

        open_ucoin_detail_page(
            page,
            detail_url,
            label="100 reis",
            issue_period="1838 - 1843",
            timeout_seconds=60,
            login_retries=2,
        )

        self.assertEqual(page.goto_calls, [detail_url, detail_url])
        self.assertEqual(page.bring_to_front_calls, 1)

    @patch("builtins.input", return_value="")
    def test_variant_page_stops_cleanly_when_login_persists(self, _input) -> None:
        detail_url = "https://pt.ucoin.net/coin/test"
        page = self.DetailPage(
            ["https://pt.ucoin.net/login/", "https://pt.ucoin.net/login/"],
            [200, 200],
        )

        with self.assertRaisesRegex(RuntimeError, "progresso anterior continua guardado"):
            open_ucoin_detail_page(
                page,
                detail_url,
                label="100 reis",
                issue_period="1838 - 1843",
                timeout_seconds=60,
                login_retries=1,
            )

    def test_variant_resume_preserves_checkpoint_and_counts_repeated_urls(self) -> None:
        first_url = "https://pt.ucoin.net/coin/portugal-a"
        second_url = "https://pt.ucoin.net/coin/portugal-b"
        coins = [
            {
                "detailUrl": detail_url,
                "denomination": {"displayName": label},
                "issuePeriod": {"displayValue": years},
            }
            for detail_url, label, years in (
                (first_url, "1 real", "1900"),
                (second_url, "2 reis", "1901"),
                (first_url, "1 real", "1900"),
            )
        ]
        checkpoint = {
            "country": "Portugal",
            "completed": 3,
            "expected": 3,
            "items": [
                {
                    "detailUrl": first_url,
                    "status": "collected",
                    "variants": [
                        {"tag": "A", "order": 0},
                        {"tag": "B", "order": 1},
                    ],
                    "excludedProofOnlyVariants": [],
                },
                {
                    "detailUrl": second_url,
                    "status": "no_multiple_variants",
                    "variants": [],
                    "excludedProofOnlyVariants": [],
                },
            ],
        }

        with TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            checkpoint_path = output_dir / "ucoin-variants-scan.json"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            page = Mock()

            summary = collect_catalogue_variants(
                page,
                {"country": "Portugal", "periods": [{"coins": coins}]},
                argparse.Namespace(
                    timeout=60,
                    variant_delay=0,
                    variant_batch_size=10,
                    variant_batch_pause=0,
                    variant_login_retries=2,
                ),
                output_dir,
            )

            saved = json.loads(checkpoint_path.read_text(encoding="utf-8"))

        page.goto.assert_not_called()
        self.assertEqual(saved["completed"], 3)
        self.assertEqual(saved["uniqueDetailUrls"], 2)
        self.assertEqual(len(saved["items"]), 2)
        self.assertEqual(summary["coinsWithVariants"], 2)
        self.assertEqual(summary["variants"], 4)

    def test_catalogue_variant_collection_checkpoints_and_skips_pro_coin(self) -> None:
        detail_url = "https://pt.ucoin.net/coin/portugal-100-reis-1838-1843/?tid=63603"
        coin = {
            "detailUrl": detail_url,
            "denomination": {"displayName": "100 reis"},
            "issuePeriod": {"displayValue": "1838 - 1843"},
        }
        page = self.DetailPage(
            ["https://pt.ucoin.net/login/?ref=/coin/portugal-100-reis-1838-1843/"],
            [200],
            ["Desbloqueie o acesso completo com uma subscrição de membro PRO."],
        )

        with TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            summary = collect_catalogue_variants(
                page,
                {"country": "Portugal", "periods": [{"coins": [coin]}]},
                argparse.Namespace(
                    timeout=60,
                    variant_delay=0,
                    variant_batch_size=0,
                    variant_batch_pause=0,
                    variant_login_retries=2,
                ),
                output_dir,
            )
            checkpoint = json.loads(
                (output_dir / "ucoin-variants-scan.json").read_text(encoding="utf-8")
            )

        self.assertEqual(coin["variantCollectionStatus"], VARIANT_STATUS_PRO_REQUIRED)
        self.assertFalse(coin["hasVariants"])
        self.assertEqual(summary["proRestricted"], 1)
        self.assertEqual(checkpoint["items"][0]["status"], VARIANT_STATUS_PRO_REQUIRED)

    def test_successful_scrape_removes_only_stale_error_report(self) -> None:
        with TemporaryDirectory() as temporary:
            output_path = Path(temporary) / "ucoin-catalog.json"
            error_path = output_path.with_name("scrape-error.json")
            checkpoint_path = output_path.with_name("ucoin-variants-scan.json")
            error_path.write_text("{}", encoding="utf-8")
            checkpoint_path.write_text("{}", encoding="utf-8")

            remove_stale_scrape_error(output_path)

            self.assertFalse(error_path.exists())
            self.assertTrue(checkpoint_path.exists())

    def test_variant_rows_include_unc_and_bu_but_exclude_proof_only(self) -> None:
        result = finalize_variant_rows(
            "2025",
            [
                {"year": "2025", "mark": "P", "unc": "10 000", "bu": "-", "proof": "-"},
                {"year": "2025", "mark": "D", "unc": "-", "bu": "2 000", "proof": "-"},
                {"year": "2025", "mark": "S", "unc": "-", "bu": "-", "proof": "1 000"},
            ],
            "https://pt.ucoin.net/coin/test",
        )

        self.assertEqual(result["variants"], [{"tag": "P", "order": 0}, {"tag": "D", "order": 1}])
        self.assertEqual(result["excludedProofOnlyVariants"], ["S"])

    def test_single_mintage_row_does_not_create_a_variant_selector(self) -> None:
        result = finalize_variant_rows(
            "2025",
            [{"year": "2025", "mark": "", "unc": "1", "bu": "-", "proof": "-"}],
            "https://pt.ucoin.net/coin/test",
        )

        self.assertFalse(result["variants"])

    def test_multi_year_set_only_bu_row_is_kept_as_a_variant(self) -> None:
        result = finalize_variant_rows(
            "1940 - 1942",
            [
                {"year": "1940", "mark": "", "unc": "1 000", "bu": "-", "proof": "-"},
                {"year": "1941", "mark": "", "unc": "-", "bu": "+", "proof": "-"},
                {"year": "1942", "mark": "", "unc": "-", "bu": "-", "proof": "500"},
            ],
            "https://pt.ucoin.net/coin/portugal-test",
        )

        self.assertEqual(
            result["variants"],
            [{"tag": "1940", "order": 0}, {"tag": "1941", "order": 1}],
        )
        self.assertEqual(result["excludedProofOnlyVariants"], ["1942"])

    def test_duplicate_mintage_rows_for_same_mark_are_merged(self) -> None:
        result = finalize_variant_rows(
            "1999",
            [
                {
                    "year": "1999",
                    "mark": "INCM",
                    "unc": "-",
                    "bu": "140.000",
                    "proof": "-",
                },
                {
                    "year": "1999",
                    "mark": "INCM",
                    "unc": "-",
                    "bu": "180.000",
                    "proof": "-",
                },
            ],
            "https://pt.ucoin.net/coin/portugal-100-escudos-1999/?tid=17782",
        )

        self.assertEqual(result["status"], "no_multiple_variants")
        self.assertFalse(result["variants"])
        self.assertFalse(result["excludedProofOnlyVariants"])

    def test_normal_mintage_wins_when_same_mark_also_has_proof_row(self) -> None:
        result = finalize_variant_rows(
            "2025",
            [
                {"year": "2025", "mark": "P", "unc": "1", "bu": "-", "proof": "-"},
                {"year": "2025", "mark": "P", "unc": "-", "bu": "-", "proof": "1"},
            ],
            "https://pt.ucoin.net/coin/test",
        )

        self.assertFalse(result["variants"])
        self.assertFalse(result["excludedProofOnlyVariants"])

    def test_multi_year_columns_do_not_shift_unc_into_the_mint_mark(self) -> None:
        parsed = parse_variant_table_cells(
            mode="multi",
            cells=["1979", "61.652.000", "-", "-", "A", "€ 0,20"],
            issue_period="1969 - 1979",
        )

        self.assertEqual(
            parsed,
            {
                "year": "1979",
                "mark": "",
                "unc": "61.652.000",
                "bu": "-",
                "proof": "-",
            },
        )

    def test_multi_year_row_preserves_display_variant_mark(self) -> None:
        parsed = parse_variant_table_cells(
            mode="multi",
            cells=["1976", "+", "-", "-", "", ""],
            issue_period="1969 - 1979",
            display_mark="↑↑",
        )

        self.assertEqual(parsed["mark"], "↑↑")
        self.assertEqual(parsed["unc"], "+")

    def test_single_year_table_without_mark_column_is_supported(self) -> None:
        parsed = parse_variant_table_cells(
            mode="single",
            cells=["Portugal, Lisboa (INCM)", "270.000", "-", "-"],
            issue_period="1981",
        )

        self.assertEqual(
            parsed,
            {
                "year": "1981",
                "mark": "",
                "unc": "270.000",
                "bu": "-",
                "proof": "-",
            },
        )

    def test_variant_issue_period_prefers_structured_single_year_over_subject(self) -> None:
        self.assertEqual(
            variant_issue_period(
                {
                    "displayValue": "Jornada Mundial da Juventude 2023 Lisboa",
                    "startYear": 2023,
                    "endYear": 2023,
                }
            ),
            "2023",
        )

    def test_variant_issue_period_keeps_a_real_range(self) -> None:
        self.assertEqual(
            variant_issue_period(
                {
                    "displayValue": "2020 - 2024",
                    "startYear": 2020,
                    "endYear": 2024,
                }
            ),
            "2020 - 2024",
        )

    @patch("scripts.ucoin_catalog.time.sleep")
    @patch("scripts.ucoin_catalog.subprocess.Popen")
    @patch("scripts.ucoin_catalog.resolve_browser_executable", return_value="/usr/bin/chromium")
    @patch("scripts.ucoin_catalog.cdp_is_available", side_effect=[False, True])
    def test_incognito_mode_launches_chromium_with_cdp(self, available, executable, popen, sleep) -> None:
        process = Mock()
        popen.return_value = process

        result = launch_incognito_chromium(
            argparse.Namespace(cdp_url="http://127.0.0.1:9222", chrome_path="", user_data_dir=".ucoin-profile")
        )

        self.assertIs(result, process)
        command = popen.call_args.args[0]
        self.assertIn("--incognito", command)
        self.assertIn("--remote-debugging-port=9222", command)

    def test_explicit_country_link_name_preserves_underscores(self) -> None:
        self.assertEqual(
            build_country_url("https://pt.ucoin.net/catalog/", "Sri Lanka", "sri_lanka"),
            "https://pt.ucoin.net/catalog/?country=sri_lanka",
        )

    def crawl(self, pages_by_url: dict[str, str], initial_url: str = BASE) -> dict[str, object]:
        return crawl_ucoin_catalogue(initial_url, lambda url: pages_by_url[url], retry_backoff_seconds=0)

    def test_two_periods_on_the_same_html_page(self) -> None:
        result = parse_catalogue_page(
            page(
                header("Canadá › Rainha Isabel II › 1953 - 2026"),
                grouped_coin(1, pid="24"),
                header("Canadá › Rei Charles III › 2023 - 2026"),
                grouped_coin(2, pid="2919"),
            ),
            BASE,
        )
        self.assertEqual([period["periodId"] for period in result["periods"]], [24, 2919])

    def test_several_coins_under_the_same_period(self) -> None:
        result = parse_catalogue_page(page(header("Canadá › Rainha Isabel II › 1953 - 2026"), grouped_coin(1), grouped_coin(2)), BASE)
        self.assertEqual(len(result["periods"]), 1)
        self.assertEqual([coin["ucoinTypeId"] for coin in result["periods"][0]["coins"]], [1, 2])

    def test_same_period_continuing_on_page_2(self) -> None:
        result = self.crawl(
            {
                BASE: page(header("Canadá › Rainha Isabel II › 1953 - 2026"), grouped_coin(1), pagination=pages(BASE, PAGE2)),
                PAGE2: page(header("Canadá › Rainha Isabel II › 1953 - 2026"), grouped_coin(2)),
            }
        )
        self.assertEqual(len(result["periods"]), 1)
        self.assertEqual([coin["ucoinTypeId"] for coin in result["periods"][0]["coins"]], [1, 2])

    def test_identical_period_id_values_are_merged(self) -> None:
        result = self.crawl(
            {
                BASE: page(header("Canadá › Rainha Isabel II › 1953 - 2026"), grouped_coin(1, pid="24"), pagination=pages(PAGE2)),
                PAGE2: page(header("Canadá › Elizabeth II › 1953 - 2026"), grouped_coin(2, pid="24")),
            }
        )
        self.assertEqual(len(result["periods"]), 1)
        self.assertEqual(result["periods"][0]["periodId"], 24)

    def test_different_period_ids_are_never_merged(self) -> None:
        result = parse_catalogue_page(page(header("Canadá › Rainha Isabel II › 1953 - 2026"), grouped_coin(1, pid="24"), grouped_coin(2, pid="2919")), BASE)
        self.assertEqual([period["periodId"] for period in result["periods"]], [24, 2919])

    def test_missing_period_id_uses_fallback_key(self) -> None:
        result = self.crawl(
            {
                BASE: page(header("Canadá › Território Unido › 1800 - 1850"), grouped_coin(1, pid=""), pagination=pages(PAGE2)),
                PAGE2: page(header("Canadá › Território Unido › 1800 - 1850"), grouped_coin(2, pid="")),
            }
        )
        self.assertEqual(len(result["periods"]), 1)
        self.assertIsNone(result["periods"][0]["periodId"])
        self.assertEqual(len(result["periods"][0]["coins"]), 2)

    def test_period_hint_assigned_to_the_correct_period(self) -> None:
        result = parse_catalogue_page(
            page(
                header("Canadá › Rainha Isabel II › 1953 - 2026"),
                hint("1 dólar = 100 cêntimos"),
                grouped_coin(1),
                header("Canadá › Rei Charles III › 2023 - 2026"),
                hint("1 libra = 20 xelins"),
                grouped_coin(2, pid="2919"),
            ),
            BASE,
        )
        self.assertEqual([period["currencyDescription"] for period in result["periods"]], ["1 dólar = 100 cêntimos", "1 libra = 20 xelins"])

    def test_coin_appearing_before_any_valid_period_header(self) -> None:
        result = parse_catalogue_page(page(grouped_coin(1)), BASE)
        self.assertEqual(len(result["periods"]), 1)
        self.assertEqual(result["periods"][0]["periodId"], 24)
        self.assertIn("historicalPeriod", {warning["field"] for warning in result["warnings"]})

    def test_mismatching_data_pid(self) -> None:
        result = parse_catalogue_page(page(header("Canadá › Rainha Isabel II › 1953 - 2026"), grouped_coin(1, pid="24"), grouped_coin(2, pid="2919")), BASE)
        self.assertEqual([period["periodId"] for period in result["periods"]], [24, 2919])
        self.assertIn("ucoinPeriodId", {warning["field"] for warning in result["warnings"]})

    def test_duplicate_coin_across_two_pages(self) -> None:
        result = self.crawl(
            {
                BASE: page(header("Canadá › Rainha Isabel II › 1953 - 2026"), grouped_coin(1), pagination=pages(PAGE2)),
                PAGE2: page(header("Canadá › Rainha Isabel II › 1953 - 2026"), grouped_coin(1)),
            }
        )
        self.assertEqual(len(result["periods"][0]["coins"]), 1)

    def test_period_order_preserved(self) -> None:
        result = parse_catalogue_page(
            page(
                header("Canadá › Primeiro › 1900 - 1910"),
                grouped_coin(1, pid="101"),
                header("Canadá › Segundo › 1911 - 1920"),
                grouped_coin(2, pid="102"),
                header("Canadá › Terceiro › 1921 - 1930"),
                grouped_coin(3, pid="103"),
            ),
            BASE,
        )
        self.assertEqual([period["rulerOrPeriodName"] for period in result["periods"]], ["Primeiro", "Segundo", "Terceiro"])

    def test_coin_order_preserved_inside_each_period(self) -> None:
        result = parse_catalogue_page(page(header("Canadá › Rainha Isabel II › 1953 - 2026"), grouped_coin(3), grouped_coin(1), grouped_coin(2)), BASE)
        self.assertEqual([coin["ucoinTypeId"] for coin in result["periods"][0]["coins"]], [3, 1, 2])

    def test_period_metadata_not_duplicated_inside_every_coin(self) -> None:
        result = parse_catalogue_page(page(header("Canadá › Rainha Isabel II › 1953 - 2026"), grouped_coin(1)), BASE)
        parsed_coin = result["periods"][0]["coins"][0]
        self.assertNotIn("rulerOrPeriod", parsed_coin)
        self.assertIn("ucoinPeriodId", parsed_coin)

    def test_same_ruler_name_with_different_date_ranges_remain_separate(self) -> None:
        result = parse_catalogue_page(
            page(
                header("Canadá › Rainha › 1900 - 1910"),
                grouped_coin(1, pid=""),
                header("Canadá › Rainha › 1911 - 1920"),
                grouped_coin(2, pid=""),
            ),
            BASE,
        )
        self.assertEqual(len(result["periods"]), 2)
        self.assertEqual([period["startYear"] for period in result["periods"]], [1900, 1911])

    def test_period_containing_coins_with_several_denominations(self) -> None:
        result = parse_catalogue_page(
            page(
                header("Canadá › Rainha Isabel II › 1953 - 2026"),
                grouped_coin(1, value="1 cêntimo, 1982-1989"),
                grouped_coin(5, value="5 cêntimos, 1982-1989"),
                grouped_coin(10, value="10 cêntimos, 1982-1989"),
            ),
            BASE,
        )
        self.assertEqual([coin["denomination"]["displayName"] for coin in result["periods"][0]["coins"]], ["1 cêntimo", "5 cêntimos", "10 cêntimos"])

    def test_start_year_filter_keeps_only_coins_starting_at_or_after_year(self) -> None:
        periods = [
            {
                "fullTitle": "Periodo misto",
                "coins": [
                    {"detailUrl": "https://example.com/a", "startYear": 1957, "endYear": 2020},
                    {"detailUrl": "https://example.com/b", "startYear": 1958, "endYear": 2000},
                    {"detailUrl": "https://example.com/c", "startYear": 1943, "endYear": 1957},
                ],
            }
        ]
        filtered = filter_periods_by_start_year(periods, 1957)
        self.assertEqual([coin["detailUrl"] for coin in filtered[0]["coins"]], ["https://example.com/a", "https://example.com/b"])

    def test_start_year_filter_drops_empty_periods(self) -> None:
        periods = [{"fullTitle": "Periodo antigo", "coins": [{"startYear": 1943, "endYear": 1957}]}]
        self.assertEqual(filter_periods_by_start_year(periods, 1957), [])


if __name__ == "__main__":
    unittest.main()
