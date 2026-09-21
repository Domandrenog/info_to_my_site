#!/usr/bin/env python3
"""Recheck whether uCoin photos previously reported as unavailable now exist."""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from scripts.import_base44_coins import format_duration
from scripts.ucoin_catalog import launch_incognito_chromium


IMAGE_FIELDS = {
    "obverseImage": "frente",
    "reverseImage": "verso",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Volta a consultar no uCoin as moedas marcadas como sem fotografia.",
    )
    parser.add_argument("--input", required=True, help="Relatório JSON criado pelo check de diferenças.")
    browser_mode = parser.add_mutually_exclusive_group(required=True)
    browser_mode.add_argument(
        "--attach-cdp",
        action="store_true",
        help="Liga ao Chromium já aberto com remote debugging.",
    )
    browser_mode.add_argument(
        "--incognito",
        action="store_true",
        help="Abre automaticamente Chromium em modo incógnito.",
    )
    parser.add_argument(
        "--cdp-url",
        default="http://127.0.0.1:9222",
        help="URL CDP do browser (default: http://127.0.0.1:9222).",
    )
    parser.add_argument(
        "--manual-session",
        action="store_true",
        help="Pausa uma vez para resolver Cloudflare/login antes da revisão.",
    )
    parser.add_argument(
        "--no-manual-session",
        action="store_true",
        help="Não pausa antes da revisão (compatibilidade com o menu).",
    )
    parser.add_argument("--timeout", type=int, default=60, help="Timeout por página em segundos.")
    parser.add_argument("--user-data-dir", default=".ucoin-profile", help="Perfil usado pelo Chromium.")
    parser.add_argument("--chrome-path", default="", help="Caminho para Chromium/Chrome, se necessário.")
    return parser.parse_args()


