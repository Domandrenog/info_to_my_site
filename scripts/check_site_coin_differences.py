#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from scripts.catalog_paths import CATALOG_ROOT, find_country_directory, iter_country_directories, slugify


def normalize_url(value: str) -> tuple[str, bool]:
    raw = str(value or "").strip()
    markdown_match = re.fullmatch(r"\[[^\]]+\]\((https?://[^)]+)\)", raw, flags=re.IGNORECASE)
    if markdown_match:
        return markdown_match.group(1).strip(), True
    return raw, False


def parse_links_file(path: Path) -> dict[str, dict[str, str]]:
    entries: dict[str, dict[str, str]] = {}
    current_slug = ""
    if not path.exists():
        return entries

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        slug_match = re.match(r"^([^\s:][^:]*):$", line)
        if slug_match:
            current_slug = slug_match.group(1).strip()
            entries.setdefault(current_slug, {})
            continue
        link_match = re.match(r"^\s+(frente|tras):\s+(https?://\S+)\s*$", line)
        if current_slug and link_match:
            entries[current_slug][link_match.group(1)] = link_match.group(2)
    return entries


def url_stem(url: str) -> str:
    path = unquote(urlparse(url).path)
    return slugify(Path(path).stem)


def detail_stem(detail_url: str) -> str:
    path = unquote(urlparse(detail_url).path).strip("/")
    if path.startswith("coin/"):
        path = path[len("coin/") :]
    return slugify(path)


def strip_trailing_years(slug: str) -> str:
    parts = [part for part in slug.split("-") if part]
    while parts and re.fullmatch(r"\d{4}", parts[-1]):
        parts.pop()
    return "-".join(parts)


def slug_compatible(detail_slug: str, image_slug: str) -> bool:
    detail_base = strip_trailing_years(detail_slug)
    image_base = strip_trailing_years(image_slug)
    if not detail_base or not image_base:
        return True
    return detail_base == image_base or detail_base in image_base or image_base in detail_base


def record_matches_image_map(
    record: dict[str, object],
    expected_image_urls: set[str],
    reverse: dict[str, dict[str, str]],
) -> bool:
    mapped_ucoin_urls: set[str] = set()
    for api_field in ("image_frente", "image_verso"):
        api_image = normalize_url(str(record.get(api_field) or ""))[0]
        mapped = reverse.get(api_image)
        mapped_ucoin = normalize_url(str(mapped.get("ucoin_url") or ""))[0] if mapped else ""
        if mapped_ucoin:
            mapped_ucoin_urls.add(mapped_ucoin)
    return bool(expected_image_urls and mapped_ucoin_urls == expected_image_urls)


def expected_country_folder(country_slug: str) -> str:
    mapping = {
        "bielorrussia": "Bielorrussia",
        "coreia-do-sul": "CoreiaDoSul",
        "emirados-arabes-unidos": "EmiradosArabesUnidos",
        "eua": "EUA",
        "hong-kong": "HongKong",
        "japao": "Japao",
        "sri-lanka": "SriLanka",
        "tailandia": "Tailandia",
    }
    if country_slug in mapping:
        return mapping[country_slug]
    return "".join(part.capitalize() for part in country_slug.split("-") if part)


def build_reverse_map(country_folder: Path) -> dict[str, dict[str, str]]:
    links_raw = parse_links_file(country_folder / "links.txt")
    links_ucoin = parse_links_file(country_folder / "links-ucoin.txt")

    reverse: dict[str, dict[str, str]] = {}
    for coin_slug, raw_sides in links_raw.items():
        ucoin_sides = links_ucoin.get(coin_slug, {})
        for side in ("frente", "tras"):
            raw_url = raw_sides.get(side, "")
            if not raw_url:
                continue
            reverse[raw_url] = {
                "slug": coin_slug,
                "side": side,
                "ucoin_url": ucoin_sides.get(side, ""),
            }
    return reverse


def type_counts(findings: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        key = finding.get("type", "unknown")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def compact_findings(findings: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "field": str(finding.get("field", "")),
            "value": str(finding.get("value", "")),
            "missing_value": str(finding.get("missing_value", "")),
        }
        for finding in findings
    ]


def summarize_issue_labels(issues: list[dict[str, str]]) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for issue in issues:
        field = str(issue.get("field", ""))
        missing_value = str(issue.get("missing_value", ""))
        label = "Not found" if field == "api" and missing_value == "Not found" else field
        if label and label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def make_finding(finding_type: str, field: str, value: str = "", missing_value: str = "") -> dict[str, str]:
    return {
        "type": finding_type,
        "field": field,
        "value": value,
        "missing_value": missing_value,
    }


