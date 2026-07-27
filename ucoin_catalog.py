#!/usr/bin/env python3
"""Extrai moedas do catálogo do uCoin por país e época.

O site do uCoin pode responder com proteção anti-bot em pedidos diretos.
Este script usa um browser real via Playwright, com suporte para perfil
persistente e login manual, para abrir o catálogo e recolher os links
visíveis das moedas.
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path
from shutil import which
from urllib.parse import quote, urljoin

from ucoin_to_mysite.catalog_parser import CatalogueFetchError, CatalogueFetchResponse, coin_start_year, crawl_ucoin_catalogue


DEFAULT_BASE_URL = "https://pt.ucoin.net/catalog/"
UCOIN_CATALOG_FILENAME = "ucoin-catalog.json"


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Busca moedas do uCoin por país e época.")
    parser.add_argument("country", help="País a consultar, ex.: Canada ou Canadá.")
    parser.add_argument(
        "period",
        nargs="?",
        default=None,
        help="Época/intervalo/ano a filtrar, ex.: 1901-1910, 1990, Era da Federação.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="URL base do catálogo do uCoin.")
    parser.add_argument("--json", action="store_true", help="Imprime o resultado em JSON.")
    parser.add_argument("--output", help="Guarda o resultado num ficheiro JSON.")
    parser.add_argument(
        "--output-dir",
        help="Pasta de destino para guardar o resultado. Se omitida, é criada uma pasta automática.",
    )
    parser.add_argument("--headful", action="store_true", help="(Compatibilidade) Mantém browser visível.")
    parser.add_argument("--headless", action="store_true", help="Corre sem janela visível (desaconselhado para Cloudflare).")
    parser.add_argument(
        "--manual-session",
        action="store_true",
        help="Força sessão manual: espera confirmação antes de extrair.",
    )
    parser.add_argument(
        "--no-manual-session",
        action="store_true",
        help="Desativa a espera manual antes da extração.",
    )
    parser.add_argument("--manual-login", action="store_true", help="Pausa para login manual antes de extrair.")
    parser.add_argument(
        "--attach-cdp",
        action="store_true",
        help="Liga a um browser ja aberto com remote debugging, em vez de abrir outro browser.",
    )
    parser.add_argument(
        "--cdp-url",
        default="http://127.0.0.1:9222",
        help="URL CDP do browser aberto (default: http://127.0.0.1:9222).",
    )
    parser.add_argument("--user-data-dir", default=".ucoin-profile", help="Perfil persistente do browser.")
    parser.add_argument("--chrome-path", default="", help="Caminho para Chromium/Chrome, se necessário.")
    parser.add_argument("--timeout", type=int, default=60, help="Timeout base em segundos.")
    parser.add_argument("--max-pages", type=int, default=50, help="Limite máximo de páginas do catálogo a visitar.")
    parser.add_argument(
        "--start-year",
        type=int,
        help="Mantém apenas moedas/periodos cujo ano inicial seja maior ou igual a este ano.",
    )
    parser.add_argument("--retries", type=int, default=2, help="Tentativas extra para erros temporários por página.")
    parser.add_argument(
        "--sequential-fallback",
        action="store_true",
        help="Tenta page=N+1 quando a paginação HTML não mostra links seguintes.",
    )
    return parser.parse_args()


def resolve_browser_executable(chrome_path: str) -> str | None:
    if chrome_path:
        candidate = Path(chrome_path)
        if candidate.exists():
            return str(candidate)
        raise RuntimeError(f"Browser não encontrado em {chrome_path}")

    for candidate in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        resolved = which(candidate)
        if resolved:
            return resolved
    return None


def build_country_url(base_url: str, country: str) -> str:
    country_slug = quote(slugify(country))
    separator = "&" if "?" in base_url else "?"
    if "country=" in base_url:
        return base_url
    return f"{base_url.rstrip('/')}/?country={country_slug}" if base_url.endswith("catalog/") else f"{base_url}{separator}country={country_slug}"


def auto_scroll(page) -> None:
    page.evaluate(
        """
        async () => {
            await new Promise((resolve) => {
                let totalHeight = 0;
                const step = 700;
                const timer = setInterval(() => {
                    const scrollHeight = document.body.scrollHeight;
                    window.scrollBy(0, step);
                    totalHeight += step;
                    if (totalHeight >= scrollHeight) {
                        clearInterval(timer);
                        window.scrollTo(0, 0);
                        resolve();
                    }
                }, 250);
            });
        }
        """
    )


def click_first_match(page, epoch: str) -> bool:
    period_query = epoch.strip()
    if not period_query:
        return False

    lowered = re.sub(r"\s*-\s*", "-", period_query.lower())

    period_links = page.locator("a[href*='period=']")
    count = period_links.count()
    for index in range(count):
        link = period_links.nth(index)
        try:
            text = link.inner_text(timeout=1000).replace("\n", " ").strip()
            href = link.get_attribute("href") or ""
        except Exception:  # noqa: BLE001
            continue

        normalized_text = re.sub(r"\s*-\s*", "-", text.lower())
        normalized_href = re.sub(r"\s*-\s*", "-", href.lower())
        if lowered in normalized_text or lowered in normalized_href:
            try:
                page.goto(urljoin(page.url, href), wait_until="domcontentloaded", timeout=60_000)
                return True
            except Exception:  # noqa: BLE001
                try:
                    link.click(timeout=2000)
                    return True
                except Exception:  # noqa: BLE001
                    continue

    return False


def fetch_catalogue_page(page, url: str, args: argparse.Namespace) -> CatalogueFetchResponse:
    transient_statuses = {429, 500, 502, 503, 504}
    response = page.goto(url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
    page.wait_for_timeout(1500)

    if response is not None and response.status >= 400:
        raise CatalogueFetchError(
            f"HTTP {response.status} while loading {url}",
            retryable=response.status in transient_statuses,
        )

    auto_scroll(page)
    page.wait_for_timeout(700)
    return CatalogueFetchResponse(html=page.content(), final_url=page.url)


def filter_periods_by_start_year(periods: list[object], start_year: int | None) -> list[object]:
    if start_year is None:
        return periods
    filtered_periods: list[object] = []
    for period in periods:
        if not isinstance(period, dict):
            continue
        coins = []
        for coin in period.get("coins", []):
            if not isinstance(coin, dict):
                continue
            start_year_value = coin_start_year(coin)
            if start_year_value is not None and start_year_value >= start_year:
                coins.append(coin)
        if not coins:
            continue
        filtered_period = dict(period)
        filtered_period["coins"] = coins
        filtered_periods.append(filtered_period)
    return filtered_periods


def scrape_catalog(args: argparse.Namespace, output_dir: Path) -> dict[str, object]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Playwright não está instalado. Instala com: python3 -m pip install playwright") from exc

    manual_session = (args.manual_session or args.manual_login) or (not args.no_manual_session)
    if manual_session and args.headless:
        raise RuntimeError("--headless não pode ser usado com sessão manual ativa. Usa --no-manual-session.")

    page_url = build_country_url(args.base_url, args.country)
    report: dict[str, object] = {
        "country": args.country,
        "period": args.period,
        "page_url": page_url,
        "periods": [],
        "warnings": [],
    }

    with sync_playwright() as playwright:
        browser = None
        context = None
        if args.attach_cdp:
            try:
                browser = playwright.chromium.connect_over_cdp(args.cdp_url)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    "Falha ao ligar ao browser aberto via CDP. Inicia o browser com "
                    "--remote-debugging-port=9222 e usa --attach-cdp."
                ) from exc
            context = browser.contexts[0] if browser.contexts else browser.new_context(viewport={"width": 1600, "height": 1200})
        else:
            executable = resolve_browser_executable(args.chrome_path)
            launch_options = {
                "headless": args.headless,
                "ignore_https_errors": True,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            if executable:
                launch_options["executable_path"] = executable

            context = playwright.chromium.launch_persistent_context(
                str(Path(args.user_data_dir).resolve()),
                **launch_options,
                viewport={"width": 1600, "height": 1200},
            )
        try:
            page = context.new_page() if args.attach_cdp else (context.pages[0] if context.pages else context.new_page())
            page.goto(page_url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
            page.wait_for_timeout(2500)

            if manual_session:
                print("Modo sessão manual ativo.")
                print("No browser aberto, resolve o challenge/login do Cloudflare e volta aqui.")
                try:
                    input("Carrega Enter para continuar... ")
                except EOFError:
                    pass
                page.goto(page_url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
                page.wait_for_timeout(3500)

            if args.period:
                click_first_match(page, args.period)
                page.wait_for_timeout(2000)
            initial_catalogue_url = page.url

            report["title"] = page.title()
            report["final_url"] = initial_catalogue_url

            crawl_result = crawl_ucoin_catalogue(
                initial_catalogue_url,
                lambda url: fetch_catalogue_page(page, url, args),
                max_pages=args.max_pages,
                retries=args.retries,
                retry_backoff_seconds=1.0,
                enable_sequential_fallback=args.sequential_fallback,
                base_url="https://pt.ucoin.net",
                stop_after_start_year_miss=args.start_year,
                stop_after_start_year_miss_limit=3,
            )

            report["country"] = crawl_result.get("country") or report["country"]
            report["startYearFilter"] = args.start_year
            report["periods"] = filter_periods_by_start_year(crawl_result["periods"], args.start_year)
            report["warnings"] = crawl_result["warnings"]
            report["pagination"] = crawl_result["pagination"]
            report["count"] = sum(len(period.get("coins", [])) for period in report["periods"])
            return report
        finally:
            if context is not None and browser is None:
                context.close()


def write_output(path: str, payload: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def default_output_dir(country: str, period: str | None) -> Path:
    return Path(slugify(country))


def default_output_path(args: argparse.Namespace) -> Path:
    folder = Path(args.output_dir) if args.output_dir else default_output_dir(args.country, args.period)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / UCOIN_CATALOG_FILENAME


def prepare_output_path(args: argparse.Namespace) -> Path:
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target
    return default_output_path(args)


def print_human(report: dict[str, object]) -> None:
    periods = report.get("periods", [])
    assert isinstance(periods, list)

    print(f"country={report.get('country')}")
    print(f"period={report.get('period')}")
    print(f"count={report.get('count', 0)}")
    print(f"url={report.get('final_url')}")
    print("")
    for historical_period in periods:
        if not isinstance(historical_period, dict):
            continue
        print(f"{historical_period.get('fullTitle') or historical_period.get('rulerOrPeriodName')}")
        for coin in historical_period.get("coins", []):
            if not isinstance(coin, dict):
                continue
            denomination = coin.get("denomination") or {}
            issue_period = coin.get("issuePeriod") or {}
            print(f"- {denomination.get('displayName')} | {issue_period.get('displayValue')}")
            print(f"  {coin.get('detailUrl')}")


def main() -> int:
    args = parse_args()
    output_path = prepare_output_path(args)
    output_dir = output_path.parent

    try:
        report = scrape_catalog(args, output_dir)
        report["output_path"] = str(output_path)
        write_output(str(output_path), report)
    except Exception as exc:  # noqa: BLE001
        error_report = {
            "country": args.country,
            "period": args.period,
            "output_path": str(output_path),
            "error": str(exc),
        }
        error_path = output_path.with_name("scrape-error.json")
        write_output(str(error_path), error_report)
        print(json.dumps(error_report, ensure_ascii=False, indent=2))
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_human(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())