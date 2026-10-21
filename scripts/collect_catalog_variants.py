#!/usr/bin/env python3
"""Collect uCoin variants into a normal or collection app catalogue."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from scripts.import_catalog_variants import CatalogVariantError, detect_catalog_type
from scripts.ucoin_catalog import (
    VARIANT_COLLECTION_COMPLETE_STATUSES,
    VARIANT_STATUS_PRO_REQUIRED,
    cdp_is_available,
    extract_coin_variants,
    launch_incognito_chromium,
    open_ucoin_detail_page,
    write_json_atomic,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Visita os URLs uCoin de um catálogo normal ou de coleção e guarda "
            "as variantes de circulação e de set (UNC/BU) no próprio catálogo."
        )
    )
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument(
        "--catalog-type", choices=("auto", "normal", "special"), default="auto"
    )
    browser = parser.add_mutually_exclusive_group(required=True)
    browser.add_argument("--attach-cdp", action="store_true")
    browser.add_argument("--incognito", action="store_true")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    parser.add_argument("--chrome-path", default="")
    parser.add_argument("--user-data-dir", default=".ucoin-profile")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--manual-session", action="store_true")
    parser.add_argument("--no-manual-session", action="store_true")
    parser.add_argument("--delay", type=float, default=20.0)
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--batch-pause", type=float, default=120.0)
    parser.add_argument(
        "--login-retries",
        type=int,
        default=2,
        help=(
            "Número de vezes que permite resolver um login/challenge no browser "
            "e repetir apenas a moeda atual."
        ),
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Volta a consultar moedas que já tenham metadados de variantes.",
    )
    parser.add_argument(
        "--detail-url",
        action="append",
        default=[],
        help="Limita a recolha ao URL uCoin exato; pode ser repetido.",
    )
    parser.add_argument(
        "--group-title",
        action="append",
        default=[],
        help=(
            "Limita a recolha ao título exato de um período/programa; pode ser "
            "repetido."
        ),
    )
    return parser.parse_args(argv)


def load_catalog(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogVariantError(f"Catálogo inválido ou ilegível: {path}") from exc
    if not isinstance(value, dict):
        raise CatalogVariantError("O catálogo tem de ser um objeto JSON.")
    return value


def catalog_coins(
    catalogue: dict[str, Any],
    catalog_type: str,
    group_titles: list[str] | None = None,
) -> list[dict[str, Any]]:
    group_key = "periods" if catalog_type == "normal" else "programmes"
    groups = [
        group for group in catalogue.get(group_key, []) if isinstance(group, dict)
    ]
    requested_titles = {
        title.strip() for title in (group_titles or []) if title.strip()
    }
    if requested_titles:
        available_titles = {
            str(group.get("title") or "").strip() for group in groups
        }
        unknown_titles = sorted(requested_titles - available_titles)
        if unknown_titles:
            raise CatalogVariantError(
                "Os seguintes períodos/programas não existem no catálogo: "
                + ", ".join(unknown_titles)
            )
        groups = [
            group
            for group in groups
            if str(group.get("title") or "").strip() in requested_titles
        ]
    result = [
        coin
        for group in groups
        for coin in group.get("coins", [])
        if isinstance(coin, dict)
    ]
    unique: list[dict[str, Any]] = []
    identity_by_url: dict[str, tuple[str, str]] = {}
    for coin in result:
        detail_url = str(coin.get("detailUrl") or "").strip()
        if not detail_url:
            raise CatalogVariantError("O catálogo contém um URL uCoin ausente.")
        identity = coin_label(coin, catalog_type)
        previous = identity_by_url.get(detail_url)
        if previous is not None:
            if previous != identity:
                raise CatalogVariantError(
                    f"O URL uCoin repetido tem moedas contraditórias: {detail_url}."
                )
            continue
        identity_by_url[detail_url] = identity
        unique.append(coin)
    return unique


def propagate_variant_metadata(
    catalogue: dict[str, Any],
    catalog_type: str,
    source: dict[str, Any],
) -> None:
    """Keep identical duplicate URL entries synchronized in the catalogue."""

    detail_url = str(source.get("detailUrl") or "").strip()
    group_key = "periods" if catalog_type == "normal" else "programmes"
    fields = (
        "variants",
        "hasVariants",
        "excludedProofOnlyVariants",
        "variantCollectionStatus",
        "excludedCollectionSVariants",
    )
    for group in catalogue.get(group_key, []):
        if not isinstance(group, dict):
            continue
        for coin in group.get("coins", []):
            if not isinstance(coin, dict) or str(coin.get("detailUrl") or "").strip() != detail_url:
                continue
            for field in fields:
                if field in source:
                    coin[field] = deepcopy(source[field])


def select_catalog_coins(
    coins: list[dict[str, Any]], detail_urls: list[str]
) -> list[dict[str, Any]]:
    requested = {value.strip() for value in detail_urls if value.strip()}
    if not requested:
        return coins
    by_url = {str(coin.get("detailUrl") or "").strip(): coin for coin in coins}
    unknown = sorted(requested - set(by_url))
    if unknown:
        raise CatalogVariantError(
            "Os seguintes URLs não existem no catálogo: " + ", ".join(unknown)
        )
    return [coin for coin in coins if str(coin.get("detailUrl") or "").strip() in requested]


def coin_label(coin: dict[str, Any], catalog_type: str) -> tuple[str, str]:
    denomination = str(coin.get("denomination") or "Moeda").strip()
    years = str(
        coin.get("issuePeriod")
        if catalog_type == "normal"
        else coin.get("year") or coin.get("commemorativeName") or ""
    ).strip()
    return denomination, years


def is_s_variant(tag: str) -> bool:
    normalized = " ".join(tag.upper().split())
    return normalized == "S" or normalized.endswith(" S")


def detail_page_identity_error(
    *,
    detail_url: str,
    final_url: str,
    document_title: str,
    h1: str,
    body_text: str,
    country: str,
) -> str:
    normalized_body = " ".join(body_text.split()).casefold()
    if "your ip" in normalized_body and "blocked" in normalized_body:
        countdown = re.search(r"\b\d{2}:\d{2}:\d{2}\b", normalized_body)
        wait = countdown.group(0) if countdown else "o tempo indicado no browser"
        return f"uCoin bloqueou temporariamente este IP; espera {wait}"

    expected_tid = parse_qs(urlparse(detail_url).query).get("tid", [])
    actual_tid = parse_qs(urlparse(final_url).query).get("tid", [])
    if not expected_tid or actual_tid != expected_tid:
        return f"tid/URL final inesperado: {final_url}"

    normalized_country = " ".join(country.split()).casefold()
    normalized_title = " ".join(document_title.split()).casefold()
    normalized_h1 = " ".join(h1.split()).casefold()
    if not normalized_title.endswith(f", {normalized_country} - ucoin.net"):
        return f"título de outro país ou página inesperada: {document_title!r}"
    if normalized_h1 and not (
        normalized_h1 == normalized_country
        or normalized_h1.startswith(f"{normalized_country} ")
    ):
        return f"cabeçalho de outro país: {h1!r}"
    return ""


def validate_loaded_coin_page(page: Any, detail_url: str, country: str) -> None:
    try:
        h1 = page.locator("h1").first.inner_text(timeout=2_000)
    except Exception:  # noqa: BLE001 - title remains the stable fallback
        h1 = ""
    try:
        body_text = page.locator("body").inner_text(timeout=2_000)
    except Exception:  # noqa: BLE001 - title and URL are still checked
        body_text = ""
    error = detail_page_identity_error(
        detail_url=detail_url,
        final_url=str(page.url),
        document_title=str(page.title()),
        h1=h1,
        body_text=body_text,
        country=country,
    )
    if error:
        raise CatalogVariantError(
            f"Página uCoin rejeitada para {detail_url}: {error}. "
            "O progresso anterior continua guardado."
        )


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    catalogue = load_catalog(args.catalog)
    country = str(catalogue.get("country") or "").strip()
    if not country:
        raise CatalogVariantError("O catálogo não identifica o país.")
    catalog_type = detect_catalog_type(catalogue, args.catalog_type)
    coins = select_catalog_coins(
        catalog_coins(catalogue, catalog_type, args.group_title), args.detail_url
    )

    incognito_process = None
    if args.incognito:
        incognito_process = launch_incognito_chromium(args)
    elif not cdp_is_available(args.cdp_url):
        raise CatalogVariantError(f"Browser CDP indisponível em {args.cdp_url}.")

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        raise CatalogVariantError("Playwright não está instalado.") from exc

    playwright = sync_playwright().start()
    browser = playwright.chromium.connect_over_cdp(args.cdp_url)
    pages = [page for context in browser.contexts for page in context.pages]
    page = next((candidate for candidate in pages if "ucoin.net/coin/" in candidate.url), None)
    if page is None:
        page = browser.contexts[0].new_page()
    if args.manual_session and not args.no_manual_session:
        print("Resolve o challenge/login do uCoin no browser aberto.")
        try:
            input("Carrega Enter para continuar... ")
        except EOFError:
            pass
    visited = 0
    try:
        for position, coin in enumerate(coins, start=1):
            denomination, years = coin_label(coin, catalog_type)
            collection_status = str(coin.get("variantCollectionStatus") or "")
            if (
                not args.refresh
                and (
                    collection_status in VARIANT_COLLECTION_COMPLETE_STATUSES
                    or (
                        isinstance(coin.get("variants"), list)
                        and bool(coin["variants"])
                    )
                )
            ):
                if collection_status == VARIANT_STATUS_PRO_REQUIRED:
                    message = "ignoradas: requer subscrição uCoin PRO"
                else:
                    message = "já verificadas"
                print(
                    f"Variantes {position}/{len(coins)} — {denomination} ({years}) — {message}",
                    flush=True,
                )
                continue
            if visited:
                page.wait_for_timeout(max(0, int(args.delay * 1000)))
            detail_url = str(coin["detailUrl"])
            page_status = open_ucoin_detail_page(
                page,
                detail_url,
                label=denomination,
                issue_period=years,
                timeout_seconds=args.timeout,
                login_retries=args.login_retries,
            )
            if page_status == VARIANT_STATUS_PRO_REQUIRED:
                result = {
                    "status": VARIANT_STATUS_PRO_REQUIRED,
                    "variants": [],
                    "excludedProofOnlyVariants": [],
                }
            else:
                page.wait_for_timeout(1000)
                validate_loaded_coin_page(page, detail_url, country)
                result = extract_coin_variants(page, years, detail_url)
            variants = result["variants"]
            excluded_by_policy: list[dict[str, Any]] = []
            if catalog_type == "special":
                excluded_by_policy = [
                    variant
                    for variant in variants
                    if is_s_variant(str(variant.get("tag") or ""))
                ]
                variants = [
                    variant
                    for variant in variants
                    if not is_s_variant(str(variant.get("tag") or ""))
                ]
                variants = [
                    {**variant, "order": index}
                    for index, variant in enumerate(variants)
                ]
            coin["variants"] = variants if len(variants) >= 2 else []
            coin["hasVariants"] = bool(coin["variants"])
            coin["excludedProofOnlyVariants"] = result[
                "excludedProofOnlyVariants"
            ]
            coin["variantCollectionStatus"] = result["status"]
            if catalog_type == "special":
                coin["excludedCollectionSVariants"] = excluded_by_policy
            propagate_variant_metadata(catalogue, catalog_type, coin)
            write_json_atomic(args.catalog, catalogue)
            visited += 1
            if result["status"] == VARIANT_STATUS_PRO_REQUIRED:
                print(
                    f"Variantes {position}/{len(coins)} — {denomination} ({years}) — "
                    "ignoradas: requer subscrição uCoin PRO",
                    flush=True,
                )
            else:
                print(
                    f"Variantes {position}/{len(coins)} — {denomination} ({years}) — "
                    f"{len(coin['variants'])} válidas · "
                    f"{len(coin['excludedProofOnlyVariants'])} Proof excluídas",
                    flush=True,
                )
            if (
                args.batch_size > 0
                and visited % args.batch_size == 0
                and position < len(coins)
            ):
                print(f"Pausa segura de {args.batch_pause:.0f}s entre lotes...", flush=True)
                page.wait_for_timeout(max(0, int(args.batch_pause * 1000)))
    finally:
        playwright.stop()
        if incognito_process is not None and incognito_process.poll() is None:
            incognito_process.terminate()

    variants = sum(
        len(coin.get("variants", []))
        for coin in coins
        if isinstance(coin.get("variants"), list)
    )
    with_variants = sum(bool(coin.get("variants")) for coin in coins)
    pro_restricted = sum(
        coin.get("variantCollectionStatus") == VARIANT_STATUS_PRO_REQUIRED
        for coin in coins
    )
    print(
        f"Concluído: {with_variants}/{len(coins)} moedas com variantes; "
        f"{variants} variantes guardadas em {args.catalog}; "
        f"{pro_restricted} ignoradas por requererem uCoin PRO."
    )
    print("Site Base44: nenhuma alteração efetuada.")
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except (CatalogVariantError, RuntimeError, ValueError) as exc:
        print(f"Erro: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
