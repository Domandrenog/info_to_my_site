#!/usr/bin/env python3
"""Build a review-only index from a PennyCollector area page."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
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
    build_catalog as build_location_catalog,
    country_settings,
    default_output_directory as default_location_output_directory,
    fetch_html,
    parse_designs,
    reference_id,
    slugify,
    write_outputs as write_location_outputs,
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
        "collection": {
            "collected": 0,
            "pending": len(locations),
            "failed": 0,
        },
        "locations": [
            {
                **asdict(location),
                "collection_status": "pending",
                "catalog_path": "",
                "preview_path": "",
                "collection_error": "",
            }
            for location in locations
        ],
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


def pressed_design_count(value: str) -> int:
    """Return the pressed-penny count from values such as ``48p 16t``."""
    match = re.search(r"(?:^|\s)(\d+)\s*p(?:\s|$)", value, flags=re.IGNORECASE)
    return int(match.group(1)) if match else 0


def location_candidates(catalog: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    locations = list(catalog["locations"])
    if mode == "all":
        return locations
    active = [
        location
        for location in locations
        if str(location.get("status", "")).casefold() == "active"
        and pressed_design_count(str(location.get("designs", ""))) > 0
    ]
    if mode == "active":
        return active
    if mode == "with_images":
        return [location for location in active if location.get("has_images")]
    raise ValueError(f"Modo de seleção desconhecido: {mode}")


def parse_location_selection(value: str, catalog: dict[str, Any]) -> list[str]:
    valid_ids = {str(location["location_id"]) for location in catalog["locations"]}
    selected: list[str] = []
    invalid: list[str] = []
    for token in re.split(r"[,;\s]+", value.strip()):
        if not token:
            continue
        try:
            location_id = reference_id(token, "location")
        except ValueError:
            invalid.append(token)
            continue
        if location_id not in valid_ids:
            invalid.append(location_id)
        elif location_id not in selected:
            selected.append(location_id)
    if invalid:
        raise ValueError(
            "Estas localizações não pertencem à área ou são inválidas: "
            + ", ".join(invalid)
        )
    if not selected:
        raise ValueError("Indica pelo menos um ID ou link de localização.")
    return selected


def country_catalog_root(catalog: dict[str, Any]) -> Path:
    _continent, continent_folder, _country, country_folder = country_settings(
        str(catalog["source"].get("country") or "")
    )
    return Path("info") / "souvenirs" / continent_folder / country_folder


def existing_location_catalogs(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    if not root.exists():
        return found
    for catalog_path in root.rglob("pennycollector-catalog.json"):
        try:
            payload = json.loads(catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        source = payload.get("source", {})
        location_id = str(source.get("location_id") or "")
        if location_id:
            found[location_id] = catalog_path
    return found


def refresh_collection_summary(catalog: dict[str, Any]) -> None:
    statuses = Counter(
        str(location.get("collection_status") or "pending")
        for location in catalog["locations"]
    )
    catalog["collection"] = {
        "collected": statuses["collected"] + statuses["already_collected"],
        "pending": statuses["pending"],
        "failed": statuses["failed"],
    }


def sync_existing_location_catalogs(
    catalog: dict[str, Any], root: Path | None = None
) -> dict[str, Path]:
    existing = existing_location_catalogs(root or country_catalog_root(catalog))
    for location in catalog["locations"]:
        location_id = str(location["location_id"])
        catalog_path = existing.get(location_id)
        if not catalog_path:
            continue
        location["collection_status"] = "already_collected"
        location["catalog_path"] = str(catalog_path)
        location["preview_path"] = str(catalog_path.with_name("preview.html"))
        location["collection_error"] = ""
    refresh_collection_summary(catalog)
    return existing


def _duration(value: float) -> str:
    seconds = max(0, round(value))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


def collect_locations(
    catalog: dict[str, Any],
    location_ids: list[str],
    *,
    area_output_dir: Path,
    force: bool = False,
    fetcher: Any = None,
) -> dict[str, int]:
    """Collect selected locations and persist area progress after every item."""
    fetcher = fetcher or fetch_html
    by_id = {str(location["location_id"]): location for location in catalog["locations"]}
    missing = [location_id for location_id in location_ids if location_id not in by_id]
    if missing:
        raise ValueError("Localizações fora da área: " + ", ".join(missing))

    existing = sync_existing_location_catalogs(catalog)
    stats = {"collected": 0, "skipped": 0, "failed": 0}
    total = len(location_ids)
    started = time.monotonic()
    for index, location_id in enumerate(location_ids, start=1):
        location = by_id[location_id]
        item_started = time.monotonic()
        if location_id in existing and not force:
            stats["skipped"] += 1
            outcome = "já tinha catálogo — ignorada"
        else:
            try:
                source_html = fetcher(str(location["url"]))
                designs, metadata = parse_designs(source_html)
                location_catalog = build_location_catalog(
                    designs,
                    metadata,
                    location_id=location_id,
                )
                location_output_dir = default_location_output_directory(metadata)
                catalog_path, preview_path = write_location_outputs(
                    location_catalog, location_output_dir
                )
                location["collection_status"] = "collected"
                location["catalog_path"] = str(catalog_path)
                location["preview_path"] = str(preview_path)
                location["collection_error"] = ""
                existing[location_id] = catalog_path
                stats["collected"] += 1
                outcome = f"{len(designs)} designs recolhidos"
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
                location["collection_status"] = "failed"
                location["collection_error"] = str(exc)
                stats["failed"] += 1
                outcome = f"erro: {exc}"

        refresh_collection_summary(catalog)
        write_outputs(catalog, area_output_dir)
        elapsed = time.monotonic() - started
        average = elapsed / index
        remaining = average * (total - index)
        percent = index / total * 100
        item_time = time.monotonic() - item_started
        print(
            f"{index}/{total} ({percent:.1f}%) — {location['name']} "
            f"[{outcome} | nesta: {_duration(item_time)} | "
            f"decorrido: {_duration(elapsed)} | restante: {_duration(remaining)}]"
        )
    return stats


def print_locations(locations: list[dict[str, Any]]) -> None:
    if not locations:
        print("Nenhuma localização neste filtro.")
        return
    for location in locations:
        photo = "com fotos" if location.get("has_images") else "sem fotos"
        collection_labels = {
            "pending": "sem catálogo",
            "collected": "recolhida agora",
            "already_collected": "catálogo existente",
            "failed": "erro na recolha",
        }
        collection_status = str(location.get("collection_status") or "pending")
        collection = collection_labels.get(collection_status, collection_status)
        print(
            f"- {location['location_id']} — {location['name']} — {location['city']} "
            f"— {location['designs']} — {location['status']} — {photo} — {collection}"
        )


def _confirm(prompt: str, input_fn: Any, *, default: bool) -> bool:
    token = "S/n" if default else "s/N"
    while True:
        value = str(input_fn(f"{prompt} [{token}]: ")).strip().casefold()
        if not value:
            return default
        if value in {"s", "sim", "y", "yes"}:
            return True
        if value in {"n", "não", "nao", "no"}:
            return False
        print("Responde com s ou n.")


def interactive_area_menu(
    catalog: dict[str, Any], output_dir: Path, *, input_fn: Any = input
) -> None:
    while True:
        sync_existing_location_catalogs(catalog)
        write_outputs(catalog, output_dir)
        active = location_candidates(catalog, "active")
        with_images = location_candidates(catalog, "with_images")
        summary = catalog["collection"]
        print("\n" + "#" * 72)
        print(f"ÁREA PENNYCOLLECTOR — {catalog['source']['area_name']}")
        print("#" * 72)
        print(
            f"Catálogos detalhados: {summary['collected']}/{len(catalog['locations'])} "
            f"· Sem catálogo: {summary['pending']} · Com erro: {summary['failed']}"
        )
        print(f"Ativas com moedas prensadas: {len(active)} · Com fotos: {len(with_images)}")
        print("\n1) Escolher localizações por ID ou link")
        print("2) Recolher todas as localizações ativas com moedas prensadas")
        print("3) Recolher apenas localizações ativas com fotografias")
        print("4) Ver localizações da área")
        print("5) Terminar")
        choice = str(input_fn("Escolhe uma opção [1]: ")).strip() or "1"

        if choice == "5":
            return
        if choice == "4":
            print("\n1) Ativas com moedas prensadas")
            print("2) Ativas com fotografias")
            print("3) Todas")
            view = str(input_fn("Filtro [1]: ")).strip() or "1"
            modes = {"1": "active", "2": "with_images", "3": "all"}
            if view not in modes:
                print("Opção inválida.")
                continue
            print_locations(location_candidates(catalog, modes[view]))
            input_fn("\nCarrega Enter para voltar...")
            continue

        if choice == "1":
            raw = str(input_fn("IDs ou links, separados por vírgulas: ")).strip()
            try:
                selected_ids = parse_location_selection(raw, catalog)
            except ValueError as exc:
                print(f"Erro: {exc}")
                continue
            selected = [
                location for location in catalog["locations"]
                if str(location["location_id"]) in selected_ids
            ]
            print_locations(selected)
            confirmed = _confirm(
                f"Recolher exatamente {len(selected_ids)} localizações?",
                input_fn,
                default=True,
            )
        elif choice in {"2", "3"}:
            mode = "active" if choice == "2" else "with_images"
            candidates = location_candidates(catalog, mode)
            selected_ids = [str(location["location_id"]) for location in candidates]
            if not selected_ids:
                print("Nenhuma localização disponível neste filtro.")
                continue
            confirmed = _confirm(
                f"Recolher {len(selected_ids)} localizações?",
                input_fn,
                default=False,
            )
        else:
            print("Opção inválida.")
            continue

        if not confirmed:
            print("Recolha cancelada.")
            continue
        stats = collect_locations(
            catalog,
            selected_ids,
            area_output_dir=output_dir,
        )
        print(
            "\nResultado: "
            f"recolhidas={stats['collected']} · já existentes={stats['skipped']} "
            f"· erros={stats['failed']}"
        )
        print("Site Base44: nenhuma alteração efetuada.")


def preview_html(catalog: dict[str, Any]) -> str:
    rows = []
    for location in catalog["locations"]:
        css_class = slugify(str(location["status"]))
        image_label = "sim" if location["has_images"] else "não"
        collection_labels = {
            "pending": "por recolher",
            "collected": "recolhida agora",
            "already_collected": "catálogo existente",
            "failed": "erro",
        }
        collection_status = str(location.get("collection_status") or "pending")
        collection_label = collection_labels.get(collection_status, collection_status)
        collection_error = str(location.get("collection_error") or "")
        collection_detail = (
            f'<br><small>{html.escape(collection_error)}</small>' if collection_error else ""
        )
        rows.append(
            f'<tr class="{css_class}"><td><a href="{html.escape(location["url"])}">'
            f'{html.escape(location["name"])}</a><br><small>{html.escape(location["address"])}</small></td>'
            f'<td>{html.escape(location["city"])}</td><td>{html.escape(location["designs"])}</td>'
            f'<td>{image_label}</td><td>{html.escape(location["status"])}</td>'
            f'<td>{html.escape(collection_label)}{collection_detail}</td>'
            f'<td>{html.escape(location["updated"])}</td><td>{location["location_id"]}</td></tr>'
        )
    source = catalog["source"]
    return f"""<!doctype html>
