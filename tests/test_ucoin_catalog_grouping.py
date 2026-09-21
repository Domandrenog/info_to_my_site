from __future__ import annotations

import argparse
import unittest
from unittest.mock import Mock, patch

from tests.test_ucoin_catalog_parser import coin
from scripts.ucoin_catalog import build_country_url, filter_periods_by_start_year, launch_incognito_chromium
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
