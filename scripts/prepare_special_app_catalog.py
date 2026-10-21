#!/usr/bin/env python3
"""Prepare and approve a SpecialCoin catalogue from official uCoin details."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from scripts import ucoin_detail_subjects


class SpecialCatalogPreparationError(RuntimeError):
    """Raised when a collection catalogue cannot be prepared safely."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Cria e aprova um special-app-catalog a partir do catálogo "
            "comemorativo e dos temas oficiais recolhidos no uCoin."
        )
    )
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--subjects", type=Path, required=True)
    parser.add_argument("--existing-catalog", type=Path, required=True)
    parser.add_argument("--pending", type=Path, required=True)
    parser.add_argument("--preview", type=Path, required=True)
    parser.add_argument("--approved-output", type=Path, required=True)
    parser.add_argument("--country", required=True)
    parser.add_argument("--site-country", required=True)
    parser.add_argument("--continent", required=True)
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Promove o catálogo pendente validado para special-app-catalog.json.",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "Permite preparar/aprovar apenas as moedas cujos temas oficiais já "
            "foram validados; as restantes ficam explicitamente pendentes."
        ),
    )
    parser.add_argument(
        "--resequence",
        action="store_true",
        help=(
            "Recalcula a ordem cronológica dentro de cada programa, sem "
            "alterar os restantes campos das moedas preservadas."
        ),
    )
    return parser.parse_args(argv)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpecialCatalogPreparationError(f"JSON inválido ou ilegível: {path}") from exc


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def iter_special_coins(
    catalogue: dict[str, Any],
) -> Iterator[tuple[str, dict[str, Any]]]:
    programmes = catalogue.get("programmes")
    if not isinstance(programmes, list):
        raise SpecialCatalogPreparationError("Catálogo de coleção sem programmes.")
    for programme in programmes:
        if not isinstance(programme, dict):
            raise SpecialCatalogPreparationError("Programa inválido no catálogo.")
        title = str(programme.get("title") or "").strip()
        coins = programme.get("coins")
        if not title or not isinstance(coins, list):
            raise SpecialCatalogPreparationError("Programa sem título ou moedas.")
        for coin in coins:
            if not isinstance(coin, dict):
                raise SpecialCatalogPreparationError(f"Moeda inválida em {title}.")
            yield title, coin


def validate_special_catalog(
    catalogue: dict[str, Any], *, require_approved: bool = False
) -> tuple[int, int]:
    if catalogue.get("catalogType") != "special":
        raise SpecialCatalogPreparationError("catalogType tem de ser special.")
    for field in ("country", "siteCountry", "continent"):
        if not str(catalogue.get(field) or "").strip():
            raise SpecialCatalogPreparationError(f"Campo obrigatório ausente: {field}.")
    review_status = catalogue.get("reviewStatus")
    if require_approved and review_status not in {None, "approved"}:
        raise SpecialCatalogPreparationError(
            "O catálogo ainda não foi aprovado. Revê a pré-visualização primeiro."
        )

    urls: set[str] = set()
    identities: set[tuple[str, str, str]] = set()
    orders: set[int] = set()
    variant_count = 0
    coin_count = 0
    for programme, coin in iter_special_coins(catalogue):
        denomination = str(coin.get("denomination") or "").strip()
        subject = str(coin.get("commemorativeName") or "").strip()
        year = str(coin.get("year") or "").strip()
        detail_url = str(coin.get("detailUrl") or "").strip()
        order = coin.get("order")
        if not all((denomination, subject, year, detail_url)):
            raise SpecialCatalogPreparationError(
                f"Moeda incompleta em {programme}: {denomination or subject or detail_url}."
            )
        if not isinstance(order, int) or order < 1 or order in orders:
            raise SpecialCatalogPreparationError(
                f"Ordem ausente ou repetida em {denomination} ({year}): {order!r}."
            )
        orders.add(order)
        identity = (denomination, subject, year)
        if identity in identities:
            raise SpecialCatalogPreparationError(f"Identidade repetida: {identity}.")
        identities.add(identity)
        if detail_url in urls:
            raise SpecialCatalogPreparationError(f"URL uCoin repetido: {detail_url}.")
        urls.add(detail_url)
        for image_field in ("obverseImage", "reverseImage"):
            if not isinstance(coin.get(image_field), str):
                raise SpecialCatalogPreparationError(
                    f"Fotografia inválida em {denomination}: {image_field}."
                )
        variants = coin.get("variants")
        if not isinstance(variants, list):
            raise SpecialCatalogPreparationError(f"Variantes inválidas em {identity}.")
        tags: set[str] = set()
        for variant in variants:
            if not isinstance(variant, dict):
                raise SpecialCatalogPreparationError(f"Variante inválida em {identity}.")
            tag = str(variant.get("tag") or "").strip()
            if not tag or tag in tags:
                raise SpecialCatalogPreparationError(
                    f"Tag de variante ausente ou repetida em {identity}."
                )
            tags.add(tag)
        if bool(coin.get("hasVariants")) != bool(variants):
            raise SpecialCatalogPreparationError(
                f"hasVariants não corresponde às variantes em {identity}."
            )
        variant_count += len(variants)
        coin_count += 1
    if not coin_count:
        raise SpecialCatalogPreparationError("O catálogo de coleção está vazio.")
    return coin_count, variant_count


