#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse, urlunparse

PENDING_AVAILABILITY = "still needed to calculate"
COIN_AVAILABILITY_ORDER = ("circulating", "scarce", "withdrawn", "historical")
COIN_AVAILABILITIES = set(COIN_AVAILABILITY_ORDER)
RESUME_AVAILABILITIES = COIN_AVAILABILITIES | {PENDING_AVAILABILITY}
CONFIDENCE_VALUES = {"high", "medium", "low"}
SOURCE_TYPES = {
    "central-bank",
    "mint",
    "government",
    "legislation",
    "numismatic-institution",
    "catalogue",
    "specialist-source",
}
AVAILABILITY_RESEARCH_MAX_AGE_DAYS = 180
RESEARCH_CONCURRENCY = 2
MAX_RETRIES = 3
BASE_RETRY_DELAY_MS = 2000
HISTORICAL_PERIOD_PATTERNS = [
    "Domínio de Terra Nova",
    "Províncias do Canadá",
    "Baixo Canadá",
    "Canadá Superior",
    "Província do Canadá",
]
UCOIN_CATALOG_FILENAME = "ucoin-catalog.json"
PENDING_APP_CATALOG_FILENAME = "app-catalog-pending.json"
FINAL_APP_CATALOG_INPUT_FILENAME = "app-catalog-final.json"
APP_CATALOG_FILENAME = "app-catalog.json"
AVAILABILITY_STATISTICS_FILENAME = "availability-statistics.json"
COINS_AVAILABILITY_EXCEL_FILENAME = "coins-availability.xlsx"
AVAILABILITY_RESEARCH_FILENAME = "availability-research.json"
AVAILABILITY_RESEARCH_ERRORS_FILENAME = "availability-research-errors.json"
PENDING_DIFFERENCES_FILENAME = "differences-pending.json"


class ResearchFailure(Exception):
    pass


class TemporaryResearchFailure(ResearchFailure):
    pass


class AvailabilityResearchProvider(Protocol):
    provider_name: str

    async def research_coin(self, input_value: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def first_env_value(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return default


def load_dotenv(path: str = ".env") -> None:
    target = Path(path)
    if not target.exists():
        return
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ[key] = value


def normalize_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9À-ÿ]+", "-", text, flags=re.IGNORECASE)
    return text.strip("-")


def normalize_detail_url(value: str) -> str:
    raw_value = str(value or "").strip()
    markdown_match = re.fullmatch(r"\[[^\]]+\]\((https?://[^)]+)\)", raw_value, flags=re.IGNORECASE)
    if markdown_match:
        raw_value = markdown_match.group(1).strip()
    parsed = urlparse(raw_value)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid detailUrl: {value}")
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, ""))


def normalize_image_url(value: str) -> str:
    raw_value = str(value or "").strip()
    # Some valid uCoin entries have no image yet.  Keep those records in the
    # app catalogue with an empty image field instead of aborting the country.
    if not raw_value:
        return ""
    markdown_match = re.fullmatch(r"\[[^\]]+\]\((https?://[^)]+)\)", raw_value, flags=re.IGNORECASE)
    if markdown_match:
        raw_value = markdown_match.group(1).strip()
    parsed = urlparse(raw_value)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid image URL: {value}")
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, ""))


def build_denomination_research_key(input_value: dict[str, Any]) -> str:
    return "|".join(
        [
            normalize_text(input_value.get("country")),
            normalize_text(input_value.get("historicalPeriod")),
            normalize_text(input_value.get("denomination")),
        ]
    )


def build_coin_type_research_key(input_value: dict[str, Any]) -> str:
    return normalize_detail_url(input_value["detailUrl"])


def calculate_availability(result: dict[str, Any]) -> str:
    if result.get("belongsToHistoricalSystem") is True:
        return "historical"
    if result.get("officiallyWithdrawn") is True:
        return "withdrawn"
    if result.get("reliablyScarce") is True:
        return "scarce"
    return "circulating"


def is_configured_historical_period(period: dict[str, Any]) -> bool:
    full_title = normalize_text(period.get("fullTitle") or period.get("title"))
    return any(normalize_text(pattern) in full_title for pattern in HISTORICAL_PERIOD_PATTERNS)


def get_fallback_availability(period: dict[str, Any]) -> str:
    if is_configured_historical_period(period):
        return "historical"
    return "circulating"


