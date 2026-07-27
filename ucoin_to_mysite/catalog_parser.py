from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Iterable
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunparse


@dataclass
class HtmlNode:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["HtmlNode | str"] = field(default_factory=list)
    parent: "HtmlNode | None" = None

    def attr(self, name: str) -> str | None:
        return self.attrs.get(name)

    def has_class(self, class_name: str) -> bool:
        return class_name in (self.attrs.get("class") or "").split()

    def iter_nodes(self) -> Iterable["HtmlNode"]:
        for child in self.children:
            if isinstance(child, HtmlNode):
                yield child
                yield from child.iter_nodes()

    def find(self, tag: str | None = None, class_name: str | None = None) -> "HtmlNode | None":
        for node in self.iter_nodes():
            if tag and node.tag != tag:
                continue
            if class_name and not node.has_class(class_name):
                continue
            return node
        return None

    def find_all(self, tag: str | None = None, class_name: str | None = None) -> list["HtmlNode"]:
        result = []
        for node in self.iter_nodes():
            if tag and node.tag != tag:
                continue
            if class_name and not node.has_class(class_name):
                continue
            result.append(node)
        return result

    def text(self) -> str:
        parts: list[str] = []

        def visit(node: HtmlNode | str) -> None:
            if isinstance(node, str):
                parts.append(node)
                return
            if node.tag == "br":
                parts.append(" ")
                return
            for child in node.children:
                visit(child)

        visit(self)
        return normalize_text("".join(parts))