<html lang="pt"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PennyCollector — {html.escape(source['area_name'])}</title>
<style>body{{font:15px/1.4 system-ui,sans-serif;margin:30px;background:#f4f2ed;color:#24221f}}main{{max-width:1250px;margin:auto}}table{{background:white;border-collapse:collapse;width:100%}}th,td{{border:1px solid #ddd;padding:8px;text-align:left}}th{{background:#222;color:white;position:sticky;top:0}}tr.gone,tr.moved{{color:#777;background:#eee}}tr.out-of-order{{background:#fff1c7}}small{{color:#666}}</style>
</head><body><main><h1>{html.escape(source['area_name'])}</h1>
<p>{source['location_count']} localizações encontradas. O índice permite escolher quais serão recolhidas.</p>
<p><strong>Catálogos detalhados:</strong> {catalog['collection']['collected']} de {source['location_count']} · {catalog['collection']['pending']} sem catálogo · {catalog['collection']['failed']} com erro.</p>
<p><strong>Site Base44:</strong> nenhuma alteração efetuada.</p>
<p><a href="{html.escape(source['url'])}">Abrir área original</a></p>
<table><thead><tr><th>Localização</th><th>Cidade</th><th>Designs</th><th>Imagens</th><th>Estado</th><th>Recolha</th><th>Atualizado</th><th>ID</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></main></body></html>"""


def write_outputs(catalog: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = output_dir / "pennycollector-area.json"
    preview_path = output_dir / "preview.html"
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    preview_path.write_text(preview_html(catalog), encoding="utf-8")
    return catalog_path, preview_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Descobre uma área PennyCollector e permite recolher as suas localizações."
    )
    parser.add_argument("--area", required=True, help="ID ou link Locations.aspx?area=...")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--html-input", type=Path)
    collection = parser.add_mutually_exclusive_group()
    collection.add_argument(
        "--interactive",
        action="store_true",
        help="Abrir o menu para escolher as localizações a recolher.",
    )
    collection.add_argument(
        "--collect-active",
        action="store_true",
        help="Recolher todas as localizações ativas com moedas prensadas.",
    )
    collection.add_argument(
        "--collect-with-images",
        action="store_true",
        help="Recolher localizações ativas que indicam ter fotografias.",
    )
    collection.add_argument(
        "--locations",
        help="IDs ou links de localizações, separados por vírgulas.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Voltar a recolher catálogos que já existem.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        area_id = reference_id(args.area, "area")
        source_html = (
            args.html_input.read_text(encoding="utf-8")
            if args.html_input
            else fetch_html(area_url(area_id))
        )
        locations, metadata = parse_area(source_html)
        catalog = build_catalog(locations, metadata, area_id=area_id)
        output_dir = args.output_dir or default_output_directory(metadata)
        sync_existing_location_catalogs(catalog)
        catalog_path, preview_path = write_outputs(catalog, output_dir)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    counts = catalog["source"]["status_counts"]
    print(f"Área: {catalog['source']['area_name']}")
    print(f"Localizações encontradas: {len(locations)}")
    for status, count in counts.items():
        print(f"- {status}: {count}")
    print(
        f"Ativas com moedas prensadas: {len(location_candidates(catalog, 'active'))}"
    )
    print(
        "Ativas com fotografias: "
        f"{len(location_candidates(catalog, 'with_images'))}"
    )
    print(f"Catálogos já existentes: {catalog['collection']['collected']}")
    print(f"\nÍndice: {catalog_path}")
    print(f"Pré-visualização: {preview_path}")
    print("Site Base44: nenhuma alteração efetuada.")

    try:
        if args.interactive:
            interactive_area_menu(catalog, output_dir)
            return 0
        if args.collect_active:
            selected_ids = [
                str(location["location_id"])
                for location in location_candidates(catalog, "active")
            ]
        elif args.collect_with_images:
            selected_ids = [
                str(location["location_id"])
                for location in location_candidates(catalog, "with_images")
            ]
        elif args.locations:
            selected_ids = parse_location_selection(args.locations, catalog)
        else:
            return 0
        stats = collect_locations(
            catalog,
            selected_ids,
            area_output_dir=output_dir,
            force=args.force,
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    print(
        "\nResultado: "
        f"recolhidas={stats['collected']} · já existentes={stats['skipped']} "
        f"· erros={stats['failed']}"
    )
    print("Site Base44: nenhuma alteração efetuada.")
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