def iter_coins(catalogue: dict[str, object]):
    for period in catalogue.get("periods", []):
        if not isinstance(period, dict):
            continue
        for coin in period.get("coins", []):
            if isinstance(coin, dict):
                yield period, coin


def normalize_key(value: str) -> str:
    return str(value or "").strip().lower()


def expected_notes_from_period(period: dict[str, object]) -> str:
    ruler = period.get("ruler")
    if isinstance(ruler, str) and ruler.strip():
        return ruler.strip()
    title = period.get("title") or period.get("fullTitle")
    if not isinstance(title, str) or not title.strip():
        return ""
    parts = [part.strip() for part in title.split("›") if part.strip()]
    return parts[1] if len(parts) >= 2 else title.strip()


def build_site_url(record_id: str) -> str:
    app_id = os.environ.get("BASE44_APP_ID", "").strip()
    server = os.environ.get("BASE44_SERVER_URL", "https://base44.app").rstrip("/")
    if not app_id or not record_id:
        return "Not found"
    return f"{server}/api/apps/{quote(app_id)}/entities/Coin/{quote(record_id)}"


def api_records_for_country(country_name: str) -> tuple[list[dict[str, object]] | None, str | None]:
    try:
        from scripts import import_base44_coins

        args = import_base44_coins.parse_args.__globals__.get("argparse")
        ns = args.Namespace(request_delay=0.0, rate_limit_delay=0.0, max_retries=0)
        client = import_base44_coins.create_client(ns)
        records = import_base44_coins.existing_records_for_country(client, country_name)
        return records, None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def association_record_id(entry: dict[str, object]) -> str:
    record_id = str(entry.get("record_id") or "")
    if record_id:
        return record_id
    api_url = str(entry.get("apiUrl") or entry.get("siteUrl") or "")
    parts = [part for part in urlparse(api_url).path.split("/") if part]
    return parts[-1] if parts else ""


def load_confirmed_equivalences(country_dir: Path) -> dict[str, str]:
    # Maps normalized ucoin URL -> confirmed API record_id.
    out: dict[str, str] = {}

    missing_found_path = country_dir / f"{country_dir.name}-missing-found.json"
    if missing_found_path.exists():
        try:
            data = json.loads(missing_found_path.read_text(encoding="utf-8"))
            entries = data.get("missing", []) if isinstance(data, dict) else []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("status") or "") != "connected":
                    continue
                ucoin_url = normalize_url(str(entry.get("ucoinUrl") or ""))[0]
                record_id = association_record_id(entry)
                if ucoin_url and record_id:
                    out[ucoin_url] = record_id
        except Exception:  # noqa: BLE001
            pass


    return out


