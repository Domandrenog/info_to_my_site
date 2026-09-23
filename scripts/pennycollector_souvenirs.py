#!/usr/bin/env python3
"""Build a review-only souvenir catalogue from a PennyCollector location page."""

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
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen


PENNYCOLLECTOR_BASE_URL = "http://locations.pennycollector.com/"
DEFAULT_LOCATION_ID = "1851"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/140.0 Safari/537.36"
)
ORIENTATION_LABELS = {"H": "horizontal", "V": "vertical", "": "não indicada"}
REVIEW_FLAG_LABELS = {
    "shared_machine_photo": "fotografia partilhada da máquina",
    "missing_photo": "fotografia em falta",
    "missing_orientation": "orientação em falta",
    "possible_source_typo": "possível erro no texto de origem",
}
SUSPECT_SOURCE_PHRASES = ("Artemus", "Pace Shuttle Program", "Heat with")
COUNTRY_ALIASES = {
    "Australia": "Austrália",
    "Belarus": "Bielorrússia",
    "Brazil": "Brasil",
    "Canada": "Canadá",
    "Croatia": "Croácia",
    "Czech Republic": "República Checa",
    "Denmark": "Dinamarca",
    "Hungary": "Hungria",
    "India": "Índia",
    "Japan": "Japão",
    "Kazakhstan": "Cazaquistão",
    "Macao": "Macau",
    "Malaysia": "Malásia",
    "New Zealand": "Nova Zelândia",
    "Poland": "Polónia",
    "Romania": "Roménia",
    "Russia": "Rússia",
    "Singapore": "Singapura",
    "South Africa": "África do Sul",
    "South Korea": "Coreia do Sul",
    "Sweden": "Suécia",
    "Switzerland": "Suíça",
    "Thailand": "Tailândia",
    "Turkey": "Turquia",
    "United Arab Emirates": "Emirados Árabes Unidos",
    "United States": "EUA",
}
EUROPE_COUNTRIES = {
    "Austria", "Belarus", "Belgium", "Bulgaria", "Croatia", "Cyprus",
    "Czech Republic", "Denmark", "England", "Estonia", "Finland", "France",
    "Germany", "Gibraltar", "Greece", "Hungary", "Ireland", "Italy", "Jersey",
    "Latvia", "Liechtenstein", "Lithuania", "Malta", "Netherlands",
    "Northern Ireland", "Norway", "Poland", "Portugal", "Principality of Monaco",
    "Romania", "Russia", "San Marino", "Scotland", "Slovenia", "Spain", "Sweden",
    "Switzerland", "Turkey", "Ukraine", "Wales",
}
ASIA_COUNTRIES = {
    "China", "Hong Kong", "India", "Israel", "Japan", "Kazakhstan", "Macao",
    "Malaysia", "Singapore", "South Korea", "Taiwan", "Thailand",
    "United Arab Emirates",
}
AFRICA_COUNTRIES = {"South Africa"}
OCEANIA_COUNTRIES = {"Australia", "Guam", "New Zealand"}


@dataclass(frozen=True)
class PressedDesign:
    machine_number: int
    machine_details: str
    position: int
    description: str
    orientation: str
    machine_image_url: str


