#!/usr/bin/env python3
"""Review PennyCollector souvenirs and produce an import-ready catalogue."""

from __future__ import annotations

import argparse
import copy
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from scripts import import_base44_souvenirs


SHARED_PHOTO_NOTE = "Fotografia provisória partilhada da máquina"
APPROVED = "approved"
SKIPPED = "skipped"
PENDING = "pending"
DEFAULT_CATALOG_ROOT = Path("info/souvenirs")


def read_catalog(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("O catálogo de souvenirs não contém uma lista items.")
    source = payload.get("source", {})
    if source.get("site") not in {"PennyCollector", "Presscoins"}:
        raise ValueError("Este ficheiro não é um catálogo PennyCollector ou Presscoins.")
    return payload


def default_output_path(input_path: Path) -> Path:
    if input_path.name == "presscoins-catalog.json":
        return input_path.with_name("presscoins-catalog-final.json")
    return input_path.with_name("pennycollector-catalog-final.json")


def discover_pending_catalogs(root: Path) -> list[Path]:
    """Return every reviewable source catalogue below *root*."""
    if not root.is_dir():
        return []
    return sorted(
        [
            *root.rglob("pennycollector-catalog.json"),
            *root.rglob("presscoins-catalog.json"),
        ]
    )


def item_key(item: dict[str, Any], index: int) -> str:
    source = item.get("source", {})
    catalog_number = str(source.get("catalog_number") or "").strip().casefold()
    if catalog_number:
        return f"presscoins:{catalog_number}"
    location_id = str(source.get("location_id") or "")
    machine = str(source.get("machine_number") or "")
    position = str(source.get("position") or "")
    availability = str(source.get("availability") or "active")
    if location_id and machine and position:
        return f"{location_id}:{availability}:{machine}:{position}"
    return f"index:{index}"


def unique_reference_url(item: dict[str, Any]) -> str:
    source = item.get("source", {})
    souvenir = item.get("souvenir", {})
    reference_url = str(souvenir.get("reference_url") or "")
    machine = str(source.get("machine_number") or "")
    position = str(source.get("position") or "")
    if not reference_url or not machine or not position:
        return reference_url
    parsed = urlsplit(reference_url)
    availability = str(source.get("availability") or "active")
    prefix = "retired-" if availability == "retired" else ""
    fragment = f"{prefix}machine-{machine}-position-{position}"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, fragment))


def prepare_approved_item(item: dict[str, Any], *, name: str = "") -> None:
    souvenir = item["souvenir"]
    if name:
        souvenir["name"] = name.strip()
    if not str(souvenir.get("name") or "").strip():
        raise ValueError("O nome do souvenir não pode ficar vazio.")
    source = item.get("source", {})
    if source.get("image_scope") == "machine" and souvenir.get("image_front"):
        notes = str(souvenir.get("notes") or "").strip()
        if SHARED_PHOTO_NOTE.casefold() not in notes.casefold():
            souvenir["notes"] = " · ".join(part for part in (notes, SHARED_PHOTO_NOTE) if part)
    souvenir["reference_url"] = unique_reference_url(item)
    item["review_status"] = APPROVED


def reconcile_existing_presscoins(
    catalog: dict[str, Any],
    client: Any,
    existing_by_country: dict[str, list[dict[str, Any]]],
) -> tuple[int, int]:
    """Approve pending Presscoins items whose strong identity already exists on Base44."""
    if str(catalog.get("source", {}).get("site") or "") != "Presscoins":
        return 0, 0

    pending_items: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for index, item in enumerate(catalog["items"], start=1):
        if str(item.get("review_status") or PENDING) != PENDING:
            continue
        souvenir = item.get("souvenir")
        if not isinstance(souvenir, dict):
            raise ValueError(f"Item {index}: objeto souvenir em falta.")
        record = import_base44_souvenirs.validate_record(souvenir, index)
        if import_base44_souvenirs.strong_identity(record) is None:
            continue
        pending_items.append((item, record))

    for country in sorted({str(record["country"]) for _item, record in pending_items}):
        if country not in existing_by_country:
            existing_by_country[country] = (
                import_base44_souvenirs.existing_records_for_country(client, country)
            )

    approved = 0
    for item, record in pending_items:
        _missing, already_present = import_base44_souvenirs.partition_missing(
            [record], existing_by_country[str(record["country"])]
        )
        if already_present:
            prepare_approved_item(item)
            approved += 1
    return approved, len(pending_items)