def fallback_research_result(period: dict[str, Any], message: str) -> dict[str, Any]:
    availability = get_fallback_availability(period)
    return {
        "availability": availability,
        "confidence": "low",
        "reason": message,
        "belongsToHistoricalSystem": availability == "historical",
        "officiallyWithdrawn": False,
        "reliablyScarce": False,
        "sources": [],
    }


def validate_source(source: Any) -> dict[str, Any]:
    if not isinstance(source, dict):
        raise ValueError("Research source must be an object")
    title = source.get("title")
    url = source.get("url")
    supports = source.get("supports")
    source_type = source.get("sourceType")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Research source title is required")
    if not isinstance(url, str) or not urlparse(url).scheme or not urlparse(url).netloc:
        raise ValueError("Research source url must be absolute")
    if not isinstance(supports, str) or not supports.strip():
        raise ValueError("Research source supports is required")
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"Unsupported sourceType: {source_type}")
    publisher = source.get("publisher")
    if publisher is not None and not isinstance(publisher, str):
        raise ValueError("Research source publisher must be string or null")
    return {
        "title": title.strip(),
        "url": url,
        "publisher": publisher.strip() if isinstance(publisher, str) else None,
        "sourceType": source_type,
        "supports": supports.strip(),
    }


def validate_research_result(value: Any, *, require_sources: bool) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Research result must be an object")
    availability = value.get("availability")
    confidence = value.get("confidence")
    reason = value.get("reason")
    if availability not in COIN_AVAILABILITIES:
        raise ValueError(f"Unsupported availability: {availability}")
    if confidence not in CONFIDENCE_VALUES:
        raise ValueError(f"Unsupported confidence: {confidence}")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Research reason is required")
    for key in ("belongsToHistoricalSystem", "officiallyWithdrawn", "reliablyScarce"):
        if not isinstance(value.get(key), bool):
            raise ValueError(f"Research field {key} must be boolean")
    sources_raw = value.get("sources")
    if not isinstance(sources_raw, list):
        raise ValueError("Research sources must be an array")
    if require_sources and not sources_raw:
        raise ValueError("AI research must include at least one source")
    sources = [validate_source(source) for source in sources_raw]
    calculated = calculate_availability(value)
    return {
        "availability": calculated,
        "confidence": confidence,
        "reason": reason.strip(),
        "belongsToHistoricalSystem": value["belongsToHistoricalSystem"],
        "officiallyWithdrawn": value["officiallyWithdrawn"],
        "reliablyScarce": value["reliablyScarce"],
        "sources": sources,
    }


def validate_research_entry(value: Any, max_cache_age_days: int) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        researched_at = value.get("researchedAt")
        if not isinstance(researched_at, str):
            return None
        parsed_date = datetime.fromisoformat(researched_at.replace("Z", "+00:00"))
        age_seconds = (datetime.now(timezone.utc) - parsed_date).total_seconds()
        if age_seconds > max_cache_age_days * 86400:
            return None
        result = validate_research_result(value.get("result"), require_sources=False)
        input_value = value.get("input")
        if not isinstance(input_value, dict):
            return None
        research_key = value.get("researchKey")
        detail_url = normalize_detail_url(str(value.get("detailUrl") or input_value.get("detailUrl") or ""))
        if not isinstance(research_key, str) or not research_key:
            return None
        return {
            "researchKey": research_key,
            "detailUrl": detail_url,
            "input": input_value,
            "result": result,
            "researchedAt": researched_at,
        }
    except Exception:
        return None


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_full_catalogue(path: str) -> dict[str, Any]:
    catalogue = load_json(Path(path))
    if not isinstance(catalogue, dict):
        raise ValueError("ucoin-catalog.json must contain an object")
    if not isinstance(catalogue.get("periods"), list):
        raise ValueError("ucoin-catalog.json must contain periods[]")
    for period in catalogue["periods"]:
        if not isinstance(period, dict) or not isinstance(period.get("coins"), list):
            raise ValueError("Every period in ucoin-catalog.json must contain coins[]")
    return catalogue