class TreeBuilder(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = HtmlNode(tag.lower(), {key.lower(): value or "" for key, value in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if node.tag not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)


def parse_html(html_source: str) -> HtmlNode:
    parser = TreeBuilder()
    parser.feed(html_source)
    parser.close()
    return parser.root


class CatalogueFetchError(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class CatalogueFetchResponse:
    html: str
    final_url: str | None = None


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(value).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def parse_numeric_value(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def parse_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_query_int(url: str | None, key: str) -> int | None:
    if not url:
        return None
    values = parse_qs(urlparse(url).query).get(key)
    return parse_int(values[0]) if values else None


def is_catalogue_path(path: str) -> bool:
    normalized_path = path.rstrip("/") or "/"
    return normalized_path == "/catalog"


def normalize_catalogue_url(url: str, base_url: str = "https://pt.ucoin.net/catalog/") -> str | None:
    absolute_url = urljoin(base_url, html.unescape(url or ""))
    parsed = urlparse(absolute_url)
    if not parsed.scheme or not parsed.netloc or not is_catalogue_path(parsed.path):
        return None

    query_items = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key == "page" and value in ("", "1"):
            continue
        query_items.append((key, value))
    query = urlencode(sorted(query_items))
    return urlunparse((parsed.scheme, parsed.netloc.lower(), "/catalog/", "", query, ""))


def query_without_page(url: str) -> dict[str, list[str]]:
    query = parse_qs(urlparse(url).query, keep_blank_values=True)
    query.pop("page", None)
    return {key: sorted(values) for key, values in query.items()}


def is_valid_catalogue_pagination_url(url: str, initial_url: str) -> bool:
    normalized_url = normalize_catalogue_url(url, initial_url)
    normalized_initial = normalize_catalogue_url(initial_url)
    if not normalized_url or not normalized_initial:
        return False

    parsed_url = urlparse(normalized_url)
    parsed_initial = urlparse(normalized_initial)
    if (parsed_url.scheme, parsed_url.netloc, parsed_url.path) != (parsed_initial.scheme, parsed_initial.netloc, parsed_initial.path):
        return False

    candidate_filters = query_without_page(normalized_url)
    for key, values in query_without_page(normalized_initial).items():
        if candidate_filters.get(key) != values:
            return False
    return True


def page_number_from_url(url: str) -> int:
    return parse_query_int(url, "page") or 1


def catalogue_url_for_page(url: str, page_number: int) -> str | None:
    normalized = normalize_catalogue_url(url)
    if not normalized:
        return None
    parsed = urlparse(normalized)
    query_items = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "page"]
    if page_number > 1:
        query_items.append(("page", str(page_number)))
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(sorted(query_items)), ""))


def parse_years(value: str) -> tuple[int | None, int | None]:
    match = re.search(r"(\d{4})(?:\s*-\s*(\d{4}))?", value)
    if not match:
        return None, None
    start_year = int(match.group(1))
    end_year = int(match.group(2) or match.group(1))
    return start_year, end_year


def parse_denomination_and_years(value: str) -> dict[str, object]:
    value = normalize_text(value)
    display_name, _, period = value.partition(",")
    display_name = normalize_text(display_name) or None
    display_period = normalize_text(period) or None

    denomination_value = None
    unit = None
    if display_name:
        match = re.match(r"([0-9]+(?:[.,][0-9]+)?)\s+(.+)", display_name)
        if match:
            denomination_value = parse_numeric_value(match.group(1))
            unit = normalize_text(match.group(2)) or None

    start_year, end_year = parse_years(display_period or "")
    return {
        "denomination": {
            "displayName": display_name,
            "value": denomination_value,
            "unit": unit,
        },
        "issuePeriod": {
            "displayValue": display_period,
            "startYear": start_year,
            "endYear": end_year,
        },
    }


def parse_catalogue_identifier(value: str) -> dict[str, str | None]:
    match = re.search(r"\b([A-Z]{1,8})#\s*([^·,\s]+)", value)
    if not match:
        return {"type": None, "number": None, "displayValue": None}
    cat_type = match.group(1)
    number = match.group(2)
    return {"type": cat_type, "number": number, "displayValue": f"{cat_type}# {number}"}


def parse_technical_information(value: str) -> dict[str, object]:
    value = normalize_text(value)
    weight_match = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s*g\b", value, re.IGNORECASE)
    diameter_match = re.search(r"ø\s*([0-9]+(?:[.,][0-9]+)?)\s*mm\b", value, re.IGNORECASE)
    catalogue = parse_catalogue_identifier(value)

    composition = None
    if weight_match:
        composition = normalize_text(value[: weight_match.start()].rstrip(" ,")) or None

    circulation_type = None
    if "·" in value:
        circulation_type = normalize_text(value.rsplit("·", 1)[1]) or None

    return {
        "composition": composition,
        "weightGrams": parse_numeric_value(weight_match.group(1)) if weight_match else None,
        "diameterMm": parse_numeric_value(diameter_match.group(1)) if diameter_match else None,
        "catalogue": catalogue,
        "circulationType": circulation_type,
    }


def parse_image_year(url: str | None) -> int | None:
    if not url:
        return None
    filename = urlparse(url).path.rsplit("/", 1)[-1]
    match = re.search(r"(?:^|-)(\d{4})(?=\.[a-z0-9]+$)", filename, re.IGNORECASE)
    return int(match.group(1)) if match else None


def parse_period_header(value: str) -> dict[str, object]:
    full_title = normalize_text(value)
    parts = [normalize_text(part) for part in full_title.split("›")]
    if len(parts) < 3:
        return {
            "periodId": None,
            "fullTitle": full_title or None,
            "country": None,
            "rulerOrPeriodName": None,
            "startYear": None,
            "endYear": None,
            "currencyDescription": None,
            "coins": [],
        }
    start_year, end_year = parse_years(parts[-1])
    return {
        "periodId": None,
        "fullTitle": full_title or None,
        "country": parts[0] or None,
        "rulerOrPeriodName": parts[-2] or None,
        "startYear": start_year,
        "endYear": end_year,
        "currencyDescription": None,
        "coins": [],
    }


def resolve_country(root: HtmlNode, canonical_url: str | None = None) -> str | None:
    h1 = root.find("h1")
    if h1 and h1.text():
        return h1.text()
    if canonical_url:
        country = parse_qs(urlparse(canonical_url).query).get("country")
        if country:
            return country[0]
    canonical = root.find("link")
    if canonical and canonical.attr("rel") == "canonical":
        country = parse_qs(urlparse(canonical.attr("href") or "").query).get("country")
        if country:
            return country[0]
    img = root.find("img")
    alt = normalize_text(img.attr("alt") if img else "")
    return alt or None


def direct_element_children(node: HtmlNode) -> list[HtmlNode]:
    return [child for child in node.children if isinstance(child, HtmlNode)]


def find_by_id(root: HtmlNode, node_id: str) -> HtmlNode | None:
    for node in root.iter_nodes():
        if node.attr("id") == node_id:
            return node
    return None


def get_image_src(img: HtmlNode) -> str | None:
    for attr in ("src", "data-src", "data-original", "data-lazy-src"):
        value = img.attr(attr)
        if value:
            return value
    srcset = img.attr("srcset")
    if srcset:
        return srcset.split(",", 1)[0].strip().split(" ", 1)[0]
    return None


def warning(warnings: list[dict[str, object]], page_url: str | None, coin_path: str | None, field: str, message: str) -> None:
    warnings.append({"pageUrl": page_url, "coinPath": coin_path, "field": field, "message": message})


def has_ancestor(node: HtmlNode, tag: str | None = None, class_name: str | None = None, node_id: str | None = None) -> bool:
    current = node.parent
    while current is not None:
        if tag and current.tag != tag:
            current = current.parent
            continue
        if class_name and not current.has_class(class_name):
            current = current.parent
            continue
        if node_id and current.attr("id") != node_id:
            current = current.parent
            continue
        return True
    return False


def extract_pagination_urls(root: HtmlNode, page_url: str, initial_url: str | None = None) -> list[str]:
    initial_url = initial_url or page_url
    urls: list[str] = []
    seen: set[str] = set()

    page_containers = [node for node in root.find_all("div", "pages") if has_ancestor(node, node_id="catalog-list")]
    if not page_containers:
        page_containers = root.find_all("div", "pages")

    for container in page_containers:
        for link in container.find_all("a"):
            href = link.attr("href")
            if not href:
                continue
            normalized = normalize_catalogue_url(href, page_url)
            if not normalized or not is_valid_catalogue_pagination_url(normalized, initial_url):
                continue
            if normalized not in seen:
                seen.add(normalized)
                urls.append(normalized)
    return urls


def current_page_number(root: HtmlNode, page_url: str) -> int | None:
    for container in root.find_all("div", "pages"):
        for link in container.find_all("a"):
            if link.has_class("current"):
                number = parse_int(link.text())
                if number is not None:
                    return number
    return page_number_from_url(page_url)


def parse_coin_table(
    table: HtmlNode,
    *,
    base_url: str,
    warnings: list[dict[str, object]],
    page_url: str | None = None,
) -> dict[str, object]:
    coin_info = table.find("td", "coin-info")
    value_link = coin_info.find("a", "value") if coin_info else None
    detail_path = value_link.attr("href") if value_link else None
    detail_url = urljoin(base_url, detail_path) if detail_path else None

    if not value_link:
        warning(warnings, page_url, detail_path, "detailPath", "Missing td.coin-info > a.value")

    parsed_value = parse_denomination_and_years(value_link.text() if value_link else "")

    subject_node = coin_info.find("div", "subject") if coin_info else None
    info_node = coin_info.find("div", "info") if coin_info else None
    technical = parse_technical_information(info_node.text() if info_node else "")
    if not info_node:
        warning(warnings, page_url, detail_path, "composition", "Missing td.coin-info > div.info")

    stat_node = coin_info.find("div", "coin-stat") if coin_info else None
    explicit_tid = parse_int(stat_node.attr("data-tid") if stat_node else None)
    explicit_pid = parse_int(stat_node.attr("data-pid") if stat_node else None)
    ucoin_type_id = explicit_tid or parse_query_int(detail_path, "tid")
    ucoin_period_id = explicit_pid

    images = {"obverse": None, "reverse": None}
    image_example_year = None
    for img in table.find_all("img"):
        alt = normalize_text(img.attr("alt"))
        src = get_image_src(img)
        if not src:
            continue
        if alt.endswith("- Obverse"):
            images["obverse"] = src
        elif alt.endswith("- Reverse"):
            images["reverse"] = src
        if image_example_year is None:
            image_example_year = parse_image_year(src)

    if images["obverse"] is None:
        warning(warnings, page_url, detail_path, "images.obverse", "Missing obverse image")
    if images["reverse"] is None:
        warning(warnings, page_url, detail_path, "images.reverse", "Missing reverse image")
    if technical["catalogue"]["displayValue"] is None:
        warning(warnings, page_url, detail_path, "catalogue", "Missing catalogue identifier")

    return {
        "denomination": parsed_value["denomination"],
        "issuePeriod": parsed_value["issuePeriod"],
        "imageExampleYear": image_example_year,
        "subject": subject_node.text() if subject_node else None,
        "composition": technical["composition"],
        "weightGrams": technical["weightGrams"],
        "diameterMm": technical["diameterMm"],
        "catalogue": technical["catalogue"],
        "circulationType": technical["circulationType"],
        "ucoinTypeId": ucoin_type_id,
        "ucoinPeriodId": ucoin_period_id,
        "detailPath": detail_path,
        "detailUrl": detail_url,
        "images": images,
    }


def coin_unique_key(coin: dict[str, object]) -> object:
    return coin.get("ucoinTypeId") or (
        coin.get("country"),
        coin.get("denomination", {}).get("displayName"),
        coin.get("issuePeriod", {}).get("displayValue"),
        coin.get("catalogue", {}).get("displayValue"),
        coin.get("detailPath"),
    )


def completeness_score(value: object) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, dict):
        return sum(completeness_score(item) for item in value.values())
    if isinstance(value, list):
        return sum(completeness_score(item) for item in value)
    return 1