def merge_previous_review(
    catalog: dict[str, Any], previous: dict[str, Any] | None
) -> dict[str, Any]:
    working = copy.deepcopy(catalog)
    if not previous:
        return working
    previous_by_key = {
        item_key(item, index): item
        for index, item in enumerate(previous.get("items", []), start=1)
        if isinstance(item, dict)
    }
    for index, item in enumerate(working["items"], start=1):
        reviewed = previous_by_key.get(item_key(item, index))
        if not reviewed:
            continue
        status = str(reviewed.get("review_status") or PENDING)
        if status in {APPROVED, SKIPPED}:
            item["review_status"] = status
            if status == APPROVED and isinstance(reviewed.get("souvenir"), dict):
                item["souvenir"] = copy.deepcopy(reviewed["souvenir"])
    return working


def review_summary(catalog: dict[str, Any]) -> dict[str, int]:
    counts = {APPROVED: 0, SKIPPED: 0, PENDING: 0}
    for item in catalog["items"]:
        status = str(item.get("review_status") or PENDING)
        counts[status if status in counts else PENDING] += 1
    return counts


def update_catalog_status(catalog: dict[str, Any]) -> dict[str, int]:
    summary = review_summary(catalog)
    complete = summary[PENDING] == 0
    catalog["status"] = "ready_for_import" if complete else "review_in_progress"
    catalog["import_ready"] = complete
    catalog["base44_updated"] = False
    catalog["review"] = {
        "approved": summary[APPROVED],
        "skipped": summary[SKIPPED],
        "pending": summary[PENDING],
        "completed": complete,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "shared_machine_photos_accepted_as_temporary": True,
    }
    return summary


def machine_label(source: dict[str, Any]) -> str:
    presscoins_location = str(source.get("location") or "").strip()
    if source.get("catalog_number") and presscoins_location:
        return presscoins_location
    machine_number = source.get("machine_number", "?")
    if str(source.get("availability") or "active") == "retired":
        return f"Máquina retirada {machine_number}"
    return f"Machine {machine_number}"


def review_html(catalog: dict[str, Any]) -> str:
    cards: list[str] = []
    labels = {APPROVED: "Aprovado", SKIPPED: "Não importar", PENDING: "Pendente"}
    for item in catalog["items"]:
        source = item.get("source", {})
        souvenir = item.get("souvenir", {})
        status = str(item.get("review_status") or PENDING)
        image_url = str(souvenir.get("image_front") or "")
        image = (
            f'<a href="{html.escape(image_url)}"><img src="{html.escape(image_url)}" '
            f'alt="{html.escape(str(souvenir.get("name") or ""))}"></a>'
            if image_url
            else '<div class="missing">Sem fotografia</div>'
        )
        cards.append(
            "".join(
                [
                    f'<article class="card {status}">',
                    image,
                    f'<p class="status">{labels.get(status, status)}</p>',
                    f'<h2>{html.escape(str(souvenir.get("name") or ""))}</h2>',
                    f'<p>{html.escape(machine_label(source))} · Posição {source.get("position", "?")}</p>',
                    f'<p>{html.escape(str(souvenir.get("description") or ""))}</p>',
                    "</article>",
                ]
            )
        )
    summary = catalog["review"]
    catalog_source = catalog.get("source", {})
    source_site = str(catalog_source.get("site") or "Souvenirs")
    location = catalog_source.get("location_name") or catalog_source.get("location") or source_site
    photo_note = (
        "As fotografias partilhadas das máquinas são provisórias e podem ser recortadas posteriormente."
        if source_site == "PennyCollector"
        else "As fotografias Presscoins correspondem ao desenho apresentado no catálogo de origem."
    )
    return f"""<!doctype html>
<html lang="pt"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Revisão {html.escape(source_site)} — {html.escape(str(location))}</title>
<style>body{{font:15px/1.4 system-ui,sans-serif;margin:30px;background:#f4f2ed;color:#24221f}}main{{display:grid;gap:18px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}}.card{{background:#fff;border:3px solid #ddd;border-radius:12px;padding:15px}}.approved{{border-color:#48a868}}.skipped{{border-color:#999;opacity:.65}}.pending{{border-color:#d79b28}}img{{display:block;height:220px;max-width:100%;margin:auto;object-fit:contain}}.status{{font-weight:700}}.missing{{padding:70px;text-align:center;background:#eee}}</style>
</head><body><h1>{html.escape(str(location))}</h1>
<p>{summary['approved']} aprovados · {summary['skipped']} não importar · {summary['pending']} pendentes.</p>
<p>{html.escape(photo_note)}</p>
<main>{''.join(cards)}</main></body></html>"""