def compare_country(
    paises_dir: Path,
    all_coins_dir: Path,
    country_slug: str,
    catalog_filename: str,
    include_markdown_as_issue: bool,
    check_api: bool,
    include_warnings: bool,
) -> dict[str, object]:
    try:
        country_dir = find_country_directory(paises_dir, country_slug)
    except ValueError as exc:
        result = {
            "country": country_slug,
            "summary": {"coins": []},
            "coins_with_issues": [],
            "error": str(exc),
        }
        if include_warnings:
            result["coins_with_warnings"] = []
        return result

    country_catalog = country_dir / catalog_filename
    if not country_catalog.exists():
        result = {
            "country": country_slug,
            "summary": {"coins": []},
            "coins_with_issues": [],
            "error": f"Catalog not found: {country_catalog}",
        }
        if include_warnings:
            result["coins_with_warnings"] = []
        return result

    country_folder = all_coins_dir / expected_country_folder(country_slug)
    reverse = build_reverse_map(country_folder)
    confirmed_equivalences = load_confirmed_equivalences(country_dir)

    data = json.loads(country_catalog.read_text(encoding="utf-8"))
    country_name = str(data.get("country") or country_slug)

    api_records: list[dict[str, object]] | None = None
    by_record_id: dict[str, dict[str, object]] = {}

    if check_api:
        api_records, _ = api_records_for_country(country_name)
        if api_records is not None:
            for record in api_records:
                if not isinstance(record, dict):
                    continue
                record_id = str(record.get("id") or "")
                if record_id:
                    by_record_id[record_id] = record

    all_issues: list[dict[str, str]] = []
    all_warnings: list[dict[str, str]] = []
    summary_coins: list[dict[str, object]] = []
    coins_with_issues: list[dict[str, object]] = []
    coins_with_warnings: list[dict[str, object]] = []
    image_matched_ucoin_urls: list[str] = []

    for period, coin in iter_coins(data):
        ucoin_url = str(coin.get("detailUrl") or "")
        detail_norm = normalize_url(ucoin_url)[0]
        expected_detail_slug = detail_stem(ucoin_url)
        expected_image_urls: set[str] = set()
        site_url = "Not found"
        coin_issues: list[dict[str, str]] = []
        coin_warnings: list[dict[str, str]] = []

        for side_label, field in (("frente", "obverseImage"), ("tras", "reverseImage")):
            raw_value = str(coin.get(field) or "")
            normalized, was_markdown = normalize_url(raw_value)
            if normalized:
                expected_image_urls.add(normalized)

            if was_markdown:
                markdown_warning = make_finding("markdown_url", field, value=raw_value, missing_value="normalized_url")
                if include_warnings:
                    coin_warnings.append(markdown_warning)
                if include_markdown_as_issue:
                    coin_issues.append(markdown_warning)

            if not normalized:
                coin_issues.append(make_finding("missing_image_url", field, value="", missing_value="image_url"))
                continue

            mapped_ucoin = ""
            mapped_side = ""

            host = (urlparse(normalized).netloc or "").lower()
            if "i.ucoin.net" in host:
                mapped_ucoin = normalized
            elif normalized in reverse:
                mapped = reverse[normalized]
                mapped_ucoin = mapped.get("ucoin_url", "")
                mapped_side = mapped.get("side", "")
                if mapped_side and mapped_side != side_label:
                    coin_issues.append(make_finding("side_mismatch", field, value=mapped_side, missing_value=side_label))
            else:
                coin_issues.append(make_finding("url_not_in_all_coins_map", field, value=normalized, missing_value="all_coins_mapping"))

            if mapped_ucoin and expected_detail_slug:
                image_slug = url_stem(mapped_ucoin)
                if not slug_compatible(expected_detail_slug, image_slug):
                    coin_issues.append(make_finding("detail_image_slug_mismatch", field, value=image_slug, missing_value=expected_detail_slug))

        if check_api and api_records is not None:
            matches = [
                record
                for record in api_records
                if isinstance(record, dict) and record_matches_image_map(record, expected_image_urls, reverse)
            ]
            confirmed_record_id = confirmed_equivalences.get(detail_norm, "") if detail_norm else ""

            if matches:
                if detail_norm:
                    image_matched_ucoin_urls.append(detail_norm)
            elif confirmed_record_id:
                confirmed_record = by_record_id.get(confirmed_record_id)
                matches = [confirmed_record] if confirmed_record is not None else []

            if not matches:
                name_year_candidates = [
                    record
                    for record in api_records
                    if isinstance(record, dict)
                    and normalize_key(str(record.get("name") or ""))
                    == normalize_key(str(coin.get("denomination") or ""))
                    and normalize_key(str(record.get("years") or ""))
                    == normalize_key(str(coin.get("issuePeriod") or ""))
                ]
                if len(name_year_candidates) == 1:
                    site_url = build_site_url(str(name_year_candidates[0].get("id") or ""))
                coin_issues.append(make_finding("missing_api_coin_record", "api", value="", missing_value="Not found"))
            else:
                if len(matches) > 1 and include_warnings:
                    coin_warnings.append(make_finding("multiple_api_matches", "api", value=str(len(matches)), missing_value="single_api_match"))
                record = matches[0]
                site_url = build_site_url(str(record.get("id") or ""))
                notes = str(record.get("notes") or "").strip()
                url_ucoin = normalize_url(str(record.get("url_ucoin") or ""))[0]

                if not notes:
                    expected_notes = expected_notes_from_period(period)
                    coin_issues.append(make_finding("missing_notes", "notes", value="", missing_value=expected_notes or "notes"))
                if not url_ucoin:
                    coin_issues.append(make_finding("missing_url_ucoin", "url_ucoin", value="", missing_value=detail_norm or "url_ucoin"))
                elif detail_norm and url_ucoin != detail_norm:
                    coin_issues.append(make_finding("mismatched_url_ucoin", "url_ucoin", value=url_ucoin, missing_value=detail_norm))

        if coin_issues:
            compact_issues = compact_findings(coin_issues)
            labels = summarize_issue_labels(compact_issues)
            all_issues.extend(coin_issues)
            summary_coins.append(
                {
                    "coin": str(coin.get("denomination") or ""),
                    "issue_count": len(compact_issues),
                    "types": labels,
                }
            )
            coins_with_issues.append(
                {
                    "denomination": str(coin.get("denomination") or ""),
                    "issuePeriod": str(coin.get("issuePeriod") or ""),
                    "ucoinUrl": ucoin_url,
                    "siteUrl": site_url,
                    "issue_count": len(compact_issues),
                    "issues": compact_issues,
                }
            )

        if include_warnings and coin_warnings:
            all_warnings.extend(coin_warnings)
            coins_with_warnings.append(
                {
                    "denomination": str(coin.get("denomination") or ""),
                    "issuePeriod": str(coin.get("issuePeriod") or ""),
                    "ucoinUrl": ucoin_url,
                    "siteUrl": site_url,
                    "warning_count": len(coin_warnings),
                    "warnings": compact_findings(coin_warnings),
                }
            )

    summary: dict[str, object] = {
        "coins": summary_coins,
        "total_coins": len(summary_coins),
        "total_issues": len(all_issues),
        "by_type": type_counts(all_issues),
    }
    if include_warnings:
        summary["total_warnings"] = len(all_warnings)
        summary["warnings_by_type"] = type_counts(all_warnings)

    result: dict[str, object] = {
        "country": country_slug,
        "summary": summary,
        "coins_with_issues": coins_with_issues,
        "image_matched_ucoin_urls": image_matched_ucoin_urls,
    }
    if include_warnings:
        result["coins_with_warnings"] = coins_with_warnings
    return result


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Compara moedas do site com mapeamento All_Coins e API para encontrar divergencias reais.",
    )
    parser.add_argument(
        "--country",
        default="",
        help="Slug do pais em info/paises/<continente>/, ex.: bielorrussia. Sem isto processa todos.",
    )
    parser.add_argument("--paises-dir", default=str(project_root / CATALOG_ROOT), help="Pasta com os catalogos do site.")
    parser.add_argument("--all-coins-dir", default=str(project_root.parent / "All_Coins"), help="Pasta raiz do repo All_Coins.")
    parser.add_argument("--catalog-file", default="app-catalog.json", help="Nome do ficheiro de catalogo por pais.")
    parser.add_argument("--max-issues", type=int, default=50, help="Limite de moedas com issues mostradas por pais em modo texto.")
    parser.add_argument("--json", action="store_true", help="Imprime o relatorio completo em JSON.")
    parser.add_argument("--output", default="", help="Caminho para guardar o relatorio em JSON.")
    parser.add_argument("--include-markdown-as-issue", action="store_true", help="Conta markdown_url como diferenca real.")
    parser.add_argument("--include-warnings", action="store_true", help="Inclui warnings no relatorio (desligado por defeito).")
    parser.add_argument("--no-check-api", action="store_true", help="Nao verifica notes/url_ucoin na API do site.")
    return parser.parse_args()


