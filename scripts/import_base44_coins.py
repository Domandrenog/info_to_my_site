#!/usr/bin/env python3
"""Import app-catalog.json coins into Base44 Coin records."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


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
    parser.add_argument("--allow-duplicates", action="store_true", help="With --create-only, do not skip existing country/name records.")
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


def build_notes(period: dict[str, Any]) -> str:
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
    continent = args.continent or COUNTRY_TO_CONTINENT.get(country, "")
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
        "notes": build_notes(period),
        "ordem": ordem,
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
    ) -> None:
        self.app_id = app_id
        self.api_key = api_key
        self.base_url = f"{server_url.rstrip('/')}/api/apps/{quote(app_id)}/entities/Coin"
        self.request_delay_seconds = max(0.0, request_delay_seconds)
        self.rate_limit_delay_seconds = max(0.0, rate_limit_delay_seconds)
        self.max_retries = max(0, max_retries)

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


def existing_url_set(records: list[dict[str, Any]]) -> set[str]:
    return {record["url_ucoin"] for record in records if isinstance(record.get("url_ucoin"), str) and record["url_ucoin"]}


def matching_existing_record(client: Base44Client, record: dict[str, Any]) -> dict[str, Any] | None:
    if record.get("url_ucoin"):
        result = client.filter({"country": record["country"], "url_ucoin": record["url_ucoin"]}, limit=1)
        if isinstance(result, list) and result:
            return result[0]
        return None
    result = client.filter({"country": record["country"], "name": record["name"]}, limit=1)
    if isinstance(result, list) and result:
        return result[0]
    return None


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


def create_only(client: Base44Client, records: list[dict[str, Any]], allow_duplicates: bool) -> int:
    created = 0
    updated = 0
    total = len(records)
    import_started_at = time.monotonic()
    total_record_duration = 0.0
    for index, record in enumerate(records, start=1):
        progress = f"{index}/{total}"
        record_started_at = time.monotonic()
        operation = "Created"
        if not allow_duplicates:
            existing = matching_existing_record(client, record)
            if existing is not None:
                record_id = existing.get("id")
                if isinstance(record_id, str) and record_id:
                    client.update(record_id, record)
                    updated += 1
                    operation = "Updated"
                else:
                    operation = "Skipped existing without id"
            else:
                client.bulk_create([record])
                created += 1
        else:
            client.bulk_create([record])
            created += 1

        record_duration = time.monotonic() - record_started_at
        total_record_duration += record_duration
        elapsed = time.monotonic() - import_started_at
        remaining = total - index
        estimated_remaining = (total_record_duration / index) * remaining
        remaining_text = "concluído" if not remaining else f"~{format_duration(estimated_remaining)}"
        print(
            f"{operation}: {progress} — {record['name']} "
            f"[demorou nesta: {format_duration(record_duration, precise=True)} "
            f"| decorrido: {format_duration(elapsed)} | restante: {remaining_text}]"
        )
    print(f"Create-only summary: created={created}, updated={updated}")
    return created + updated


def create_missing_only(client: Base44Client, records: list[dict[str, Any]], country: str, batch_size: int) -> int:
    existing_urls = existing_url_set(existing_records_for_country(client, country))
    missing = [record for record in records if not record.get("url_ucoin") or record["url_ucoin"] not in existing_urls]
    if not missing:
        print("Missing-only summary: created=0, already_present=%d" % len(records))
        return 0
    created = 0
    for batch in chunk(missing, max(1, batch_size)):
        client.bulk_create(batch)
        created += len(batch)
        print(f"Created {len(batch)} missing records")
    print(f"Missing-only summary: created={created}, already_present={len(records) - created}")
    return created


def replace_country(client: Base44Client, records: list[dict[str, Any]], country: str) -> int:
    print(f"Deleting existing Coin records for {country}...")
    client.delete_many({"country": country})
    created = 0
    for batch in chunk(records, BULK_SIZE):
        client.bulk_create(batch)
        created += len(batch)
        print(f"Created {len(batch)} records")
    return created


def main() -> int:
    args = parse_args()
    if args.replace and args.create_only:
        raise ValueError("Use only one of --replace or --create-only")
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
            created = create_only(client, records, args.allow_duplicates)
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