def merge_coin_records(existing_coin: dict[str, object], incoming_coin: dict[str, object]) -> dict[str, object]:
    if existing_coin.get("ucoinTypeId") and incoming_coin.get("ucoinTypeId") and existing_coin.get("ucoinTypeId") != incoming_coin.get("ucoinTypeId"):
        return existing_coin

    incoming_is_more_complete = completeness_score(incoming_coin) > completeness_score(existing_coin)
    base = dict(incoming_coin if incoming_is_more_complete else existing_coin)
    other = existing_coin if incoming_is_more_complete else incoming_coin
    for key, value in other.items():
        if base.get(key) in (None, "") and value not in (None, ""):
            base[key] = value
        elif isinstance(base.get(key), dict) and isinstance(value, dict):
            nested = dict(base[key])
            for nested_key, nested_value in value.items():
                if nested.get(nested_key) in (None, "") and nested_value not in (None, ""):
                    nested[nested_key] = nested_value
            base[key] = nested
    return base


def deduplicate_coins(coins: list[dict[str, object]]) -> list[dict[str, object]]:
    deduped: dict[object, dict[str, object]] = {}
    order: list[object] = []
    for coin in coins:
        unique_key = coin_unique_key(coin)
        if unique_key not in deduped:
            deduped[unique_key] = coin
            order.append(unique_key)
        else:
            deduped[unique_key] = merge_coin_records(deduped[unique_key], coin)
    return [deduped[key] for key in order]


