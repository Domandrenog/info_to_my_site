#!/usr/bin/env python3
"""Collect and normalize uCoin variants for the 30 normal USA coin types.

The collector is read-only with respect to Base44.  It reuses an existing CDP
browser session, reads the official uCoin mintage tables slowly and writes a
reviewable import plan.  Business and BU issues are included; rows that exist
only in the Proof column are explicitly excluded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.ucoin_catalog import cdp_is_available
from scripts.update_eua_normal_coin_metadata import DEFAULT_PLAN as METADATA_PLAN
from scripts.update_eua_normal_coin_metadata import load_plan as load_metadata_plan


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = PROJECT_ROOT / "info/paises/america/eua/app-catalog.json"
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "info/paises/america/eua/normal-coin-variants-approved.json"
)
DEFAULT_SCAN = PROJECT_ROOT / "backups/audits/eua-normal-ucoin-variants-scan.json"
EXPECTED_PARENTS = 30
EXPECTED_VARIANTS = 1239
EXPECTED_PROOF_EXCLUSIONS = 260
PLAN_KIND = "eua_normal_coin_variants"
SCAN_KIND = "eua_normal_ucoin_variant_scan"
MARK_LABELS = {
    "D_": "D (sem FG)",
    "D_FG": "D (FG)",
    "D|ob": "D (anverso)",
    "D|rv": "D (reverso)",
    "S|ob": "S (anverso)",
    "S|rv": "S (reverso)",
}


class VariantCollectionError(RuntimeError):
    """Raised when the uCoin variant evidence is incomplete or ambiguous."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recolhe lentamente as variantes das moedas normais dos EUA no "
            "uCoin e gera um plano; não altera o Site Base44."
        )
    )
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--scan", type=Path, default=DEFAULT_SCAN)
    parser.add_argument(
        "--from-scan",
        type=Path,
        help="Gera o plano a partir de uma recolha completa já guardada.",
    )
    parser.add_argument("--attach-cdp", action="store_true")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--delay", type=float, default=5.0)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--batch-pause", type=float, default=20.0)
    return parser.parse_args(argv)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VariantCollectionError(f"JSON inválido ou ilegível: {path}") from exc


