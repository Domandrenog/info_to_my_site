#!/usr/bin/env python3
"""Enrich a local uCoin catalogue with names from official detail pages.

Catalogue list pages do not reliably expose the commemorative subject.  This
read-only browser collector visits each already-known detail URL and records
its heading, making it possible to validate name-to-URL associations instead
of trusting a sample photograph alone.  Progress is saved after every page and
an interrupted run resumes from the same output file.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from scripts.ucoin_catalog import (
    cdp_is_available,
    launch_incognito_chromium,
    resolve_browser_executable,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recolhe os temas oficiais das páginas de detalhe uCoin."
    )
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    browser_mode = parser.add_mutually_exclusive_group()
    browser_mode.add_argument("--attach-cdp", action="store_true")
    browser_mode.add_argument("--incognito", action="store_true")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    parser.add_argument("--user-data-dir", default=".ucoin-detail-profile")
    parser.add_argument("--chrome-path", default="")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument(
        "--retries",
        type=int,
        default=0,
        help="Por defeito para ao primeiro bloqueio; ativa retentativas apenas manualmente.",
    )
    parser.add_argument("--delay", type=float, default=5.0)
    parser.add_argument("--retry-backoff", type=float, default=30.0)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--batch-pause", type=float, default=60.0)
    parser.add_argument(
        "--max-items",
        type=int,
        default=0,
        help="Limite de diagnóstico; zero recolhe todos.",
    )
    return parser.parse_args(argv)


def read_catalog(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    periods = payload.get("periods") if isinstance(payload, dict) else None
    if not isinstance(periods, list):
        raise RuntimeError("Catálogo sem periods.")
    catalog_country = str(payload.get("country") or "").strip()
    coins: list[dict[str, Any]] = []
    for period in periods:
        if not isinstance(period, dict):
            continue
        period_country = str(period.get("country") or catalog_country).strip()
        for coin in period.get("coins", []):
            if not isinstance(coin, dict):
                continue
            coins.append(
                {
                    **coin,
                    "_catalogCountry": catalog_country,
                    "_periodCountry": period_country,
                }
            )
    if not coins:
        raise RuntimeError("Catálogo sem moedas.")
    return coins


def expected_tid(url: str) -> int | None:
    values = parse_qs(urlparse(url).query).get("tid", [])
    try:
        return int(values[0]) if values else None
    except (TypeError, ValueError):
        return None


def valid_detail_item(
    item: dict[str, Any], source_coin: dict[str, Any] | None = None
) -> bool:
    url = str(item.get("detailUrl") or "")
    tid = expected_tid(url)
    h1 = str(item.get("h1") or "").strip().casefold()
    expected_countries = {
        str(value or "").strip().casefold()
        for value in (
            (source_coin or {}).get("_catalogCountry"),
            (source_coin or {}).get("_periodCountry"),
            item.get("catalogCountry"),
            item.get("periodCountry"),
        )
        if str(value or "").strip()
    }
    country_matches = any(
        h1 == country or h1.startswith(f"{country} ")
        for country in expected_countries
    )
    return (
        tid is not None
        and item.get("ucoinTypeId") == tid
        and bool(expected_countries)
        and country_matches
        and bool(str(item.get("subject") or "").strip())
    )


def read_existing(
    path: Path, coins_by_url: dict[str, dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], int]:
    if not path.exists():
        return {}, 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("Output existente inválido.")
    result: dict[str, dict[str, Any]] = {}
    rejected = 0
    for item in items:
        if not isinstance(item, dict) or not item.get("detailUrl"):
            raise RuntimeError("Output existente contém um item inválido.")
        source_coin = coins_by_url.get(str(item["detailUrl"]))
        if source_coin is None or not valid_detail_item(item, source_coin):
            rejected += 1
            continue
        result[str(item["detailUrl"])] = item
    return result, rejected


def write_output(
    path: Path, catalog: Path, expected: int, items: dict[str, dict[str, Any]]
) -> None:
    payload = {
        "kind": "ucoin_detail_subjects",
        "source_catalog": str(catalog),
        "expected": expected,
        "completed": len(items),
        "items": list(items.values()),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    json.loads(temporary.read_text(encoding="utf-8"))
    temporary.replace(path)


def extract_metadata(page: Any) -> dict[str, Any]:
    return page.evaluate(
        r"""
        () => {
          const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
          const headings = [...document.querySelectorAll('h2')]
            .map((node) => clean(node.textContent))
            .filter(Boolean);
          const ignored = new Set([
            'Collection', 'Offers', 'Language', 'Popular in Catalog',
            'Varieties', 'Mintage, Rarity, Worth', 'Mintage',
            'Configurações da conta', 'Coleção', 'Língua', 'Ofertas',
            'Variedades', 'Cunhagem', 'Cunhagem, Raridade, Valor'
          ]);
          const h1 = clean(document.querySelector('h1')?.textContent);
          const subject = headings.find((value) =>
            !ignored.has(value) && value !== h1
          ) || '';
          return {documentTitle: document.title, h1, subject, headings};
        }
        """
    )


def collect_one(
    page: Any, coin: dict[str, Any], url: str, args: argparse.Namespace
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(args.retries + 1):
        try:
            response = page.goto(
                url, wait_until="domcontentloaded", timeout=args.timeout * 1000
            )
            page.wait_for_timeout(500)
            status = response.status if response is not None else None
            if status is not None and status >= 400:
                raise RuntimeError(f"HTTP {status}")
            metadata = extract_metadata(page)
            if not metadata.get("subject"):
                raise RuntimeError("Tema ausente na página de detalhe.")
            result = {
                "ucoinTypeId": coin.get("ucoinTypeId"),
                "detailUrl": url,
                "finalUrl": page.url,
                "status": status,
                "catalogCountry": coin.get("_catalogCountry"),
                "periodCountry": coin.get("_periodCountry"),
                **metadata,
            }
            if not valid_detail_item(result, coin):
                raise RuntimeError(
                    f"Conteúdo inesperado ou bloqueado: {metadata.get('h1')!r}"
                )
            return result
        except Exception as exc:  # noqa: BLE001 - bounded browser retry
            last_error = exc
            if attempt < args.retries:
                delay = min(60.0, args.retry_backoff * (2**attempt))
                print(
                    f"Bloqueio/erro em {url}; nova tentativa em {delay:.0f}s "
                    f"({attempt + 1}/{args.retries})."
                )
                page.wait_for_timeout(int(1000 * delay))
    raise RuntimeError(f"Falhou {url}: {last_error}") from last_error


def run(args: argparse.Namespace) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Playwright não está instalado.") from exc

    coins = read_catalog(args.catalog)
    selected = coins[: args.max_items] if args.max_items > 0 else coins
    coins_by_url = {
        str(coin.get("detailUrl") or "").strip(): coin
        for coin in coins
        if str(coin.get("detailUrl") or "").strip()
    }
    existing, rejected = read_existing(args.output, coins_by_url)
    if rejected:
        print(
            f"Descartadas {rejected} respostas inválidas/bloqueadas do progresso anterior."
        )
        write_output(args.output, args.catalog, len(coins), existing)
    incognito_process: subprocess.Popen[bytes] | None = None
    with sync_playwright() as playwright:
        browser = None
        context = None
        if args.incognito:
            incognito_process = launch_incognito_chromium(args)
        try:
            if args.attach_cdp or args.incognito:
                if not cdp_is_available(args.cdp_url):
                    raise RuntimeError(f"Browser CDP indisponível em {args.cdp_url}.")
                browser = playwright.chromium.connect_over_cdp(args.cdp_url)
                context = (
                    browser.contexts[0]
                    if browser.contexts
                    else browser.new_context(viewport={"width": 1400, "height": 1000})
                )
            else:
                executable = resolve_browser_executable(args.chrome_path)
                options: dict[str, Any] = {
                    "headless": args.headless,
                    "ignore_https_errors": True,
                    "args": ["--no-sandbox", "--disable-dev-shm-usage"],
                }
                if executable:
                    options["executable_path"] = executable
                context = playwright.chromium.launch_persistent_context(
                    str(Path(args.user_data_dir).resolve()),
                    **options,
                    viewport={"width": 1400, "height": 1000},
                )
            page = context.new_page()
            total = len(selected)
            collected_this_run = 0
            for position, coin in enumerate(selected, start=1):
                url = str(coin.get("detailUrl") or "").strip()
                if not url:
                    raise RuntimeError("Moeda sem detailUrl no catálogo.")
                if url not in existing:
                    metadata = collect_one(page, coin, url, args)
                    existing[url] = {
                        "denomination": coin.get("denomination"),
                        "issuePeriod": coin.get("issuePeriod"),
                        **metadata,
                    }
                    write_output(args.output, args.catalog, len(coins), existing)
                    collected_this_run += 1
                    if args.delay:
                        page.wait_for_timeout(int(args.delay * 1000))
                    if (
                        args.batch_size > 0
                        and collected_this_run % args.batch_size == 0
                        and position < total
                    ):
                        print(
                            f"Pausa preventiva de {args.batch_pause:.0f}s após "
                            f"{collected_this_run} páginas novas."
                        )
                        page.wait_for_timeout(int(args.batch_pause * 1000))
                print(
                    f"{position}/{total} ({position / total * 100:.1f}%) — "
                    f"{existing[url]['subject']}"
                )
        finally:
            if context is not None and browser is None:
                context.close()
            if incognito_process is not None and incognito_process.poll() is None:
                incognito_process.terminate()
    print(f"Guardado: {args.output} — {len(existing)}/{len(coins)} páginas")
    print("Site uCoin: apenas leitura. Site Base44: nenhuma alteração efetuada.")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
