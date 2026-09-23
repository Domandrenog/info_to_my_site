#!/usr/bin/env python3
"""Build a review-only index from a PennyCollector area page."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from scripts.pennycollector_souvenirs import (
    PENNYCOLLECTOR_BASE_URL,
    PennyCollectorPageParser,
    country_settings,
    fetch_html,
    reference_id,
    slugify,
)


US_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "Washington DC",
}


@dataclass(frozen=True)
class AreaLocation:
    location_id: str
    name: str
    address: str
    city: str
    designs: str
    has_images: bool
    updated: str
    status: str
    url: str


class AreaTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.locations: list[AreaLocation] = []
        self._in_table = False
        self._table_depth = 0
        self._in_row = False
        self._row_class = ""
        self._cells: list[str] = []
        self._cell_parts: list[str] | None = None
        self._location_id = ""
        self._image_src = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "table":
            if not self._in_table and attributes.get("id") == "DG":
                self._in_table = True
                self._table_depth = 1
                return
            if self._in_table:
                self._table_depth += 1
        if not self._in_table:
            return
        if tag == "tr":
            self._in_row = True
            self._row_class = attributes.get("class", "")
            self._cells = []
            self._location_id = ""
            self._image_src = ""
        elif tag == "td" and self._in_row:
            self._cell_parts = []
        elif tag == "br" and self._cell_parts is not None:
            self._cell_parts.append("\n")
        elif tag == "a" and self._in_row:
            match = re.search(r"Details\.aspx\?location=(\d+)", attributes.get("href", ""))
            if match:
                self._location_id = match.group(1)
        elif tag == "img" and self._in_row:
            self._image_src = attributes.get("src", "")

    def handle_endtag(self, tag: str) -> None:
        if not self._in_table:
            return
        if tag == "td" and self._cell_parts is not None:
            raw = "".join(self._cell_parts)
            lines = [" ".join(line.split()) for line in raw.splitlines()]
            self._cells.append("\n".join(line for line in lines if line))
            self._cell_parts = None
        elif tag == "tr" and self._in_row:
            self._finish_row()
            self._in_row = False
        elif tag == "table":
            self._table_depth -= 1
            if self._table_depth == 0:
                self._in_table = False

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def _finish_row(self) -> None:
        if not self._location_id or len(self._cells) < 5:
            return
        name_lines = self._cells[0].splitlines()
        name = name_lines[0].strip() if name_lines else ""
        address = " ".join(line.strip() for line in name_lines[1:] if line.strip())
        designs = self._cells[2].strip()
        if designs in {"Gone", "Moved"}:
            status = designs
        elif "outoforder" in self._row_class.casefold():
            status = "Out of Order"
        else:
            status = "Active"
        self.locations.append(
            AreaLocation(
                location_id=self._location_id,
                name=name,
                address=address,
                city=self._cells[1].strip(),
                designs=designs,
                has_images=self._image_src.casefold().endswith("camera.gif"),
                updated=self._cells[4].strip(),
                status=status,
                url=urljoin(
                    PENNYCOLLECTOR_BASE_URL,
                    f"Details.aspx?location={self._location_id}",
                ),
            )
        )


def area_url(area_id: str) -> str:
    return f"{PENNYCOLLECTOR_BASE_URL}Locations.aspx?area={area_id}"


def parse_area(source_html: str) -> tuple[list[AreaLocation], dict[str, Any]]:
    table_parser = AreaTableParser()
    table_parser.feed(source_html)
    table_parser.close()
    if not table_parser.locations:
        raise ValueError("A página não contém localizações reconhecíveis.")

    page_parser = PennyCollectorPageParser()
    page_parser.feed(source_html)
    page_parser.close()
    country = page_parser.selected_options.get("MyReportLocation_CountryList", "")
    state = page_parser.selected_options.get("MyReportLocation_StateList", "")
    area_name = US_STATE_NAMES.get(state, state) if country == "United States" else country
    return table_parser.locations, {
        "area_name": area_name or "Área desconhecida",
        "country": country,
        "state": state,
    }


def build_catalog(
    locations: list[AreaLocation], metadata: dict[str, Any], *, area_id: str
) -> dict[str, Any]:
    status_counts = Counter(location.status for location in locations)
    return {
        "status": "discovery_only",
        "base44_updated": False,
        "import_ready": False,
        "source": {
            "site": "PennyCollector",
            "area_id": area_id,
            "url": area_url(area_id),
            **metadata,
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "location_count": len(locations),
            "status_counts": dict(sorted(status_counts.items())),
        },
        "locations": [asdict(location) for location in locations],
    }


def default_output_directory(metadata: dict[str, Any]) -> Path:
    _continent, continent_folder, _country, country_folder = country_settings(
        str(metadata.get("country") or "")
    )
    return (
        Path("info")
        / "souvenirs"
        / continent_folder
        / country_folder
        / "areas"
        / slugify(str(metadata.get("area_name") or "area"))
    )


def preview_html(catalog: dict[str, Any]) -> str:
    rows = []
    for location in catalog["locations"]:
        css_class = slugify(str(location["status"]))
        image_label = "sim" if location["has_images"] else "não"
        rows.append(
            f'<tr class="{css_class}"><td><a href="{html.escape(location["url"])}">'
            f'{html.escape(location["name"])}</a><br><small>{html.escape(location["address"])}</small></td>'
            f'<td>{html.escape(location["city"])}</td><td>{html.escape(location["designs"])}</td>'
            f'<td>{image_label}</td><td>{html.escape(location["status"])}</td>'
            f'<td>{html.escape(location["updated"])}</td><td>{location["location_id"]}</td></tr>'
        )
    source = catalog["source"]
    return f"""<!doctype html>