def canonical_coin_url(value: str) -> str:
    """Turn a uCoin login wrapper into the referenced public coin URL."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.path.rstrip("/") != "/login":
        return raw
    references = parse_qs(parsed.query).get("ref", [])
    if not references:
        return raw
    return urljoin(f"{parsed.scheme or 'https'}://{parsed.netloc or 'pt.ucoin.net'}", unquote(references[0]))


def normalize_reports(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    reports = payload.get("reports") or payload.get("countries")
    if isinstance(reports, list):
        return [item for item in reports if isinstance(item, dict)]
    return [payload]


def missing_photo_entries(payload: object) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for report in normalize_reports(payload):
        country = str(report.get("country_name") or report.get("country") or "País desconhecido")
        coins = report.get("coins_with_issues", [])
        for coin in coins if isinstance(coins, list) else []:
            if not isinstance(coin, dict):
                continue
            missing_sides = {
                IMAGE_FIELDS[str(issue.get("field"))]
                for issue in coin.get("issues", [])
                if isinstance(issue, dict)
                and issue.get("type") == "missing_image_url"
                and str(issue.get("field")) in IMAGE_FIELDS
            }
            if not missing_sides:
                continue
            denomination = str(coin.get("denomination") or "Moeda")
            issue_period = str(coin.get("issuePeriod") or "")
            ucoin_url = canonical_coin_url(str(coin.get("ucoinUrl") or ""))
            key = (country, denomination, issue_period, ucoin_url)
            if key in seen:
                continue
            seen.add(key)
            entries.append(
                {
                    "country": country,
                    "denomination": denomination,
                    "issuePeriod": issue_period,
                    "ucoinUrl": ucoin_url,
                    "missingSides": sorted(missing_sides, key=lambda side: (side != "frente", side)),
                }
            )
    return entries


def load_missing_photo_entries(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return missing_photo_entries(payload)


def image_side(url: str, alt: str = "") -> str:
    normalized_alt = str(alt or "").casefold()
    if any(label in normalized_alt for label in ("obverse", "anverso", "frente")):
        return "frente"
    if any(label in normalized_alt for label in ("reverse", "reverso", "verso")):
        return "verso"

    path = unquote(urlparse(url).path).casefold()
    if re.search(r"/\d+-1[a-z]*/", path):
        return "frente"
    if re.search(r"/\d+-2[a-z]*/", path):
        return "verso"
    return ""


def image_urls(image: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key in ("href", "currentSrc", "src", "dataSrc", "dataOriginal"):
        value = str(image.get(key) or "").strip()
        if value:
            candidates.append(value)
    srcset = str(image.get("srcset") or "")
    candidates.extend(part.strip().split(" ", 1)[0] for part in srcset.split(",") if part.strip())
    return candidates


def available_photo_sides(images: list[dict[str, Any]]) -> dict[str, str]:
    available: dict[str, str] = {}
    for image in images:
        if not isinstance(image, dict):
            continue
        alt = str(image.get("alt") or "")
        for raw_url in image_urls(image):
            parsed = urlparse(raw_url)
            host = parsed.netloc.casefold()
            path = unquote(parsed.path).casefold()
            if not host.endswith("ucoin.net") or not path.startswith("/coin/"):
                continue
            side = image_side(raw_url, alt)
            if side:
                available.setdefault(side, raw_url)
    return available


def extract_page_images(page: Any) -> list[dict[str, Any]]:
    return page.locator("img").evaluate_all(
        """
        (images) => images.map((image) => ({
            alt: image.getAttribute('alt') || '',
            currentSrc: image.currentSrc || '',
            src: image.getAttribute('src') || '',
            dataSrc: image.getAttribute('data-src') || '',
            dataOriginal: image.getAttribute('data-original') || '',
            srcset: image.getAttribute('srcset') || '',
            href: image.closest('a')?.href || ''
        }))
        """
    )


def inspect_entry(page: Any, entry: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    url = str(entry.get("ucoinUrl") or "")
    if not url:
        return {**entry, "status": "error", "error": "URL do uCoin em falta"}

    response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
    page.wait_for_timeout(1200)
    if response is not None and response.status >= 400:
        return {**entry, "status": "error", "error": f"HTTP {response.status}"}

    final_url = page.url
    if urlparse(final_url).path.rstrip("/") == "/login":
        return {**entry, "status": "error", "error": "o uCoin pediu login"}

    title = page.title().casefold()
    if any(value in title for value in ("just a moment", "cloudflare", "attention required")):
        return {**entry, "status": "error", "error": "Cloudflare ainda não foi resolvido"}

    available = available_photo_sides(extract_page_images(page))
    missing_sides = set(entry.get("missingSides", []))
    newly_available = sorted(missing_sides.intersection(available), key=lambda side: (side != "frente", side))
    if not newly_available:
        return {**entry, "status": "unchanged", "availableSides": []}
    status = "available" if set(newly_available) == missing_sides else "partial"
    return {
        **entry,
        "status": status,
        "availableSides": newly_available,
        "photoUrls": {side: available[side] for side in newly_available},
    }


def side_text(sides: list[str]) -> str:
    side_set = set(sides)
    if side_set == {"frente", "verso"}:
        return "frente e verso"
    return " e ".join(sides)


def entry_label(entry: dict[str, Any]) -> str:
    period = str(entry.get("issuePeriod") or "")
    suffix = f" ({period})" if period else ""
    return f"{entry.get('country', '')} — {entry.get('denomination', '')}{suffix}"


def print_result(result: dict[str, Any]) -> None:
    status = result.get("status")
    if status == "available":
        print(f"  Já disponível: {side_text(result.get('availableSides', []))}")
    elif status == "partial":
        print(f"  Parcialmente disponível: {side_text(result.get('availableSides', []))}")
    elif status == "unchanged":
        print(f"  Continua indisponível: {side_text(result.get('missingSides', []))}")
    else:
        print(f"  Não foi possível verificar: {result.get('error', 'erro desconhecido')}")


def print_summary(results: list[dict[str, Any]]) -> None:
    available = [result for result in results if result.get("status") in {"available", "partial"}]
    unchanged = [result for result in results if result.get("status") == "unchanged"]
    errors = [result for result in results if result.get("status") == "error"]

    print("\n" + "#" * 72)
    print("RESULTADO DA REVISÃO DE FOTOGRAFIAS NO uCoin")
    print("#" * 72)
    available_label = "moeda" if len(available) == 1 else "moedas"
    unchanged_label = "moeda" if len(unchanged) == 1 else "moedas"
    print(f"- Com fotografias novas: {len(available)} {available_label}")
    print(f"- Ainda sem fotografias: {len(unchanged)} {unchanged_label}")
    if errors:
        error_label = "moeda" if len(errors) == 1 else "moedas"
        print(f"- Não foi possível verificar: {len(errors)} {error_label}")

    if available:
        print("\nFotografias encontradas agora:")
        for result in available:
            print(f"- {entry_label(result)}: {side_text(result.get('availableSides', []))}")
        print("\nNenhum ficheiro ou registo foi alterado.")


def run_recheck(args: argparse.Namespace, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Playwright não está instalado. Instala com: python3 -m pip install playwright") from exc

    results: list[dict[str, Any]] = []
    incognito_process = None
    with sync_playwright() as playwright:
        if args.incognito:
            incognito_process = launch_incognito_chromium(args)
        try:
            browser = playwright.chromium.connect_over_cdp(args.cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            try:
                if args.manual_session and not args.no_manual_session and entries:
                    first_url = str(entries[0].get("ucoinUrl") or "https://pt.ucoin.net")
                    page.goto(first_url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
                    print("No browser aberto, resolve Cloudflare/login do uCoin.")
                    try:
                        input("Carrega Enter para começar a revisão... ")
                    except EOFError:
                        pass

                started_at = time.monotonic()
                total = len(entries)
                for index, entry in enumerate(entries, start=1):
                    item_started_at = time.monotonic()
                    print(f"\n[{index}/{total}] {index / total:.0%} | {entry_label(entry)}", flush=True)
                    try:
                        result = inspect_entry(page, entry, args.timeout)
                    except Exception as exc:  # noqa: BLE001
                        result = {**entry, "status": "error", "error": str(exc)}
                    results.append(result)
                    print_result(result)

                    elapsed = time.monotonic() - started_at
                    item_elapsed = time.monotonic() - item_started_at
                    remaining = (elapsed / index) * (total - index) if index else 0.0
                    print(
                        f"  tempo: {format_duration(item_elapsed, precise=True)} | "
                        f"decorrido: {format_duration(elapsed)} | restante: {format_duration(remaining)}"
                    )
            finally:
                page.close()
        finally:
            if incognito_process is not None and incognito_process.poll() is None:
                incognito_process.terminate()
    return results


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    try:
        entries = load_missing_photo_entries(input_path)
    except Exception as exc:  # noqa: BLE001
        print(f"Não foi possível ler o relatório: {exc}")
        return 1

    if not entries:
        print("O relatório não contém moedas com fotografias indisponíveis no uCoin.")
        return 0

    if len(entries) == 1:
        print("Será revista 1 moeda diretamente no uCoin.")
    else:
        print(f"Serão revistas {len(entries)} moedas diretamente no uCoin.")
    print("Esta operação é apenas de leitura e não altera ficheiros nem o Site Base44.")
    try:
        results = run_recheck(args, entries)
    except Exception as exc:  # noqa: BLE001
        print(f"Não foi possível executar a revisão: {exc}")
        return 1
    print_summary(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
