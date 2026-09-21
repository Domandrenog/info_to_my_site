#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from scripts import check_site_coin_differences as checker
from scripts import import_base44_coins
from scripts.catalog_paths import CATALOG_ROOT, continent_label_for_country, find_country_directory


SAFE_UPDATE_FIELDS = ("url_ucoin", "notes", "name", "years")
PRESERVED_API_FIELDS = {
    "name",
    "country",
    "continent",
    "years",
    "condition",
    "rarity",
    "has_variants",
    "image_frente",
    "image_verso",
    "url_ucoin",
    "url_numista",
    "notes",
    "ordem",
}
PHOTO_ISSUE_TYPES = {
    "detail_image_slug_mismatch",
    "missing_image_url",
    "non_ucoin_external_image",
    "side_mismatch",
    "url_not_in_all_coins_map",
}


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description=(
            "Build and optionally apply Base44 fixes "
            "(notes/url_ucoin/name/years/missing record)."
        )
    )
    parser.add_argument("--country", required=True, help="Country slug inside info/paises/<continent>/, e.g. bielorrussia")
    parser.add_argument("--paises-dir", default=str(project_root / CATALOG_ROOT))
    parser.add_argument("--all-coins-dir", default=str(project_root.parent / "All_Coins"))
    parser.add_argument("--catalog-file", default="app-catalog.json")
    parser.add_argument("--continent", default="", help="Used when creating missing API records; inferred from the country by default")
    parser.add_argument("--condition", default="Não Tenho", help="Used when creating missing API records")
    parser.add_argument("--output", default="", help="Optional path to save fix plan/report JSON")
    parser.add_argument("--apply", action="store_true", help="Apply changes to Base44 API. Without this, only dry-run plan is generated.")
    parser.add_argument(
        "--update-fields",
        nargs="+",
        choices=("notes", "url_ucoin", "name", "years"),
        default=("notes", "url_ucoin"),
        help="Fields allowed in API updates. Defaults to notes and url_ucoin.",
    )
    parser.add_argument("--skip-create-missing", action="store_true", help="Do not create records when API issue is Not found")
    parser.add_argument(
        "--reconcile-missing-interactive",
        action="store_true",
        help=(
            "Try to match missing entries against Base44 records, confirm them in the terminal, "
            "and optionally correct name/years."
        ),
    )
    parser.add_argument(
        "--associations-file",
        default="",
        help="Optional custom path for missing associations JSON.",
    )
    parser.add_argument("--max-match-candidates", type=int, default=5)
    parser.add_argument("--request-delay", type=float, default=1.5)
    parser.add_argument("--rate-limit-delay", type=float, default=30.0)
    parser.add_argument("--max-retries", type=int, default=5)
    return parser.parse_args()


def extract_record_id(site_url: str) -> str:
    if not site_url or site_url == "Not found":
        return ""
    parts = [part for part in urlparse(site_url).path.split("/") if part]
    if not parts:
        return ""
    return parts[-1]


def normalize_url(value: str) -> str:
    return checker.normalize_url(str(value or ""))[0]


def normalize_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    normalized = "".join(character for character in normalized if not unicodedata.combining(character))
    tokens = re.findall(r"\d+(?:[.,]\d+)?|[a-zø]+|[½¼¾]", normalized)
    unit_aliases = {
        "c": "cent",
        "ct": "cent",
        "cts": "cent",
        "cent": "cent",
        "cents": "cent",
        "centimo": "cent",
        "centimos": "cent",
        "dolar": "dollar",
        "dolares": "dollar",
        "dollar": "dollar",
        "dollars": "dollar",
        "euro": "euro",
        "euros": "euro",
        "escudo": "escudo",
        "escudos": "escudo",
        "kopek": "kopek",
        "kopeks": "kopek",
        "ore": "ore",
        "øre": "ore",
        "pence": "penny",
        "pennies": "penny",
        "penny": "penny",
        "rand": "rand",
        "rupee": "rupee",
        "rupees": "rupee",
    }
    return " ".join(unit_aliases.get(token, token) for token in tokens)


def parse_association_record_id(entry: dict[str, Any]) -> str:
    record_id = str(entry.get("record_id") or "")
    if record_id:
        return record_id
    api_url = str(entry.get("apiUrl") or entry.get("siteUrl") or "")
    return extract_record_id(api_url)