def period_key(period: dict[str, object]) -> str:
    if period.get("periodId") is not None:
        return f"pid:{period.get('periodId')}"
    return ":".join(
        [
            "fallback",
            str(period.get("country") or ""),
            str(period.get("rulerOrPeriodName") or ""),
            str(period.get("startYear") or ""),
            str(period.get("endYear") or ""),
        ]
    )


def merge_period_metadata(existing_period: dict[str, object], incoming_period: dict[str, object]) -> None:
    for key in ("periodId", "fullTitle", "country", "rulerOrPeriodName", "startYear", "endYear", "currencyDescription"):
        if existing_period.get(key) in (None, "") and incoming_period.get(key) not in (None, ""):
            existing_period[key] = incoming_period[key]
    existing_pages = existing_period.setdefault("sourcePages", [])
    if isinstance(existing_pages, list):
        for page_url in incoming_period.get("sourcePages", []):
            if page_url not in existing_pages:
                existing_pages.append(page_url)


def merge_periods(periods: list[dict[str, object]]) -> list[dict[str, object]]:
    periods_by_key: dict[str, dict[str, object]] = {}
    order: list[str] = []
    for period in periods:
        key = period_key(period)
        if key not in periods_by_key:
            copied = dict(period)
            copied["coins"] = list(period.get("coins", []))
            copied["sourcePages"] = list(period.get("sourcePages", []))
            periods_by_key[key] = copied
            order.append(key)
            continue
        existing = periods_by_key[key]
        merge_period_metadata(existing, period)
        existing["coins"].extend(period.get("coins", []))

    result = []
    for key in order:
        period = periods_by_key[key]
        period["coins"] = deduplicate_coins(period.get("coins", []))
        result.append(period)
    return result


