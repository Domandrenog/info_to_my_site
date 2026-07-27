from __future__ import annotations

import unittest

from tests.test_ucoin_catalog_parser import coin
from ucoin_to_mysite.catalog_parser import CatalogueFetchError, crawl_ucoin_catalogue, normalize_catalogue_url


BASE = "https://pt.ucoin.net/catalog/?country=canada"


def catalogue_url(page_number: int = 1, query: str = "country=canada") -> str:
    suffix = f"&page={page_number}" if page_number > 1 else ""
    return f"https://pt.ucoin.net/catalog/?{query}{suffix}"


def pagination(*hrefs: str, current: int | None = None) -> str:
    links = []
    for index, href in enumerate(hrefs, start=1):
        css_class = ' class="current"' if current == index else ""
        links.append(f'<a href="{href}"{css_class}>{index}</a>')
    return '<div class="pages">%s</div>' % "".join(links)


def catalogue_page(*coins: str, pages: str = "") -> str:
    return """
    <html>
      <head><link rel="canonical" href="https://pt.ucoin.net/catalog/?country=canada"></head>
      <body>
        <h1>Canadá</h1>
        <div id="catalog-list">
          <header><h2><span>Canadá › Rei Charles III › 2023 - 2026</span></h2></header>
          %s
          %s
        </div>
      </body>
    </html>
    """ % ("\n".join(coins), pages)


def catalogue_page_with_header(header: str, *coins: str, pages: str = "") -> str:
        return """
        <html>
            <head><link rel="canonical" href="https://pt.ucoin.net/catalog/?country=canada"></head>
            <body>
                <h1>Canadá</h1>
                <div id="catalog-list">
                    <header><h2><span>%s</span></h2></header>
                    %s
                    %s
                </div>
            </body>
        </html>
        """ % (header, "\n".join(coins), pages)


def test_coin(tid: int, value: str | None = None) -> str:
    return coin(
        value=value or f"{tid} cêntimos, 2023-2026",
        tid=str(tid),
        pid="2919",
        detail=f"/coin/canada-{tid}-cents-2023-2026/?tid={tid}",
        info=f"Níquel, {tid}.0g, ø 20mm<br>UC# {tid} · Circulação normal",
        obverse=f"https://i.ucoin.net/coin/{tid}-1s/canada-{tid}-cents-2023.jpg",
        reverse=f"https://i.ucoin.net/coin/{tid}-2s/canada-{tid}-cents-2023.jpg",
    )


class FakeFetcher:
    def __init__(self, pages: dict[str, object]) -> None:
        self.pages = {normalize_catalogue_url(url): response for url, response in pages.items()}
        self.requests: list[str] = []

    def __call__(self, url: str) -> object:
        normalized = normalize_catalogue_url(url)
        assert normalized is not None
        self.requests.append(normalized)
        response = self.pages[normalized]
        if isinstance(response, Exception):
            raise response
        return response