def build_research_inputs(catalogue: dict[str, Any]) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    country = catalogue.get("country")
    for period in catalogue.get("periods", []):
        for coin in period.get("coins", []):
            denomination = coin.get("denomination") if isinstance(coin.get("denomination"), dict) else {}
            issue_period = coin.get("issuePeriod") if isinstance(coin.get("issuePeriod"), dict) else {}
            detail_url = normalize_detail_url(str(coin.get("detailUrl") or ""))
            inputs.append(
                {
                    "country": country,
                    "historicalPeriod": period.get("fullTitle"),
                    "rulerOrAuthority": period.get("rulerOrPeriodName"),
                    "denomination": denomination.get("displayName"),
                    "issuePeriod": issue_period.get("displayValue"),
                    "startYear": issue_period.get("startYear"),
                    "endYear": issue_period.get("endYear"),
                    "detailUrl": detail_url,
                }
            )
    return inputs


def load_research_cache(path: str, max_cache_age_days: int) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {"generatedAt": utc_now_iso(), "provider": "none", "entries": []}
    raw = load_json(target)
    entries = []
    if isinstance(raw, dict) and isinstance(raw.get("entries"), list):
        for entry in raw["entries"]:
            valid = validate_research_entry(entry, max_cache_age_days)
            if valid is not None:
                entries.append(valid)
    return {
        "generatedAt": raw.get("generatedAt") if isinstance(raw, dict) and isinstance(raw.get("generatedAt"), str) else utc_now_iso(),
        "provider": raw.get("provider") if isinstance(raw, dict) and isinstance(raw.get("provider"), str) else "unknown",
        "entries": entries,
    }


def research_prompt(input_value: dict[str, Any]) -> str:
    return f"""You are researching the current availability category of a coin type.

Classify the coin using exactly one value:

- circulating
- scarce
- withdrawn
- historical

Classification definitions:

- historical: belongs to a historical country, territory, currency or monetary system that no longer exists.
- withdrawn: belongs to a modern or recent monetary system but was officially withdrawn, demonetized or removed from normal circulation.
- scarce: belongs to a current or recent monetary system and reliable evidence shows that this specific type is significantly less common in normal circulation.
- circulating: still reasonably obtainable through normal circulation and does not meet any higher-priority category.

Use this priority:

historical > withdrawn > scarce > circulating

Do not classify based only on age, issue period, monarch, commemorative status or marketplace price.
Prefer official central-bank, mint, government, legislation and recognized numismatic sources.
Do not invent sources. If evidence for scarcity is weak, set reliablyScarce to false.

Coin:

Country: {input_value.get('country')}
Historical period: {input_value.get('historicalPeriod')}
Ruler or authority: {input_value.get('rulerOrAuthority')}
Denomination: {input_value.get('denomination')}
Issue period: {input_value.get('issuePeriod')}
Start year: {input_value.get('startYear')}
End year: {input_value.get('endYear')}
Reference URL: {input_value.get('detailUrl')}

Return strict JSON matching this schema:
{{
  "availability": "circulating" | "scarce" | "withdrawn" | "historical",
  "confidence": "high" | "medium" | "low",
  "reason": "string",
  "belongsToHistoricalSystem": boolean,
  "officiallyWithdrawn": boolean,
  "reliablyScarce": boolean,
  "sources": [
    {{
      "title": "string",
      "url": "absolute URL",
      "publisher": "string or null",
      "sourceType": "central-bank" | "mint" | "government" | "legislation" | "numismatic-institution" | "catalogue" | "specialist-source",
      "supports": "string"
    }}
  ]
}}

Do not include markdown."""


class NoConfiguredResearchProvider:
    provider_name = "fallback-no-ai-provider"

    async def research_coin(self, input_value: dict[str, Any]) -> dict[str, Any]:
        raise ResearchFailure("No search-capable AI provider configured. Set AI_PROVIDER, AI_API_KEY and AI_MODEL to perform grounded availability research.")