def find_period_for_coin(periods: list[dict[str, object]], period_id: int | None) -> dict[str, object] | None:
    if period_id is None:
        return None
    return next((period for period in periods if period.get("periodId") == period_id), None)


def create_fallback_period(country: str | None, period_id: int | None, page_url: str | None) -> dict[str, object]:
    return {
        "periodId": period_id,
        "fullTitle": None,
        "country": country,
        "rulerOrPeriodName": None,
        "startYear": None,
        "endYear": None,
        "currencyDescription": None,
        "coins": [],
        "sourcePages": [page_url] if page_url else [],
    }


def parse_catalogue_page(
    html_source: str,
    page_url: str | None = None,
    *,
    initial_url: str | None = None,
    base_url: str = "https://pt.ucoin.net",
) -> dict[str, object]:
    root = parse_html(html_source)
    canonical_node = next((node for node in root.find_all("link") if node.attr("rel") == "canonical"), None)
    canonical_url = canonical_node.attr("href") if canonical_node else None
    country = resolve_country(root, canonical_url)
    warnings: list[dict[str, object]] = []
    periods: list[dict[str, object]] = []
    current_period: dict[str, object] | None = None
    catalogue_root = find_by_id(root, "catalog-list") or root.find("main") or root

    for node in direct_element_children(catalogue_root):
        if node.tag == "header":
            h2 = node.find("h2")
            text = h2.text() if h2 else node.text()
            if "›" in text and re.search(r"\d{4}\s*-\s*\d{4}", text):
                current_period = parse_period_header(text)
                current_period["sourcePages"] = [page_url] if page_url else []
                periods.append(current_period)
            continue

        if current_period is not None and node.has_class("period-hint"):
            current_period["currencyDescription"] = node.text() or None
            continue

        if node.tag != "table" or not node.has_class("coin"):
            continue

        if current_period is None:
            current_period = create_fallback_period(country, None, page_url)
            periods.append(current_period)
            warning(warnings, page_url, None, "historicalPeriod", "Coin appears before any valid catalogue period header")

        coin = parse_coin_table(node, base_url=base_url, warnings=warnings, page_url=page_url)
        explicit_period_id = coin.get("ucoinPeriodId")
        if current_period.get("periodId") is None and coin.get("ucoinPeriodId") is not None:
            current_period["periodId"] = coin.get("ucoinPeriodId")

        target_period = current_period
        if explicit_period_id is not None and current_period.get("periodId") is not None and explicit_period_id != current_period.get("periodId"):
            warning(warnings, page_url, coin.get("detailPath"), "ucoinPeriodId", "Coin data-pid does not match the active catalogue section.")
            matching_period = find_period_for_coin(periods, explicit_period_id if isinstance(explicit_period_id, int) else None)
            if matching_period is None:
                matching_period = create_fallback_period(country, explicit_period_id if isinstance(explicit_period_id, int) else None, page_url)
                periods.append(matching_period)
            target_period = matching_period
        target_period["coins"].append(coin)

    pagination_base_url = page_url or canonical_url or base_url
    pagination_urls = extract_pagination_urls(root, pagination_base_url, initial_url) if page_url or canonical_url else []
    return {
        "country": country,
        "periods": merge_periods(periods),
        "warnings": warnings,
        "paginationUrls": pagination_urls,
        "currentPage": current_page_number(root, pagination_base_url),
    }