def write_json_atomic(path: Path, payload: Any, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if read_json(temporary) != payload:
            raise VariantCollectionError(f"Falhou a verificação de {path}.")
        if private:
            temporary.chmod(0o600)
        os.replace(temporary, path)
        if private:
            path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def catalog_coins(path: Path) -> list[dict[str, str]]:
    payload = read_json(path)
    periods = payload.get("periods") if isinstance(payload, dict) else None
    if not isinstance(periods, list):
        raise VariantCollectionError("O catálogo não contém periods.")
    result: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for period in periods:
        if not isinstance(period, dict):
            continue
        for coin in period.get("coins", []):
            if not isinstance(coin, dict):
                continue
            url = str(coin.get("detailUrl") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            result.append(
                {
                    "denomination": str(coin.get("denomination") or "").strip(),
                    "issuePeriod": str(coin.get("issuePeriod") or "").strip(),
                    "detailUrl": url,
                }
            )
    if len(result) != EXPECTED_PARENTS:
        raise VariantCollectionError(
            f"Esperavam-se {EXPECTED_PARENTS} tipos uCoin únicos; existem {len(result)}."
        )
    return result


def has_mintage(value: object) -> bool:
    text = str(value or "").strip()
    return text != "-" and bool(re.search(r"\d|\+", text))


def normalize_mark(value: object) -> str:
    mark = " ".join(str(value or "").split())
    return MARK_LABELS.get(mark, mark)


def mint_sort_key(mark: str) -> tuple[int, str]:
    first = mark.split(" ", 1)[0].split("-", 1)[0]
    return ({"": 0, "P": 1, "D": 2, "S": 3, "W": 4}.get(first, 50), mark)


def variant_tag(*, year: str, mark: str, single_year: bool) -> str:
    normalized_mark = normalize_mark(mark)
    if single_year:
        return normalized_mark or "Sem marca"
    return f"{year} {normalized_mark}".strip()


def finalize_scan_item(item: dict[str, Any]) -> dict[str, Any]:
    denomination = str(item.get("denomination") or "").strip()
    issue_period = str(item.get("issuePeriod") or "").strip()
    detail_url = str(item.get("detailUrl") or "").strip()
    rows = item.get("rows")
    if not denomination or not issue_period or not detail_url or not isinstance(rows, list):
        raise VariantCollectionError(f"Entrada de recolha incompleta: {detail_url!r}")
    single_year = bool(re.fullmatch(r"\d{4}", issue_period))
    included: list[tuple[int, tuple[int, str], str]] = []
    excluded: list[tuple[int, tuple[int, str], str]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise VariantCollectionError(f"Linha uCoin inválida em {detail_url}.")
        year = str(row.get("year") or "").strip()
        if not re.fullmatch(r"\d{4}", year):
            raise VariantCollectionError(f"Ano inválido em {detail_url}: {year!r}")
        mark = normalize_mark(row.get("mint"))
        tag = variant_tag(year=year, mark=mark, single_year=single_year)
        sort_key = (int(year), mint_sort_key(mark), tag)
        if has_mintage(row.get("unc_mintage")) or has_mintage(row.get("bu_mintage")):
            included.append(sort_key)
        elif has_mintage(row.get("proof_mintage")):
            excluded.append(sort_key)
    variants = [entry[2] for entry in sorted(included)]
    proof_only = [entry[2] for entry in sorted(excluded)]
    if len(variants) < 2:
        raise VariantCollectionError(
            f"{denomination} ({issue_period}) não produziu pelo menos duas variantes."
        )
    if len(variants) != len(set(variants)):
        duplicates = sorted({tag for tag in variants if variants.count(tag) > 1})
        raise VariantCollectionError(
            f"Variantes ambíguas/repetidas em {detail_url}: {duplicates[:10]}"
        )
    if set(variants) & set(proof_only):
        raise VariantCollectionError(f"Uma variante normal também aparece como Proof em {detail_url}.")
    return {
        "denomination": denomination,
        "issuePeriod": issue_period,
        "detailUrl": detail_url,
        "h1": str(item.get("h1") or "").strip(),
        "variants": variants,
        "excluded_proof_only": proof_only,
    }


def validate_scan(scan: Any, expected_coins: list[dict[str, str]]) -> list[dict[str, Any]]:
    if not isinstance(scan, dict) or scan.get("kind") != SCAN_KIND:
        raise VariantCollectionError("O ficheiro não é uma recolha de variantes dos EUA.")
    items = scan.get("items")
    if not isinstance(items, list) or len(items) != EXPECTED_PARENTS:
        raise VariantCollectionError("A recolha não contém exatamente 30 moedas.")
    expected_urls = [coin["detailUrl"] for coin in expected_coins]
    by_url: dict[str, dict[str, Any]] = {}
    for raw in items:
        if not isinstance(raw, dict):
            raise VariantCollectionError("A recolha contém uma entrada inválida.")
        item = finalize_scan_item(raw)
        url = item["detailUrl"]
        if url in by_url:
            raise VariantCollectionError(f"URL repetido na recolha: {url}")
        by_url[url] = item
    if set(by_url) != set(expected_urls):
        raise VariantCollectionError("Os URLs da recolha não correspondem ao catálogo atual.")
    return [by_url[url] for url in expected_urls]


def build_plan(
    *,
    scan: Any,
    expected_coins: list[dict[str, str]],
    scan_path: Path,
) -> dict[str, Any]:
    collected = validate_scan(scan, expected_coins)
    metadata = load_metadata_plan(METADATA_PLAN)
    metadata_by_url = {item["url_ucoin"]: item for item in metadata["updates"]}
    if set(metadata_by_url) != {item["detailUrl"] for item in collected}:
        raise VariantCollectionError("O plano de metadata e a recolha têm URLs diferentes.")

    items: list[dict[str, Any]] = []
    for source in collected:
        metadata_item = metadata_by_url[source["detailUrl"]]
        target = metadata_item["set"]
        items.append(
            {
                "coin_id": metadata_item["id"],
                "url_ucoin": source["detailUrl"],
                "name": target["name"],
                "years": target["years"],
                "ucoin_heading": source["h1"],
                "variants": source["variants"],
                "excluded_proof_only": source["excluded_proof_only"],
            }
        )

    variant_count = sum(len(item["variants"]) for item in items)
    proof_count = sum(len(item["excluded_proof_only"]) for item in items)
    if variant_count != EXPECTED_VARIANTS or proof_count != EXPECTED_PROOF_EXCLUSIONS:
        raise VariantCollectionError(
            "A contagem final divergiu do diagnóstico aprovado: "
            f"variantes={variant_count}, Proof={proof_count}."
        )
    scan_bytes = json.dumps(scan, ensure_ascii=False, sort_keys=True).encode("utf-8")
    try:
        scan_label = str(scan_path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        scan_label = "private source scan (not committed)"
    return {
        "format_version": 1,
        "kind": PLAN_KIND,
        "status": "approved_by_user",
        "approved_at": "2026-10-12",
        "country": "EUA",
        "purpose": "Criar as variantes não exclusivamente Proof que faltam nas 30 moedas normais dos EUA.",
        "source": {
            "catalog": str(DEFAULT_CATALOG.relative_to(PROJECT_ROOT)),
            "scan": scan_label,
            "scan_sha256": hashlib.sha256(scan_bytes).hexdigest(),
            "collected_at": scan.get("updated_at") or scan.get("generated_at") or "2026-10-12",
        },
        "policy": {
            "include": "Linhas uCoin com cunhagem UNC ou BU; inclui marca S quando não é exclusivamente Proof.",
            "exclude": "Linhas cuja emissão existe exclusivamente na coluna Proof.",
            "new_variant_condition": "Não Tenho",
            "existing_variants": "Preservar condição, ordem e todos os restantes campos.",
        },
        "safety": {
            "coin_updates_allowed": ["has_variants"],
            "coin_variant_creates_maximum": variant_count,
            "coin_variant_updates": 0,
            "coin_variant_deletes": 0,
            "coin_creates": 0,
            "coin_deletes": 0,
        },
        "summary": {
            "parents": len(items),
            "variants": variant_count,
            "proof_only_excluded": proof_count,
        },
        "items": items,
    }


def extract_page(page: Any, coin: dict[str, str]) -> dict[str, Any]:
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

    selected: tuple[int, Any] | None = None
    mode = ""
    for table_index, table in enumerate(page.locator("table").all()):
        if not table.locator("thead").count():
            continue
        headers = " ".join(table.locator("thead").inner_text().split())
        if not all(label in headers for label in ("UNC", "BU", "Proof")):
            continue
        if "Ano" in headers:
            selected = (table_index, table)
            mode = "multi"
            break
        if "Casa Moeda" in headers:
            selected = (table_index, table)
            mode = "single"
            break
    if selected is None:
        raise VariantCollectionError(f"Tabela de cunhagem não encontrada em {coin['detailUrl']}.")

    table_index, table = selected
    issue_period = coin["issuePeriod"]
    if mode == "single" and not re.fullmatch(r"\d{4}", issue_period):
        raise VariantCollectionError(f"Tabela de ano único inesperada em {coin['detailUrl']}.")
    rows: list[dict[str, Any]] = []
    for row in table.locator("tbody tr").all():
        cells = [" ".join(cell.inner_text().split()) for cell in row.locator("th,td").all()]
        if mode == "multi":
            if len(cells) < 5 or not re.fullmatch(r"\d{4}", cells[0]):
                continue
            year, generic_mark, unc, bu, proof = cells[:5]
            row_id = row.get_attribute("id") or ""
            display = display_by_row_id.get(row_id, {})
            mark = display.get("mark", generic_mark)
            description = display.get("description", "")
        else:
            if len(cells) < 5:
                continue
            year = issue_period
            generic_mark = cells[1]
            mark = generic_mark
            unc, bu, proof = cells[2:5]
            row_id = ""
            description = cells[0]
        rows.append(
            {
                "year": year,
                "mint": mark,
                "ucoin_mint": generic_mark,
                "unc_mintage": unc,
                "bu_mintage": bu,
                "proof_mintage": proof,
                "ucoin_row_id": row_id,
                "description": description,
            }
        )
    if not rows:
        raise VariantCollectionError(f"A tabela de cunhagem está vazia em {coin['detailUrl']}.")
    raw = {
        **coin,
        "h1": " ".join(page.locator("h1").first.inner_text().split()),
        "table_index": table_index,
        "table_mode": mode,
        "rows": rows,
    }
    finalized = finalize_scan_item(raw)
    return {**raw, **finalized}


def collect_scan(args: argparse.Namespace, expected_coins: list[dict[str, str]]) -> dict[str, Any]:
    if not args.attach_cdp:
        raise VariantCollectionError("A recolha exige --attach-cdp para reutilizar o browser aberto.")
    if not cdp_is_available(args.cdp_url):
        raise VariantCollectionError(f"Browser CDP indisponível em {args.cdp_url}.")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        raise VariantCollectionError("Playwright não está instalado.") from exc

    existing: dict[str, dict[str, Any]] = {}
    if args.scan.exists():
        cached = read_json(args.scan)
        cached_items = cached.get("items") if isinstance(cached, dict) else None
        if isinstance(cached_items, list):
            for item in cached_items:
                if isinstance(item, dict) and item.get("detailUrl"):
                    existing[str(item["detailUrl"])] = item

    playwright = sync_playwright().start()
    browser = playwright.chromium.connect_over_cdp(args.cdp_url)
    pages = [page for context in browser.contexts for page in context.pages]
    page = next((candidate for candidate in pages if "ucoin.net/coin/" in candidate.url), None)
    if page is None:
        page = browser.contexts[0].new_page()
    collected_this_run = 0
    try:
        for position, coin in enumerate(expected_coins, start=1):
            if coin["detailUrl"] in existing:
                continue
            started = time.monotonic()
            if page.url != coin["detailUrl"]:
                if collected_this_run:
                    page.wait_for_timeout(max(0, int(args.delay * 1000)))
                response = page.goto(
                    coin["detailUrl"],
                    wait_until="domcontentloaded",
                    timeout=max(1, args.timeout) * 1000,
                )
                status = response.status if response is not None else None
                if status != 200:
                    raise VariantCollectionError(
                        f"uCoin devolveu HTTP {status} em {coin['detailUrl']}."
                    )
            page.wait_for_timeout(1000)
            item = extract_page(page, coin)
            existing[coin["detailUrl"]] = item
            collected_this_run += 1
            ordered = [existing[c["detailUrl"]] for c in expected_coins if c["detailUrl"] in existing]
            scan = {
                "kind": SCAN_KIND,
                "expected_coins": len(expected_coins),
                "completed": len(ordered),
                "updated_at": utc_now(),
                "items": ordered,
            }
            write_json_atomic(args.scan, scan, private=True)
            print(
                f"{position}/{len(expected_coins)} — {coin['denomination']} "
                f"({coin['issuePeriod']}) — variantes: {len(item['variants'])} · "
                f"Proof excluídas: {len(item['excluded_proof_only'])} · "
                f"{time.monotonic() - started:.1f}s",
                flush=True,
            )
            if (
                collected_this_run
                and args.batch_size > 0
                and collected_this_run % args.batch_size == 0
                and len(existing) < len(expected_coins)
            ):
                print(f"Pausa segura de {args.batch_pause:.0f}s entre lotes...", flush=True)
                page.wait_for_timeout(max(0, int(args.batch_pause * 1000)))
    finally:
        # Disconnect from CDP without closing the user's browser session.
        playwright.stop()
    scan = read_json(args.scan)
    validate_scan(scan, expected_coins)
    return scan


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    expected_coins = catalog_coins(args.catalog)
    if args.from_scan:
        scan_path = args.from_scan
        scan = read_json(scan_path)
    else:
        scan_path = args.scan
        scan = collect_scan(args, expected_coins)
    plan = build_plan(scan=scan, expected_coins=expected_coins, scan_path=scan_path)
    write_json_atomic(args.output, plan)
    summary = plan["summary"]
    print(f"Moedas normais: {summary['parents']}")
    print(f"Variantes não exclusivamente Proof: {summary['variants']}")
    print(f"Linhas exclusivamente Proof excluídas: {summary['proof_only_excluded']}")
    print(f"Plano aprovado: {args.output}")
    print("Site Base44: nenhuma alteração efetuada.")
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except VariantCollectionError as exc:
        print(f"Erro: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
