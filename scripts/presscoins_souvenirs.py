#!/usr/bin/env python3
"""Collect Presscoins search results into a reviewable Base44 Souvenir preview."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen


PRESSCOINS_BASE_URL = "https://www.presscoins.com/"
PRESSCOINS_SEARCH_URL = urljoin(PRESSCOINS_BASE_URL, "search/")
AVAILABILITY_FOLDER = {"1": "atuais", "All": "todas", "0": "retiradas"}
AVAILABILITY_LABEL = {
    "1": "atuais / disponíveis",
    "All": "todos os designs",
    "0": "retirados",
}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/140.0 Safari/537.36"
)


@dataclass(frozen=True)
class Presscoin:
    catalog_number: str
    location: str
    description: str
    position: str
    coin_type: str
    orientation: str
    availability: str
    image_url: str
    thumbnail_url: str


class PresscoinsSearchParser(HTMLParser):
    """Parse the result tables used by the Presscoins search page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[Presscoin] = []
        self._table_depth = 0
        self._current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "table":
            classes = set(attributes.get("class", "").split())
            if self._current is None and classes.intersection({"row", "row_alt"}):
                self._current = {"text": [], "image_url": "", "thumbnail_url": ""}
                self._table_depth = 1
                return
            if self._current is not None:
                self._table_depth += 1

        if self._current is None:
            return
        if tag == "a" and "lightbox" in attributes.get("class", "").split():
            self._current["image_url"] = absolute_url(attributes.get("href", ""))
        elif tag == "img" and "thumbnail" in attributes.get("class", "").split():
            self._current["thumbnail_url"] = absolute_url(attributes.get("src", ""))

    def handle_endtag(self, tag: str) -> None:
        if self._current is None or tag != "table":
            return
        self._table_depth -= 1
        if self._table_depth == 0:
            parsed = parse_result_table(
                " ".join(self._current["text"]),
                image_url=str(self._current["image_url"]),
                thumbnail_url=str(self._current["thumbnail_url"]),
            )
            if parsed is not None:
                self.results.append(parsed)
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            cleaned = " ".join(data.replace("\xa0", " ").split())
            if cleaned:
                self._current["text"].append(cleaned)


def absolute_url(value: str) -> str:
    if not value:
        return ""
    # Keep URL separators intact while encoding spaces in paths such as
    # "coin_images/Magic Kingdom".
    return quote(urljoin(PRESSCOINS_BASE_URL, value), safe=":/?=&%#")


def labeled_value(text: str, label: str, following_labels: list[str]) -> str:
    boundaries = "|".join(re.escape(next_label) for next_label in following_labels)
    match = re.search(
        rf"\b{re.escape(label)}:\s*(.*?)(?=\s+(?:{boundaries}):|$)",
        text,
        flags=re.IGNORECASE,
    )
    return " ".join(match.group(1).split()) if match else ""


def parse_result_table(text: str, *, image_url: str, thumbnail_url: str) -> Presscoin | None:
    labels = [
        "Location",
        "Description",
        "Position",
        "Coin Type",
        "Orientation",
        "Availability",
        "Series",
        "Variation",
        "Catalog Number",
    ]
    values = {
        label: labeled_value(text, label, [item for item in labels if item != label])
        for label in labels
    }
    catalog_number = values["Catalog Number"]
    if not catalog_number:
        return None
    return Presscoin(
        catalog_number=catalog_number,
        location=values["Location"],
        description=values["Description"],
        position=values["Position"],
        coin_type=values["Coin Type"],
        orientation=values["Orientation"],
        availability=values["Availability"],
        image_url=image_url,
        thumbnail_url=thumbnail_url,
    )


def parse_search_results(source_html: str) -> list[Presscoin]:
    parser = PresscoinsSearchParser()
    parser.feed(source_html)
    parser.close()
    return parser.results


def build_search_url(
    *,
    location: str,
    search: str,
    availability: str = "All",
    coin_type: str = "All",
    page: int = 1,
) -> str:
    query = urlencode(
        {
            "availability": availability,
            "locations": location,
            "search": search,
            "limit": "50",
            "sort": "catalog",
            "cointype": coin_type,
            "Submit": "Search",
            "page": str(page),
        }
    )
    return f"{PRESSCOINS_SEARCH_URL}?{query}"


def pagination_page_count(source_html: str) -> int:
    decoded = html.unescape(source_html)
    page_numbers = [int(value) for value in re.findall(r"[?&]page=(\d+)", decoded)]
    return max(page_numbers, default=1)


def fetch_html(url: str, timeout: float = 30.0) -> str:
    request = Request(url, headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "text/html"})
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"Presscoins respondeu com HTTP {exc.code}: {url}") from exc
    except URLError as exc:
        raise RuntimeError(f"Não foi possível aceder ao Presscoins: {exc.reason}") from exc