def parse_ucoin_catalogue(html_source: str, base_url: str = "https://pt.ucoin.net") -> dict[str, object]:
    page_result = parse_catalogue_page(html_source, base_url=base_url)
    coins: list[dict[str, object]] = []
    for period in page_result["periods"]:
        coins.extend(period.get("coins", []))
    return {"coins": deduplicate_coins(coins), "periods": page_result["periods"], "warnings": page_result["warnings"]}


def coerce_fetch_response(response: str | tuple[str, str | None] | CatalogueFetchResponse, requested_url: str) -> CatalogueFetchResponse:
    if isinstance(response, CatalogueFetchResponse):
        return response
    if isinstance(response, tuple):
        return CatalogueFetchResponse(html=response[0], final_url=response[1])
    return CatalogueFetchResponse(html=response, final_url=requested_url)


def coin_id_set(coins: list[dict[str, object]]) -> set[object]:
    return {coin_unique_key(coin) for coin in coins if coin_unique_key(coin) is not None}


def period_coin_id_set(periods: list[dict[str, object]]) -> set[object]:
    coin_ids: set[object] = set()
    for period in periods:
        coins = period.get("coins", [])
        if isinstance(coins, list):
            coin_ids.update(coin_id_set(coins))
    return coin_ids


def coin_start_year(coin: dict[str, object]) -> int | None:
    start_year = coin.get("startYear")
    if isinstance(start_year, int):
        return start_year
    issue_period = coin.get("issuePeriod")
    if isinstance(issue_period, dict) and isinstance(issue_period.get("startYear"), int):
        return issue_period["startYear"]
    return None


def period_has_coin_starting_at_or_after(period: dict[str, object], start_year: int) -> bool:
    coins = period.get("coins", [])
    if not isinstance(coins, list):
        return False
    for coin in coins:
        if isinstance(coin, dict) and (coin_start_year(coin) or -1) >= start_year:
            return True
    return False


