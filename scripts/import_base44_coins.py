#!/usr/bin/env python3
"""Import app-catalog.json coins into Base44 Coin records."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from scripts.admin_stats_sync import MutationRun, mark_numisvault_stats_stale
from scripts.catalog_paths import continent_label_for_country


AVAILABILITY_TO_RARITY = {
    "circulating": "Circulante",
    "scarce": "Escassa",
    "withdrawn": "Retirada",
    "historical": "Histórica",
}

COUNTRY_TO_CONTINENT = {
    "Canada": "América",
    "Canadá": "América",
    "India": "Ásia",
    "Índia": "Ásia",
}

VALID_CONTINENTS = {"Europa", "América", "Ásia", "África", "Oceânia"}
BULK_SIZE = 100
DEFAULT_BASE44_URL = "https://base44.app"
DEFAULT_REQUEST_DELAY_SECONDS = 1.5
DEFAULT_RATE_LIMIT_DELAY_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import app-catalog.json into Base44 Coin records.")
    parser.add_argument("--input", required=True, help="Path to app-catalog.json.")
    parser.add_argument("--country", default="", help="Override country from the JSON.")
    parser.add_argument("--continent", default="", help="Europa|América|Ásia|África|Oceânia.")
    parser.add_argument("--condition", default="Não Tenho", help="Default Coin.condition value.")
    parser.add_argument("--dry-run", action="store_true", help="Convert and validate locally without writing.")
    parser.add_argument("--create-only", action="store_true", help="Create records without deleting anything first.")
    parser.add_argument("--replace", action="store_true", help="Delete existing Coin records for the country, then recreate.")
    parser.add_argument("--missing-only", action="store_true", help="With --create-only, only create records whose country/url_ucoin is missing.")
    parser.add_argument(
        "--allow-duplicates",
        action="store_true",
        help="Disabled legacy option: normal Coin imports always protect country + url_ucoin identity.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Import only the first N records.")
    parser.add_argument("--batch-size", type=int, default=10, help="Records per bulk create request.")
    parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS, help="Seconds to pause after each Base44 request.")
    parser.add_argument("--rate-limit-delay", type=float, default=DEFAULT_RATE_LIMIT_DELAY_SECONDS, help="Seconds to wait after HTTP 429.")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES, help="Maximum retries for HTTP 429 and temporary server errors.")
    return parser.parse_args()


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def read_json(path: str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Input must contain a JSON object")
    return payload


def flatten_coins(catalogue: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    entries: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for period in catalogue.get("periods", []):
        if not isinstance(period, dict):
            continue
        for coin in period.get("coins", []):
            if isinstance(coin, dict):
                entries.append((period, coin))
    return entries


def build_name(coin: dict[str, Any]) -> str:
    return str(coin.get("denomination") or "Moeda")


def build_notes(period: dict[str, Any], coin: dict[str, Any] | None = None) -> str:
    if coin is not None:
        notes = coin.get("notes")
        if isinstance(notes, str) and notes.strip():
            return notes.strip()
    ruler = period.get("ruler")
    if isinstance(ruler, str) and ruler:
        return ruler
    title = period.get("title") or period.get("fullTitle")
    if not isinstance(title, str) or not title:
        return ""
    parts = [part.strip() for part in title.split("›") if part.strip()]
    return parts[1] if len(parts) >= 2 else title


def resolve_options(args: argparse.Namespace, catalogue: dict[str, Any]) -> dict[str, str]:
    country = args.country or catalogue.get("country")
    if not isinstance(country, str) or not country:
        raise ValueError("Country missing in JSON. Pass --country.")
    continent = args.continent or COUNTRY_TO_CONTINENT.get(country, "") or continent_label_for_country(country)
    if continent not in VALID_CONTINENTS:
        raise ValueError(f"Invalid or missing continent for {country}. Pass --continent Europa|América|Ásia|África|Oceânia.")
    return {"country": country, "continent": continent, "condition": args.condition}


def to_coin_record(entry: tuple[dict[str, Any], dict[str, Any]], options: dict[str, str], ordem: int) -> dict[str, Any]:
    period, coin = entry
    availability = coin.get("availability")
    rarity = AVAILABILITY_TO_RARITY.get(str(availability))
    if rarity is None:
        raise ValueError(f"Unsupported availability value: {availability}")
    detail_url = coin.get("detailUrl")
    stored_order = coin.get("ordem")
    return {
        "name": build_name(coin),
        "country": options["country"],
        "continent": options["continent"],
        "years": coin.get("issuePeriod") or "",
        "condition": options["condition"],
        "rarity": rarity,
        "has_variants": False,
        "image_frente": coin.get("obverseImage") or "",
        "image_verso": coin.get("reverseImage") or "",
        "url_ucoin": detail_url or "",
        "url_numista": "",
        "notes": build_notes(period, coin),
        "ordem": stored_order if isinstance(stored_order, int) else ordem,
    }


def chunk(records: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [records[index : index + size] for index in range(0, len(records), size)]


class Base44Client:
    def __init__(
        self,
        app_id: str,
        api_key: str,
        server_url: str = DEFAULT_BASE44_URL,
        request_delay_seconds: float = DEFAULT_REQUEST_DELAY_SECONDS,
        rate_limit_delay_seconds: float = DEFAULT_RATE_LIMIT_DELAY_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        entity_name: str = "Coin",
    ) -> None:
        self.app_id = app_id
        self.api_key = api_key
        self.server_url = server_url.rstrip("/")
        self.entity_name = entity_name
        self.base_url = (
            f"{self.server_url}/api/apps/{quote(app_id)}/entities/{quote(entity_name)}"
        )
        self.request_delay_seconds = max(0.0, request_delay_seconds)
        self.rate_limit_delay_seconds = max(0.0, rate_limit_delay_seconds)
        self.max_retries = max(0, max_retries)

    def for_entity(self, entity_name: str) -> "Base44Client":
        """Return a client for another entity with identical auth/retry settings."""

        return Base44Client(
            app_id=self.app_id,
            api_key=self.api_key,
            server_url=self.server_url,
            request_delay_seconds=self.request_delay_seconds,
            rate_limit_delay_seconds=self.rate_limit_delay_seconds,
            max_retries=self.max_retries,
            entity_name=entity_name,
        )

    def request(self, method: str, url: str, payload: Any | None = None) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-App-Id": self.app_id,
                "api_key": self.api_key,
            },
        )
        for attempt in range(self.max_retries + 1):
            try:
                with urlopen(request, timeout=60) as response:
                    text = response.read().decode("utf-8")
                if self.request_delay_seconds:
                    time.sleep(self.request_delay_seconds)
                return json.loads(text) if text else None
            except HTTPError as exc:
                error_text = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 and attempt < self.max_retries:
                    delay = retry_after_seconds(exc) or self.rate_limit_delay_seconds
                    print(f"Rate limit from Base44. Waiting {delay:.1f}s before retry {attempt + 1}/{self.max_retries}...")
                    time.sleep(delay)
                    continue
                if 500 <= exc.code <= 599 and attempt < self.max_retries:
                    delay = min(self.rate_limit_delay_seconds, 2.0 * (attempt + 1))
                    print(f"Temporary Base44 error HTTP {exc.code}. Waiting {delay:.1f}s before retry {attempt + 1}/{self.max_retries}...")
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"Base44 request failed: HTTP {exc.code} {error_text}") from exc
            except URLError as exc:
                if attempt < self.max_retries:
                    delay = min(self.rate_limit_delay_seconds, 2.0 * (attempt + 1))
                    print(f"Base44 network error. Waiting {delay:.1f}s before retry {attempt + 1}/{self.max_retries}...")
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"Base44 request failed: {exc.reason}") from exc
        raise RuntimeError("Base44 request failed after retries")

    def filter(self, query: dict[str, Any], limit: int = 1, skip: int = 0) -> Any:
        params = urlencode({"q": json.dumps(query, ensure_ascii=False), "limit": str(limit), "skip": str(skip)})
        return self.request("GET", f"{self.base_url}?{params}")

    def delete_many(self, query: dict[str, Any]) -> Any:
        return self.request("DELETE", self.base_url, query)

    def bulk_create(self, records: list[dict[str, Any]]) -> Any:
        return self.request("POST", f"{self.base_url}/bulk", records)

    def update(self, record_id: str, record: dict[str, Any]) -> Any:
        return self.request("PUT", f"{self.base_url}/{quote(record_id)}", record)


def retry_after_seconds(exc: HTTPError) -> float | None:
    retry_after = exc.headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        return max(0.0, float(retry_after))
    except ValueError:
        return None


def create_client(args: argparse.Namespace | None = None) -> Base44Client:
    load_dotenv()
    app_id = os.environ.get("BASE44_APP_ID", "")
    api_key = os.environ.get("BASE44_API_KEY", "")
    server_url = os.environ.get("BASE44_SERVER_URL", DEFAULT_BASE44_URL)
    if not app_id or not api_key:
        raise ValueError("Missing BASE44_APP_ID or BASE44_API_KEY in environment/.env")
    return Base44Client(
        app_id=app_id,
        api_key=api_key,
        server_url=server_url,
        request_delay_seconds=args.request_delay if args is not None else DEFAULT_REQUEST_DELAY_SECONDS,
        rate_limit_delay_seconds=args.rate_limit_delay if args is not None else DEFAULT_RATE_LIMIT_DELAY_SECONDS,
        max_retries=args.max_retries if args is not None else DEFAULT_MAX_RETRIES,
    )


def existing_records_for_country(client: Base44Client, country: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    page_size = 1000
    skip = 0
    while True:
        result = client.filter({"country": country}, limit=page_size, skip=skip)
        if not isinstance(result, list):
            raise RuntimeError(f"Unexpected Base44 list response: {result!r}")
        records.extend(record for record in result if isinstance(record, dict))
        if len(result) < page_size:
            return records
        skip += page_size


@dataclass
class NormalCoinImportPlan:
    """Complete, mutually-exclusive classification for a normal Coin import."""

    prepared_count: int
    to_create: list[dict[str, Any]] = field(default_factory=list)
    already_existing: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    missing_ucoin_url: list[dict[str, Any]] = field(default_factory=list)
    multiple_existing_matches: list[
        tuple[dict[str, Any], list[dict[str, Any]]]
    ] = field(default_factory=list)
    duplicate_input: list[dict[str, Any]] = field(default_factory=list)


def required_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def normal_coin_identity(record: dict[str, Any]) -> tuple[str, str]:
    """Return the only identity accepted by the normal Coin creation flow."""

    return required_text(record.get("country")), required_text(record.get("url_ucoin"))


def existing_records_by_identity(
    records: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    lookup: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        identity = normal_coin_identity(record)
        if not identity[0] or not identity[1]:
            continue
        lookup.setdefault(identity, []).append(record)
    return lookup


def classify_normal_coin_records(
    records: list[dict[str, Any]],
    existing_records: list[dict[str, Any]],
) -> NormalCoinImportPlan:
    """Classify every input before any Base44 mutation is attempted."""

    plan = NormalCoinImportPlan(prepared_count=len(records))
    existing_by_identity = existing_records_by_identity(existing_records)
    seen_input_identities: set[tuple[str, str]] = set()

    for record in records:
        identity = normal_coin_identity(record)
        if not identity[1]:
            plan.missing_ucoin_url.append(record)
            continue
        if identity in seen_input_identities:
            plan.duplicate_input.append(record)
            continue
        seen_input_identities.add(identity)

        matches = existing_by_identity.get(identity, [])
        if not matches:
            plan.to_create.append(record)
        elif len(matches) == 1:
            plan.already_existing.append((record, matches[0]))
        else:
            plan.multiple_existing_matches.append((record, matches))

    return plan


def format_duration(seconds: float, *, precise: bool = False) -> str:
    """Format a duration for the import progress output."""
    seconds = max(0.0, seconds)
    if precise and seconds < 60:
        return f"{seconds:.1f}".replace(".", ",") + " s"

    total_seconds = round(seconds)
    minutes, remaining_seconds = divmod(total_seconds, 60)
    hours, remaining_minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {remaining_minutes:02d}m {remaining_seconds:02d}s"
    if minutes:
        return f"{minutes}m {remaining_seconds:02d}s"
    return f"{remaining_seconds}s"


def print_pre_write_summary(plan: NormalCoinImportPlan) -> None:
    print("\nNormal Coins prepared: %d" % plan.prepared_count)
    print(f"To create: {len(plan.to_create)}")
    print(f"Already exist: {len(plan.already_existing)}")
    print(f"Missing uCoin URL: {len(plan.missing_ucoin_url)}")
    print(f"Multiple Base44 matches: {len(plan.multiple_existing_matches)}")
    print(f"Duplicate input entries: {len(plan.duplicate_input)}")


def coin_report_line(record: dict[str, Any], *, include_url: bool = True) -> str:
    parts = [
        required_text(record.get("country")) or "(country missing)",
        required_text(record.get("name")) or "(name missing)",
        required_text(record.get("years")) or "(years missing)",
    ]
    if include_url:
        parts.append(required_text(record.get("url_ucoin")) or "(uCoin URL missing)")
    return " | ".join(parts)


def print_coin_section(
    title: str,
    records: list[dict[str, Any]],
    *,
    include_url: bool = True,
) -> None:
    print(f"\n{title} ({len(records)})")
    if not records:
        print("(none)")
        return
    for record in records:
        print(coin_report_line(record, include_url=include_url))


def print_import_report(
    plan: NormalCoinImportPlan,
    created: list[dict[str, Any]],
    failed_creates: list[dict[str, Any]],
) -> None:
    print_coin_section("CREATED", created)
    print_coin_section(
        "ALREADY EXISTED — NOT MODIFIED",
        [record for record, _existing in plan.already_existing],
    )
    print_coin_section(
        "MISSING UCOIN URL — NOT CREATED",
        plan.missing_ucoin_url,
        include_url=False,
    )

    print(
        "\nMULTIPLE EXISTING MATCHES — MANUAL REVIEW "
        f"({len(plan.multiple_existing_matches)})"
    )
    if not plan.multiple_existing_matches:
        print("(none)")
    else:
        for record, matches in plan.multiple_existing_matches:
            print(coin_report_line(record))
            for match in matches:
                match_id = required_text(match.get("id")) or "(ID missing)"
                match_name = required_text(match.get("name")) or "(name missing)"
                match_years = required_text(match.get("years")) or "(years missing)"
                print(
                    f"  - Base44 ID: {match_id} | Name: {match_name} | "
                    f"Years: {match_years}"
                )

    print_coin_section(
        "DUPLICATE INPUT — NOT CREATED AGAIN",
        plan.duplicate_input,
    )
    print_coin_section("FAILED CREATES", failed_creates)

    print("\nFINAL SUMMARY")
    print(f"Created: {len(created)}")
    print(f"Already existed — untouched: {len(plan.already_existing)}")
    print(f"Missing uCoin URL: {len(plan.missing_ucoin_url)}")
    print(f"Multiple existing matches: {len(plan.multiple_existing_matches)}")
    print(f"Duplicate input entries: {len(plan.duplicate_input)}")
    print(f"Failed creates: {len(failed_creates)}")


def selected_country(records: list[dict[str, Any]], country: str = "") -> str:
    selected = required_text(country)
    record_countries = {
        required_text(record.get("country"))
        for record in records
        if required_text(record.get("country"))
    }
    if selected and any(record_country != selected for record_country in record_countries):
        raise ValueError("Normal Coin input contains records outside the selected country.")
    if selected:
        return selected
    if len(record_countries) > 1:
        raise ValueError("Normal Coin creation accepts one country per import.")
    return next(iter(record_countries), "")


def create_missing_normal_coins(
    client: Base44Client,
    records: list[dict[str, Any]],
    *,
    country: str = "",
    batch_size: int = 1,
) -> int:
    """Create only normal Coins absent by exact country + url_ucoin identity."""

    country = selected_country(records, country)
    existing = existing_records_for_country(client, country) if country else []
    plan = classify_normal_coin_records(records, existing)
    print_pre_write_summary(plan)

    created: list[dict[str, Any]] = []
    failed_creates: list[dict[str, Any]] = []
    import_started_at = time.monotonic()
    total_request_duration = 0.0
    batches = chunk(plan.to_create, max(1, batch_size))

    try:
        with MutationRun(
            lambda: mark_numisvault_stats_stale(client), mutation_label="Coin record"
        ) as mutations:
            for batch in batches:
                request_started_at = time.monotonic()
                client.bulk_create(batch)
                request_duration = time.monotonic() - request_started_at
                total_request_duration += request_duration
                mutations.record_success(len(batch))

                successful_after_batch = len(created) + len(batch)
                for record in batch:
                    created.append(record)
                    completed = len(created)
                    remaining = len(plan.to_create) - completed
                    estimated_remaining = (
                        (total_request_duration / successful_after_batch) * remaining
                        if successful_after_batch
                        else 0.0
                    )
                    remaining_text = (
                        "concluído"
                        if not remaining
                        else f"~{format_duration(estimated_remaining)}"
                    )
                    print(
                        f"Created: {completed}/{len(plan.to_create)} — "
                        f"{required_text(record.get('country'))} — "
                        f"{required_text(record.get('name'))} "
                        f"[demorou neste pedido: "
                        f"{format_duration(request_duration, precise=True)} | "
                        f"decorrido: {format_duration(time.monotonic() - import_started_at)} "
                        f"| restante: {remaining_text}]"
                    )
    except Exception:
        failed_creates = plan.to_create[len(created) :]
        print_import_report(plan, created, failed_creates)
        raise

    print_import_report(plan, created, failed_creates)
    return len(created)


def create_only(
    client: Base44Client,
    records: list[dict[str, Any]],
    allow_duplicates: bool = False,
    country: str = "",
) -> int:
    if allow_duplicates:
        raise ValueError(
            "--allow-duplicates is disabled for normal Coin creation; "
            "country + url_ucoin protection cannot be bypassed."
        )
    return create_missing_normal_coins(
        client,
        records,
        country=country,
        batch_size=1,
    )


def create_missing_only(
    client: Base44Client,
    records: list[dict[str, Any]],
    country: str,
    batch_size: int,
) -> int:
    return create_missing_normal_coins(
        client,
        records,
        country=country,
        batch_size=batch_size,
    )


def replace_country(client: Base44Client, records: list[dict[str, Any]], country: str) -> int:
    created = 0
    with MutationRun(
        lambda: mark_numisvault_stats_stale(client), mutation_label="Coin operation"
    ) as mutations:
        print(f"Deleting existing Coin records for {country}...")
        client.delete_many({"country": country})
        mutations.record_success()
        for batch in chunk(records, BULK_SIZE):
            client.bulk_create(batch)
            mutations.record_success(len(batch))
            created += len(batch)
            print(f"Created {len(batch)} records")
    return created


def main() -> int:
    args = parse_args()
    if args.replace and args.create_only:
        raise ValueError("Use only one of --replace or --create-only")
    if args.allow_duplicates:
        raise ValueError(
            "--allow-duplicates is disabled for normal Coin creation; "
            "country + url_ucoin protection cannot be bypassed."
        )
    if not args.replace and not args.create_only:
        args.dry_run = True
    catalogue = read_json(args.input)
    options = resolve_options(args, catalogue)
    entries = flatten_coins(catalogue)
    if args.limit > 0:
        entries = entries[: args.limit]
    records = [to_coin_record(entry, options, index + 1) for index, entry in enumerate(entries)]
    print(f"Prepared {len(records)} Coin records for {options['country']} ({options['continent']}).")
    if records:
        print(json.dumps(records[0], ensure_ascii=False, indent=2))
    if args.dry_run:
        print("Dry-run only. Nothing was written to Base44.")
        return 0
    client = create_client(args)
    if args.create_only:
        if args.missing_only:
            created = create_missing_only(client, records, options["country"], args.batch_size)
        else:
            created = create_only(
                client,
                records,
                args.allow_duplicates,
                country=options["country"],
            )
    else:
        created = replace_country(client, records, options["country"])
    print(f"Base44 import complete. Created {created} records.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
