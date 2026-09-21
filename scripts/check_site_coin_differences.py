#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from scripts.catalog_paths import CATALOG_ROOT, find_country_directory, iter_country_directories, slugify


ISSUE_TYPE_LABELS = {
    "detail_image_slug_mismatch": "Wrong photo",
    "markdown_url": "Markdown URL",
    "mismatched_url_ucoin": "Wrong uCoin URL",
    "missing_api_coin_record": "Not found",
    "missing_image_url": "No photo",
    "missing_notes": "Missing notes",
    "missing_url_ucoin": "Missing uCoin URL",
    "multiple_api_matches": "Multiple API matches",
    "side_mismatch": "Wrong photo side",
    "url_not_in_all_coins_map": "Photo not mapped",
}

TEXT_REPORT_ISSUE_LABELS = {
    "detail_image_slug_mismatch": "Fotografia incorreta",
    "markdown_url": "URL em formato Markdown",
    "mismatched_url_ucoin": "URL do uCoin incorreto",
    "missing_notes": "Sem notes",
    "missing_url_ucoin": "Sem URL do uCoin",
    "multiple_api_matches": "Várias associações possíveis no Site Base44",
    "side_mismatch": "Lado da fotografia incorreto",
    "url_not_in_all_coins_map": "Fotografia sem mapeamento",
}


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
    parsed = urlparse(detail_url)
    path = unquote(parsed.path).strip("/")
    if path == "login":
        referenced_urls = parse_qs(parsed.query).get("ref", [])
        if referenced_urls:
            path = unquote(urlparse(referenced_urls[0]).path).strip("/")
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
        "croatia": "Croacia",
        "egipto": "Egito",
        "emirados-arabes-unidos": "EmiradosArabesUnidos",
        "eua": "EUA",
        "hong-kong": "HongKong",
        "japao": "Japao",
        "mauricia": "Mauricias",
        "sri-lanka": "SriLanka",
        "tailandia": "Tailandia",
    }
    if country_slug in mapping:
        return mapping[country_slug]
    return "".join(part.capitalize() for part in country_slug.split("-") if part)


def api_country_name(country_slug: str, catalog_country_name: str) -> str:
    aliases = {
        "eua": "EUA",
        "mauricia": "Maurícia",
        "seychelles": "Seychelles",
    }
    return aliases.get(country_slug, catalog_country_name)


def find_all_coins_country_folder(all_coins_dir: Path, country_slug: str) -> Path:
    country_folder_name = expected_country_folder(country_slug)
    photos_root = all_coins_dir / "fotos" / "paises"
    matches = sorted(
        path
        for path in photos_root.glob(f"*/{country_folder_name}/normal")
        if path.is_dir()
    )
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"Pasta All_Coins duplicada para {country_slug}: "
            f"{', '.join(str(path) for path in matches)}"
        )

    legacy_folder = all_coins_dir / country_folder_name
    if legacy_folder.is_dir():
        return legacy_folder
    raise FileNotFoundError(
        f"Pasta All_Coins não encontrada para {country_slug}: "
        f"{photos_root}/<continente>/{country_folder_name}/normal"
    )


def build_reverse_map(country_folder: Path) -> dict[str, dict[str, str]]:
    internal_path = country_folder / "links-internos.txt"
    external_path = country_folder / "links-externos.txt"
    if not internal_path.exists() and not external_path.exists():
        internal_path = country_folder / "links.txt"
        external_path = country_folder / "links-ucoin.txt"
    if not internal_path.is_file() or not external_path.is_file():
        raise FileNotFoundError(
            f"Mapas de imagens não encontrados em {country_folder}: "
            "links-internos.txt e links-externos.txt"
        )

    links_raw = parse_links_file(internal_path)
    links_ucoin = parse_links_file(external_path)

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


def issue_type_label(issue_type: str) -> str:
    return ISSUE_TYPE_LABELS.get(issue_type, issue_type.replace("_", " ").capitalize())