def crawl_ucoin_catalogue(
    initial_url: str,
    fetch_catalogue_page: Callable[[str], str | tuple[str, str | None] | CatalogueFetchResponse],
    *,
    max_pages: int = 50,
    retries: int = 2,
    retry_backoff_seconds: float = 0.5,
    enable_sequential_fallback: bool = False,
    base_url: str = "https://pt.ucoin.net",
    stop_after_start_year_miss: int | None = None,
    stop_after_start_year_miss_limit: int = 3,
) -> dict[str, object]:
    normalized_initial = normalize_catalogue_url(initial_url)
    if not normalized_initial:
        raise ValueError(f"Invalid catalogue URL: {initial_url}")

    pending: dict[str, bool] = {normalized_initial: False}
    visited: list[str] = []
    visited_set: set[str] = set()
    discovered: set[str] = {normalized_initial}
    failed_pages: list[dict[str, str]] = []
    collected_periods: list[dict[str, object]] = []
    collected_warnings: list[dict[str, object]] = []
    previous_coin_ids: set[object] | None = None
    consecutive_start_year_misses = 0
    stopped_early_reason: str | None = None

    while pending and len(visited) < max_pages:
        current_url = sorted(pending, key=lambda item: (page_number_from_url(item), item))[0]
        came_from_fallback = pending.pop(current_url)
        if current_url in visited_set:
            continue

        response: CatalogueFetchResponse | None = None
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = coerce_fetch_response(fetch_catalogue_page(current_url), current_url)
                break
            except CatalogueFetchError as exc:
                last_error = exc
                if not exc.retryable or attempt >= retries:
                    break
                if retry_backoff_seconds > 0:
                    time.sleep(retry_backoff_seconds * (2**attempt))
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                break

        if response is None:
            failed_pages.append({"url": current_url, "message": str(last_error) if last_error else "Request failed"})
            visited.append(current_url)
            visited_set.add(current_url)
            continue

        final_url = normalize_catalogue_url(response.final_url or current_url, current_url)
        if not final_url or not is_valid_catalogue_pagination_url(final_url, normalized_initial):
            failed_pages.append({"url": current_url, "message": f"Redirect left catalogue: {response.final_url or current_url}"})
            visited.append(current_url)
            visited_set.add(current_url)
            continue
        if final_url != current_url and final_url in visited_set:
            failed_pages.append({"url": current_url, "message": f"Redirected to already visited page: {final_url}"})
            visited.append(current_url)
            visited_set.add(current_url)
            continue

        visited.append(final_url)
        visited_set.add(current_url)
        visited_set.add(final_url)

        page_result = parse_catalogue_page(response.html, final_url, initial_url=normalized_initial, base_url=base_url)
        page_periods = page_result["periods"]
        assert isinstance(page_periods, list)
        current_coin_ids = period_coin_id_set(page_periods)
        is_repeated_fallback_page = came_from_fallback and previous_coin_ids is not None and current_coin_ids == previous_coin_ids

        if not is_repeated_fallback_page:
            collected_periods.extend(page_periods)
            collected_warnings.extend(page_result["warnings"])

        if stop_after_start_year_miss is not None:
            for period in page_periods:
                if not isinstance(period, dict):
                    continue
                if period_has_coin_starting_at_or_after(period, stop_after_start_year_miss):
                    consecutive_start_year_misses = 0
                else:
                    consecutive_start_year_misses += 1
                if consecutive_start_year_misses >= stop_after_start_year_miss_limit:
                    stopped_early_reason = (
                        f"Stopped after {consecutive_start_year_misses} consecutive periods with no coins "
                        f"starting at or after {stop_after_start_year_miss}."
                    )
                    pending.clear()
                    break
            if stopped_early_reason is not None:
                break

        for pagination_url in page_result["paginationUrls"]:
            if not isinstance(pagination_url, str):
                continue
            normalized_page_url = normalize_catalogue_url(pagination_url, final_url)
            if not normalized_page_url or not is_valid_catalogue_pagination_url(normalized_page_url, normalized_initial):
                continue
            discovered.add(normalized_page_url)
            if normalized_page_url not in visited_set and normalized_page_url not in pending and len(discovered) <= max_pages:
                pending[normalized_page_url] = False

        if enable_sequential_fallback and not page_result["paginationUrls"] and not came_from_fallback:
            page_number = page_result.get("currentPage")
            if isinstance(page_number, int) and page_number < max_pages:
                next_url = catalogue_url_for_page(final_url, page_number + 1)
                if next_url and next_url not in visited_set and next_url not in pending and is_valid_catalogue_pagination_url(next_url, normalized_initial):
                    discovered.add(next_url)
                    pending[next_url] = True
        elif enable_sequential_fallback and came_from_fallback and current_coin_ids and not is_repeated_fallback_page:
            page_number = page_result.get("currentPage")
            if isinstance(page_number, int) and page_number < max_pages:
                next_url = catalogue_url_for_page(final_url, page_number + 1)
                if next_url and next_url not in visited_set and next_url not in pending and is_valid_catalogue_pagination_url(next_url, normalized_initial):
                    discovered.add(next_url)
                    pending[next_url] = True

        previous_coin_ids = current_coin_ids

    return {
        "country": next((period.get("country") for period in collected_periods if period.get("country")), None),
        "periods": merge_periods(collected_periods),
        "warnings": collected_warnings,
        "pagination": {
            "initialUrl": normalized_initial,
            "visitedPages": visited,
            "pagesProcessed": len(visited),
            "discoveredPages": len(discovered),
            "failedPages": failed_pages,
            "stoppedEarly": stopped_early_reason is not None,
            "stoppedEarlyReason": stopped_early_reason,
        },
    }