class OpenAIAvailabilityResearchProvider:
    provider_name = "openai-web-search"

    def __init__(self) -> None:
        self.api_key = first_env_value("AI_API_KEY", "OPENAI_API_KEY")
        self.model = first_env_value("AI_MODEL", "OPENAI_MODEL")
        self.base_url = first_env_value("AI_BASE_URL", "OPENAI_API_BASE", default="https://api.openai.com/v1").rstrip("/")
        if not self.api_key or not self.model:
            raise ValueError("AI_PROVIDER=openai requires AI_API_KEY/OPENAI_API_KEY and AI_MODEL/OPENAI_MODEL")

    async def research_coin(self, input_value: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._request, input_value)

    def _request(self, input_value: dict[str, Any]) -> dict[str, Any]:
        prompt = research_prompt(input_value)
        if self.base_url.endswith("/invocations"):
            request_url = self.base_url
            payload = {
                "messages": [{"role": "user", "content": prompt}],
                "model": self.model,
                "temperature": 0,
                "max_tokens": 4096,
            }
        else:
            request_url = f"{self.base_url}/responses"
            payload = {
                "model": self.model,
                "input": prompt,
                "tools": [{"type": "web_search_preview"}],
            }
        request = urllib.request.Request(
            request_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in {429, 500, 502, 503, 504}:
                raise TemporaryResearchFailure(f"Temporary AI provider error: HTTP {exc.code}") from exc
            raise ResearchFailure(f"AI provider error: HTTP {exc.code}") from exc
        except TimeoutError as exc:
            raise TemporaryResearchFailure("AI provider timeout") from exc
        text = self._extract_text(data)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ResearchFailure("AI provider did not return strict JSON") from exc

    def _extract_text(self, data: dict[str, Any]) -> str:
        if isinstance(data.get("output_text"), str):
            return data["output_text"]
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
        predictions = data.get("predictions")
        if isinstance(predictions, list) and predictions:
            first_prediction = predictions[0]
            if isinstance(first_prediction, str):
                return first_prediction
            if isinstance(first_prediction, dict):
                for key in ("content", "text", "output"):
                    if isinstance(first_prediction.get(key), str):
                        return first_prediction[key]
        parts: list[str] = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if isinstance(content.get("text"), str):
                    parts.append(content["text"])
        if parts:
            return "".join(parts)
        raise ResearchFailure("AI provider response did not contain text output")


def create_provider() -> AvailabilityResearchProvider:
    provider = (os.environ.get("AI_PROVIDER") or "").strip().lower()
    if not provider and (first_env_value("OPENAI_API_BASE") or first_env_value("OPENAI_MODEL") or first_env_value("OPENAI_API_KEY")):
        provider = "openai"
    if not provider:
        return NoConfiguredResearchProvider()
    if provider == "openai":
        if not first_env_value("AI_API_KEY", "OPENAI_API_KEY") or not first_env_value("AI_MODEL", "OPENAI_MODEL"):
            return NoConfiguredResearchProvider()
        return OpenAIAvailabilityResearchProvider()
    raise ValueError(f"Unsupported AI_PROVIDER: {provider}")


async def research_with_retries(provider: AvailabilityResearchProvider, input_value: dict[str, Any]) -> dict[str, Any]:
    for attempt in range(MAX_RETRIES + 1):
        try:
            return await provider.research_coin(input_value)
        except TemporaryResearchFailure:
            if attempt >= MAX_RETRIES:
                raise
            await asyncio.sleep((BASE_RETRY_DELAY_MS / 1000) * (2**attempt))


def period_for_input(catalogue: dict[str, Any], input_value: dict[str, Any]) -> dict[str, Any]:
    for period in catalogue.get("periods", []):
        if period.get("fullTitle") == input_value.get("historicalPeriod"):
            return period
    return {}


async def research_missing_entries(
    inputs: list[dict[str, Any]],
    cache: dict[str, Any],
    provider: AvailabilityResearchProvider,
    catalogue: dict[str, Any],
    *,
    refresh_research: bool,
    concurrency: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    now = utc_now_iso()
    cached_by_key = {entry["researchKey"]: entry for entry in cache.get("entries", [])}
    unique_inputs: dict[str, dict[str, Any]] = {}
    for input_value in inputs:
        unique_inputs[build_coin_type_research_key(input_value)] = input_value

    reusable = 0
    missing: list[tuple[str, dict[str, Any]]] = []
    entries_by_key: dict[str, dict[str, Any]] = {}
    for key, input_value in unique_inputs.items():
        if not refresh_research and key in cached_by_key:
            entries_by_key[key] = cached_by_key[key]
            reusable += 1
        else:
            missing.append((key, input_value))

    denomination_groups = {build_denomination_research_key(input_value) for input_value in inputs}
    print(f"Loaded {len(inputs)} coins from ucoin-catalog.json")
    print(f"Found {len(denomination_groups)} reusable denomination research groups")
    print(f"Loaded {reusable} valid cached research results")
    print(f"Researching {len(missing)} missing coin-type groups")

    semaphore = asyncio.Semaphore(max(1, concurrency))
    errors: list[dict[str, Any]] = []
    processed = 0

    async def research_one(key: str, input_value: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        nonlocal processed
        async with semaphore:
            period = period_for_input(catalogue, input_value)
            try:
                raw_result = await research_with_retries(provider, input_value)
                result = validate_research_result(raw_result, require_sources=True)
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                errors.append({"researchKey": key, "detailUrl": input_value.get("detailUrl"), "message": message, "failedAt": utc_now_iso()})
                result = fallback_research_result(period, f"Availability research failed; fallback category applied. {message}")
            processed += 1
            if processed % 10 == 0 or processed == len(missing):
                print(f"Processed {processed}/{len(missing)} research groups")
            return key, {
                "researchKey": key,
                "detailUrl": input_value["detailUrl"],
                "input": input_value,
                "result": result,
                "researchedAt": utc_now_iso(),
            }

    if missing:
        for key, entry in await asyncio.gather(*(research_one(key, input_value) for key, input_value in missing)):
            entries_by_key[key] = entry

    ordered_entries = [entries_by_key[build_coin_type_research_key(input_value)] for input_value in inputs]
    research_file = {"generatedAt": now, "provider": provider.provider_name, "entries": ordered_entries}
    return research_file, errors


def to_resume_coin(coin: dict[str, Any], availability: str) -> dict[str, Any]:
    denomination = coin.get("denomination") if isinstance(coin.get("denomination"), dict) else {}
    issue_period = coin.get("issuePeriod") if isinstance(coin.get("issuePeriod"), dict) else {}
    images = coin.get("images") if isinstance(coin.get("images"), dict) else {}
    return {
        "denomination": denomination.get("displayName"),
        "value": denomination.get("value"),
        "unit": denomination.get("unit"),
        "issuePeriod": issue_period.get("displayValue"),
        "startYear": issue_period.get("startYear"),
        "endYear": issue_period.get("endYear"),
        "availability": availability,
        "detailUrl": normalize_detail_url(str(coin.get("detailUrl") or "")),
        "obverseImage": images.get("obverse"),
        "reverseImage": images.get("reverse"),
    }


def build_resume_catalogue(full_catalogue: dict[str, Any], research: dict[str, Any] | None = None, *, use_research: bool = False) -> dict[str, Any]:
    research_by_url = {entry["detailUrl"]: entry for entry in (research or {}).get("entries", [])} if use_research else {}
    periods = []
    source_coin_count = 0
    resume_coin_count = 0
    for period in full_catalogue.get("periods", []):
        coins = []
        for coin in period.get("coins", []):
            source_coin_count += 1
            detail_url = normalize_detail_url(str(coin.get("detailUrl") or ""))
            availability = PENDING_AVAILABILITY
            if use_research:
                research_entry = research_by_url.get(detail_url)
                if research_entry is None:
                    availability = get_fallback_availability(period)
                else:
                    availability = calculate_availability(research_entry["result"])
            coins.append(to_resume_coin(coin, availability))
            resume_coin_count += 1
        periods.append(
            {
                "title": period.get("fullTitle"),
                "ruler": period.get("rulerOrPeriodName"),
                "startYear": period.get("startYear"),
                "endYear": period.get("endYear"),
                "coins": coins,
            }
        )
    if source_coin_count != resume_coin_count:
        raise ValueError(f"Coin count mismatch: source={source_coin_count} resume={resume_coin_count}")
    return {"country": full_catalogue.get("country"), "periods": periods}


def build_availability_statistics(catalogue: dict[str, Any]) -> dict[str, Any]:
    statistics = {
        availability: {"count": 0, "earliestIssueYear": None, "latestIssueYear": None}
        for availability in COIN_AVAILABILITY_ORDER
    }
    for period in catalogue.get("periods", []):
        for coin in period.get("coins", []):
            availability = coin.get("availability")
            if availability not in statistics:
                continue
            entry = statistics[availability]
            entry["count"] += 1
            start_year = coin.get("startYear")
            end_year = coin.get("endYear")
            if isinstance(start_year, int):
                current = entry["earliestIssueYear"]
                entry["earliestIssueYear"] = start_year if current is None else min(current, start_year)
            if isinstance(end_year, int):
                current = entry["latestIssueYear"]
                entry["latestIssueYear"] = end_year if current is None else max(current, end_year)
    return {"availability": statistics}


def add_statistics(catalogue: dict[str, Any]) -> dict[str, Any]:
    return {"statistics": build_availability_statistics(catalogue), **catalogue}


def without_statistics(catalogue: dict[str, Any]) -> dict[str, Any]:
    result = dict(catalogue)
    result.pop("statistics", None)
    return result


def statistics_document(catalogue: dict[str, Any]) -> dict[str, Any]:
    return {"statistics": build_availability_statistics(catalogue)}


def resolve_output_path(configured_path: str, source_path: str, filename: str) -> str:
    if configured_path:
        return configured_path
    return str(Path(source_path).parent / filename)


def default_final_input_path(source_path: str) -> str:
    return str(Path(source_path).parent / FINAL_APP_CATALOG_INPUT_FILENAME)


def column_letter(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def xlsx_cell(row_index: int, column_index: int, value: Any, *, style: int | None = None) -> str:
    reference = f"{column_letter(column_index)}{row_index}"
    style_attr = f' s="{style}"' if style is not None else ""
    if value is None:
        return f'<c r="{reference}"{style_attr}/>'
    text = escape(str(value), quote=True)
    return f'<c r="{reference}" t="inlineStr"{style_attr}><is><t>{text}</t></is></c>'


def write_coins_excel(path: str, catalogue: dict[str, Any]) -> None:
    headers = ["Historical Period", "Denomination", "Issue Period", "Availability"]
    rows = [headers]
    for period in catalogue.get("periods", []):
        for coin in period.get("coins", []):
            rows.append(
                [
                    period.get("title"),
                    coin.get("denomination"),
                    coin.get("issuePeriod"),
                    coin.get("availability"),
                ]
            )
    widths = []
    for column_index in range(len(headers)):
        max_length = max(len(str(row[column_index] or "")) for row in rows)
        widths.append(min(max(max_length + 2, 12), 80))
    sheet_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = [xlsx_cell(row_index, column_index, value, style=1 if row_index == 1 else None) for column_index, value in enumerate(row, start=1)]
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    columns_xml = "".join(f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>' for index, width in enumerate(widths, start=1))
    last_row = len(rows)
    sheet_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <cols>{columns_xml}</cols>
  <sheetData>{"".join(sheet_rows)}</sheetData>
  <autoFilter ref="A1:D{last_row}"/>
</worksheet>'''
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>''')
        archive.writestr("_rels/.rels", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>''')
        archive.writestr("xl/workbook.xml", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Coins" sheetId="1" r:id="rId1"/></sheets>
</workbook>''')
        archive.writestr("xl/_rels/workbook.xml.rels", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>''')
        archive.writestr("xl/styles.xml", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="1"><fill><patternFill patternType="none"/></fill></fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>
</styleSheet>''')
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def validate_resume_catalogue(value: Any, expected_coin_count: int) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("periods"), list):
        raise ValueError("app catalogue must contain periods[]")
    forbidden_top_level = {"warnings", "pagination", "page_url", "final_url", "count", "output_path"}
    if forbidden_top_level.intersection(value):
        raise ValueError("app catalogue contains forbidden top-level technical fields")
    coin_count = 0
    allowed_coin_keys = {"denomination", "value", "unit", "issuePeriod", "startYear", "endYear", "availability", "detailUrl", "obverseImage", "reverseImage"}
    forbidden_coin_keys = {
        "imageExampleYear",
        "subject",
        "composition",
        "weightGrams",
        "diameterMm",
        "catalogue",
        "circulationType",
        "ucoinTypeId",
        "ucoinPeriodId",
        "detailPath",
        "confidence",
        "reason",
        "sources",
        "researchedAt",
        "legalTender",
        "officiallyWithdrawn",
        "commonlyCirculating",
        "images",
    }
    for period in value["periods"]:
        if not isinstance(period, dict):
            raise ValueError("resume period must be an object")
        if "designs" in period or "periodId" in period or "sourcePages" in period or "currencyDescription" in period:
            raise ValueError("resume period contains forbidden grouping or technical fields")
        if not isinstance(period.get("coins"), list):
            raise ValueError("resume period must contain coins[]")
        for coin in period["coins"]:
            coin_count += 1
            if set(coin.keys()) != allowed_coin_keys:
                raise ValueError(f"resume coin has invalid fields: {sorted(coin.keys())}")
            if forbidden_coin_keys.intersection(coin):
                raise ValueError("resume coin contains forbidden fields")
            if isinstance(coin.get("denomination"), dict) or isinstance(coin.get("issuePeriod"), dict):
                raise ValueError("resume coin contains nested denomination or issuePeriod")
            if coin.get("availability") not in RESUME_AVAILABILITIES:
                raise ValueError(f"invalid availability: {coin.get('availability')}")
            coin["detailUrl"] = normalize_detail_url(coin["detailUrl"])
            coin["obverseImage"] = normalize_image_url(coin["obverseImage"])
            coin["reverseImage"] = normalize_image_url(coin["reverseImage"])
    if coin_count != expected_coin_count:
        raise ValueError(f"Resume coin count mismatch: expected={expected_coin_count} actual={coin_count}")


def validate_final_availability_catalogue(value: dict[str, Any]) -> None:
    for period in value.get("periods", []):
        for coin in period.get("coins", []):
            if coin.get("availability") not in COIN_AVAILABILITIES:
                raise ValueError(f"app-catalog-final.json contains non-final availability: {coin.get('availability')}")


async def write_json_atomic(path: str, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)


def sha256_file(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cleanup_intermediate_files(final_input_path: str) -> None:
    folder = Path(final_input_path).parent
    for filename in (UCOIN_CATALOG_FILENAME, PENDING_APP_CATALOG_FILENAME, FINAL_APP_CATALOG_INPUT_FILENAME, PENDING_DIFFERENCES_FILENAME):
        target = folder / filename
        if not target.exists():
            continue
        target.unlink()
        print(f"Deleted {target}")


def generate_pending_differences_report(
    pending_catalog_path: str,
    *,
    source_catalog_path: str = "",
    precheck_output: str = "",
    include_markdown_as_issue: bool = False,
) -> str | None:
    pending_path = Path(pending_catalog_path).resolve()
    source_path = Path(source_catalog_path).resolve() if source_catalog_path else pending_path
    country_dir = source_path.parent
    country_slug = country_dir.name
    paises_dir = country_dir.parent.parent
    project_root = Path(__file__).resolve().parent.parent
    all_coins_dir = project_root.parent / "All_Coins"

    try:
        from scripts import check_site_coin_differences as checker

        report = checker.compare_country(
            paises_dir,
            all_coins_dir,
            country_slug,
            pending_path.name,
            include_markdown_as_issue,
            True,
            False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: failed to generate pending differences report: {exc}")
        return None

    output_path = Path(precheck_output) if precheck_output else country_dir / PENDING_DIFFERENCES_FILENAME
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps([report], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary = report.get("summary", {}) if isinstance(report, dict) else {}
    by_type = summary.get("by_type", {}) if isinstance(summary, dict) else {}
    print(f"Generated {output_path}")
    print(
        "Pending precheck summary: "
        f"issues={summary.get('total_issues', 0)} "
        f"missing_notes={by_type.get('missing_notes', 0)} "
        f"missing_url_ucoin={by_type.get('missing_url_ucoin', 0)} "
        f"missing_api_coin_record={by_type.get('missing_api_coin_record', 0)}"
    )
    api_error = summary.get("api_error", "") if isinstance(summary, dict) else ""
    if api_error:
        print(f"Pending precheck API warning: {api_error}")
    return str(output_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate simplified coin catalogue with placeholder availability values.")
    parser.add_argument("--input", default=UCOIN_CATALOG_FILENAME)
    parser.add_argument("--final-input", default="", help="JSON returned by the external AI with final availability values.")
    parser.add_argument("--wait-for-final", action="store_true", help="After generating the pending app catalogue, wait until app-catalog-final.json is pasted and continue.")
    parser.add_argument("--output", default="")
    parser.add_argument("--statistics-output", default="")
    parser.add_argument("--research-output", default="")
    parser.add_argument("--excel-output", default="")
    parser.add_argument("--errors-output", default="")
    parser.add_argument("--refresh-research", default="false")
    parser.add_argument("--use-ai-research", action="store_true", help="Use the configured AI provider instead of placeholder availability values.")
    parser.add_argument("--max-cache-age-days", type=int, default=AVAILABILITY_RESEARCH_MAX_AGE_DAYS)
    parser.add_argument("--concurrency", type=int, default=int(os.environ.get("AVAILABILITY_RESEARCH_CONCURRENCY", RESEARCH_CONCURRENCY)))
    parser.add_argument("--cleanup-intermediate", action="store_true", help="After generating final outputs, remove intermediate files (ucoin-catalog, pending and final input).")
    parser.add_argument("--skip-pending-precheck", action="store_true", help="Do not generate differences-pending.json after creating app-catalog-pending.json.")
    parser.add_argument("--pending-precheck-output", default="", help="Optional output path for pending differences report JSON.")
    return parser.parse_args()


async def generate_final_outputs(args: argparse.Namespace, final_input_path: str) -> None:
    final_catalogue = load_json(Path(final_input_path))
    if not isinstance(final_catalogue, dict):
        raise ValueError("final input must contain a JSON object")
    expected_coin_count = sum(len(period.get("coins", [])) for period in final_catalogue.get("periods", []))
    validate_resume_catalogue(final_catalogue, expected_coin_count)
    validate_final_availability_catalogue(final_catalogue)
    app_catalogue = without_statistics(final_catalogue)
    validate_resume_catalogue(app_catalogue, expected_coin_count)
    output_path = resolve_output_path(args.output, final_input_path, APP_CATALOG_FILENAME)
    statistics_output_path = resolve_output_path(args.statistics_output, final_input_path, AVAILABILITY_STATISTICS_FILENAME)
    excel_output_path = resolve_output_path(args.excel_output, final_input_path, COINS_AVAILABILITY_EXCEL_FILENAME)
    await write_json_atomic(output_path, app_catalogue)
    statistics = statistics_document(load_json(Path(output_path)))
    await write_json_atomic(statistics_output_path, statistics)
    write_coins_excel(excel_output_path, app_catalogue)
    print(f"Generated {output_path}")
    print(f"Generated {statistics_output_path}")
    print(f"Generated {excel_output_path}")
    if args.cleanup_intermediate:
        cleanup_intermediate_files(final_input_path)


async def main() -> None:
    load_dotenv()
    args = parse_args()
    if args.final_input:
        await generate_final_outputs(args, args.final_input)
        return

    input_hash_before = sha256_file(args.input)
    full_catalogue = load_full_catalogue(args.input)
    inputs = build_research_inputs(full_catalogue)
    research = {"generatedAt": utc_now_iso(), "provider": "external-placeholder", "entries": []}
    errors: list[dict[str, Any]] = []
    if args.use_ai_research:
        research_output_path = resolve_output_path(args.research_output, args.input, AVAILABILITY_RESEARCH_FILENAME)
        errors_output_path = resolve_output_path(args.errors_output, args.input, AVAILABILITY_RESEARCH_ERRORS_FILENAME)
        cache = load_research_cache(research_output_path, args.max_cache_age_days)
        provider = create_provider()
        research, errors = await research_missing_entries(
            inputs,
            cache,
            provider,
            full_catalogue,
            refresh_research=parse_bool(args.refresh_research),
            concurrency=args.concurrency,
        )
    resume = build_resume_catalogue(full_catalogue, research, use_research=args.use_ai_research)
    validate_resume_catalogue(resume, len(inputs))
    output_path = resolve_output_path(args.output, args.input, PENDING_APP_CATALOG_FILENAME)
    await write_json_atomic(output_path, resume)
    if args.use_ai_research:
        await write_json_atomic(research_output_path, research)
        if errors:
            await write_json_atomic(errors_output_path, {"generatedAt": utc_now_iso(), "errors": errors})
        elif Path(errors_output_path).exists():
            await write_json_atomic(errors_output_path, {"generatedAt": utc_now_iso(), "errors": []})
    input_hash_after = sha256_file(args.input)
    if input_hash_before != input_hash_after:
        raise RuntimeError("Input ucoin-catalog.json changed during app catalogue generation")
    if args.use_ai_research:
        print(f"Generated {research_output_path}")
    print(f"Generated {output_path}")
    if not args.skip_pending_precheck:
        generate_pending_differences_report(
            output_path,
            source_catalog_path=args.input,
            precheck_output=args.pending_precheck_output,
            include_markdown_as_issue=False,
        )
    if args.wait_for_final:
        final_input_path = default_final_input_path(args.input)
        final_target = Path(final_input_path)
        if not final_target.exists():
            final_target.touch()
            print(f"Created empty {final_input_path}")
        print(f"Paste the external AI result into {final_input_path}.")
        input("Press Enter here when the file is ready...")
        if not final_target.read_text(encoding="utf-8").strip():
            raise ValueError(f"{final_input_path} is still empty")
        await generate_final_outputs(args, final_input_path)


if __name__ == "__main__":
    asyncio.run(main())