def write_review(catalog: dict[str, Any], output_path: Path) -> tuple[Path, Path]:
    update_catalog_status(catalog)
    preview_path = output_path.with_name("review.html")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    preview_path.write_text(review_html(catalog), encoding="utf-8")
    return output_path, preview_path


def approve_all(catalog: dict[str, Any]) -> None:
    for item in catalog["items"]:
        prepare_approved_item(item)


def interactive_review(
    catalog: dict[str, Any], output_path: Path, *, input_fn: Any = input
) -> dict[str, int]:
    summary = update_catalog_status(catalog)
    print(f"Localização: {catalog['source'].get('location_name', '')}")
    print(
        f"Aprovados: {summary[APPROVED]} · Não importar: {summary[SKIPPED]} "
        f"· Pendentes: {summary[PENDING]}"
    )
    if not summary[PENDING]:
        write_review(catalog, output_path)
        return summary

    while True:
        print("\n1) Rever pendentes um a um")
        print("2) Aprovar todos os pendentes com os dados atuais")
        print("3) Guardar e terminar")
        mode = str(input_fn("Escolhe uma opção [1]: ")).strip() or "1"
        if mode in {"1", "2", "3"}:
            break
        print("Opção inválida. Escolhe 1, 2 ou 3.")
    if mode == "2":
        for item in catalog["items"]:
            if str(item.get("review_status") or PENDING) == PENDING:
                prepare_approved_item(item)
        return update_catalog_status(catalog)
    if mode == "3":
        write_review(catalog, output_path)
        return update_catalog_status(catalog)
    pending_items = [
        item for item in catalog["items"]
        if str(item.get("review_status") or PENDING) == PENDING
    ]
    previous_machine: tuple[str, str] | None = None
    for index, item in enumerate(pending_items, start=1):
        source = item.get("source", {})
        souvenir = item["souvenir"]
        presscoins_location = str(source.get("location") or "").strip()
        machine_key = (
            "presscoins",
            presscoins_location,
        ) if source.get("catalog_number") else (
            str(source.get("availability") or "active"),
            str(source.get("machine_number") or "?"),
        )
        if machine_key != previous_machine:
            print("\n" + "#" * 72)
            print(machine_label(source).upper())
            print("#" * 72)
            previous_machine = machine_key
        print("\n" + "-" * 72)
        print(
            f"{index}/{len(pending_items)} — {machine_label(source)} "
            f"· Posição {source.get('position')}"
        )
        print(f"Nome: {souvenir.get('name', '')}")
        print(f"Descrição: {souvenir.get('description', '')}")
        print(f"Fotografia: {souvenir.get('image_front') or 'sem fotografia'}")
        print(f"Origem: {souvenir.get('reference_url', '')}")
        while True:
            print("\n1) Aprovar nome, dados e fotografia atuais")
            print("2) Editar o nome e aprovar")
            print("3) Não importar")
            print("4) Guardar e terminar a revisão")
            choice = str(input_fn("Escolhe uma opção [1]: ")).strip() or "1"
            if choice == "1":
                prepare_approved_item(item)
                break
            if choice == "2":
                name = str(input_fn(f"Nome [{souvenir.get('name', '')}]: ")).strip()
                prepare_approved_item(item, name=name or str(souvenir.get("name") or ""))
                break
            if choice == "3":
                item["review_status"] = SKIPPED
                break
            if choice == "4":
                write_review(catalog, output_path)
                return update_catalog_status(catalog)
            print("Opção inválida. Escolhe 1, 2, 3 ou 4.")
        write_review(catalog, output_path)
    return update_catalog_status(catalog)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Revê catálogos de souvenirs e gera ficheiros aprovados para o Base44."
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--input", type=Path)
    selection.add_argument(
        "--all-catalogs",
        action="store_true",
        help="Rever todos os catálogos PennyCollector e Presscoins existentes sob --root.",
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_CATALOG_ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--approve-all", action="store_true")
    parser.add_argument(
        "--no-site-reconciliation",
        action="store_true",
        help="Não reconhecer automaticamente entradas Presscoins já existentes no Site Base44.",
    )
    return parser.parse_args(argv)


def review_catalog(
    input_path: Path,
    *,
    output_path: Path | None = None,
    approve_everything: bool = False,
    site_client: Any = None,
    existing_by_country: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[dict[str, int], dict[str, Any], Path, Path]:
    destination = output_path or default_output_path(input_path)
    pending = read_catalog(input_path)
    previous = read_catalog(destination) if destination.is_file() else None
    catalog = merge_previous_review(pending, previous)
    if site_client is not None:
        approved, pending_count = reconcile_existing_presscoins(
            catalog,
            site_client,
            existing_by_country if existing_by_country is not None else {},
        )
        if pending_count:
            print(
                "Reconciliação com o Site Base44: "
                f"{approved}/{pending_count} pendentes já existiam e foram aprovados localmente."
            )
    if approve_everything:
        approve_all(catalog)
        summary = update_catalog_status(catalog)
    else:
        summary = interactive_review(catalog, destination)
    destination, preview_path = write_review(catalog, destination)
    return summary, catalog, destination, preview_path


def print_review_result(
    summary: dict[str, int],
    catalog: dict[str, Any],
    output_path: Path,
    preview_path: Path,
) -> None:

    print(f"\nAprovados: {summary[APPROVED]}")
    print(f"Não importar: {summary[SKIPPED]}")
    print(f"Pendentes: {summary[PENDING]}")
    print(f"Catálogo final: {output_path}")
    print(f"Revisão: {preview_path}")
    if catalog["import_ready"]:
        print("Pronto para verificar e importar no Site Base44.")
    else:
        print("Ainda não está pronto para importar; existem decisões pendentes.")
    print("Site Base44: nenhuma alteração efetuada.")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.output and args.all_catalogs:
        print("Erro: --output só pode ser usado com --input.", file=sys.stderr)
        return 1

    input_paths = discover_pending_catalogs(args.root) if args.all_catalogs else [args.input]
    if not input_paths:
        print(f"Nenhum catálogo de souvenirs para rever encontrado em {args.root}.")
        return 0

    if args.all_catalogs:
        print(f"Catálogos de souvenirs encontrados: {len(input_paths)}")

    site_client = None
    existing_by_country: dict[str, list[dict[str, Any]]] = {}
    should_reconcile = (
        not args.approve_all
        and not args.no_site_reconciliation
        and any(path.name == "presscoins-catalog.json" for path in input_paths)
    )
    if should_reconcile:
        try:
            site_client = import_base44_souvenirs.create_client(
                argparse.Namespace(
                    request_delay=import_base44_souvenirs.DEFAULT_REQUEST_DELAY_SECONDS,
                    rate_limit_delay=import_base44_souvenirs.DEFAULT_RATE_LIMIT_DELAY_SECONDS,
                    max_retries=import_base44_souvenirs.DEFAULT_MAX_RETRIES,
                )
            )
            print(
                "Presscoins: os pendentes serão comparados com o Site Base44 "
                "em modo só de leitura."
            )
        except ValueError as exc:
            print(
                f"Aviso: não foi possível preparar a reconciliação com o Site Base44: {exc}\n"
                "A revisão continuará apenas com o estado guardado localmente.",
                file=sys.stderr,
            )

    totals = {APPROVED: 0, SKIPPED: 0, PENDING: 0}
    completed = 0
    for index, input_path in enumerate(input_paths, start=1):
        if args.all_catalogs:
            print("\n" + "#" * 72)
            print(f"CATÁLOGO {index}/{len(input_paths)} — {input_path}")
            print("#" * 72)
        try:
            summary, catalog, output_path, preview_path = review_catalog(
                input_path,
                output_path=args.output,
                approve_everything=args.approve_all,
                site_client=site_client,
                existing_by_country=existing_by_country,
            )
        except RuntimeError as exc:
            if site_client is None or input_path.name != "presscoins-catalog.json":
                print(f"Erro em {input_path}: {exc}", file=sys.stderr)
                return 1
            print(
                f"Aviso: não foi possível consultar o Site Base44: {exc}\n"
                "Este catálogo continuará em revisão manual.",
                file=sys.stderr,
            )
            site_client = None
            try:
                summary, catalog, output_path, preview_path = review_catalog(
                    input_path,
                    output_path=args.output,
                    approve_everything=args.approve_all,
                )
            except (OSError, ValueError, json.JSONDecodeError) as fallback_exc:
                print(f"Erro em {input_path}: {fallback_exc}", file=sys.stderr)
                return 1
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"Erro em {input_path}: {exc}", file=sys.stderr)
            return 1
        print_review_result(summary, catalog, output_path, preview_path)
        for status in totals:
            totals[status] += summary[status]
        completed += int(bool(catalog["import_ready"]))

    if args.all_catalogs:
        print("\n" + "=" * 72)
        print("RESUMO GERAL DA REVISÃO")
        print("=" * 72)
        print(f"Catálogos prontos: {completed}/{len(input_paths)}")
        print(f"Souvenirs aprovados: {totals[APPROVED]}")
        print(f"Souvenirs a não importar: {totals[SKIPPED]}")
        print(f"Decisões ainda pendentes: {totals[PENDING]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