<html lang="pt"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PennyCollector — {html.escape(source['area_name'])}</title>
<style>body{{font:15px/1.4 system-ui,sans-serif;margin:30px;background:#f4f2ed;color:#24221f}}main{{max-width:1250px;margin:auto}}table{{background:white;border-collapse:collapse;width:100%}}th,td{{border:1px solid #ddd;padding:8px;text-align:left}}th{{background:#222;color:white;position:sticky;top:0}}tr.gone,tr.moved{{color:#777;background:#eee}}tr.out-of-order{{background:#fff1c7}}small{{color:#666}}</style>
</head><body><main><h1>{html.escape(source['area_name'])}</h1>
<p>{source['location_count']} localizações encontradas. Este é apenas um índice para escolher localizações.</p>
<p><strong>Site Base44:</strong> nenhuma alteração efetuada.</p>
<p><a href="{html.escape(source['url'])}">Abrir área original</a></p>
<table><thead><tr><th>Localização</th><th>Cidade</th><th>Designs</th><th>Imagens</th><th>Estado</th><th>Atualizado</th><th>ID</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></main></body></html>"""


def write_outputs(catalog: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = output_dir / "pennycollector-area.json"
    preview_path = output_dir / "preview.html"
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    preview_path.write_text(preview_html(catalog), encoding="utf-8")
    return catalog_path, preview_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cria um índice de localizações de uma área PennyCollector.")
    parser.add_argument("--area", required=True, help="ID ou link Locations.aspx?area=...")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--html-input", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        area_id = reference_id(args.area, "area")
        source_html = args.html_input.read_text(encoding="utf-8") if args.html_input else fetch_html(area_url(area_id))
        locations, metadata = parse_area(source_html)
        catalog = build_catalog(locations, metadata, area_id=area_id)
        output_dir = args.output_dir or default_output_directory(metadata)
        catalog_path, preview_path = write_outputs(catalog, output_dir)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    counts = catalog["source"]["status_counts"]
    print(f"Área: {catalog['source']['area_name']}")
    print(f"Localizações encontradas: {len(locations)}")
    for status, count in counts.items():
        print(f"- {status}: {count}")
    print(f"\nÍndice: {catalog_path}")
    print(f"Pré-visualização: {preview_path}")
    print("Site Base44: nenhuma alteração efetuada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
