#!/usr/bin/env python3
"""Collect uCoin variants into a normal or collection app catalogue."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

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
            "as variantes UNC/BU no próprio catálogo."
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
    parser.add_argument("--delay", type=float, default=5.0)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--batch-pause", type=float, default=20.0)
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
    return parser.parse_args(argv)


def load_catalog(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogVariantError(f"Catálogo inválido ou ilegível: {path}") from exc
    if not isinstance(value, dict):
        raise CatalogVariantError("O catálogo tem de ser um objeto JSON.")
    return value


def catalog_coins(catalogue: dict[str, Any], catalog_type: str) -> list[dict[str, Any]]:
    group_key = "periods" if catalog_type == "normal" else "programmes"
    result = [
        coin
        for group in catalogue.get(group_key, [])
        if isinstance(group, dict)
        for coin in group.get("coins", [])
        if isinstance(coin, dict)
    ]
    urls = [str(coin.get("detailUrl") or "").strip() for coin in result]
    if any(not url for url in urls) or len(urls) != len(set(urls)):
        raise CatalogVariantError("O catálogo contém URLs uCoin ausentes ou repetidos.")
    return result


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


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    catalogue = load_catalog(args.catalog)
    catalog_type = detect_catalog_type(catalogue, args.catalog_type)
    coins = catalog_coins(catalogue, catalog_type)

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