def collect_image_issue_ucoin_urls(report: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    coins_with_issues = report.get("coins_with_issues", [])
    if not isinstance(coins_with_issues, list):
        return out

    for coin in coins_with_issues:
        if not isinstance(coin, dict):
            continue
        issues = coin.get("issues", [])
        if not isinstance(issues, list):
            continue
        has_image_issue = any(
            isinstance(issue, dict)
            and str(issue.get("field") or "") in {"obverseImage", "reverseImage", "image_frente", "image_verso"}
            for issue in issues
        )
        if not has_image_issue:
            continue
        ucoin_url = normalize_url(str(coin.get("ucoinUrl") or ""))
        if ucoin_url:
            out.add(ucoin_url)

    return out


def build_image_non_equivalence_entries(report: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    coins_with_issues = report.get("coins_with_issues", [])
    if not isinstance(coins_with_issues, list):
        return entries

    for coin in coins_with_issues:
        if not isinstance(coin, dict):
            continue
        issues = coin.get("issues", [])
        if not isinstance(issues, list):
            continue
        image_issues = [
            issue
            for issue in issues
            if isinstance(issue, dict)
            and str(issue.get("field") or "") in {"image_frente", "image_verso"}
        ]
        if not image_issues:
            continue

        current_images = {
            str(issue.get("field") or ""): str(issue.get("value") or "")
            for issue in image_issues
        }
        expected_images = {
            str(issue.get("field") or ""): str(issue.get("missing_value") or "")
            for issue in image_issues
        }
        entries.append(
            {
                "status": "image_not_equivalent",
                "source": "api_image_check",
                "denomination": str(coin.get("denomination") or ""),
                "issuePeriod": str(coin.get("issuePeriod") or ""),
                "ucoinUrl": normalize_url(str(coin.get("ucoinUrl") or "")),
                "apiUrl": str(coin.get("siteUrl") or "Not found"),
                "currentImages": current_images,
                "expectedImages": expected_images,
            }
        )

    return entries


def load_catalog_index(path: Path) -> tuple[dict[str, tuple[dict[str, Any], dict[str, Any], int]], dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    index: dict[str, tuple[dict[str, Any], dict[str, Any], int]] = {}
    ordem = 1
    for period in data.get("periods", []):
        if not isinstance(period, dict):
            continue
        for coin in period.get("coins", []):
            if not isinstance(coin, dict):
                continue
            key = normalize_url(str(coin.get("detailUrl") or ""))
            if key:
                index[key] = (period, coin, ordem)
            ordem += 1
    return index, data


def build_fix_plan(report: dict[str, Any], catalog_index: dict[str, tuple[dict[str, Any], dict[str, Any], int]]) -> dict[str, Any]:
    updates: list[dict[str, Any]] = []
    creates: list[dict[str, Any]] = []

    for coin in report.get("coins_with_issues", []):
        if not isinstance(coin, dict):
            continue
        ucoin_url = normalize_url(str(coin.get("ucoinUrl") or ""))
        site_url = str(coin.get("siteUrl") or "Not found")
        record_id = extract_record_id(site_url)
        issues = coin.get("issues", [])
        if not isinstance(issues, list):
            issues = []

        update_fields: dict[str, str] = {}
        current_fields: dict[str, str] = {}
        has_missing_api = False

        for issue in issues:
            if not isinstance(issue, dict):
                continue
            field = str(issue.get("field") or "")
            missing_value = str(issue.get("missing_value") or "")
            if field == "notes" and missing_value:
                update_fields["notes"] = missing_value
                current_fields["notes"] = str(issue.get("value") or "")
            elif field == "url_ucoin" and missing_value:
                update_fields["url_ucoin"] = missing_value
                current_fields["url_ucoin"] = str(issue.get("value") or "")
            elif field == "api" and missing_value == "Not found":
                has_missing_api = True

        if record_id and update_fields:
            updates.append(
                {
                    "denomination": coin.get("denomination", ""),
                    "issuePeriod": coin.get("issuePeriod", ""),
                    "record_id": record_id,
                    "siteUrl": site_url,
                    "current": current_fields,
                    "set": update_fields,
                }
            )

        if has_missing_api:
            catalog_item = catalog_index.get(ucoin_url)
            if catalog_item is not None:
                period, coin_data, ordem = catalog_item
                creates.append(
                    {
                        "denomination": coin.get("denomination", ""),
                        "issuePeriod": coin.get("issuePeriod", ""),
                        "ucoinUrl": ucoin_url,
                        "period": period,
                        "coin": coin_data,
                        "ordem": ordem,
                    }
                )

    return {"updates": updates, "creates": creates}


def consolidate_updates(updates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Merge duplicate proposals for one API record and isolate conflicting values."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for index, item in enumerate(updates):
        record_id = str(item.get("record_id") or "")
        key = record_id or f"missing-record-id-{index}"
        grouped.setdefault(key, []).append(item)

    consolidated: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for record_id, items in grouped.items():
        base = dict(items[0])
        merged_set: dict[str, Any] = {}
        merged_current: dict[str, Any] = {}
        fields = {
            field
            for item in items
            for field in item.get("set", {})
            if isinstance(item.get("set"), dict)
        }
        for field in fields:
            proposals = {
                str(item.get("set", {}).get(field) or "")
                for item in items
                if isinstance(item.get("set"), dict) and field in item.get("set", {})
            }
            if len(proposals) > 1:
                conflicts.append(
                    {
                        "record_id": record_id,
                        "field": field,
                        "values": sorted(proposals),
                        "coins": [
                            {
                                "denomination": str(item.get("denomination") or ""),
                                "issuePeriod": str(item.get("issuePeriod") or ""),
                            }
                            for item in items
                            if isinstance(item.get("set"), dict) and field in item.get("set", {})
                        ],
                    }
                )
                continue
            merged_set[field] = next(iter(proposals))
            for item in items:
                current = item.get("current", {})
                if isinstance(current, dict) and field in current:
                    merged_current[field] = current[field]
                    break

        if merged_set:
            base["set"] = merged_set
            base["current"] = merged_current
            consolidated.append(base)

    return consolidated, conflicts


def collect_global_fix_plan(
    paises_dir: Path,
    all_coins_dir: Path,
    catalog_filename: str = "app-catalog.json",
) -> dict[str, Any]:
    """Build one read-only update plan for every locally tracked country."""
    api_records, api_error = checker.api_records_for_all_countries()
    if api_records is None:
        raise RuntimeError(api_error or "Erro desconhecido ao consultar a API Base44.")

    api_records_by_country = checker.group_api_records_by_country(api_records)
    updates: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    notes_status: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    manual_counts = {"photos": 0}

    for country_slug in checker.country_slugs_with_catalog(paises_dir, catalog_filename):
        report = checker.compare_country(
            paises_dir,
            all_coins_dir,
            country_slug,
            catalog_filename,
            include_markdown_as_issue=False,
            check_api=True,
            include_warnings=False,
            api_records_by_country=api_records_by_country,
        )
        country_name = str(report.get("country_name") or country_slug)
        if report.get("error"):
            errors.append({"country": country_name, "error": str(report["error"])})
            continue

        country_dir = find_country_directory(paises_dir, country_slug)
        catalog_index, catalog_data = load_catalog_index(country_dir / catalog_filename)
        country_name = str(catalog_data.get("country") or country_name)
        country_plan = build_fix_plan(report, catalog_index)
        country_updates, country_conflicts = consolidate_updates(list(country_plan.get("updates", [])))
        for conflict in country_conflicts:
            conflicts.append({**conflict, "country": country_name, "country_slug": country_slug})
        api_country = checker.api_country_name(country_slug, country_name)
        country_api_records = api_records_by_country.get(checker.slugify(api_country), [])
        records_with_notes = sum(
            1
            for record in country_api_records
            if str(record.get("notes") or "").strip()
        )
        fixable_missing_notes = sum(
            1
            for item in country_updates
            if "notes" in item.get("set", {})
        )
        total_api_records = len(country_api_records)
        if not records_with_notes:
            notes_state = "none"
        elif records_with_notes < total_api_records:
            notes_state = "partial"
        else:
            notes_state = "complete"
        notes_status.append(
            {
                "country": country_name,
                "country_slug": country_slug,
                "total": total_api_records,
                "with_notes": records_with_notes,
                "without_notes": total_api_records - records_with_notes,
                "fixable_missing_notes": fixable_missing_notes,
                "state": notes_state,
            }
        )

        for item in country_updates:
            updates.append({**item, "country": country_name, "country_slug": country_slug})
        for item in country_plan.get("creates", []):
            missing.append(
                {
                    "country": country_name,
                    "country_slug": country_slug,
                    "denomination": item.get("denomination", ""),
                    "issuePeriod": item.get("issuePeriod", ""),
                    "ucoinUrl": item.get("ucoinUrl", ""),
                }
            )

        summary = report.get("summary", {})
        by_type = summary.get("by_type", {}) if isinstance(summary, dict) else {}
        if isinstance(by_type, dict):
            manual_counts["photos"] += sum(int(by_type.get(issue_type, 0)) for issue_type in PHOTO_ISSUE_TYPES)

    return {
        "updates": updates,
        "missing": missing,
        "errors": errors,
        "notes_status": notes_status,
        "conflicts": conflicts,
        "manual_counts": manual_counts,
    }


def update_field_counts(plan: dict[str, Any]) -> dict[str, int]:
    counts = {field: 0 for field in SAFE_UPDATE_FIELDS}
    for item in plan.get("updates", []):
        set_fields = item.get("set", {})
        if not isinstance(set_fields, dict):
            continue
        for field in SAFE_UPDATE_FIELDS:
            if field in set_fields:
                counts[field] += 1
    return {field: count for field, count in counts.items() if count}


def select_update_fields(plan: dict[str, Any], selected_fields: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Return a plan containing only explicitly selected safe API fields."""
    allowed = set(selected_fields) & set(SAFE_UPDATE_FIELDS)
    updates: list[dict[str, Any]] = []
    for item in plan.get("updates", []):
        raw_set = item.get("set", {})
        raw_current = item.get("current", {})
        if not isinstance(raw_set, dict):
            continue
        selected_set = {field: raw_set[field] for field in SAFE_UPDATE_FIELDS if field in allowed and field in raw_set}
        if not selected_set:
            continue
        selected_current = {
            field: raw_current.get(field, "")
            for field in selected_set
            if isinstance(raw_current, dict)
        }
        updates.append({**item, "current": selected_current, "set": selected_set})
    return {**plan, "updates": updates}


def restrict_update_field_to_countries(
    plan: dict[str, Any],
    field: str,
    country_slugs: set[str],
) -> dict[str, Any]:
    """Keep one selected field only for the explicitly allowed countries."""
    if field not in SAFE_UPDATE_FIELDS:
        raise ValueError(f"Campo não permitido: {field}")

    updates: list[dict[str, Any]] = []
    for item in plan.get("updates", []):
        set_fields = dict(item.get("set", {}))
        current_fields = dict(item.get("current", {}))
        if field in set_fields and str(item.get("country_slug") or "") not in country_slugs:
            set_fields.pop(field, None)
            current_fields.pop(field, None)
        if set_fields:
            updates.append({**item, "current": current_fields, "set": set_fields})
    return {**plan, "updates": updates}


def default_associations_path(country_dir: Path, country_slug: str) -> Path:
    return country_dir / f"{country_slug}-missing-associations.json"


def default_missing_found_path(country_dir: Path, country_slug: str) -> Path:
    return country_dir / f"{country_slug}-missing-found.json"


def default_name_match_decisions_path(country_dir: Path, country_slug: str) -> Path:
    return country_dir / f"{country_slug}-name-match-decisions.json"


def load_name_match_decisions(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    decisions = data.get("decisions", []) if isinstance(data, dict) else []
    return [item for item in decisions if isinstance(item, dict)]


def save_name_match_decision(
    path: Path,
    country_slug: str,
    decision: dict[str, Any],
) -> None:
    decisions_by_url = {
        normalize_url(str(item.get("ucoinUrl") or "")): dict(item)
        for item in load_name_match_decisions(path)
        if normalize_url(str(item.get("ucoinUrl") or ""))
    }
    ucoin_url = normalize_url(str(decision.get("ucoinUrl") or ""))
    if not ucoin_url:
        return
    decisions_by_url[ucoin_url] = {
        **decision,
        "ucoinUrl": ucoin_url,
        "decidedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    decisions = sorted(
        decisions_by_url.values(),
        key=lambda item: (
            str(item.get("catalogName") or ""),
            str(item.get("catalogYears") or ""),
            str(item.get("ucoinUrl") or ""),
        ),
    )
    payload = {
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "country": country_slug,
        "decisions": decisions,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def update_name_match_application_status(
    path: Path,
    application_results: list[dict[str, Any]],
) -> None:
    decisions = load_name_match_decisions(path)
    if not decisions:
        return
    results_by_record_id = {
        str(item.get("record_id") or ""): item
        for item in application_results
        if str(item.get("record_id") or "")
    }
    changed = False
    for decision in decisions:
        requested_fields = set()
        if decision.get("rename") == "yes":
            requested_fields.add("name")
        if decision.get("updateYears") == "yes":
            requested_fields.add("years")
        result = results_by_record_id.get(str(decision.get("siteRecordId") or ""))
        result_fields = set(result.get("fields", [])) if result is not None else set()
        if result is None or not requested_fields or not requested_fields.issubset(result_fields):
            continue
        decision["applyStatus"] = str(result.get("status") or "not_applied")
        decision["applyMessage"] = str(result.get("message") or "")
        status_updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        decision["statusUpdatedAt"] = status_updated_at
        if decision["applyStatus"] in {"applied", "already_correct"}:
            decision["appliedAt"] = status_updated_at
        else:
            decision.pop("appliedAt", None)
        changed = True
    if not changed:
        return
    payload = {
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "country": str(json.loads(path.read_text(encoding="utf-8")).get("country") or ""),
        "decisions": decisions,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def update_creation_application_status(
    path: Path,
    application_results: list[dict[str, Any]],
) -> None:
    decisions = load_name_match_decisions(path)
    if not decisions:
        return
    results_by_url = {
        normalize_url(str(item.get("ucoin_url") or "")): item
        for item in application_results
        if normalize_url(str(item.get("ucoin_url") or ""))
        and "create" in item.get("fields", [])
    }
    changed = False
    for decision in decisions:
        if decision.get("createRecord") != "yes":
            continue
        result = results_by_url.get(normalize_url(str(decision.get("ucoinUrl") or "")))
        if result is None:
            continue
        decision["applyStatus"] = str(result.get("status") or "not_applied")
        decision["applyMessage"] = str(result.get("message") or "")
        status_updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        decision["statusUpdatedAt"] = status_updated_at
        if decision["applyStatus"] in {"created", "already_exists"}:
            decision["appliedAt"] = status_updated_at
            decision["siteRecordId"] = str(result.get("record_id") or "")
            decision["siteUrl"] = checker.build_site_url(str(result.get("record_id") or ""))
        else:
            decision.pop("appliedAt", None)
        changed = True
    if not changed:
        return
    existing = json.loads(path.read_text(encoding="utf-8"))
    payload = {
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "country": str(existing.get("country") or "") if isinstance(existing, dict) else "",
        "decisions": decisions,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def confirm_applied_name_matches(
    missing_found_path: Path,
    application_results: list[dict[str, Any]],
) -> None:
    entries = load_missing_found(missing_found_path)
    if not entries:
        return
    successful_record_ids = {
        str(item.get("record_id") or "")
        for item in application_results
        if str(item.get("status") or "") in {"applied", "already_correct"}
        and {"name", "years"}.intersection(item.get("fields", []))
    }
    changed = False
    for entry in entries:
        if entry.get("status") not in {
            "connected_pending_name_update",
            "connected_pending_metadata_update",
        }:
            continue
        if parse_association_record_id(entry) not in successful_record_ids:
            continue
        entry["status"] = "connected"
        changed = True
    if not changed:
        return
    existing = json.loads(missing_found_path.read_text(encoding="utf-8"))
    payload = {
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "country": str(existing.get("country") or "") if isinstance(existing, dict) else "",
        "missing": entries,
    }
    missing_found_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def confirm_created_matches(
    missing_found_path: Path,
    application_results: list[dict[str, Any]],
) -> None:
    entries = load_missing_found(missing_found_path)
    if not entries:
        return
    created_by_url = {
        normalize_url(str(item.get("ucoin_url") or "")): (
            str(item.get("status") or ""),
            str(item.get("record_id") or ""),
        )
        for item in application_results
        if item.get("status") in {"created", "already_exists"}
        and normalize_url(str(item.get("ucoin_url") or ""))
    }
    changed = False
    for entry in entries:
        ucoin_url = normalize_url(str(entry.get("ucoinUrl") or ""))
        result_status, record_id = created_by_url.get(ucoin_url, ("", ""))
        if entry.get("status") != "confirmed_create" or not record_id:
            continue
        entry["status"] = result_status
        entry["record_id"] = record_id
        entry["siteUrl"] = checker.build_site_url(record_id)
        changed = True
    if not changed:
        return
    existing = json.loads(missing_found_path.read_text(encoding="utf-8"))
    payload = {
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "country": str(existing.get("country") or "") if isinstance(existing, dict) else "",
        "missing": entries,
    }
    missing_found_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_missing_found(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    entries = data.get("missing", []) if isinstance(data, dict) else []
    return [entry for entry in entries if isinstance(entry, dict)]


def save_missing_found(
    path: Path,
    country_slug: str,
    entries: list[dict[str, Any]],
    exclude_ucoin_urls: set[str] | None = None,
) -> None:
    previous_entries = load_missing_found(path)
    excluded = exclude_ucoin_urls or set()

    # Keep a memory of previous associations and update with latest run data by ucoin URL.
    merged_by_url: dict[str, dict[str, str]] = {}
    for entry in previous_entries:
        if str(entry.get("status") or "") == "image_not_equivalent":
            continue
        key = normalize_url(str(entry.get("ucoinUrl") or ""))
        if key and key not in excluded:
            merged_by_url[key] = dict(entry)

    for entry in entries:
        key = normalize_url(str(entry.get("ucoinUrl") or ""))
        if key and key not in excluded:
            merged_by_url[key] = dict(entry)

    merged_entries = sorted(
        merged_by_url.values(),
        key=lambda item: (
            str(item.get("status") or ""),
            str(item.get("denomination") or ""),
            str(item.get("issuePeriod") or ""),
            str(item.get("ucoinUrl") or ""),
        ),
    )

    payload = {
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "country": country_slug,
        "missing": merged_entries,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_associations(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    entries = data.get("associations", []) if isinstance(data, dict) else []
    out: dict[str, dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ucoin_url = normalize_url(str(entry.get("ucoinUrl") or ""))
        record_id = parse_association_record_id(entry)
        api_url = str(entry.get("apiUrl") or entry.get("siteUrl") or "")
        if ucoin_url and record_id:
            out[ucoin_url] = {
                "record_id": record_id,
                "api_url": api_url or checker.build_site_url(record_id),
                "name": str(entry.get("name") or ""),
                "years": str(entry.get("years") or ""),
            }
    return out


def save_associations(
    path: Path,
    associations: dict[str, dict[str, str]],
    only_ucoin_urls: set[str] | None = None,
) -> None:
    filtered: dict[str, dict[str, str]] = {}
    for ucoin_url, value in associations.items():
        if only_ucoin_urls is not None and ucoin_url not in only_ucoin_urls:
            continue
        filtered[ucoin_url] = value

    if not filtered:
        if path.exists():
            path.unlink()
        return

    payload = {
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "associations": [
            {
                "ucoinUrl": ucoin_url,
                "apiUrl": value.get("api_url", "") or checker.build_site_url(str(value.get("record_id") or "")),
                "name": value.get("name", ""),
                "years": value.get("years", ""),
            }
            for ucoin_url, value in sorted(filtered.items())
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def score_candidate(item: dict[str, Any], record: dict[str, Any]) -> float:
    item_name = normalize_key(str(item.get("denomination") or ""))
    item_years = normalize_key(str(item.get("coin", {}).get("issuePeriod") or ""))
    rec_name = normalize_key(str(record.get("name") or ""))
    rec_years = normalize_key(str(record.get("years") or ""))

    score = 0.0
    name_ratio = SequenceMatcher(None, item_name, rec_name).ratio() if item_name and rec_name else 0.0

    if item_name and rec_name and item_name == rec_name:
        score += 5.0
    else:
        score += name_ratio * 3.0

    if item_years and rec_years:
        if item_years == rec_years:
            score += 5.0
        elif item_years in rec_years or rec_years in item_years:
            score += 2.5

    return score


def best_candidates(item: dict[str, Any], api_records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    scored: list[tuple[float, dict[str, Any]]] = []
    for record in api_records:
        if not isinstance(record, dict):
            continue
        score = score_candidate(item, record)
        if score <= 0:
            continue
        scored.append((score, record))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [{"score": score, "record": record} for score, record in scored[: max(1, limit)]]


def merge_update(
    plan_updates: list[dict[str, Any]],
    denomination: str,
    issue_period: str,
    record: dict[str, Any],
    set_fields: dict[str, str],
) -> None:
    if not set_fields:
        return

    record_id = str(record.get("id") or "")
    if not record_id:
        return

    for item in plan_updates:
        if str(item.get("record_id") or "") == record_id:
            current_set = item.get("set", {})
            if isinstance(current_set, dict):
                current_set.update(set_fields)
            else:
                item["set"] = dict(set_fields)
            current_fields = item.get("current", {})
            if not isinstance(current_fields, dict):
                current_fields = {}
                item["current"] = current_fields
            for field in set_fields:
                current_fields.setdefault(field, str(record.get(field) or ""))
            return

    plan_updates.append(
        {
            "denomination": denomination,
            "issuePeriod": issue_period,
            "record_id": record_id,
            "siteUrl": checker.build_site_url(record_id),
            "current": {
                field: str(record.get(field) or "")
                for field in set_fields
            },
            "set": dict(set_fields),
        }
    )


def reconcile_missing(
    country_name: str,
    missing_creates: list[dict[str, Any]],
    plan_updates: list[dict[str, Any]],
    associations_path: Path,
    missing_found_path: Path,
    interactive: bool,
    max_candidates: int,
    association_ucoin_filter: set[str] | None = None,
    name_decisions_path: Path | None = None,
    decision_country_slug: str = "",
) -> dict[str, list[dict[str, Any]]]:
    api_records_raw, _ = checker.api_records_for_country(country_name)
    api_records = [record for record in (api_records_raw or []) if isinstance(record, dict)]
    by_record_id = {str(record.get("id") or ""): record for record in api_records if str(record.get("id") or "")}

    associations: dict[str, dict[str, str]] = {}
    known_from_missing_found: dict[str, dict[str, str]] = {}
    for entry in load_missing_found(missing_found_path):
        if str(entry.get("status") or "") != "connected":
            continue
        key = normalize_url(str(entry.get("ucoinUrl") or ""))
        record_id = parse_association_record_id(entry)
        if key and record_id:
            known_from_missing_found[key] = {
                "record_id": record_id,
                "api_url": str(entry.get("siteUrl") or "") or checker.build_site_url(record_id),
                "name": str(entry.get("record_name") or ""),
                "years": str(entry.get("record_years") or ""),
            }

    associations_changed = False
    unresolved: list[dict[str, str]] = []
    all_entries: list[dict[str, str]] = []
    confirmed_creates: list[dict[str, Any]] = []

    for item in missing_creates:
        denomination = str(item.get("denomination") or "")
        ucoin_url = normalize_url(str(item.get("ucoinUrl") or ""))
        issue_period = str(item.get("coin", {}).get("issuePeriod") or "")
        expected_notes = checker.expected_notes_from_period(item.get("period", {}))

        resolved_record: dict[str, Any] | None = None
        resolved_source = ""
        candidate_for_decision: dict[str, Any] | None = None
        same_coin_decision = ""
        create_decision = False

        if resolved_record is None:
            known_missing = known_from_missing_found.get(ucoin_url)
            if known_missing:
                resolved_record = by_record_id.get(str(known_missing.get("record_id") or ""))
                if resolved_record is not None:
                    resolved_source = "missing_found_file"
                    candidate_for_decision = resolved_record
                    same_coin_decision = "yes"
                    associations[ucoin_url] = {
                        "record_id": str(resolved_record.get("id") or ""),
                        "api_url": checker.build_site_url(str(resolved_record.get("id") or "")),
                        "name": str(resolved_record.get("name") or ""),
                        "years": str(resolved_record.get("years") or ""),
                    }
                    associations_changed = True

        if resolved_record is None and interactive:
            print("\n" + "-" * 72)
            print("MOEDA SEM ASSOCIAÇÃO CONFIRMADA")
            print("-" * 72)
            print(f"Catálogo local: {denomination} ({issue_period})")
            print(f"uCoin: {ucoin_url or '(sem link)'}")

            exact_candidates = [
                record
                for record in api_records
                if normalize_key(str(record.get("name") or "")) == normalize_key(denomination)
                and normalize_key(str(record.get("years") or "")) == normalize_key(issue_period)
            ]
            if len(exact_candidates) == 1:
                candidate = exact_candidates[0]
                candidate_for_decision = candidate
                print("Possível correspondência no Site Base44:")
                print(f"- {candidate.get('name', '')} ({candidate.get('years', '')})")
                try:
                    choice = input("Confirmar que é a mesma moeda? [s/N]: ").strip().lower()
                except EOFError:
                    choice = ""
                if choice in {"y", "yes", "s", "sim"}:
                    resolved_record = candidate
                    resolved_source = "interactive"
                    same_coin_decision = "yes"
                else:
                    try:
                        create_choice = input(
                            "Não existe no Site Base44 — criar moeda nova? [s/N]: "
                        ).strip().lower()
                    except EOFError:
                        create_choice = ""
                    if create_choice in {"y", "yes", "s", "sim"}:
                        create_decision = True
                        candidate_for_decision = None
                        same_coin_decision = "new_record"
                    else:
                        same_coin_decision = "no"
            else:
                candidates = best_candidates(item, api_records, max_candidates)
                if not candidates:
                    print("Sem candidatos por nome e anos.")
                    print("  1) Não existe no Site Base44 — criar moeda nova")
                    print("  0) Não associar por agora")
                    while True:
                        choice = input("Escolhe uma opção: ").strip()
                        if choice in {"", "0"}:
                            same_coin_decision = "no_candidate"
                            break
                        if choice == "1":
                            create_decision = True
                            same_coin_decision = "new_record"
                            break
                        print("Escolhe 1 ou 0.")
                else:
                    print("Possíveis correspondências no Site Base44:")
                    for idx, candidate in enumerate(candidates, start=1):
                        record = candidate["record"]
                        print(
                            f"  {idx}) {record.get('name', '')} ({record.get('years', '')})"
                        )
                    create_option = len(candidates) + 1
                    print(f"  {create_option}) Não existe no Site Base44 — criar moeda nova")
                    print("  0) Não associar por agora")

                    while True:
                        choice = input("Escolhe candidato: ").strip()
                        if choice in {"", "0"}:
                            same_coin_decision = "no"
                            break
                        try:
                            idx = int(choice)
                        except ValueError:
                            print("Escreve um número válido.")
                            continue
                        if idx == create_option:
                            create_decision = True
                            same_coin_decision = "new_record"
                            break
                        if idx < 1 or idx > len(candidates):
                            print("Opção inválida.")
                            continue
                        resolved_record = candidates[idx - 1]["record"]
                        candidate_for_decision = resolved_record
                        resolved_source = "interactive"
                        same_coin_decision = "yes"
                        break

        if resolved_record is None:
            if create_decision:
                confirmed_creates.append(item)
                if interactive and name_decisions_path is not None:
                    save_name_match_decision(
                        name_decisions_path,
                        decision_country_slug or checker.slugify(country_name),
                        {
                            "catalogName": denomination,
                            "catalogYears": issue_period,
                            "ucoinUrl": ucoin_url,
                            "siteRecordId": "",
                            "siteUrl": "",
                            "siteName": "",
                            "siteYears": "",
                            "sameCoin": "new_record",
                            "createRecord": "yes",
                            "rename": "not_asked",
                            "proposedName": denomination,
                            "updateYears": "not_asked",
                            "proposedYears": issue_period,
                            "applyStatus": "pending_create",
                        },
                    )
                all_entries.append(
                    {
                        "status": "confirmed_create",
                        "source": "interactive",
                        "denomination": denomination,
                        "issuePeriod": issue_period,
                        "ucoinUrl": ucoin_url,
                        "record_id": "",
                        "record_name": "",
                        "record_years": "",
                        "siteUrl": "",
                    }
                )
                continue

            candidate = candidate_for_decision or {}
            if interactive and name_decisions_path is not None:
                save_name_match_decision(
                    name_decisions_path,
                    decision_country_slug or checker.slugify(country_name),
                    {
                        "catalogName": denomination,
                        "catalogYears": issue_period,
                        "ucoinUrl": ucoin_url,
                        "siteRecordId": str(candidate.get("id") or ""),
                        "siteUrl": checker.build_site_url(str(candidate.get("id") or "")),
                        "siteName": str(candidate.get("name") or ""),
                        "siteYears": str(candidate.get("years") or ""),
                        "sameCoin": same_coin_decision or "no",
                        "createRecord": "no",
                        "rename": "not_asked",
                        "proposedName": denomination,
                        "updateYears": "not_asked",
                        "proposedYears": issue_period,
                        "applyStatus": "not_requested",
                    },
                )
            all_entries.append(
                {
                    "status": "rejected" if same_coin_decision == "no" else "unresolved",
                    "source": "interactive" if interactive else "not_matched",
                    "denomination": denomination,
                    "issuePeriod": issue_period,
                    "ucoinUrl": ucoin_url,
                    "record_id": str(candidate.get("id") or ""),
                    "record_name": str(candidate.get("name") or ""),
                    "record_years": str(candidate.get("years") or ""),
                    "siteUrl": checker.build_site_url(str(candidate.get("id") or "")),
                }
            )
            unresolved.append({"denomination": denomination, "ucoinUrl": ucoin_url})
            continue

        current_url = normalize_url(str(resolved_record.get("url_ucoin") or ""))
        current_notes = str(resolved_record.get("notes") or "").strip()
        current_name = str(resolved_record.get("name") or "").strip()
        current_years = str(resolved_record.get("years") or "").strip()
        set_fields: dict[str, str] = {}
        rename_decision = "not_needed"
        years_decision = "not_needed"

        if current_url != ucoin_url:
            set_fields["url_ucoin"] = ucoin_url
        if not current_notes and expected_notes:
            set_fields["notes"] = expected_notes
        if interactive and denomination and current_name.casefold() != denomination.strip().casefold():
            print("\nNome diferente:")
            print(f"- Site Base44 agora: {current_name or '(vazio)'}")
            print(f"- Nome completo do catálogo: {denomination}")
            try:
                rename_choice = input("Corrigir apenas o nome no Site Base44? [s/N]: ").strip().lower()
            except EOFError:
                rename_choice = ""
            if rename_choice in {"y", "yes", "s", "sim"}:
                set_fields["name"] = denomination
                rename_decision = "yes"
            else:
                rename_decision = "no"

        if interactive and issue_period and normalize_key(current_years) != normalize_key(issue_period):
            print("\nAnos diferentes:")
            print(f"- Site Base44 agora: {current_years or '(vazio)'}")
            print(f"- Período completo do catálogo: {issue_period}")
            try:
                years_choice = input("Corrigir os anos no Site Base44? [s/N]: ").strip().lower()
            except EOFError:
                years_choice = ""
            if years_choice in {"y", "yes", "s", "sim"}:
                set_fields["years"] = issue_period
                years_decision = "yes"
            else:
                years_decision = "no"

        has_pending_metadata_update = rename_decision == "yes" or years_decision == "yes"

        if interactive and name_decisions_path is not None:
            save_name_match_decision(
                name_decisions_path,
                decision_country_slug or checker.slugify(country_name),
                {
                    "catalogName": denomination,
                    "catalogYears": issue_period,
                    "ucoinUrl": ucoin_url,
                    "siteRecordId": str(resolved_record.get("id") or ""),
                    "siteUrl": checker.build_site_url(str(resolved_record.get("id") or "")),
                    "siteName": current_name,
                    "siteYears": str(resolved_record.get("years") or ""),
                    "sameCoin": same_coin_decision or "yes",
                    "rename": rename_decision,
                    "proposedName": denomination,
                    "updateYears": years_decision,
                    "proposedYears": issue_period,
                    "applyStatus": "pending" if has_pending_metadata_update else "not_requested",
                },
            )

        merge_update(plan_updates, denomination, issue_period, resolved_record, set_fields)

        all_entries.append(
            {
                "status": "connected_pending_metadata_update" if has_pending_metadata_update else "connected",
                "source": resolved_source,
                "denomination": denomination,
                "issuePeriod": issue_period,
                "ucoinUrl": ucoin_url,
                "apiUrl": checker.build_site_url(str(resolved_record.get("id") or "")),
                "record_name": str(set_fields.get("name") or resolved_record.get("name") or ""),
                "record_years": str(set_fields.get("years") or resolved_record.get("years") or ""),
            }
        )


    return {
        "unresolved": unresolved,
        "all": all_entries,
        "creates": confirmed_creates,
    }


def confirm_equivalences_for_updates(
    country_name: str,
    report: dict[str, Any],
    plan_updates: list[dict[str, Any]],
    associations_path: Path,
    missing_found_path: Path,
    interactive: bool,
    association_ucoin_filter: set[str] | None = None,
) -> dict[str, list[dict[str, str]]]:
    api_records_raw, _ = checker.api_records_for_country(country_name)
    api_records = [record for record in (api_records_raw or []) if isinstance(record, dict)]
    by_record_id = {str(record.get("id") or ""): record for record in api_records if str(record.get("id") or "")}

    associations = load_associations(associations_path)
    connected_from_missing_found: dict[str, dict[str, str]] = {}
    for entry in load_missing_found(missing_found_path):
        if str(entry.get("status") or "") != "connected":
            continue
        key = normalize_url(str(entry.get("ucoinUrl") or ""))
        record_id = str(entry.get("record_id") or "")
        if key and record_id:
            connected_from_missing_found[key] = {
                "record_id": record_id,
                "api_url": str(entry.get("siteUrl") or "") or checker.build_site_url(record_id),
                "record_name": str(entry.get("record_name") or ""),
                "record_years": str(entry.get("record_years") or ""),
            }

    associations_changed = False
    unresolved: list[dict[str, str]] = []
    entries: list[dict[str, str]] = []

    coins_with_issues = report.get("coins_with_issues", [])
    if not isinstance(coins_with_issues, list):
        coins_with_issues = []

    for coin in coins_with_issues:
        if not isinstance(coin, dict):
            continue

        issues = coin.get("issues", [])
        if not isinstance(issues, list):
            continue

        has_image_issue = any(
            isinstance(issue, dict) and str(issue.get("field") or "") in {"obverseImage", "reverseImage", "image_frente", "image_verso"}
            for issue in issues
        )
        if not has_image_issue:
            continue

        ucoin_url = normalize_url(str(coin.get("ucoinUrl") or ""))
        site_url = str(coin.get("siteUrl") or "")
        record_id = extract_record_id(site_url)
        denomination = str(coin.get("denomination") or "")
        issue_period = str(coin.get("issuePeriod") or "")

        if not ucoin_url or not record_id:
            continue

        record = by_record_id.get(record_id, {})
        record_name = str(record.get("name") or "")
        record_years = str(record.get("years") or "")
        resolved_source = ""

        assoc = associations.get(ucoin_url)
        if assoc and str(assoc.get("record_id") or "") == record_id:
            resolved_source = "associations_file"

        if not resolved_source:
            from_missing = connected_from_missing_found.get(ucoin_url)
            if from_missing and str(from_missing.get("record_id") or "") == record_id:
                resolved_source = "missing_found_file"
                associations[ucoin_url] = {
                    "record_id": record_id,
                      "api_url": str(from_missing.get("api_url") or "") or checker.build_site_url(record_id),
                    "name": record_name or str(from_missing.get("record_name") or ""),
                    "years": record_years or str(from_missing.get("record_years") or ""),
                }
                associations_changed = True

        if not resolved_source and interactive:
            print("\nEquivalencia pendente:")
            print(f"- moeda: {denomination}")
            print(f"- anos: {issue_period}")
            print(f"- ucoin: {ucoin_url}")
            print(f"- site_record: id={record_id} | name={record_name} | years={record_years}")
            choice = input("Confirmar que e a mesma moeda? [y/N]: ").strip().lower()
            if choice in {"y", "yes", "s", "sim"}:
                resolved_source = "interactive"
                associations[ucoin_url] = {
                    "record_id": record_id,
                      "api_url": checker.build_site_url(record_id),
                    "name": record_name,
                    "years": record_years,
                }
                associations_changed = True

        if resolved_source:
            entries.append(
                {
                    "status": "connected",
                    "source": resolved_source,
                    "denomination": denomination,
                    "issuePeriod": issue_period,
                    "ucoinUrl": ucoin_url,
                    "record_id": record_id,
                    "record_name": record_name,
                    "record_years": record_years,
                    "siteUrl": checker.build_site_url(record_id),
                }
            )
            continue

        # Not confirmed: treat as unresolved and block update for this coin.
        plan_updates[:] = [item for item in plan_updates if str(item.get("record_id") or "") != record_id]
        unresolved.append({"denomination": denomination, "ucoinUrl": ucoin_url})
        entries.append(
            {
                "status": "unresolved",
                "source": "equivalence_not_confirmed",
                "denomination": denomination,
                "issuePeriod": issue_period,
                "ucoinUrl": ucoin_url,
                "record_id": record_id,
                "record_name": record_name,
                "record_years": record_years,
                "siteUrl": checker.build_site_url(record_id),
            }
        )

    if associations_changed:
        save_associations(associations_path, associations, only_ucoin_urls=association_ucoin_filter)
        if associations_path.exists():
            print(f"\nAssociacoes guardadas em: {associations_path}")
        else:
            print(f"\nSem associacoes de imagem; ficheiro removido: {associations_path}")

    return {"unresolved": unresolved, "entries": entries}


def apply_plan(
    args: argparse.Namespace,
    country_name: str,
    plan: dict[str, Any],
    application_results: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    client_args = SimpleNamespace(
        request_delay=args.request_delay,
        rate_limit_delay=args.rate_limit_delay,
        max_retries=args.max_retries,
    )
    client = import_base44_coins.create_client(client_args)

    updated = 0
    created = 0
    skipped = 0
    updates = list(plan.get("updates", []))
    total_updates = len(updates)
    update_started_at = time.monotonic()
    total_item_duration = 0.0

    for index, item in enumerate(updates, start=1):
        item_started_at = time.monotonic()
        operation = "Ignorado"
        operation_detail = ""
        record_id = str(item.get("record_id") or "")
        raw_set_fields = item.get("set", {})
        set_fields = (
            {
                field: value
                for field, value in raw_set_fields.items()
                if field in SAFE_UPDATE_FIELDS
            }
            if isinstance(raw_set_fields, dict)
            else {}
        )

        if not record_id:
            skipped += 1
            operation_detail = "sem id do registo"
        elif not set_fields:
            skipped += 1
            operation_detail = "sem campos selecionados válidos"
        else:
            current = client.request("GET", f"{client.base_url}/{record_id}")
            if not isinstance(current, dict):
                skipped += 1
                operation_detail = "resposta atual inválida da API"
            else:
                expected_current = item.get("current", {})
                changed_since_preview = [
                    field
                    for field in set_fields
                    if isinstance(expected_current, dict)
                    and field in expected_current
                    and str(current.get(field) or "").strip() != str(expected_current.get(field) or "").strip()
                    and str(current.get(field) or "").strip() != str(set_fields[field] or "").strip()
                ]
                if changed_since_preview:
                    skipped += 1
                    operation_detail = f"mudou desde a pré-visualização: {', '.join(changed_since_preview)}"
                else:
                    fields_to_write = {
                        field: value
                        for field, value in set_fields.items()
                        if str(current.get(field) or "").strip() != str(value or "").strip()
                    }
                    if not fields_to_write:
                        skipped += 1
                        operation = "Já correto"
                        operation_detail = ", ".join(set_fields)
                    else:
                        before_preserved = {
                            field: current.get(field)
                            for field in PRESERVED_API_FIELDS - set(fields_to_write)
                            if field in current
                        }
                        payload = dict(current)
                        payload.update(fields_to_write)
                        client.update(record_id, payload)

                        verified = client.request("GET", f"{client.base_url}/{record_id}")
                        if not isinstance(verified, dict):
                            raise RuntimeError(
                                f"Não foi possível verificar a atualização de {item.get('denomination', '')}."
                            )
                        incorrect = [
                            field
                            for field, value in fields_to_write.items()
                            if str(verified.get(field) or "").strip() != str(value or "").strip()
                        ]
                        changed_unselected = [
                            field
                            for field, value in before_preserved.items()
                            if verified.get(field) != value
                        ]
                        if incorrect or changed_unselected:
                            details = []
                            if incorrect:
                                details.append(f"campos não confirmados: {', '.join(incorrect)}")
                            if changed_unselected:
                                details.append(f"campos não selecionados alterados: {', '.join(changed_unselected)}")
                            raise RuntimeError(
                                f"Verificação falhou para {item.get('denomination', '')}: {'; '.join(details)}"
                            )

                        updated += 1
                        operation = "Atualizado"
                        operation_detail = ", ".join(fields_to_write)

        if application_results is not None:
            if operation == "Atualizado":
                result_status = "applied"
            elif operation == "Já correto":
                result_status = "already_correct"
            else:
                result_status = "not_applied"
            application_results.append(
                {
                    "record_id": record_id,
                    "fields": sorted(set_fields),
                    "status": result_status,
                    "message": operation_detail,
                }
            )

        item_duration = time.monotonic() - item_started_at
        total_item_duration += item_duration
        elapsed = time.monotonic() - update_started_at
        remaining = total_updates - index
        estimated_remaining = (total_item_duration / index) * remaining
        remaining_text = (
            "concluído"
            if not remaining
            else f"~{import_base44_coins.format_duration(estimated_remaining)}"
        )
        percentage = f"{(index / total_updates) * 100:.1f}".replace(".", ",")
        item_country = str(item.get("country") or country_name or "País desconhecido")
        issue_period = str(item.get("issuePeriod") or "")
        period_text = f" ({issue_period})" if issue_period else ""
        detail_text = f" — {operation_detail}" if operation_detail else ""
        print(
            f"{operation}: {index}/{total_updates} ({percentage}%) — {item_country} — "
            f"{item.get('denomination', '')}{period_text} "
            f"[faltam: {remaining} | "
            f"demorou nesta: {import_base44_coins.format_duration(item_duration, precise=True)} | "
            f"decorrido: {import_base44_coins.format_duration(elapsed)} | restante: {remaining_text}]"
            f"{detail_text}",
            flush=True,
        )

    creates = list(plan.get("creates", []))
    if creates:
        continent = args.continent or continent_label_for_country(country_name)
        if not continent:
            raise ValueError(f"Missing continent for {country_name}. Pass --continent.")
        options = {
            "country": country_name,
            "continent": continent,
            "condition": args.condition,
        }

        create_started_at = time.monotonic()
        total_create_duration = 0.0
        for index, item in enumerate(creates, start=1):
            item_started_at = time.monotonic()
            entry = (item["period"], item["coin"])
            record = import_base44_coins.to_coin_record(entry, options, int(item.get("ordem", 1)))
            lookup_query = {
                "country": country_name,
                "url_ucoin": str(record.get("url_ucoin") or ""),
            }
            existing = client.filter(
                lookup_query,
                limit=10,
            )
            verified_record = next(
                (
                    candidate
                    for candidate in existing
                    if isinstance(candidate, dict)
                    and normalize_url(str(candidate.get("url_ucoin") or ""))
                    == normalize_url(str(record.get("url_ucoin") or ""))
                ),
                None,
            ) if isinstance(existing, list) else None

            if verified_record is None:
                client.bulk_create([record])
                verification = client.filter(
                    lookup_query,
                    limit=10,
                )
                verified_record = next(
                    (
                        candidate
                        for candidate in verification
                        if isinstance(candidate, dict)
                        and normalize_url(str(candidate.get("url_ucoin") or ""))
                        == normalize_url(str(record.get("url_ucoin") or ""))
                    ),
                    None,
                ) if isinstance(verification, list) else None
                operation = "Criado"
                result_status = "created"
                result_message = "criação confirmada no Site Base44"
                created += 1
            else:
                operation = "Já existe"
                result_status = "already_exists"
                result_message = "registo já existente; criação não executada"
                skipped += 1

            if verified_record is None:
                raise RuntimeError(
                    f"A criação de {item.get('denomination', '')} não foi confirmada no Site Base44."
                )
            if application_results is not None:
                application_results.append(
                    {
                        "record_id": str(verified_record.get("id") or ""),
                        "ucoin_url": str(record.get("url_ucoin") or ""),
                        "fields": ["create"],
                        "status": result_status,
                        "message": result_message,
                    }
                )

            item_duration = time.monotonic() - item_started_at
            total_create_duration += item_duration
            elapsed = time.monotonic() - create_started_at
            remaining = len(creates) - index
            estimated_remaining = (total_create_duration / index) * remaining
            remaining_text = (
                "concluído"
                if not remaining
                else f"~{import_base44_coins.format_duration(estimated_remaining)}"
            )
            percentage = f"{(index / len(creates)) * 100:.1f}".replace(".", ",")
            issue_period = str(item.get("issuePeriod") or "")
            period_text = f" ({issue_period})" if issue_period else ""
            print(
                f"{operation}: {index}/{len(creates)} ({percentage}%) — {country_name} — "
                f"{item.get('denomination', '')}{period_text} "
                f"[faltam: {remaining} | "
                f"demorou nesta: {import_base44_coins.format_duration(item_duration, precise=True)} | "
                f"decorrido: {import_base44_coins.format_duration(elapsed)} | restante: {remaining_text}]",
                flush=True,
            )

    return {"updated": updated, "created": created, "skipped": skipped}


def filter_summary_coins(coins: Any, update_fields: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    allowed_fields = set(update_fields)
    filtered_coins = []
    for coin in coins if isinstance(coins, list) else []:
        if not isinstance(coin, dict):
            continue
        issue_types = [field for field in coin.get("types", []) if field in allowed_fields]
        if issue_types:
            filtered_coins.append({**coin, "issue_count": len(issue_types), "types": issue_types})
    return filtered_coins


def main() -> int:
    args = parse_args()
    country_slug = checker.slugify(args.country)
    paises_dir = Path(args.paises_dir)
    all_coins_dir = Path(args.all_coins_dir)
    country_dir = find_country_directory(paises_dir, country_slug)
    catalog_path = country_dir / args.catalog_file

    report = checker.compare_country(
        paises_dir,
        all_coins_dir,
        country_slug,
        args.catalog_file,
        include_markdown_as_issue=False,
        check_api=True,
        include_warnings=False,
    )

    summary = report.get("summary", {})
    summary_coins = filter_summary_coins(summary.get("coins", []), args.update_fields)
    print(f"\n[{country_slug}]")
    for coin in summary_coins:
        issue_count = int(coin.get("issue_count", 0))
        issue_label = "issue" if issue_count == 1 else "issues"
        print(f"- {coin.get('coin', '')}: {issue_count} {issue_label} -> {', '.join(coin.get('types', []))}")

    catalog_index, catalog_data = load_catalog_index(catalog_path)
    country_name = str(catalog_data.get("country") or country_slug)

    full_plan = build_fix_plan(report, catalog_index)
    plan_updates = list(full_plan.get("updates", []))

    associations_path = Path(args.associations_file) if args.associations_file else default_associations_path(country_dir, country_slug)
    missing_found_path = default_missing_found_path(country_dir, country_slug)
    name_decisions_path = default_name_match_decisions_path(country_dir, country_slug)
    association_ucoin_filter = collect_image_issue_ucoin_urls(report)

    if args.skip_create_missing:
        reconcile_result = reconcile_missing(
            country_name=country_name,
            missing_creates=list(full_plan.get("creates", [])),
            plan_updates=plan_updates,
            associations_path=associations_path,
            missing_found_path=missing_found_path,
            interactive=args.reconcile_missing_interactive,
            max_candidates=args.max_match_candidates,
            association_ucoin_filter=association_ucoin_filter,
            name_decisions_path=name_decisions_path,
            decision_country_slug=country_slug,
        )
        missing_entries = list(reconcile_result.get("unresolved", []))
        missing_found_entries = list(reconcile_result.get("all", []))
        plan_creates = list(reconcile_result.get("creates", []))
    else:
        plan_creates = list(full_plan.get("creates", []))
        missing_entries = [
            {
                "denomination": item.get("denomination", ""),
                "ucoinUrl": item.get("ucoinUrl", ""),
            }
            for item in plan_creates
        ]
        missing_found_entries = [
            {
                "status": "pending_create",
                "denomination": item.get("denomination", ""),
                "issuePeriod": str(item.get("coin", {}).get("issuePeriod") or ""),
                "ucoinUrl": item.get("ucoinUrl", ""),
            }
            for item in plan_creates
        ]


    filtered_updates = []
    for item in plan_updates:
        set_fields = {
            field: value
            for field, value in item.get("set", {}).items()
            if field in args.update_fields
        }
        if set_fields:
            filtered_updates.append({**item, "set": set_fields})

    plan = {
        "updates": filtered_updates,
        "creates": plan_creates,
    }

    if plan_creates:
        print("\nConfirmadas como inexistentes no Site Base44 — prontas para revisão final:")
        for item in plan_creates:
            print(
                f"- {item.get('denomination', '')} "
                f"({item.get('issuePeriod', '')}): {item.get('ucoinUrl', '')}"
            )
    if missing_entries:
        print("\nSem associação e sem criação autorizada:")
        for item in missing_entries:
            print(f"- {item.get('denomination', '')}: {item.get('ucoinUrl', '')}")
    elif not plan_creates:
        print("\nSem moedas pendentes de associação ou criação.")

    save_missing_found(
        missing_found_path,
        country_slug,
        missing_found_entries,
        exclude_ucoin_urls=set(report.get("image_matched_ucoin_urls", [])),
    )
    print(f"Missing guardados em: {missing_found_path}")
    if name_decisions_path.exists():
        print(f"Decisões de nomes guardadas em: {name_decisions_path}")

    plan_summary = {
        "country": country_slug,
        "summary": {
            "updates": len(plan.get("updates", [])),
            "creates": len(plan.get("creates", [])),
            "missing": len(missing_entries),
            "connected_missing": sum(1 for entry in missing_found_entries if str(entry.get("status") or "") == "connected"),
            "total_issues": sum(int(coin.get("issue_count", 0)) for coin in summary_coins),
        },
        "coins": summary_coins,
        "plan": {
            "updates": [
                {
                    "denomination": item.get("denomination", ""),
                    "siteUrl": item.get("siteUrl", ""),
                    "set": item.get("set", {}),
                }
                for item in plan.get("updates", [])
            ],
            "creates": [
                {
                    "denomination": item.get("denomination", ""),
                    "ucoinUrl": item.get("ucoinUrl", ""),
                }
                for item in plan.get("creates", [])
            ],
        },
        "missing_entries": missing_entries,
        "associationsFile": str(associations_path),
        "missingFoundFile": str(missing_found_path),
        "nameMatchDecisionsFile": str(name_decisions_path),
    }

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = country_dir / f"{country_slug}-autofix-plan.json"

    has_any_action = bool(plan.get("updates")) or bool(plan.get("creates")) or bool(missing_entries)
    if has_any_action:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(plan_summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved {output_path}")
    elif output_path.exists():
        output_path.unlink()
        print(f"No autofix actions found. Removed stale file: {output_path}")
    else:
        print("No autofix actions found. Plan file was not created.")

    if not args.apply:
        print("Dry-run only. Use --apply to write changes to API.")
        return 0

    if not has_any_action:
        print("Nothing to apply.")
        return 0

    metadata_updates = [
        item
        for item in plan.get("updates", [])
        if isinstance(item.get("set"), dict)
        and {"name", "years"}.intersection(item.get("set", {}))
    ]
    if metadata_updates and args.reconcile_missing_interactive:
        print("\nAlterações confirmadas de nome/anos:")
        change_count = 0
        for item in metadata_updates:
            current_fields = item.get("current", {})
            proposed_fields = item.get("set", {})
            print(f"- {item.get('denomination', '')} — {item.get('siteUrl', '')}")
            for field, label in (("name", "Nome"), ("years", "Anos")):
                if field not in proposed_fields:
                    continue
                change_count += 1
                current_value = str(current_fields.get(field) or "(vazio)")
                proposed_value = str(proposed_fields.get(field) or "(vazio)")
                print(f"  - {label}: {current_value} → {proposed_value}")
        change_label = "alteração" if change_count == 1 else "alterações"
        coin_label = "moeda" if len(metadata_updates) == 1 else "moedas"
        try:
            apply_choice = input(
                f"Aplicar exatamente {change_count} {change_label} em "
                f"{len(metadata_updates)} {coin_label} no Site Base44? [s/N]: "
            ).strip().lower()
        except EOFError:
            apply_choice = ""
        if apply_choice not in {"y", "yes", "s", "sim"}:
            cancelled_results = [
                {
                    "record_id": str(item.get("record_id") or ""),
                    "fields": sorted({"name", "years"}.intersection(item.get("set", {}))),
                    "status": "approved_not_applied",
                    "message": "aplicação final cancelada",
                }
                for item in metadata_updates
            ]
            update_name_match_application_status(name_decisions_path, cancelled_results)
            cancelled_record_ids = {
                str(item.get("record_id") or "")
                for item in metadata_updates
            }
            plan["updates"] = [
                item
                for item in plan.get("updates", [])
                if str(item.get("record_id") or "") not in cancelled_record_ids
            ]
            print("Alterações de nome/anos canceladas. As decisões ficaram guardadas no JSON.")

    confirmed_creates = list(plan.get("creates", []))
    if confirmed_creates and args.reconcile_missing_interactive:
        continent = args.continent or continent_label_for_country(country_name)
        if not continent:
            raise ValueError(f"Missing continent for {country_name}. Pass --continent.")
        options = {
            "country": country_name,
            "continent": continent,
            "condition": args.condition,
        }
        print("\nMoedas confirmadas para criar no Site Base44:")
        for item in confirmed_creates:
            record = import_base44_coins.to_coin_record(
                (item["period"], item["coin"]),
                options,
                int(item.get("ordem", 1)),
            )
            photos = []
            if record.get("image_frente"):
                photos.append("frente")
            if record.get("image_verso"):
                photos.append("verso")
            photo_text = " e ".join(photos) if photos else "nenhuma"
            print(f"- {record.get('name', '')} ({record.get('years', '')})")
            print(f"  - Raridade: {record.get('rarity', '')}")
            print(f"  - Fotografias: {photo_text}")
            print(f"  - uCoin: {record.get('url_ucoin', '')}")
        create_label = "moeda" if len(confirmed_creates) == 1 else "moedas"
        try:
            create_choice = input(
                f"Criar exatamente {len(confirmed_creates)} {create_label} "
                "no Site Base44? [s/N]: "
            ).strip().lower()
        except EOFError:
            create_choice = ""
        if create_choice not in {"y", "yes", "s", "sim"}:
            cancelled_results = [
                {
                    "record_id": "",
                    "ucoin_url": str(item.get("ucoinUrl") or ""),
                    "fields": ["create"],
                    "status": "approved_not_applied",
                    "message": "criação final cancelada",
                }
                for item in confirmed_creates
            ]
            update_creation_application_status(name_decisions_path, cancelled_results)
            plan["creates"] = []
            print("Criação cancelada. A decisão ficou guardada no JSON.")

    if not plan.get("updates") and not plan.get("creates"):
        print("Nenhuma alteração autorizada para aplicar.")
        return 0

    application_results: list[dict[str, Any]] = []
    results = apply_plan(args, country_name, plan, application_results)
    update_name_match_application_status(name_decisions_path, application_results)
    update_creation_application_status(name_decisions_path, application_results)
    confirm_applied_name_matches(missing_found_path, application_results)
    confirm_created_matches(missing_found_path, application_results)
    print(f"Applied fixes: updated={results['updated']} created={results['created']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