def collect_search_results(
    *,
    location: str,
    search: str,
    availability: str,
    coin_type: str,
    max_pages: int,
    fetcher: Any = fetch_html,
) -> tuple[list[Presscoin], int]:
    first_url = build_search_url(
        location=location,
        search=search,
        availability=availability,
        coin_type=coin_type,
        page=1,
    )
    first_html = fetcher(first_url)
    total_pages = pagination_page_count(first_html)
    if total_pages > max_pages:
        raise ValueError(
            f"A pesquisa tem {total_pages} páginas, acima do limite de segurança "
            f"de {max_pages}. Aumenta --max-pages para continuar."
        )

    all_coins: list[Presscoin] = []
    seen_catalog_numbers: set[str] = set()
    for page in range(1, total_pages + 1):
        source_html = first_html if page == 1 else fetcher(
            build_search_url(
                location=location,
                search=search,
                availability=availability,
                coin_type=coin_type,
                page=page,
            )
        )
        page_coins = parse_search_results(source_html)
        new_count = 0
        for coin in page_coins:
            if coin.catalog_number in seen_catalog_numbers:
                continue
            seen_catalog_numbers.add(coin.catalog_number)
            all_coins.append(coin)
            new_count += 1
        print(
            f"Página {page}/{total_pages} — {len(page_coins)} resultados "
            f"({new_count} novos; {len(all_coins)} acumulados)"
        )
    return all_coins, total_pages


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.casefold()).strip("-")


def default_output_directory(location: str, search: str, availability: str = "All") -> Path:
    base = (
        Path("info")
        / "souvenirs"
        / "america"
        / "eua"
        / "orlando"
        / slugify(location)
    )
    search_slug = slugify(search)
    availability_folder = AVAILABILITY_FOLDER.get(availability, slugify(availability))
    if not search_slug:
        return base / availability_folder
    if availability == "All":
        return base / search_slug
    return base / search_slug / availability_folder


def subject_from_description(description: str) -> str:
    if not description:
        return "Moeda prensada"
    match = re.match(
        r"^(.+?)(?:\s+(?:standing|looking|walking|above|behind|facing|with|on)\b|$)",
        description,
        flags=re.IGNORECASE,
    )
    subject = match.group(1).strip(" ,.-") if match else description.strip()
    return subject or "Moeda prensada"


def reference_url(catalog_number: str) -> str:
    return build_search_url(location="All", search=catalog_number)


def display_location(location: str) -> str:
    park, _, _venue = location.partition(",")
    return park.strip()


def venue_from_location(location: str) -> str:
    _park, separator, venue = location.partition(",")
    return venue.strip() if separator else ""


def notes_for(coin: Presscoin) -> str:
    parts = [
        venue_from_location(coin.location),
        f"Posição {coin.position}" if coin.position else "",
        f"Catálogo Presscoins: {coin.catalog_number}",
    ]
    return " · ".join(part for part in parts if part)