def denomination_text(coin: dict[str, Any]) -> str:
    value = coin.get("denomination")
    if isinstance(value, dict):
        value = value.get("displayName")
    result = str(value or "").strip()
    if not result:
        raise SpecialCatalogPreparationError("Moeda técnica sem denominação.")
    return result


def single_year(coin: dict[str, Any]) -> str:
    detail_url = str(coin.get("detailUrl") or "").strip()
    match = re.search(r"-(\d{4})/?$", urlparse(detail_url).path)
    if not match:
        raise SpecialCatalogPreparationError(
            f"URL da moeda comemorativa sem ano de emissão: {detail_url!r}."
        )
    url_year = match.group(1)
    period = coin.get("issuePeriod")
    if not isinstance(period, dict):
        raise SpecialCatalogPreparationError("Moeda técnica sem issuePeriod.")
    start = period.get("startYear")
    end = period.get("endYear")
    if not isinstance(start, int) or not isinstance(end, int) or start != end:
        raise SpecialCatalogPreparationError(
            f"Moeda comemorativa sem ano único: {period!r}."
        )
    # Algumas linhas do catálogo usam no texto o ano do acontecimento
    # comemorado (por exemplo, 1974) em vez do ano da emissão (2024). O slug
    # do URL oficial identifica a emissão e é também o que distingue a moeda.
    return url_year


def collection_coin_sort_key(coin: dict[str, Any]) -> tuple[int, float, str, str]:
    try:
        year = int(str(coin.get("year") or ""))
    except ValueError:
        year = 10**9
    denomination = str(coin.get("denomination") or "")
    number_match = re.search(r"\d+(?:[.,]\d+)?", denomination)
    number = (
        float(number_match.group(0).replace(",", "."))
        if number_match
        else float("inf")
    )
    return (
        year,
        number,
        denomination.casefold(),
        str(coin.get("commemorativeName") or "").casefold(),
    )


def resequence_programmes(
    programme_order: list[str], programmes: dict[str, list[dict[str, Any]]]
) -> None:
    next_order = 1
    for title in programme_order:
        programmes[title].sort(key=collection_coin_sort_key)
        for coin in programmes[title]:
            coin["order"] = next_order
            next_order += 1


