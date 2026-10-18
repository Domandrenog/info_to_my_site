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
import subprocess
import time
import sys
import unicodedata
from pathlib import Path
from shutil import which
from urllib.error import URLError
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse
from urllib.request import urlopen

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.catalog_paths import country_directory, slugify
from ucoin_to_mysite.catalog_parser import CatalogueFetchError, CatalogueFetchResponse, coin_start_year, crawl_ucoin_catalogue


DEFAULT_BASE_URL = "https://pt.ucoin.net/catalog/"
UCOIN_CATALOG_FILENAME = "ucoin-catalog.json"
COIN_TYPE_CHOICES = ("normal", "commemorative", "collection", "all")
COIN_TYPE_LABELS = {
    "normal": "Circulação normal",
    "commemorative": "Comemorativa",
    "collection": "Moeda de coleção",
}
UCOIN_CATALOG_TYPE_VALUES = {
    "normal": "1",
    "commemorative": "2",
    "collection": "3",
}


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
    parser.add_argument(
        "--country-link-name",
        default="",
        help="Nome do país usado no parâmetro country= da URL (quando diferente do nome do país).",
    )
    parser.add_argument("--json", action="store_true", help="Imprime o resultado em JSON.")
    parser.add_argument("--output", help="Guarda o resultado num ficheiro JSON.")
    parser.add_argument(
        "--continent",
        default="",
        help="Continente usado na pasta info/paises/<continente>/<pais> quando não for reconhecido automaticamente.",
    )
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
    browser_mode = parser.add_mutually_exclusive_group()
    browser_mode.add_argument(
        "--attach-cdp",
        action="store_true",
        help="Liga a um browser ja aberto com remote debugging, em vez de abrir outro browser.",
    )
    browser_mode.add_argument(
        "--incognito",
        action="store_true",
        help="Abre automaticamente Chromium em modo incognito e liga-se a essa janela.",
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
    parser.add_argument(
        "--coin-type",
        choices=COIN_TYPE_CHOICES,
        default="normal",
        help=(
            "Tipo uCoin a recolher: normal=Circulação normal (default), "
            "commemorative=Comemorativa, collection=Moeda de coleção ou "
            "all=todos explicitamente."
        ),
    )
    parser.add_argument("--retries", type=int, default=2, help="Tentativas extra para erros temporários por página.")
    parser.add_argument(
        "--sequential-fallback",
        action="store_true",
        help="Tenta page=N+1 quando a paginação HTML não mostra links seguintes.",
    )
    parser.add_argument(
        "--include-variants",
        action="store_true",
        help=(
            "Visita cada moeda e recolhe variantes UNC/BU; linhas exclusivamente "
            "Proof ficam excluídas."
        ),
    )
    parser.add_argument(
        "--variant-delay",
        type=float,
        default=5.0,
        help="Pausa entre páginas de moedas durante a recolha de variantes.",
    )
    parser.add_argument(
        "--variant-batch-size",
        type=int,
        default=10,
        help="Número de moedas entre pausas longas da recolha de variantes.",
    )
    parser.add_argument(
        "--variant-batch-pause",
        type=float,
        default=20.0,
        help="Pausa longa entre lotes da recolha de variantes.",
    )
    parser.add_argument(
        "--variant-login-retries",
        type=int,
        default=2,
        help=(
            "Número de vezes que permite resolver um login/challenge no browser "
            "e repetir apenas a moeda atual."
        ),
    )
    parser.add_argument(
        "--variant-scan-output",
        default="",
        help=(
            "Checkpoint da recolha de variantes. Por omissão usa "
            "ucoin-variants-scan.json junto ao catálogo."
        ),
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


def cdp_is_available(cdp_url: str) -> bool:
    try:
        with urlopen(f"{cdp_url.rstrip('/')}/json/version", timeout=0.5):
            return True
    except (OSError, URLError):
        return False


def launch_incognito_chromium(args: argparse.Namespace) -> subprocess.Popen[bytes]:
    if cdp_is_available(args.cdp_url):
        raise RuntimeError(
            f"Ja existe um browser CDP em {args.cdp_url}. Fecha-o ou escolhe a opcao de ligar ao browser aberto."
        )

    parsed_cdp_url = urlparse(args.cdp_url)
    if parsed_cdp_url.hostname not in {"127.0.0.1", "localhost"} or parsed_cdp_url.port is None:
        raise RuntimeError("--incognito requer um --cdp-url local com porta, por exemplo http://127.0.0.1:9222.")

    executable = resolve_browser_executable(args.chrome_path)
    if not executable:
        raise RuntimeError("Chromium/Chrome nao encontrado. Indica --chrome-path com o executavel do browser.")

    command = [
        executable,
        "--incognito",
        f"--remote-debugging-address={parsed_cdp_url.hostname}",
        f"--remote-debugging-port={parsed_cdp_url.port}",
        f"--user-data-dir={Path(args.user_data_dir).resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    process = subprocess.Popen(command)
    for _ in range(50):
        if cdp_is_available(args.cdp_url):
            return process
        if process.poll() is not None:
            raise RuntimeError("Chromium fechou antes de ativar o modo incognito com CDP.")
        time.sleep(0.2)

    process.terminate()
    raise RuntimeError(f"Chromium nao ativou CDP em {args.cdp_url} dentro de 10 segundos.")


def build_country_url(base_url: str, country: str, country_link_name: str = "") -> str:
    # ``--country-link-name`` is an explicit uCoin URL value.  It must not be
    # passed through the filesystem-friendly slugifier: e.g. uCoin uses
    # ``sri_lanka``, while the local country folder is ``sri-lanka``.
    country_parameter = country_link_name.strip() or slugify(country)
    country_slug = quote(country_parameter, safe="_")
    separator = "&" if "?" in base_url else "?"
    if "country=" in base_url:
        return base_url
    return f"{base_url.rstrip('/')}/?country={country_slug}" if base_url.endswith("catalog/") else f"{base_url}{separator}country={country_slug}"


def apply_coin_type_to_catalog_url(url: str, coin_type: str) -> str:
    """Select the requested category on uCoin before pagination starts.

    Filtering the parsed rows alone is insufficient because the unfiltered
    catalogue can omit whole categories or mix rows from related territories.
    The explicit uCoin ``type`` query is therefore the first safety boundary;
    ``filter_periods_by_coin_type`` remains a second, fail-closed boundary.
    """

    if coin_type not in COIN_TYPE_CHOICES:
        raise ValueError(f"Tipo de moeda uCoin inválido: {coin_type}")
    parsed = urlparse(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "type"
    ]
    type_value = UCOIN_CATALOG_TYPE_VALUES.get(coin_type)
    if type_value is not None:
        query.append(("type", type_value))
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(query),
            parsed.fragment,
        )
    )


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


VARIANT_MARK_LABELS = {
    "D_": "D (sem FG)",
    "D_FG": "D (FG)",
    "D|ob": "D (anverso)",
    "D|rv": "D (reverso)",
    "S|ob": "S (anverso)",
    "S|rv": "S (reverso)",
}
VARIANT_SCAN_FILENAME = "ucoin-variants-scan.json"
VARIANT_STATUS_PRO_REQUIRED = "ucoin_pro_required"
DETAIL_PAGE_LOADED = "loaded"
VARIANT_COLLECTION_COMPLETE_STATUSES = frozenset(
    {"collected", "no_multiple_variants", "no_mintage_table", VARIANT_STATUS_PRO_REQUIRED}
)


def has_mintage(value: object) -> bool:
    text = str(value or "").strip()
    return text != "-" and bool(re.search(r"\d|\+", text))


def normalize_variant_mark(value: object) -> str:
    mark = " ".join(str(value or "").split())
    return VARIANT_MARK_LABELS.get(mark, mark)


def variant_sort_key(mark: str) -> tuple[int, str]:
    first = mark.split(" ", 1)[0].split("-", 1)[0]
    return ({"": 0, "P": 1, "D": 2, "S": 3, "W": 4}.get(first, 50), mark)


def variant_tag(*, year: str, mark: str, single_year: bool) -> str:
    normalized_mark = normalize_variant_mark(mark)
    if single_year:
        return normalized_mark or "Sem marca"
    return f"{year} {normalized_mark}".strip()


def finalize_variant_rows(
    issue_period: str,
    rows: list[dict[str, str]],
    detail_url: str,
) -> dict[str, object]:
    single_year = bool(re.fullmatch(r"\d{4}", issue_period.strip()))
    included: dict[str, tuple[int, tuple[int, str], str]] = {}
    excluded: dict[str, tuple[int, tuple[int, str], str]] = {}
    for row in rows:
        year = row["year"]
        normalized_mark = normalize_variant_mark(row.get("mark"))
        tag = variant_tag(year=year, mark=normalized_mark, single_year=single_year)
        sort_key = (int(year), variant_sort_key(normalized_mark), tag)
        if has_mintage(row.get("unc")) or has_mintage(row.get("bu")):
            included.setdefault(tag, sort_key)
        elif has_mintage(row.get("proof")):
            excluded.setdefault(tag, sort_key)

    included_tags = [entry[2] for entry in sorted(included.values())]
    proof_only_tags = [
        entry[2]
        for tag, entry in sorted(excluded.items(), key=lambda item: item[1])
        if tag not in included
    ]

    variants = (
        [{"tag": tag, "order": index} for index, tag in enumerate(included_tags)]
        if len(included_tags) >= 2
        else []
    )
    return {
        "status": "collected" if variants else "no_multiple_variants",
        "variants": variants,
        "excludedProofOnlyVariants": proof_only_tags,
    }


def parse_variant_table_cells(
    *,
    mode: str,
    cells: list[str],
    issue_period: str,
    display_mark: str = "",
) -> dict[str, str] | None:
    """Map the visible uCoin mintage columns without shifting UNC/BU/Proof.

    Multi-year rows are ``year, UNC, BU, Proof, rarity, price``. Single-year
    rows may either include a separate mint-mark column or contain only the
    mint description followed by UNC, BU and Proof.
    """

    if mode == "multi":
        if len(cells) < 4 or not re.fullmatch(r"\d{4}", cells[0]):
            return None
        year = cells[0]
        unc, bu, proof = cells[1:4]
        mark = display_mark
    elif mode == "single":
        year = issue_period.strip()
        if len(cells) >= 5:
            mark = cells[1]
            unc, bu, proof = cells[2:5]
        elif len(cells) >= 4:
            mark = ""
            unc, bu, proof = cells[1:4]
        else:
            return None
    else:
        raise RuntimeError(f"Modo de tabela de variantes inválido: {mode}.")
    return {
        "year": year,
        "mark": mark,
        "unc": unc,
        "bu": bu,
        "proof": proof,
    }


def variant_issue_period(issue: dict[str, object]) -> str:
    """Prefer uCoin's structured year for a single-year commemorative issue.

    Some commemoratives put the event title in ``displayValue`` even though
    ``startYear`` and ``endYear`` identify one exact year.  The detail page then
    correctly exposes the single-year mint table, so using the title would make
    that valid table look contradictory.
    """

    display_value = str(issue.get("displayValue") or "").strip()
    start_year = issue.get("startYear")
    end_year = issue.get("endYear")
    if (
        isinstance(start_year, int)
        and not isinstance(start_year, bool)
        and isinstance(end_year, int)
        and not isinstance(end_year, bool)
        and start_year == end_year
    ):
        return str(start_year)
    return display_value


def extract_coin_variants(page, issue_period: str, detail_url: str) -> dict[str, object]:
    """Read one uCoin mintage table without treating Proof-only rows as variants."""

    display_by_row_id: dict[str, dict[str, str]] = {}
    for row in page.locator("tr").all():
        anchors = row.locator('a[href^="#Year-"]')
        if not anchors.count():
            continue
        href = anchors.first.get_attribute("href") or ""
        cells = [" ".join(cell.inner_text().split()) for cell in row.locator("th,td").all()]
        if len(cells) >= 2:
            display_by_row_id[href.lstrip("#")] = {
                "mark": cells[1],
                "description": row.get_attribute("title") or "",
            }

    selected = None
    mode = ""
    for table in page.locator("table").all():
        if not table.locator("thead").count():
            continue
        headers = " ".join(table.locator("thead").inner_text().split())
        if not all(label in headers for label in ("UNC", "BU", "Proof")):
            continue
        if "Ano" in headers:
            selected = table
            mode = "multi"
            break
        if "Casa Moeda" in headers:
            selected = table
            mode = "single"
            break
    if selected is None:
        return {
            "status": "no_mintage_table",
            "variants": [],
            "excludedProofOnlyVariants": [],
        }

    single_year = bool(re.fullmatch(r"\d{4}", issue_period.strip()))
    if mode == "single" and not single_year:
        raise RuntimeError(f"Tabela de ano único inesperada em {detail_url}.")

    rows: list[dict[str, str]] = []
    for row in selected.locator("tbody tr").all():
        cells = [" ".join(cell.inner_text().split()) for cell in row.locator("th,td").all()]
        if mode == "multi":
            row_id = row.get_attribute("id") or ""
            display = display_by_row_id.get(row_id, {})
            parsed = parse_variant_table_cells(
                mode=mode,
                cells=cells,
                issue_period=issue_period,
                display_mark=display.get("mark", ""),
            )
        else:
            parsed = parse_variant_table_cells(
                mode=mode,
                cells=cells,
                issue_period=issue_period,
            )
        if parsed is not None:
            rows.append(parsed)
    return finalize_variant_rows(issue_period, rows, detail_url)


def write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def is_ucoin_pro_paywall_text(value: object) -> bool:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", normalized.casefold()).split())
    portuguese = (
        "acesso completo" in normalized
        and ("subscricao" in normalized or "assinatura" in normalized)
        and "membro pro" in normalized
    )
    english = (
        "full access" in normalized
        and ("pro membership" in normalized or "pro member" in normalized)
    )
    return portuguese or english


def page_requires_ucoin_pro(page) -> bool:
    try:
        text = page.locator("body").inner_text(timeout=2000)
    except Exception:  # noqa: BLE001 - fallback supports partial/error pages
        try:
            text = page.content()
        except Exception:  # noqa: BLE001 - absence of readable content is not proof
            return False
    return is_ucoin_pro_paywall_text(text)


def login_redirect_references_coin(url: str) -> bool:
    parsed = urlparse(url)
    if "/login/" not in parsed.path:
        return False
    references = parse_qs(parsed.query).get("ref", [])
    return any(reference.startswith("/coin/") for reference in references)


def open_ucoin_detail_page(
    page,
    detail_url: str,
    *,
    label: str,
    issue_period: str,
    timeout_seconds: int,
    login_retries: int,
) -> str:
    """Open one coin page and recover interactively from uCoin login redirects."""

    retries = max(0, login_retries)
    for attempt in range(retries + 1):
        response = page.goto(
            detail_url,
            wait_until="domcontentloaded",
            timeout=max(1, timeout_seconds) * 1000,
        )
        status = response.status if response is not None else None
        final_url = str(page.url)
        redirected_to_login = "/login/" in final_url
        # A coin-specific login redirect is the shape used by the uCoin PRO
        # restriction. Do not ask for a login that cannot unlock the variants.
        # The text check covers translated/alternative paywall redirects too.
        if redirected_to_login and (
            login_redirect_references_coin(final_url) or page_requires_ucoin_pro(page)
        ):
            return VARIANT_STATUS_PRO_REQUIRED
        if status == 200 and not redirected_to_login:
            return DETAIL_PAGE_LOADED

        if not redirected_to_login:
            raise RuntimeError(
                f"A recolha de variantes parou em {label} ({issue_period}): "
                f"HTTP {status}, URL final {final_url}. "
                "O progresso anterior continua guardado."
            )

        if attempt >= retries:
            raise RuntimeError(
                f"A recolha de variantes continua a pedir login em "
                f"{label} ({issue_period}), após {retries} tentativa(s) manual(is). "
                "O progresso anterior continua guardado; volta a executar para "
                "retomar nesta moeda."
            )

        tab_brought_to_front = True
        try:
            page.bring_to_front()
        except Exception:  # noqa: BLE001 - losing focus must not lose the checkpoint
            tab_brought_to_front = False

        print("", flush=True)
        print("#" * 72, flush=True)
        print("UCOIN PEDIU LOGIN — PROGRESSO GUARDADO", flush=True)
        print("#" * 72, flush=True)
        print(f"Moeda pendente: {label} ({issue_period})", flush=True)
        if tab_brought_to_front:
            print("A aba de login do uCoin foi trazida para a frente.", flush=True)
        else:
            print("Não foi possível trazer automaticamente a aba para a frente.", flush=True)
        print(f"Endereço aberto: {final_url}", flush=True)
        print(
            "Se não vires a aba, abre esse endereço no Chromium ligado à porta 9222.",
            flush=True,
        )
        print("Conclui o login/challenge nessa janela.", flush=True)
        try:
            input("Carrega Enter para repetir apenas esta moeda... ")
        except EOFError as exc:
            raise RuntimeError(
                f"O uCoin pediu login em {label} ({issue_period}), mas o terminal "
                "não permite confirmar a sessão manual. O progresso anterior "
                "continua guardado; volta a executar para retomar nesta moeda."
            ) from exc


def collect_catalogue_variants(
    page,
    report: dict[str, object],
    args: argparse.Namespace,
    output_dir: Path,
) -> dict[str, int]:
    coins = [
        coin
        for period in report.get("periods", [])
        if isinstance(period, dict)
        for coin in period.get("coins", [])
        if isinstance(coin, dict)
    ]
    configured_checkpoint = str(getattr(args, "variant_scan_output", "") or "").strip()
    checkpoint_path = (
        Path(configured_checkpoint)
        if configured_checkpoint
        else output_dir / VARIANT_SCAN_FILENAME
    )
    cached_by_url: dict[str, dict[str, object]] = {}
    if checkpoint_path.is_file():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            for item in checkpoint.get("items", []):
                if isinstance(item, dict) and isinstance(item.get("detailUrl"), str):
                    cached_by_url[item["detailUrl"]] = item
        except (OSError, json.JSONDecodeError, AttributeError):
            cached_by_url = {}

    current_urls = {
        str(coin.get("detailUrl") or "").strip() for coin in coins
    }
    cached_by_url = {
        detail_url: item
        for detail_url, item in cached_by_url.items()
        if detail_url in current_urls
    }
    # Keep every previously completed URL in memory before rewriting the
    # checkpoint. This prevents a second interruption from discarding entries
    # that the resumed loop has not printed yet.
    collected_by_url: dict[str, dict[str, object]] = dict(cached_by_url)
    visited_this_run = 0
    for position, coin in enumerate(coins, start=1):
        detail_url = str(coin.get("detailUrl") or "").strip()
        issue = coin.get("issuePeriod") if isinstance(coin.get("issuePeriod"), dict) else {}
        denomination = coin.get("denomination") if isinstance(coin.get("denomination"), dict) else {}
        issue_period = variant_issue_period(issue)
        label = str(denomination.get("displayName") or "Moeda").strip()
        cached = cached_by_url.get(detail_url)
        if cached is not None:
            result = {
                "status": cached.get("status"),
                "variants": cached.get("variants", []),
                "excludedProofOnlyVariants": cached.get(
                    "excludedProofOnlyVariants", []
                ),
            }
        else:
            if visited_this_run:
                page.wait_for_timeout(max(0, int(args.variant_delay * 1000)))
            page_status = open_ucoin_detail_page(
                page,
                detail_url,
                label=label,
                issue_period=issue_period,
                timeout_seconds=args.timeout,
                login_retries=args.variant_login_retries,
            )
            if page_status == VARIANT_STATUS_PRO_REQUIRED:
                result = {
                    "status": VARIANT_STATUS_PRO_REQUIRED,
                    "variants": [],
                    "excludedProofOnlyVariants": [],
                }
            else:
                page.wait_for_timeout(1000)
                result = extract_coin_variants(page, issue_period, detail_url)
            visited_this_run += 1

        variants = result.get("variants", [])
        proof_only = result.get("excludedProofOnlyVariants", [])
        result_status = str(result.get("status") or "")
        coin["hasVariants"] = bool(variants)
        coin["variants"] = variants
        coin["excludedProofOnlyVariants"] = proof_only
        coin["variantCollectionStatus"] = result_status
        collected_by_url[detail_url] = {
            "detailUrl": detail_url,
            "status": result_status,
            "variants": variants,
            "excludedProofOnlyVariants": proof_only,
        }
        completed_positions = sum(
            str(item.get("detailUrl") or "") in collected_by_url for item in coins
        )
        write_json_atomic(
            checkpoint_path,
            {
                "country": report.get("country"),
                "completed": completed_positions,
                "expected": len(coins),
                "uniqueDetailUrls": len(collected_by_url),
                "items": list(collected_by_url.values()),
            },
        )
        source = "retomado" if cached is not None else "recolhido"
        if result_status == VARIANT_STATUS_PRO_REQUIRED:
            print(
                f"Variantes {position}/{len(coins)} — {label} ({issue_period}) — "
                f"ignoradas: requer subscrição uCoin PRO · {source}",
                flush=True,
            )
        else:
            print(
                f"Variantes {position}/{len(coins)} — {label} ({issue_period}) — "
                f"{len(variants)} válidas · {len(proof_only)} Proof excluídas · {source}",
                flush=True,
            )
        if (
            cached is None
            and args.variant_batch_size > 0
            and visited_this_run % args.variant_batch_size == 0
            and position < len(coins)
        ):
            print(
                f"Pausa segura de {args.variant_batch_pause:.0f}s entre lotes...",
                flush=True,
            )
            page.wait_for_timeout(max(0, int(args.variant_batch_pause * 1000)))

    summary = {
        "coinsScanned": len(coins),
        "coinsWithVariants": sum(bool(coin.get("variants")) for coin in coins),
        "variants": sum(len(coin.get("variants", [])) for coin in coins),
        "proofOnlyExcluded": sum(
            len(coin.get("excludedProofOnlyVariants", [])) for coin in coins
        ),
        "proRestricted": sum(
            coin.get("variantCollectionStatus") == VARIANT_STATUS_PRO_REQUIRED
            for coin in coins
        ),
    }
    print(
        "Recolha de variantes concluída: "
        f"{summary['coinsWithVariants']}/{summary['coinsScanned']} moedas com variantes; "
        f"{summary['variants']} variantes; "
        f"{summary['proofOnlyExcluded']} Proof excluídas; "
        f"{summary['proRestricted']} ignoradas por requererem uCoin PRO."
    )
    return summary


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


def normalize_circulation_type(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_value.casefold().split())


def classify_circulation_type(value: object) -> str:
    normalized = normalize_circulation_type(value)
    aliases = {
        "circulacao normal": "normal",
        "normal circulation": "normal",
        "comemorativa": "commemorative",
        "commemorative": "commemorative",
        "moeda de colecao": "collection",
        "collection coin": "collection",
    }
    return aliases.get(normalized, "unknown")


def filter_periods_by_coin_type(
    periods: list[object], coin_type: str
) -> tuple[list[object], dict[str, object]]:
    """Keep only the explicitly requested uCoin category.

    Unknown or missing labels are deliberately excluded from a typed scrape.
    This is a fail-closed boundary: a new uCoin label cannot silently enter the
    normal ``Coin`` catalogue.
    """

    if coin_type not in COIN_TYPE_CHOICES:
        raise ValueError(f"Tipo de moeda uCoin inválido: {coin_type}")

    counts = {key: 0 for key in (*COIN_TYPE_CHOICES[:-1], "unknown")}
    selected = 0
    total = 0
    filtered_periods: list[object] = []
    for period in periods:
        if not isinstance(period, dict):
            continue
        filtered_coins: list[dict[str, object]] = []
        for coin in period.get("coins", []):
            if not isinstance(coin, dict):
                continue
            total += 1
            category = classify_circulation_type(coin.get("circulationType"))
            counts[category] += 1
            if coin_type == "all" or category == coin_type:
                filtered_coins.append(coin)
                selected += 1
        if filtered_coins:
            filtered_period = dict(period)
            filtered_period["coins"] = filtered_coins
            filtered_periods.append(filtered_period)

    return filtered_periods, {
        "requested": coin_type,
        "requestedLabel": COIN_TYPE_LABELS.get(coin_type, "Todos"),
        "foundByType": counts,
        "found": total,
        "selected": selected,
        "excluded": total - selected,
    }


def scrape_catalog(args: argparse.Namespace, output_dir: Path) -> dict[str, object]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Playwright não está instalado. Instala com: python3 -m pip install playwright") from exc

    manual_session = (args.manual_session or args.manual_login) or (not args.no_manual_session)
    if manual_session and args.headless:
        raise RuntimeError("--headless não pode ser usado com sessão manual ativa. Usa --no-manual-session.")

    coin_type = getattr(args, "coin_type", "normal")
    page_url = apply_coin_type_to_catalog_url(
        build_country_url(args.base_url, args.country, args.country_link_name),
        coin_type,
    )
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
        incognito_process = None
        if args.incognito:
            incognito_process = launch_incognito_chromium(args)
        if args.attach_cdp or args.incognito:
            try:
                browser = playwright.chromium.connect_over_cdp(args.cdp_url)
            except Exception as exc:  # noqa: BLE001
                if incognito_process is not None and incognito_process.poll() is None:
                    incognito_process.terminate()
                raise RuntimeError(
                    "Falha ao ligar ao browser via CDP. Inicia o browser com "
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
            page = context.new_page() if (args.attach_cdp or args.incognito) else (context.pages[0] if context.pages else context.new_page())
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
            year_filtered_periods = filter_periods_by_start_year(
                crawl_result["periods"], args.start_year
            )
            report["periods"], report["coinTypeFilter"] = (
                filter_periods_by_coin_type(year_filtered_periods, coin_type)
            )
            report["warnings"] = crawl_result["warnings"]
            report["pagination"] = crawl_result["pagination"]
            report["count"] = sum(len(period.get("coins", [])) for period in report["periods"])
            if args.include_variants:
                report["variantCollection"] = collect_catalogue_variants(
                    page,
                    report,
                    args,
                    output_dir,
                )
            return report
        finally:
            if context is not None and browser is None:
                context.close()
            if incognito_process is not None and incognito_process.poll() is None:
                incognito_process.terminate()


def write_output(path: str, payload: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def remove_stale_scrape_error(output_path: Path) -> None:
    try:
        output_path.with_name("scrape-error.json").unlink()
    except FileNotFoundError:
        pass


def default_output_dir(country: str, period: str | None, continent: str = "") -> Path:
    return country_directory(country, continent)


def default_output_path(args: argparse.Namespace) -> Path:
    folder = Path(args.output_dir) if args.output_dir else default_output_dir(args.country, args.period, args.continent)
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
    coin_type_filter = report.get("coinTypeFilter")
    if isinstance(coin_type_filter, dict):
        print(f"coin_type={coin_type_filter.get('requested')}")
        print(f"excluded_by_coin_type={coin_type_filter.get('excluded', 0)}")
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
        remove_stale_scrape_error(output_path)
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