def build_catalog(
    coins: list[Presscoin],
    *,
    query_url: str,
    location: str,
    search: str,
    country: str,
    city: str,
    page_count: int = 1,
    availability: str = "All",
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for order, coin in enumerate(coins, start=1):
        subject = subject_from_description(coin.description)
        name = f"{subject} — {search}" if search and search not in subject else subject
        souvenir = {
            "name": name,
            "continent": "América",
            "country": country,
            "city": city,
            "type": "pressed",
            "condition": "Não Tenho",
            "location_name": display_location(coin.location),
            "description": coin.description,
            "display_shape": "oval",
            "image_front": coin.image_url,
            "image_back": "",
            "notes": notes_for(coin),
            "reference_url": reference_url(coin.catalog_number),
            "ordem": order,
            "hidden": False,
        }
        items.append({"source": asdict(coin), "souvenir": souvenir})

    return {
        "status": "pending_review",
        "base44_updated": False,
        "source": {
            "site": "Presscoins",
            "query_url": query_url,
            "location": location,
            "search": search,
            "availability": availability,
            "availability_label": AVAILABILITY_LABEL.get(availability, availability),
            "page_count": page_count,
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "result_count": len(items),
        },
        "items": items,
    }


def preview_html(catalog: dict[str, Any]) -> str:
    cards: list[str] = []
    for item in catalog["items"]:
        source = item["source"]
        souvenir = item["souvenir"]
        image_url = str(souvenir.get("image_front") or "")
        image = (
            f'<a href="{html.escape(image_url)}"><img src="{html.escape(image_url)}" '
            f'alt="{html.escape(str(souvenir["name"]))}"></a>'
            if image_url
            else '<div class="missing">Sem fotografia no Presscoins</div>'
        )
        cards.append(
            "\n".join(
                [
                    '<article class="card">',
                    image,
                    f'<h2>{html.escape(str(souvenir["name"]))}</h2>',
                    f'<p class="catalog">{html.escape(str(source["catalog_number"]))}</p>',
                    f'<p>{html.escape(str(souvenir["description"]))}</p>',
                    f'<p><strong>Local:</strong> {html.escape(str(souvenir["location_name"]))}</p>',
                    f'<p><strong>Notas:</strong> {html.escape(str(souvenir["notes"]))}</p>',
                    f'<p><a href="{html.escape(str(souvenir["reference_url"]))}">Ver no Presscoins</a></p>',
                    "</article>",
                ]
            )
        )

    source = catalog["source"]
    search_label = str(source["search"] or source.get("availability_label") or "todas as moedas")
    return f"""<!doctype html>
<html lang="pt">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Souvenirs Presscoins — {html.escape(str(source['location']))}</title>
  <style>
    body {{ background:#f4f2ed; color:#24221f; font:16px/1.45 system-ui,sans-serif; margin:0; padding:32px; }}
    header {{ margin:0 auto 28px; max-width:1100px; }}
    .grid {{ display:grid; gap:22px; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); margin:auto; max-width:1100px; }}
    .card {{ background:#fff; border-radius:14px; box-shadow:0 4px 18px #0001; padding:18px; }}
    .card img {{ display:block; height:250px; margin:auto; max-width:100%; object-fit:contain; }}
    h1 {{ margin-bottom:6px; }} h2 {{ font-size:1.15rem; margin-bottom:4px; }}
    .catalog {{ color:#6d665d; margin-top:0; }} .missing {{ padding:80px 10px; text-align:center; }}
  </style>
</head>
<body>
  <header>
    <h1>{html.escape(str(source['location']))} — {html.escape(search_label)}</h1>
    <p>{source['result_count']} souvenirs para rever. Nenhuma alteração foi feita no Site Base44.</p>
    <p><a href="{html.escape(str(source['query_url']))}">Abrir pesquisa original</a></p>
  </header>
  <main class="grid">{''.join(cards)}</main>
</body>
</html>
"""


def write_outputs(catalog: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = output_dir / "presscoins-catalog.json"
    preview_path = output_dir / "preview.html"
    catalog_path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    preview_path.write_text(preview_html(catalog), encoding="utf-8")
    return catalog_path, preview_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recolhe souvenirs do Presscoins e cria JSON/HTML para revisão, sem alterar o Base44."
    )
    parser.add_argument("--location", default="Magic Kingdom", help="Localização no Presscoins.")
    parser.add_argument("--search", default="", help="Texto a pesquisar; vazio recolhe todas as moedas.")
    parser.add_argument(
        "--availability",
        choices=["1", "All", "0"],
        default="1",
        help="1 (atuais), All (todos) ou 0 (retirados). Por omissão: 1.",
    )
    parser.add_argument("--coin-type", default="All", help="All, Cent, Quarter ou Dime.")
    parser.add_argument("--country", default="Estados Unidos da América")
    parser.add_argument("--city", default="Orlando")
    parser.add_argument("--output-dir", type=Path, help="Pasta para JSON e preview.html.")
    parser.add_argument("--html-input", type=Path, help="Ler HTML local em vez de aceder à rede.")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=100,
        help="Limite de segurança de páginas a percorrer (por omissão: 100).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    query_url = build_search_url(
        location=args.location,
        search=args.search,
        availability=args.availability,
        coin_type=args.coin_type,
    )
    try:
        if args.max_pages < 1:
            raise ValueError("--max-pages tem de ser pelo menos 1.")
        if args.html_input:
            coins = parse_search_results(args.html_input.read_text(encoding="utf-8"))
            page_count = 1
        else:
            coins, page_count = collect_search_results(
                location=args.location,
                search=args.search,
                availability=args.availability,
                coin_type=args.coin_type,
                max_pages=args.max_pages,
            )
        if not coins:
            print("O Presscoins não devolveu moedas para esta pesquisa.", file=sys.stderr)
            return 1
        catalog = build_catalog(
            coins,
            query_url=query_url,
            location=args.location,
            search=args.search,
            country=args.country,
            city=args.city,
            page_count=page_count,
            availability=args.availability,
        )
        output_dir = args.output_dir or default_output_directory(
            args.location, args.search, args.availability
        )
        catalog_path, preview_path = write_outputs(catalog, output_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2

    with_photos = sum(bool(coin.image_url) for coin in coins)
    print(f"Encontrados: {len(coins)} souvenirs")
    print(f"Com fotografia: {with_photos}")
    print(f"Sem fotografia: {len(coins) - with_photos}")
    if len(coins) <= 25:
        for index, item in enumerate(catalog["items"], start=1):
            photo = "com fotografia" if item["souvenir"]["image_front"] else "sem fotografia"
            print(f"{index}/{len(coins)} — {item['souvenir']['name']} — {photo}")
    else:
        print("O detalhe individual está no catálogo JSON e na pré-visualização HTML.")
    print(f"\nCatálogo pendente: {catalog_path}")
    print(f"Pré-visualização: {preview_path}")
    print("Site Base44: nenhuma alteração efetuada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