def build_catalog(
    raw_path: Path,
    subjects_path: Path,
    existing_path: Path,
    *,
    country: str,
    site_country: str,
    continent: str,
    allow_partial: bool = False,
    resequence: bool = False,
) -> dict[str, Any]:
    raw = read_json(raw_path)
    if not isinstance(raw, dict) or raw.get("country") != country:
        raise SpecialCatalogPreparationError(
            f"O catálogo técnico não pertence a {country}."
        )
    raw_coins = ucoin_detail_subjects.read_catalog(raw_path)
    raw_by_url = {
        str(coin.get("detailUrl") or "").strip(): coin for coin in raw_coins
    }
    if len(raw_by_url) != len(raw_coins) or "" in raw_by_url:
        raise SpecialCatalogPreparationError("URLs técnicas ausentes ou repetidas.")
    if existing_path.is_file():
        existing = read_json(existing_path)
        if not isinstance(existing, dict):
            raise SpecialCatalogPreparationError("Catálogo de coleção existente inválido.")
        validate_special_catalog(existing)
        if (
            existing.get("country") != country
            or existing.get("siteCountry") != site_country
            or existing.get("continent") != continent
        ):
            raise SpecialCatalogPreparationError(
                "O catálogo existente pertence a outro país."
            )
    else:
        existing = {
            "catalogType": "special",
            "country": country,
            "siteCountry": site_country,
            "continent": continent,
            "programmes": [],
        }

    existing_urls = {
        str(coin.get("detailUrl") or "").strip()
        for _programme, coin in iter_special_coins(existing)
    }
    subjects, rejected = ucoin_detail_subjects.read_existing(subjects_path, raw_by_url)
    required_subject_urls = set(raw_by_url) - existing_urls
    valid_required_subjects = required_subject_urls & set(subjects)
    missing_subject_urls = required_subject_urls - valid_required_subjects
    if rejected or (missing_subject_urls and not allow_partial):
        raise SpecialCatalogPreparationError(
            "Os temas oficiais das moedas novas ainda não estão completos: "
            f"{len(valid_required_subjects)}/{len(required_subject_urls)} válidos, "
            f"{rejected} rejeitados."
        )

    programmes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    programme_order: list[str] = []
    existing_urls = set()
    max_order = 0
    for programme, coin in iter_special_coins(existing):
        if programme not in programme_order:
            programme_order.append(programme)
        copied = json.loads(json.dumps(coin, ensure_ascii=False))
        detail_url = str(copied["detailUrl"])
        source_coin = raw_by_url.get(detail_url)
        if source_coin is None:
            raise SpecialCatalogPreparationError(
                f"Moeda preservada já não existe no catálogo técnico: {detail_url}."
            )
        expected_year = single_year(source_coin)
        if str(copied.get("year") or "") != expected_year:
            raise SpecialCatalogPreparationError(
                f"Ano preservado incoerente com o URL oficial em {detail_url}: "
                f"{copied.get('year')!r} != {expected_year!r}."
            )
        programmes[programme].append(copied)
        existing_urls.add(str(copied["detailUrl"]))
        max_order = max(max_order, int(copied["order"]))

    technical_programmes: dict[str, tuple[int, dict[str, Any]]] = {}
    for period_index, period in enumerate(raw.get("periods", [])):
        if not isinstance(period, dict):
            continue
        programme = str(period.get("rulerOrPeriodName") or period.get("country") or "").strip()
        if not programme:
            raise SpecialCatalogPreparationError("Período técnico sem nome.")
        for coin in period.get("coins", []):
            if not isinstance(coin, dict):
                continue
            url = str(coin.get("detailUrl") or "").strip()
            technical_programmes[url] = (period_index, {**coin, "_programme": programme})
    if set(technical_programmes) != set(raw_by_url):
        raise SpecialCatalogPreparationError("Períodos técnicos incompletos.")

    pending: list[tuple[int, str, int, str, str, str, dict[str, Any]]] = []
    for url, (period_index, coin) in technical_programmes.items():
        if url in existing_urls:
            continue
        if url not in valid_required_subjects:
            continue
        programme = str(coin["_programme"])
        subject = str(subjects[url].get("subject") or "").strip()
        year = single_year(coin)
        denomination = denomination_text(coin)
        images = coin.get("images") if isinstance(coin.get("images"), dict) else {}
        pending.append(
            (
                period_index,
                programme,
                int(year),
                denomination.casefold(),
                subject.casefold(),
                url,
                {
                    "denomination": denomination,
                    "commemorativeName": subject,
                    "year": year,
                    "detailUrl": url,
                    "numistaUrl": "",
                    "obverseImage": str(images.get("obverse") or ""),
                    "reverseImage": str(images.get("reverse") or ""),
                    "hidden": False,
                    "hasVariants": bool(coin.get("variants")),
                    "variants": coin.get("variants") or [],
                },
            )
        )

    next_order = max_order + 1
    for (
        _period_index,
        programme,
        _year,
        _denomination,
        _subject,
        _url,
        coin,
    ) in sorted(pending):
        if programme not in programme_order:
            programme_order.append(programme)
        coin["order"] = next_order
        next_order += 1
        programmes[programme].append(coin)

    if resequence:
        resequence_programmes(programme_order, programmes)

    result = {
        "formatVersion": 1,
        "catalogType": "special",
        "reviewStatus": "pending",
        "country": country,
        "siteCountry": site_country,
        "continent": continent,
        "source": {
            "ucoinCatalog": str(raw_path),
            "ucoinCatalogSha256": sha256_file(raw_path),
            "detailSubjects": str(subjects_path),
            "detailSubjectsSha256": sha256_file(subjects_path),
            "preservedCatalog": str(existing_path),
            "preservedCatalogSha256": (
                sha256_file(existing_path) if existing_path.is_file() else None
            ),
            "preservedCoins": len(existing_urls),
            "partial": bool(missing_subject_urls),
            "validatedNewCoins": len(valid_required_subjects),
            "remainingCoins": len(missing_subject_urls),
            "resequence": resequence,
        },
        "programmes": [
            {
                "title": title,
                "coins": sorted(programmes[title], key=lambda item: int(item["order"])),
            }
            for title in programme_order
        ],
        "summary": {
            "programmes": len(programme_order),
            "coins": len(existing_urls) + len(valid_required_subjects),
            "preservedCoins": len(existing_urls),
            "newCoins": len(valid_required_subjects),
            "catalogCoins": len(raw_by_url),
            "remainingCoins": len(missing_subject_urls),
            "complete": not missing_subject_urls,
            "variants": sum(
                len(coin.get("variants", []))
                for coins in programmes.values()
                for coin in coins
            ),
        },
    }
    validate_special_catalog(result)
    return result