def count_label(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def compact_findings(findings: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "type": str(finding.get("type", "")),
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
    normalized = str(value or "").strip().casefold()
    normalized = re.sub(r"\s*-\s*", "-", normalized)
    return re.sub(r"\s+", " ", normalized)


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


def api_records_for_all_countries() -> tuple[list[dict[str, object]] | None, str | None]:
    try:
        from scripts import import_base44_coins

        args = import_base44_coins.parse_args.__globals__.get("argparse")
        ns = args.Namespace(request_delay=0.0, rate_limit_delay=0.0, max_retries=0)
        client = import_base44_coins.create_client(ns)
        records: list[dict[str, object]] = []
        page_size = 1000
        skip = 0
        while True:
            result = client.filter({}, limit=page_size, skip=skip)
            if not isinstance(result, list):
                raise RuntimeError(f"Unexpected Base44 list response: {result!r}")
            records.extend(record for record in result if isinstance(record, dict))
            if len(result) < page_size:
                return records, None
            skip += page_size
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def group_api_records_by_country(records: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for record in records:
        country = str(record.get("country") or "").strip()
        country_key = slugify(country)
        if country_key:
            grouped.setdefault(country_key, []).append(record)
    return grouped


def tracked_api_country_keys(paises_dir: Path, country_slugs: list[str], catalog_filename: str) -> set[str]:
    tracked: set[str] = set()
    for country_slug in country_slugs:
        try:
            country_dir = find_country_directory(paises_dir, country_slug)
        except ValueError:
            continue
        country_catalog = country_dir / catalog_filename
        if not country_catalog.is_file():
            continue
        data = json.loads(country_catalog.read_text(encoding="utf-8"))
        country_name = str(data.get("country") or country_slug)
        tracked.add(slugify(api_country_name(country_slug, country_name)))
    return tracked


def untracked_api_countries(
    grouped_api_records: dict[str, list[dict[str, object]]],
    tracked_country_keys: set[str],
) -> list[dict[str, object]]:
    countries: list[dict[str, object]] = []
    for country_key, records in grouped_api_records.items():
        if country_key in tracked_country_keys:
            continue
        country_names = sorted(
            {
                str(record.get("country") or "").strip()
                for record in records
                if str(record.get("country") or "").strip()
            },
            key=slugify,
        )
        countries.append(
            {
                "country": country_names[0] if country_names else country_key,
                "coin_count": len(records),
            }
        )
    return sorted(countries, key=lambda item: slugify(str(item["country"])))


def pending_catalogue_api_countries(
    grouped_api_records: dict[str, list[dict[str, object]]],
    paises_dir: Path,
    tracked_country_keys: set[str],
) -> tuple[list[dict[str, object]], set[str]]:
    countries: list[dict[str, object]] = []
    pending_country_keys: set[str] = set()
    for country_dir in iter_country_directories(paises_dir):
        pending_path = country_dir / "app-catalog-pending.json"
        if not pending_path.is_file() or (country_dir / "app-catalog.json").is_file():
            continue
        try:
            catalogue = json.loads(pending_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        country_name = str(catalogue.get("country") or country_dir.name)
        country_key = slugify(api_country_name(country_dir.name, country_name))
        if country_key in tracked_country_keys or country_key not in grouped_api_records:
            continue

        total_types = 0
        missing_rarity = 0
        for period in catalogue.get("periods", []):
            if not isinstance(period, dict):
                continue
            for coin in period.get("coins", []):
                if not isinstance(coin, dict):
                    continue
                total_types += 1
                if coin.get("availability") == "still needed to calculate":
                    missing_rarity += 1

        records = grouped_api_records[country_key]
        api_names = sorted(
            {
                str(record.get("country") or "").strip()
                for record in records
                if str(record.get("country") or "").strip()
            },
            key=slugify,
        )
        pending_country_keys.add(country_key)
        countries.append(
            {
                "country": api_names[0] if api_names else country_name,
                "coin_count": len(records),
                "catalog_coin_type_count": total_types,
                "missing_rarity_count": missing_rarity,
            }
        )
    return sorted(countries, key=lambda item: slugify(str(item["country"]))), pending_country_keys


def api_tracking_report(countries: list[dict[str, object]]) -> dict[str, object]:
    return {
        "report_type": "api_country_tracking",
        "country": "api-country-tracking",
        "country_name": "Países no Site Base44 sem tracking local",
        "summary": {
            "total_countries": len(countries),
            "total_issues": len(countries),
            "by_type": {"untracked_api_country": len(countries)} if countries else {},
        },
        "countries": countries,
        "coins_with_issues": [],
    }


def pending_rarity_report(countries: list[dict[str, object]]) -> dict[str, object]:
    return {
        "report_type": "api_country_pending_rarity",
        "country": "api-country-pending-rarity",
        "country_name": "Países recolhidos ainda sem raridade",
        "summary": {
            "total_countries": len(countries),
            "total_issues": len(countries),
            "by_type": {"pending_rarity_country": len(countries)} if countries else {},
        },
        "countries": countries,
        "coins_with_issues": [],
    }


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
    api_records_by_country: dict[str, list[dict[str, object]]] | None = None,
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

    data = json.loads(country_catalog.read_text(encoding="utf-8"))
    country_name = str(data.get("country") or country_slug)

    try:
        country_folder = find_all_coins_country_folder(all_coins_dir, country_slug)
        reverse = build_reverse_map(country_folder)
    except (FileNotFoundError, ValueError) as exc:
        result = {
            "country": country_slug,
            "country_name": country_name,
            "summary": {"coins": [], "total_coins": 0, "total_issues": 0, "by_type": {}},
            "coins_with_issues": [],
            "error": str(exc),
        }
        if include_warnings:
            result["coins_with_warnings"] = []
        return result
    confirmed_equivalences = load_confirmed_equivalences(country_dir)

    api_records: list[dict[str, object]] | None = None
    by_record_id: dict[str, dict[str, object]] = {}

    if check_api:
        api_country = api_country_name(country_slug, country_name)
        if api_records_by_country is None:
            api_records, _ = api_records_for_country(api_country)
        else:
            api_records = api_records_by_country.get(slugify(api_country), [])
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
    analyzed_coin_count = 0

    for period, coin in iter_coins(data):
        analyzed_coin_count += 1
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
                    matches = name_year_candidates
                else:
                    coin_issues.append(make_finding("missing_api_coin_record", "api", value="", missing_value="Not found"))

            if matches:
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
        "analyzed_coins": analyzed_coin_count,
        "total_issues": len(all_issues),
        "by_type": type_counts(all_issues),
    }
    if api_records is not None:
        summary["api_coin_count"] = len(api_records)
        summary["site_missing_fields"] = {
            "url_ucoin": sum(1 for record in api_records if not normalize_url(str(record.get("url_ucoin") or ""))[0]),
            "notes": sum(1 for record in api_records if not str(record.get("notes") or "").strip()),
        }
    if include_warnings:
        summary["total_warnings"] = len(all_warnings)
        summary["warnings_by_type"] = type_counts(all_warnings)

    result: dict[str, object] = {
        "country": country_slug,
        "country_name": country_name,
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
    parser.add_argument(
        "--max-issues",
        type=int,
        default=50,
        help="Mantido por compatibilidade; o modo texto mostra agora apenas totais agregados.",
    )
    parser.add_argument("--json", action="store_true", help="Imprime o relatorio completo em JSON.")
    parser.add_argument("--output", default="", help="Caminho para guardar o relatorio em JSON.")
    parser.add_argument("--include-markdown-as-issue", action="store_true", help="Conta markdown_url como diferenca real.")
    parser.add_argument("--include-warnings", action="store_true", help="Inclui warnings no relatorio (desligado por defeito).")
    parser.add_argument("--no-check-api", action="store_true", help="Nao verifica notes/url_ucoin no Site Base44.")
    return parser.parse_args()


def print_count_breakdown(counts: object, *, singular: str, plural: str) -> None:
    if not isinstance(counts, dict):
        return
    normalized_counts = [
        (str(issue_type), int(count))
        for issue_type, count in counts.items()
        if isinstance(count, int) and count > 0
    ]
    for issue_type, count in sorted(normalized_counts, key=lambda item: (-item[1], issue_type_label(item[0]))):
        print(f"- {issue_type_label(issue_type)}: {count} {count_label(count, singular, plural)}")


def coin_issue_counts(report: dict[str, object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    coins = report.get("coins_with_issues", [])
    for coin in coins if isinstance(coins, list) else []:
        if not isinstance(coin, dict):
            continue
        issue_types = {
            str(issue.get("type") or "")
            for issue in coin.get("issues", [])
            if isinstance(issue, dict) and str(issue.get("type") or "")
        }
        for issue_type in issue_types:
            counts[issue_type] = counts.get(issue_type, 0) + 1
    return counts


def missing_photo_details(report: dict[str, object]) -> list[str]:
    details: list[str] = []
    coins = report.get("coins_with_issues", [])
    for coin in coins if isinstance(coins, list) else []:
        if not isinstance(coin, dict):
            continue
        missing_sides = {
            "frente" if issue.get("field") == "obverseImage" else "verso"
            for issue in coin.get("issues", [])
            if isinstance(issue, dict)
            and issue.get("type") == "missing_image_url"
            and issue.get("field") in {"obverseImage", "reverseImage"}
        }
        if not missing_sides:
            continue
        if missing_sides == {"frente", "verso"}:
            missing_text = "frente e verso não disponíveis"
        else:
            missing_text = f"{next(iter(missing_sides))} não disponível"
        period = str(coin.get("issuePeriod") or "")
        period_text = f" ({period})" if period else ""
        details.append(f"  - {coin.get('denomination', '')}{period_text}: {missing_text}")
    return details


def print_text_report(report: dict[str, object], include_warnings: bool) -> None:
    summary = report.get("summary", {})
    country_slug = str(report.get("country", ""))
    country = str(report.get("country_name") or country_slug.replace("-", " ").title())

    if report.get("error"):
        print(f"\n{country}: error")
        print(f"- {report['error']}")
        return

    if not isinstance(summary, dict):
        summary = {}
    total_issues = int(summary.get("total_issues", 0))

    if report.get("report_type") in {"api_country_tracking", "api_country_pending_rarity"}:
        countries = report.get("countries", [])
        if not isinstance(countries, list) or not countries:
            return
        print(f"\n{country}: {len(countries)}")
        for item in countries:
            if not isinstance(item, dict):
                continue
            coin_count = int(item.get("coin_count", 0))
            if report.get("report_type") == "api_country_pending_rarity":
                missing_rarity = int(item.get("missing_rarity_count", 0))
                type_count = int(item.get("catalog_coin_type_count", 0))
                if missing_rarity:
                    print(
                        f"- {item.get('country', '')}: {missing_rarity}/{type_count} "
                        f"{count_label(missing_rarity, 'tipo sem raridade', 'tipos sem raridade')} "
                        f"({coin_count} {count_label(coin_count, 'moeda', 'moedas')} no Site Base44)"
                    )
                else:
                    print(f"- {item.get('country', '')}: raridade preenchida; falta gerar o catálogo final")
            else:
                print(f"- {item.get('country', '')}: {coin_count} {count_label(coin_count, 'moeda', 'moedas')}")
        return

    total_warnings = int(summary.get("total_warnings", 0)) if include_warnings else 0
    if total_issues == 0 and total_warnings == 0:
        return

    if total_issues:
        analyzed_coins = int(summary.get("analyzed_coins", summary.get("total_coins", 0)))
        api_coin_count_raw = summary.get("api_coin_count")
        api_coin_count = int(api_coin_count_raw) if isinstance(api_coin_count_raw, int) else None
        issue_counts = coin_issue_counts(report)
        print(
            f"\n{country}: {analyzed_coins} "
            f"{count_label(analyzed_coins, 'moeda analisada', 'moedas analisadas')}"
        )
        print()

        unconfirmed = issue_counts.get("missing_api_coin_record", 0)
        if unconfirmed:
            print("Associação:")
            print(
                f"- Sem associação confirmada: {unconfirmed} "
                f"{count_label(unconfirmed, 'moeda', 'moedas')}"
            )
            possibly_missing = max(0, analyzed_coins - api_coin_count) if api_coin_count is not None else 0
            if possibly_missing:
                print(
                    f"  - Possivelmente em falta no Site Base44: {possibly_missing} "
                    f"{count_label(possibly_missing, 'moeda', 'moedas')}"
                )
            possibly_existing = max(0, unconfirmed - possibly_missing)
            if possibly_existing:
                print(
                    f"  - Possivelmente existentes, mas sem correspondência: {possibly_existing} "
                    f"{count_label(possibly_existing, 'moeda', 'moedas')}"
                )
            print()

        if api_coin_count is not None:
            print(f"Site Base44: {api_coin_count} {count_label(api_coin_count, 'moeda', 'moedas')}")
            site_missing_fields = summary.get("site_missing_fields", {})
            if isinstance(site_missing_fields, dict):
                missing_url = int(site_missing_fields.get("url_ucoin", 0))
                missing_notes = int(site_missing_fields.get("notes", 0))
                if missing_url:
                    print(f"- Sem URL do uCoin: {missing_url} {count_label(missing_url, 'moeda', 'moedas')}")
                if missing_notes:
                    print(f"- Sem notes: {missing_notes} {count_label(missing_notes, 'moeda', 'moedas')}")
            mismatched_urls = issue_counts.get("mismatched_url_ucoin", 0)
            if mismatched_urls:
                print(
                    f"- URL do uCoin diferente do catálogo: {mismatched_urls} "
                    f"{count_label(mismatched_urls, 'moeda', 'moedas')}"
                )
            print()

        print(f"Catálogo local: {analyzed_coins} {count_label(analyzed_coins, 'moeda', 'moedas')}")
        hidden_types = {
            "missing_api_coin_record",
            "missing_image_url",
            "missing_notes",
            "missing_url_ucoin",
            "mismatched_url_ucoin",
        }
        for issue_type, count in sorted(
            ((key, value) for key, value in issue_counts.items() if key not in hidden_types),
            key=lambda item: (-item[1], TEXT_REPORT_ISSUE_LABELS.get(item[0], issue_type_label(item[0]))),
        ):
            label = TEXT_REPORT_ISSUE_LABELS.get(issue_type, issue_type_label(issue_type))
            print(f"- {label}: {count} {count_label(count, 'moeda', 'moedas')}")

        missing_photos = issue_counts.get("missing_image_url", 0)
        if missing_photos:
            print()
            print("Fonte uCoin:")
            print(
                f"- Sem fotografia disponível: {missing_photos} "
                f"{count_label(missing_photos, 'moeda', 'moedas')}"
            )
            for detail in missing_photo_details(report):
                print(detail)

    else:
        print(f"\n{country}: {total_warnings} {count_label(total_warnings, 'warning', 'warnings')}")

    if include_warnings:
        if total_warnings:
            if total_issues:
                print(f"Warnings: {total_warnings}")
            print_count_breakdown(summary.get("warnings_by_type", {}), singular="warning", plural="warnings")


def print_text_reports(reports: list[dict[str, object]], include_warnings: bool) -> None:
    if len(reports) <= 1:
        for report in reports:
            print_text_report(report, include_warnings)
        return

    source_photo_only: list[dict[str, object]] = []
    problems_to_review: list[dict[str, object]] = []
    for report in reports:
        summary = report.get("summary", {})
        by_type = summary.get("by_type", {}) if isinstance(summary, dict) else {}
        active_types = {
            str(issue_type)
            for issue_type, count in by_type.items()
            if isinstance(count, int) and count > 0
        }
        if not report.get("error") and active_types == {"missing_image_url"}:
            source_photo_only.append(report)
        else:
            problems_to_review.append(report)

    groups = [
        ("PAÍSES APENAS COM FOTOGRAFIAS INDISPONÍVEIS NO uCoin", source_photo_only),
        ("PAÍSES COM PROBLEMAS A REVER", problems_to_review),
    ]
    printed_group = False
    for heading, group_reports in groups:
        if not group_reports:
            continue
        if printed_group:
            print()
        print("#" * 72)
        print(heading)
        print("#" * 72)
        for index, report in enumerate(group_reports):
            if index:
                print("\n" + "-" * 72)
            print_text_report(report, include_warnings)
        printed_group = True


def report_has_output(report: dict[str, object], include_warnings: bool) -> bool:
    if report.get("error"):
        return True
    summary = report.get("summary", {})
    if not isinstance(summary, dict):
        return False
    if int(summary.get("total_issues", 0)) > 0:
        return True
    return include_warnings and int(summary.get("total_warnings", 0)) > 0


def report_for_output(report: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in report.items() if key != "image_matched_ucoin_urls"}


def country_slugs_with_catalog(paises_dir: Path, catalog_filename: str) -> list[str]:
    return sorted(
        path.name
        for path in iter_country_directories(paises_dir)
        if (path / catalog_filename).is_file()
    )


def write_output_report(
    output_path: Path,
    reports: list[dict[str, object]],
    *,
    total_issues: int,
    has_errors: bool,
) -> None:
    if has_errors:
        state = "preserved" if output_path.exists() else "not created"
        print(f"Report contains errors. Existing output was {state}: {output_path}")
    elif total_issues > 0:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(reports, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved {output_path}")
    elif output_path.exists():
        output_path.unlink()
        print(f"No differences found. Removed stale file: {output_path}")
    else:
        print("No differences found. Output file was not created.")


def main() -> int:
    args = parse_args()
    paises_dir = Path(args.paises_dir)
    all_coins_dir = Path(args.all_coins_dir)

    if args.country:
        countries = [slugify(args.country)]
    else:
        countries = country_slugs_with_catalog(paises_dir, args.catalog_file)

    check_api = not args.no_check_api
    api_records_by_country: dict[str, list[dict[str, object]]] | None = None
    api_tracking_error = ""
    if check_api and not args.country:
        all_api_records, api_error = api_records_for_all_countries()
        if all_api_records is None:
            api_tracking_error = api_error or "Erro desconhecido ao consultar o Site Base44."
            check_api = False
        else:
            api_records_by_country = group_api_records_by_country(all_api_records)

    reports = [
        compare_country(
            paises_dir,
            all_coins_dir,
            country,
            args.catalog_file,
            args.include_markdown_as_issue,
            check_api,
            args.include_warnings,
            api_records_by_country,
        )
        for country in countries
    ]

    if api_records_by_country is not None:
        tracked_country_keys = tracked_api_country_keys(paises_dir, countries, args.catalog_file)
        pending_rarity, pending_country_keys = pending_catalogue_api_countries(
            api_records_by_country,
            paises_dir,
            tracked_country_keys,
        )
        if pending_rarity:
            reports.append(pending_rarity_report(pending_rarity))
        missing_tracking = untracked_api_countries(
            api_records_by_country,
            tracked_country_keys | pending_country_keys,
        )
        if missing_tracking:
            reports.append(api_tracking_report(missing_tracking))
    elif api_tracking_error:
        reports.append(
            {
                "report_type": "api_country_tracking",
                "country": "api-country-tracking",
                "country_name": "Tracking de países do Site Base44",
                "summary": {"total_issues": 0, "by_type": {}},
                "countries": [],
                "coins_with_issues": [],
                "error": api_tracking_error,
            }
        )

    total_issues = 0
    has_errors = False
    for report in reports:
        if report.get("error"):
            has_errors = True
        summary = report.get("summary", {})
        if isinstance(summary, dict):
            total_issues += int(summary.get("total_issues", 0))

    visible_reports = [report for report in reports if report_has_output(report, args.include_warnings)]
    output_reports = [report_for_output(report) for report in visible_reports]

    if args.output:
        output_path = Path(args.output)
        write_output_report(output_path, output_reports, total_issues=total_issues, has_errors=has_errors)

    if args.json:
        print(json.dumps(output_reports, ensure_ascii=False, indent=2))
    else:
        print_text_reports(visible_reports, args.include_warnings)

    return 1 if total_issues or has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