class UCoinCataloguePaginationTests(unittest.TestCase):
    def crawl(self, initial_url: str, pages: dict[str, object], **kwargs: object) -> dict[str, object]:
        fetcher = FakeFetcher(pages)
        result = crawl_ucoin_catalogue(initial_url, fetcher, retry_backoff_seconds=0, **kwargs)
        result["requests"] = fetcher.requests
        return result

    def coin_count(self, result: dict[str, object]) -> int:
        return sum(len(period["coins"]) for period in result["periods"])

    def test_catalogue_with_only_one_page(self) -> None:
        result = self.crawl(BASE, {BASE: catalogue_page(test_coin(1), pages=pagination(BASE, current=1))})
        self.assertEqual(result["pagination"]["pagesProcessed"], 1)
        self.assertEqual(self.coin_count(result), 1)

    def test_catalogue_with_pages_1_2_and_3(self) -> None:
        result = self.crawl(
            BASE,
            {
                BASE: catalogue_page(test_coin(1), pages=pagination(BASE, catalogue_url(2), catalogue_url(3), current=1)),
                catalogue_url(2): catalogue_page(test_coin(2), pages=pagination(BASE, catalogue_url(2), catalogue_url(3), current=2)),
                catalogue_url(3): catalogue_page(test_coin(3), pages=pagination(BASE, catalogue_url(2), catalogue_url(3), current=3)),
            },
        )
        self.assertEqual(self.coin_count(result), 3)
        self.assertEqual(result["pagination"]["pagesProcessed"], 3)

    def test_initial_url_starting_at_page_2(self) -> None:
        result = self.crawl(
            catalogue_url(2),
            {
                BASE: catalogue_page(test_coin(1), pages=pagination(BASE, catalogue_url(2), catalogue_url(3), current=1)),
                catalogue_url(2): catalogue_page(test_coin(2), pages=pagination(BASE, catalogue_url(2), catalogue_url(3), current=2)),
                catalogue_url(3): catalogue_page(test_coin(3), pages=pagination(BASE, catalogue_url(2), catalogue_url(3), current=3)),
            },
        )
        self.assertEqual(result["pagination"]["visitedPages"][0], catalogue_url(2))
        self.assertEqual(self.coin_count(result), 3)

    def test_relative_pagination_links(self) -> None:
        result = self.crawl(
            BASE,
            {
                BASE: catalogue_page(test_coin(1), pages=pagination("/catalog/?country=canada", "/catalog/?country=canada&page=2", current=1)),
                catalogue_url(2): catalogue_page(test_coin(2)),
            },
        )
        self.assertEqual(self.coin_count(result), 2)

    def test_amp_inside_pagination_urls(self) -> None:
        initial = "https://pt.ucoin.net/catalog/?country=canada&period=2919"
        second = "https://pt.ucoin.net/catalog/?country=canada&period=2919&page=2"
        result = self.crawl(
            initial,
            {
                initial: catalogue_page(test_coin(1), pages=pagination("/catalog/?country=canada&amp;period=2919", "/catalog/?country=canada&amp;period=2919&amp;page=2", current=1)),
                second: catalogue_page(test_coin(2)),
            },
        )
        self.assertEqual(self.coin_count(result), 2)

    def test_repeated_links_to_same_page(self) -> None:
        result = self.crawl(
            BASE,
            {
                BASE: catalogue_page(test_coin(1), pages=pagination(catalogue_url(2), catalogue_url(2), catalogue_url(2))),
                catalogue_url(2): catalogue_page(test_coin(2)),
            },
        )
        self.assertEqual(result["requests"].count(catalogue_url(2)), 1)

    def test_page_1_url_normalization(self) -> None:
        self.assertEqual(normalize_catalogue_url("https://pt.ucoin.net/catalog/?page=1&country=canada"), BASE)
        self.assertEqual(normalize_catalogue_url("https://pt.ucoin.net/catalog/?country=canada&page=1"), BASE)

    def test_preservation_of_country_and_filter_parameters(self) -> None:
        initial = "https://pt.ucoin.net/catalog/?country=canada&period=2919&type=1"
        valid_second = "https://pt.ucoin.net/catalog/?country=canada&period=2919&type=1&page=2"
        invalid_second = "https://pt.ucoin.net/catalog/?country=canada&page=2"
        result = self.crawl(
            initial,
            {
                initial: catalogue_page(test_coin(1), pages=pagination(invalid_second, valid_second)),
                valid_second: catalogue_page(test_coin(2)),
            },
        )
        self.assertEqual(self.coin_count(result), 2)
        self.assertNotIn(invalid_second, result["requests"])

    def test_duplicate_coin_appearing_on_two_pages(self) -> None:
        result = self.crawl(
            BASE,
            {
                BASE: catalogue_page(test_coin(1), pages=pagination(BASE, catalogue_url(2))),
                catalogue_url(2): catalogue_page(test_coin(1)),
            },
        )
        self.assertEqual(self.coin_count(result), 1)

    def test_one_page_request_failing_while_other_pages_are_processed(self) -> None:
        result = self.crawl(
            BASE,
            {
                BASE: catalogue_page(test_coin(1), pages=pagination(BASE, catalogue_url(2), catalogue_url(3))),
                catalogue_url(2): CatalogueFetchError("temporary unavailable", retryable=False),
                catalogue_url(3): catalogue_page(test_coin(3)),
            },
        )
        self.assertEqual(self.coin_count(result), 2)
        self.assertEqual(len(result["pagination"]["failedPages"]), 1)

    def test_pagination_element_missing(self) -> None:
        result = self.crawl(BASE, {BASE: catalogue_page(test_coin(1))})
        self.assertEqual(result["pagination"]["pagesProcessed"], 1)

    def test_pagination_containing_unrelated_external_link(self) -> None:
        result = self.crawl(BASE, {BASE: catalogue_page(test_coin(1), pages=pagination("https://example.com/catalog/?country=canada"))})
        self.assertEqual(result["pagination"]["pagesProcessed"], 1)

    def test_page_redirecting_to_previously_visited_page(self) -> None:
        result = self.crawl(
            BASE,
            {
                BASE: catalogue_page(test_coin(1), pages=pagination(BASE, catalogue_url(2))),
                catalogue_url(2): (catalogue_page(test_coin(2)), BASE),
            },
        )
        self.assertEqual(self.coin_count(result), 1)
        self.assertEqual(len(result["pagination"]["failedPages"]), 1)

    def test_sequential_fallback_stopping_on_empty_page(self) -> None:
        result = self.crawl(
            BASE,
            {BASE: catalogue_page(test_coin(1)), catalogue_url(2): catalogue_page()},
            enable_sequential_fallback=True,
        )
        self.assertEqual(result["pagination"]["pagesProcessed"], 2)
        self.assertEqual(self.coin_count(result), 1)

    def test_sequential_fallback_stopping_when_same_coin_ids_repeat(self) -> None:
        result = self.crawl(
            BASE,
            {BASE: catalogue_page(test_coin(1)), catalogue_url(2): catalogue_page(test_coin(1))},
            enable_sequential_fallback=True,
        )
        self.assertEqual(result["pagination"]["pagesProcessed"], 2)
        self.assertEqual(self.coin_count(result), 1)

    def test_catalogue_with_more_pages_than_example_html(self) -> None:
        pages = {catalogue_url(page_number): catalogue_page(test_coin(page_number)) for page_number in range(1, 8)}
        pages[BASE] = catalogue_page(test_coin(1), pages=pagination(*(catalogue_url(page_number) for page_number in range(1, 8))))
        result = self.crawl(BASE, pages)
        self.assertEqual(self.coin_count(result), 7)

    def test_current_page_links_using_current_class(self) -> None:
        result = self.crawl(
            BASE,
            {
                BASE: catalogue_page(test_coin(1), pages=pagination(BASE, catalogue_url(2), current=1)),
                catalogue_url(2): catalogue_page(test_coin(2), pages=pagination(BASE, catalogue_url(2), current=2)),
            },
        )
        self.assertEqual(result["pagination"]["pagesProcessed"], 2)
        self.assertEqual(self.coin_count(result), 2)

    def test_completing_only_after_every_discovered_page_has_been_processed(self) -> None:
        result = self.crawl(
            BASE,
            {
                BASE: catalogue_page(test_coin(1), pages=pagination(BASE, catalogue_url(2))),
                catalogue_url(2): catalogue_page(test_coin(2), pages=pagination(catalogue_url(2), catalogue_url(3))),
                catalogue_url(3): catalogue_page(test_coin(3)),
            },
        )
        self.assertEqual(result["pagination"]["pagesProcessed"], 3)
        self.assertEqual(self.coin_count(result), 3)

    def test_start_year_stop_after_first_miss_and_two_more_periods(self) -> None:
        result = self.crawl(
            BASE,
            {
                BASE: catalogue_page_with_header(
                    "Canadá › Periodo atual › 1957 - 2020",
                    test_coin(1, value="1 cêntimo, 1957-2020"),
                    pages=pagination(BASE, catalogue_url(2), catalogue_url(3), catalogue_url(4)),
                ),
                catalogue_url(2): catalogue_page_with_header("Canadá › Periodo antigo 1 › 1943 - 1957", test_coin(2, value="2 cêntimos, 1943-1957")),
                catalogue_url(3): catalogue_page_with_header("Canadá › Periodo antigo 2 › 1930 - 1940", test_coin(3, value="3 cêntimos, 1930-1940")),
                catalogue_url(4): catalogue_page_with_header("Canadá › Periodo antigo 3 › 1920 - 1929", test_coin(4, value="4 cêntimos, 1920-1929")),
            },
            stop_after_start_year_miss=1957,
        )
        self.assertEqual(result["pagination"]["pagesProcessed"], 4)
        self.assertTrue(result["pagination"]["stoppedEarly"])

    def test_start_year_stop_does_not_fetch_pages_after_three_missed_periods(self) -> None:
        result = self.crawl(
            BASE,
            {
                BASE: catalogue_page_with_header(
                    "Canadá › Periodo antigo 1 › 1943 - 1957",
                    test_coin(1, value="1 cêntimo, 1943-1957"),
                    pages=pagination(BASE, catalogue_url(2), catalogue_url(3), catalogue_url(4)),
                ),
                catalogue_url(2): catalogue_page_with_header("Canadá › Periodo antigo 2 › 1930 - 1940", test_coin(2, value="2 cêntimos, 1930-1940")),
                catalogue_url(3): catalogue_page_with_header("Canadá › Periodo antigo 3 › 1920 - 1929", test_coin(3, value="3 cêntimos, 1920-1929")),
                catalogue_url(4): catalogue_page_with_header("Canadá › Periodo que nao deve ser pedido › 1958 - 2000", test_coin(4, value="4 cêntimos, 1958-2000")),
            },
            stop_after_start_year_miss=1957,
        )
        self.assertEqual(result["pagination"]["pagesProcessed"], 3)
        self.assertNotIn(catalogue_url(4), result["requests"])


if __name__ == "__main__":
    unittest.main()