def preview_html(catalogue: dict[str, Any]) -> bytes:
    sections: list[str] = []
    for programme in catalogue["programmes"]:
        rows = []
        for coin in programme["coins"]:
            images = " ".join(
                f'<img src="{html.escape(coin[field])}" alt="" loading="lazy">'
                for field in ("obverseImage", "reverseImage")
                if coin.get(field)
            )
            rows.append(
                "<tr>"
                f"<td>{coin['order']}</td>"
                f"<td>{images}</td>"
                f"<td>{html.escape(coin['denomination'])}</td>"
                f"<td>{html.escape(coin['commemorativeName'])}</td>"
                f"<td>{html.escape(coin['year'])}</td>"
                f'<td><a href="{html.escape(coin["detailUrl"])}">uCoin</a></td>'
                "</tr>"
            )
        sections.append(
            f"<h2>{html.escape(programme['title'])}</h2>"
            "<table><thead><tr><th>Ordem</th><th>Fotos</th><th>Moeda</th>"
            "<th>Tema oficial</th><th>Ano</th><th>Origem</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
    body = f"""<!doctype html>
<html lang="pt"><meta charset="utf-8"><title>Revisão — moedas de coleção</title>
<style>body{{font:14px system-ui;margin:24px;color:#222}}table{{border-collapse:collapse;width:100%;margin-bottom:32px}}th,td{{border:1px solid #ddd;padding:8px;text-align:left}}img{{width:72px;height:72px;object-fit:contain}}h1,h2{{color:#6f4935}}</style>
<h1>{html.escape(catalogue['country'])} — catálogo de coleção pendente</h1>
<p>{catalogue['summary']['coins']} moedas · {catalogue['summary']['newCoins']} novas · estado: pendente</p>
{''.join(sections)}</html>"""
    return body.encode("utf-8")


def approve_pending(args: argparse.Namespace) -> int:
    pending = read_json(args.pending)
    if not isinstance(pending, dict):
        raise SpecialCatalogPreparationError("Catálogo pendente inválido.")
    validate_special_catalog(pending)
    if pending.get("reviewStatus") != "pending":
        raise SpecialCatalogPreparationError("O catálogo não está pendente de aprovação.")
    source = pending.get("source")
    if not isinstance(source, dict):
        raise SpecialCatalogPreparationError("Metadados de origem ausentes.")
    if source.get("partial") and not getattr(args, "allow_partial", False):
        raise SpecialCatalogPreparationError(
            "O catálogo é parcial; repete a aprovação com --allow-partial."
        )
    if (
        pending.get("country") != args.country
        or pending.get("siteCountry") != args.site_country
        or pending.get("continent") != args.continent
    ):
        raise SpecialCatalogPreparationError(
            "O catálogo pendente não corresponde ao país/continente selecionado."
        )
    expected_hashes = {
        "ucoinCatalogSha256": sha256_file(args.catalog),
        "detailSubjectsSha256": sha256_file(args.subjects),
        "preservedCatalogSha256": (
            sha256_file(args.existing_catalog)
            if args.existing_catalog.is_file()
            else None
        ),
    }
    for field, expected in expected_hashes.items():
        if source.get(field) != expected:
            raise SpecialCatalogPreparationError(
                "As fontes mudaram desde a pré-visualização; gera o catálogo novamente."
            )
    pending["reviewStatus"] = "approved"
    validate_special_catalog(pending, require_approved=True)
    write_atomic(args.approved_output, json_bytes(pending))
    print(
        f"Aprovado: {args.approved_output} — "
        f"{pending['summary']['coins']} moedas"
    )
    print("Site Base44: nenhuma alteração efetuada.")
    return 0


def run(args: argparse.Namespace) -> int:
    if args.approve:
        return approve_pending(args)
    catalogue = build_catalog(
        args.catalog,
        args.subjects,
        args.existing_catalog,
        country=args.country,
        site_country=args.site_country,
        continent=args.continent,
        allow_partial=args.allow_partial,
        resequence=args.resequence,
    )
    write_atomic(args.pending, json_bytes(catalogue))
    write_atomic(args.preview, preview_html(catalogue))
    summary = catalogue["summary"]
    print(
        f"Catálogo pendente: {args.pending} — {summary['coins']} moedas "
        f"({summary['preservedCoins']} preservadas; {summary['newCoins']} novas)"
    )
    if summary["remainingCoins"]:
        print(
            f"Progresso parcial: {summary['remainingCoins']} moedas continuam "
            "pendentes de validação no uCoin."
        )
    print(f"Pré-visualização: {args.preview}")
    print("Site Base44: nenhuma alteração efetuada.")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except (SpecialCatalogPreparationError, RuntimeError) as exc:
        print(f"Erro: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