class PennyCollectorPageParser(HTMLParser):
    """Extract form metadata and machine photo cards from the legacy page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inputs: dict[str, str] = {}
        self.selected_options: dict[str, str] = {}
        self.machine_images: dict[int, str] = {}
        self._select_id = ""
        self._selected_option = False
        self._selected_text: list[str] = []
        self._capturing_title = False
        self._title_text: list[str] = []
        self._pending_title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "input" and attributes.get("id"):
            self.inputs[attributes["id"]] = attributes.get("value", "")
        elif tag == "select":
            self._select_id = attributes.get("id", "")
        elif tag == "option" and self._select_id and "selected" in attributes:
            self._selected_option = True
            self._selected_text = []
        elif tag == "span" and "pagetitle" in attributes.get("class", "").split():
            self._capturing_title = True
            self._title_text = []
        elif tag == "img" and self._pending_title:
            match = re.match(r"Machine\s+(\d+)\b", self._pending_title, flags=re.IGNORECASE)
            source = attributes.get("src", "")
            if match and source:
                self.machine_images[int(match.group(1))] = urljoin(
                    PENNYCOLLECTOR_BASE_URL, source
                )
            self._pending_title = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._selected_option:
            self.selected_options[self._select_id] = " ".join(self._selected_text).strip()
            self._selected_option = False
        elif tag == "select":
            self._select_id = ""
        elif tag == "span" and self._capturing_title:
            self._pending_title = " ".join(self._title_text).strip()
            self._capturing_title = False

    def handle_data(self, data: str) -> None:
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        if self._selected_option:
            self._selected_text.append(cleaned)
        if self._capturing_title:
            self._title_text.append(cleaned)


class FragmentTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line).strip()


def location_url(location_id: str) -> str:
    return f"{PENNYCOLLECTOR_BASE_URL}Details.aspx?location={location_id}"


def reference_id(value: str, parameter: str) -> str:
    cleaned = value.strip()
    if cleaned.isdigit():
        return cleaned
    parsed = urlparse(cleaned)
    values = parse_qs(parsed.query).get(parameter, [])
    if parsed.netloc.casefold().endswith("pennycollector.com") and values:
        candidate = values[0].strip()
        if candidate.isdigit():
            return candidate
    raise ValueError(
        f"Indica um ID numérico ou um link PennyCollector com ?{parameter}=..."
    )


def fetch_html(url: str, timeout: float = 30.0) -> str:
    request = Request(url, headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "text/html"})
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"PennyCollector respondeu com HTTP {exc.code}: {url}") from exc
    except URLError as exc:
        raise RuntimeError(f"Não foi possível aceder ao PennyCollector: {exc.reason}") from exc


def html_fragment_text(fragment: str) -> str:
    parser = FragmentTextParser()
    parser.feed(fragment)
    parser.close()
    return parser.text()


def active_machines_fragment(source_html: str) -> str:
    starts = list(
        re.finditer(r"<b>\s*Active machines\s*</b>", source_html, flags=re.IGNORECASE)
    )
    if not starts:
        raise ValueError("A página não contém a secção Active machines.")
    start = starts[-1].end()
    end_match = re.search(
        r"<b>\s*(?:Medallion\s*/?\s*Token machines|Retired machines\s*/?\s*designs)",
        source_html[start:],
        flags=re.IGNORECASE,
    )
    end = start + end_match.start() if end_match else len(source_html)
    return source_html[start:end]


def parse_orientation(description: str) -> tuple[str, str]:
    prefix = re.match(r"^\s*\((H|V)\)\s*", description, flags=re.IGNORECASE)
    if prefix:
        return description[prefix.end() :].rstrip(" .,;"), prefix.group(1).upper()
    patterns = [
        (r"\s*\((H|V)\)\s*\.?\s*$", {"H": "H", "V": "V"}),
        (
            r"\s*\((Horizontal|Vertical)\s+design\)\s*\.?\s*$",
            {"HORIZONTAL": "H", "VERTICAL": "V"},
        ),
    ]
    for pattern, values in patterns:
        match = re.search(pattern, description, flags=re.IGNORECASE)
        if match:
            cleaned = description[: match.start()].rstrip(" .,;")
            return cleaned, values[match.group(1).upper()]
    return description.rstrip(" .,;"), ""


def parse_simple_designs(
    source_html: str, page_parser: PennyCollectorPageParser
) -> list[PressedDesign]:
    container = re.search(
        r"<td[^>]+id=[\"']DescriptionContainer[\"'][^>]*>(.*?)</td>",
        source_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not container:
        return []
    content = container.group(1)
    heading = re.search(r"Designs\s+are\s*:", content, flags=re.IGNORECASE)
    if not heading:
        return []
    design_fragment = content[heading.end() :]
    paragraph_end = re.search(r"<p\b", design_fragment, flags=re.IGNORECASE)
    if paragraph_end:
        design_fragment = design_fragment[: paragraph_end.start()]
    design_text = html_fragment_text(design_fragment)
    numbered = list(re.finditer(r"(?<!\d)([1-9]\d*)\)\s*", design_text))
    if not numbered:
        return []

    machine_numbers = sorted(
        int(match.group(1))
        for key in page_parser.inputs
        if (match := re.fullmatch(r"ReportLocation_Machine(\d+)_MachineName", key))
    )
    machine_number = machine_numbers[0] if machine_numbers else 1
    machine_name = page_parser.inputs.get(
        f"ReportLocation_Machine{machine_number}_MachineName", f"Machine {machine_number}"
    )
    context = design_text[: numbered[0].start()].strip(" :-")
    machine_details = " · ".join(part for part in (machine_name, context) if part)
    designs: list[PressedDesign] = []
    for index, marker in enumerate(numbered):
        end = numbered[index + 1].start() if index + 1 < len(numbered) else len(design_text)
        description = design_text[marker.end() : end].strip(" \n;.")
        description, orientation = parse_orientation(description)
        if description:
            designs.append(
                PressedDesign(
                    machine_number=machine_number,
                    machine_details=machine_details,
                    position=int(marker.group(1)),
                    description=description,
                    orientation=orientation,
                    machine_image_url=page_parser.machine_images.get(machine_number, ""),
                )
            )
    return designs


def parse_description_machine_designs(
    source_html: str, page_parser: PennyCollectorPageParser
) -> list[PressedDesign]:
    container = re.search(
        r"<td[^>]+id=[\"']DescriptionContainer[\"'][^>]*>(.*?)</td>",
        source_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not container:
        return []
    content = container.group(1)
    headers = list(
        re.finditer(r"<b>\s*Machine\s+(\d+)\s*</b>", content, flags=re.IGNORECASE)
    )
    designs: list[PressedDesign] = []
    for index, header in enumerate(headers):
        machine_number = int(header.group(1))
        block_end = headers[index + 1].start() if index + 1 < len(headers) else len(content)
        block = content[header.end() : block_end]
        first_number = re.search(
            r"(?:^|<br\s*/?>)\s*[1-9]\d*\s*[.)]\s*",
            block,
            flags=re.IGNORECASE,
        )
        if not first_number:
            continue
        paragraph_end = re.search(r"<p\b", block[first_number.start() :], flags=re.IGNORECASE)
        if paragraph_end:
            block = block[: first_number.start() + paragraph_end.start()]
        block_text = html_fragment_text(block)
        numbered = list(
            re.finditer(r"(?m)^\s*([1-9]\d*)\s*[.)]\s*", block_text)
        )
        if not numbered:
            continue
        machine_details = block_text[: numbered[0].start()].strip(" :-")
        for design_index, numbered_item in enumerate(numbered):
            description_end = (
                numbered[design_index + 1].start()
                if design_index + 1 < len(numbered)
                else len(block_text)
            )
            description = block_text[numbered_item.end() : description_end].strip(" \n;.")
            description, orientation = parse_orientation(description)
            if description:
                designs.append(
                    PressedDesign(
                        machine_number=machine_number,
                        machine_details=machine_details,
                        position=int(numbered_item.group(1)),
                        description=description,
                        orientation=orientation,
                        machine_image_url=page_parser.machine_images.get(machine_number, ""),
                    )
                )
    return designs


def parse_designs(source_html: str) -> tuple[list[PressedDesign], dict[str, Any]]:
    page_parser = PennyCollectorPageParser()
    page_parser.feed(source_html)
    page_parser.close()

    try:
        fragment = active_machines_fragment(source_html)
    except ValueError:
        fragment = ""
    headers = list(
        re.finditer(r"<b>\s*Machine\s+(\d+)\s*:?[\s]*</b>", fragment, flags=re.IGNORECASE)
    )

    designs: list[PressedDesign] = []
    for index, header in enumerate(headers):
        machine_number = int(header.group(1))
        block_end = headers[index + 1].start() if index + 1 < len(headers) else len(fragment)
        block_text = html_fragment_text(fragment[header.end() : block_end])
        numbered = list(re.finditer(r"(?<!\d)([1-9]\d*)\)\s*", block_text))
        if not numbered:
            continue
        machine_details = block_text[: numbered[0].start()]
        machine_details = re.sub(
            r"(?:The\s+designs\s+are|Designs)\s*:\s*$",
            "",
            machine_details,
            flags=re.IGNORECASE,
        ).strip(" :-")
        for design_index, marker in enumerate(numbered):
            content_end = (
                numbered[design_index + 1].start()
                if design_index + 1 < len(numbered)
                else len(block_text)
            )
            description = block_text[marker.end() : content_end].strip(" \n;.")
            description, orientation = parse_orientation(description)
            if description:
                designs.append(
                    PressedDesign(
                        machine_number=machine_number,
                        machine_details=machine_details,
                        position=int(marker.group(1)),
                        description=description,
                        orientation=orientation,
                        machine_image_url=page_parser.machine_images.get(machine_number, ""),
                    )
                )

    if not designs:
        designs = parse_simple_designs(source_html, page_parser)
    if not designs:
        designs = parse_description_machine_designs(source_html, page_parser)
    if not designs:
        raise ValueError("Não foram encontrados designs reconhecíveis na página.")

    metadata = {
        "location_name": page_parser.inputs.get("ReportLocation_Location", ""),
        "address": page_parser.inputs.get("ReportLocation_Address", ""),
        "city": page_parser.inputs.get("ReportLocation_City", ""),
        "zip_code": page_parser.inputs.get("ReportLocation_Zip", "").strip(),
        "status": page_parser.selected_options.get("ReportLocation_StatusList", ""),
        "state": page_parser.selected_options.get("ReportLocation_StateList", ""),
        "country": page_parser.selected_options.get("ReportLocation_CountryList", ""),
        "source_flagged_needs_update": "Needs Updating" in html_fragment_text(source_html),
    }
    return designs, metadata


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.casefold()).strip("-")


def country_settings(source_country: str) -> tuple[str, str, str, str]:
    country = COUNTRY_ALIASES.get(source_country, source_country or "Desconhecido")
    if source_country in EUROPE_COUNTRIES:
        continent, continent_folder = "Europa", "europa"
    elif source_country in ASIA_COUNTRIES:
        continent, continent_folder = "Ásia", "asia"
    elif source_country in AFRICA_COUNTRIES:
        continent, continent_folder = "África", "africa"
    elif source_country in OCEANIA_COUNTRIES:
        continent, continent_folder = "Oceânia", "oceania"
    else:
        continent, continent_folder = "América", "america"
    country_folder = "eua" if source_country == "United States" else slugify(country)
    return continent, continent_folder, country, country_folder


def short_location_name(value: str) -> str:
    return re.sub(r"\s+Visitor (?:Complex|Center)\s*$", "", value).strip() or value


def display_name(description: str) -> str:
    quoted_subject = re.match(r"^[\"'‘’]([^\"'‘’]+)[\"'‘’]\s*,?", description)
    if quoted_subject:
        name = quoted_subject.group(1)
    else:
        name = re.split(
            r",\s*(?:inscribed|with|without)\b",
            description,
            maxsplit=1,
            flags=re.I,
        )[0]
    name = name.strip(" '\".,;")
    if len(name) > 58:
        name = name[:57].rsplit(" ", 1)[0].rstrip(" ,.-") + "…"
    return name or "Pressed penny"


def build_catalog(
    designs: list[PressedDesign],
    metadata: dict[str, Any],
    *,
    location_id: str,
    country: str = "",
) -> dict[str, Any]:
    source_url = location_url(location_id)
    continent, _continent_folder, detected_country, _country_folder = country_settings(
        str(metadata.get("country") or "")
    )
    country = country or detected_country
    short_location = short_location_name(str(metadata.get("location_name") or ""))
    items: list[dict[str, Any]] = []
    for order, design in enumerate(designs, start=1):
        orientation_label = ORIENTATION_LABELS[design.orientation]
        review_flags = ["shared_machine_photo"] if design.machine_image_url else ["missing_photo"]
        if not design.orientation:
            review_flags.append("missing_orientation")
        if any(
            phrase.casefold() in design.description.casefold()
            for phrase in SUSPECT_SOURCE_PHRASES
        ):
            review_flags.append("possible_source_typo")
        source = asdict(design)
        source.update(
            {
                "location_id": location_id,
                "image_scope": "machine" if design.machine_image_url else "none",
                "review_flags": review_flags,
            }
        )
        note_parts = [
            f"Machine {design.machine_number}",
            f"Posição {design.position}",
            f"Orientação {orientation_label}",
            f"PennyCollector location {location_id}",
        ]
        if design.machine_image_url:
            note_parts.append("Fotografia provisória partilhada da máquina")
        souvenir = {
            "name": display_name(design.description),
            "continent": continent,
            "country": country,
            "city": metadata.get("city") or "Desconhecida",
            "type": "pressed",
            "condition": "Não Tenho",
            "location_name": short_location,
            "description": design.description,
            "display_shape": "oval",
            "image_front": design.machine_image_url,
            "image_back": "",
            "notes": " · ".join(note_parts),
            "reference_url": (
                f"{source_url}#machine-{design.machine_number}-position-{design.position}"
            ),
            "ordem": order,
            "hidden": False,
        }
        items.append({"source": source, "souvenir": souvenir, "review_status": "pending"})

    machine_numbers = sorted({design.machine_number for design in designs})
    return {
        "status": "pending_review",
        "base44_updated": False,
        "import_ready": False,
        "source": {
            "site": "PennyCollector",
            "location_id": location_id,
            "url": source_url,
            **metadata,
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "machine_count": len(machine_numbers),
            "design_count": len(items),
            "machine_numbers": machine_numbers,
            "photo_policy": "A fotografia pertence à máquina e é partilhada pelos seus designs.",
        },
        "items": items,
    }


def preview_html(catalog: dict[str, Any]) -> str:
    groups: dict[int, list[dict[str, Any]]] = {}
    for item in catalog["items"]:
        groups.setdefault(int(item["source"]["machine_number"]), []).append(item)

    sections: list[str] = []
    for machine_number, items in groups.items():
        first = items[0]
        source = first["source"]
        image_url = str(source.get("machine_image_url") or "")
        image = (
            f'<a href="{html.escape(image_url)}"><img src="{html.escape(image_url)}" '
            f'alt="Machine {machine_number}"></a>'
            if image_url
            else '<div class="missing">Sem fotografia da máquina</div>'
        )
        designs = []
        for item in items:
            item_source = item["source"]
            souvenir = item["souvenir"]
            flags = ", ".join(
                REVIEW_FLAG_LABELS.get(flag, flag)
                for flag in item_source["review_flags"]
            )
            designs.append(
                "".join(
                    [
                        '<li class="design">',
                        f'<h3>{item_source["position"]}. {html.escape(str(souvenir["name"]))}</h3>',
                        f'<p>{html.escape(str(item_source["description"]))}</p>',
                        f'<p><strong>Orientação:</strong> {html.escape(ORIENTATION_LABELS[item_source["orientation"]])}</p>',
                        f'<p class="flags"><strong>Revisão:</strong> {html.escape(flags)}</p>',
                        "</li>",
                    ]
                )
            )
        sections.append(
            "".join(
                [
                    '<section class="machine">',
                    f'<h2>Machine {machine_number}</h2>',
                    f'<p>{html.escape(str(source["machine_details"]))}</p>',
                    '<p class="warning">A fotografia abaixo pertence à máquina e pode mostrar os quatro designs.</p>',
                    image,
                    f'<ol>{"".join(designs)}</ol>',
                    "</section>",
                ]
            )
        )

    source = catalog["source"]
    source_warning = (
        '<p class="warning"><strong>Atenção:</strong> a própria página está marcada como '
        '“Needs Updating”. Confirma os designs antes de qualquer importação.</p>'
        if source.get("source_flagged_needs_update")
        else ""
    )
    return f"""<!doctype html>