def print_text_report(report: dict[str, object], max_issues: int, include_warnings: bool) -> None:
    summary = report.get("summary", {})
    coins_with_issues = report.get("coins_with_issues", [])
    country = report.get("country", "")

    if report.get("error"):
        print(f"\n[{country}] ERROR: {report['error']}")
        return

    print(f"\n[{country}]")
    for coin_summary in summary.get("coins", []):
        if not isinstance(coin_summary, dict):
            continue
        print(
            f"- {coin_summary.get('coin', '')}: "
            f"{coin_summary.get('issue_count', 0)} issues -> "
            f"{', '.join(coin_summary.get('types', []))}"
        )

    shown = 0
    for coin in coins_with_issues:
        if shown >= max_issues:
            break
        shown += 1
        print(f"  moeda={coin.get('denomination')} | ucoinUrl={coin.get('ucoinUrl')} | siteUrl={coin.get('siteUrl')}")
        for issue in coin.get("issues", []):
            print(
                "    * "
                f"field={issue.get('field', '')} "
                f"value={issue.get('value', '')} "
                f"missing_value={issue.get('missing_value', '')}"
            )

    if include_warnings and report.get("coins_with_warnings"):
        print(f"warnings_em_moedas={len(report.get('coins_with_warnings', []))}")


def main() -> int:
    args = parse_args()
    paises_dir = Path(args.paises_dir)
    all_coins_dir = Path(args.all_coins_dir)

    if args.country:
        countries = [slugify(args.country)]
    else:
        countries = sorted(path.name for path in iter_country_directories(paises_dir))

    reports = [
        compare_country(
            paises_dir,
            all_coins_dir,
            country,
            args.catalog_file,
            args.include_markdown_as_issue,
            not args.no_check_api,
            args.include_warnings,
        )
        for country in countries
    ]

    total_issues = 0
    has_errors = False
    for report in reports:
        if report.get("error"):
            has_errors = True
        summary = report.get("summary", {})
        if isinstance(summary, dict):
            total_issues += int(summary.get("total_issues", 0))

    if args.output:
        output_path = Path(args.output)
        if total_issues > 0 and not has_errors:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(reports, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"Saved {output_path}")
        elif output_path.exists():
            output_path.unlink()
            print(f"No differences found. Removed stale file: {output_path}")
        else:
            print("No differences found. Output file was not created.")

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for report in reports:
            print_text_report(report, args.max_issues, args.include_warnings)

    return 1 if total_issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