<html lang="pt">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PennyCollector — {html.escape(str(source['location_name']))}</title>
  <style>
    body {{ background:#f4f2ed; color:#24221f; font:16px/1.45 system-ui,sans-serif; margin:0; padding:32px; }}
    header,.machine {{ margin:0 auto 28px; max-width:1050px; }}
    .machine {{ background:#fff; border-radius:14px; box-shadow:0 4px 18px #0001; padding:22px; }}
    .machine>img,.machine>a>img {{ display:block; max-height:520px; max-width:100%; object-fit:contain; }}
    ol {{ display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); padding-left:24px; }}
    .design {{ border-top:1px solid #ddd; padding:8px 12px 4px 4px; }}
    .warning {{ background:#fff1c7; border-radius:8px; padding:10px 12px; }}
    .flags {{ color:#8a3b16; }} .missing {{ background:#eee; padding:60px; text-align:center; }}
  </style>
</head>
<body>
  <header>
    <h1>{html.escape(str(source['location_name']))}</h1>
    <p>{source['machine_count']} máquinas · {source['design_count']} designs pendentes de revisão.</p>
    <p><strong>Site Base44:</strong> nenhuma alteração efetuada. Este catálogo ainda não está pronto para importar.</p>
    {source_warning}
    <p><a href="{html.escape(str(source['url']))}">Abrir página original</a></p>
  </header>
  {''.join(sections)}
</body>
</html>
"""


def default_output_directory(metadata: dict[str, Any]) -> Path:
    _continent, continent_folder, _country, country_folder = country_settings(
        str(metadata.get("country") or "")
    )
    return (
        Path("info")
        / "souvenirs"
        / continent_folder
        / country_folder
        / slugify(str(metadata.get("city") or "cidade-desconhecida"))
        / slugify(short_location_name(str(metadata.get("location_name") or "localizacao")))
    )


def write_outputs(catalog: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = output_dir / "pennycollector-catalog.json"
    preview_path = output_dir / "preview.html"
    catalog_path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    preview_path.write_text(preview_html(catalog), encoding="utf-8")
    return catalog_path, preview_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recolhe uma localização PennyCollector para revisão sem alterar o Base44."
    )
    parser.add_argument(
        "--location-id",
        default=DEFAULT_LOCATION_ID,
        help="ID numérico ou link Details.aspx?location=...",
    )
    parser.add_argument("--country", default="", help="Substituir o país detetado.")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--html-input", type=Path, help="Usar HTML local sem aceder à rede.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        location_id = reference_id(args.location_id, "location")
        source_html = (
            args.html_input.read_text(encoding="utf-8")
            if args.html_input
            else fetch_html(location_url(location_id))
        )
        designs, metadata = parse_designs(source_html)
        if not designs:
            raise ValueError("A página não devolveu designs ativos.")
        catalog = build_catalog(
            designs, metadata, location_id=location_id, country=args.country
        )
        output_dir = args.output_dir or default_output_directory(metadata)
        catalog_path, preview_path = write_outputs(catalog, output_dir)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    machines = int(catalog["source"]["machine_count"])
    shared_photos = sum(
        item["source"]["image_scope"] == "machine" for item in catalog["items"]
    )
    missing_orientation = sum(
        not item["source"]["orientation"] for item in catalog["items"]
    )
    print(f"Localização: {catalog['source']['location_name']}")
    print(f"Máquinas analisadas: {machines}")
    print(f"Designs pendentes de revisão: {len(catalog['items'])}")
    print(f"Com fotografia partilhada da máquina: {shared_photos}")
    print(f"Sem orientação indicada: {missing_orientation}")
    print(f"\nCatálogo pendente: {catalog_path}")
    print(f"Pré-visualização: {preview_path}")
    print("Site Base44: nenhuma alteração efetuada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
