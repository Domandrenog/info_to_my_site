#!/usr/bin/env python3
"""Synchronize normal Coin display order with grouped catalogue order.

Only ``Coin.ordem`` may be changed. Equal denominations are kept together and
their issues are ordered chronologically. Every catalogue coin is matched by
the exact country + uCoin URL identity before any write is allowed.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from scripts.admin_stats_sync import MutationRun, mark_numisvault_stats_stale
from scripts.import_base44_coins import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_RATE_LIMIT_DELAY_SECONDS,
    DEFAULT_REQUEST_DELAY_SECONDS,
    build_notes,
    create_client,
    existing_records_for_country,
    fallback_catalog_orders,
    flatten_coins,
    format_duration,
    ordered_coin_entries,
    VALUE_THEN_ISSUE_ORDER,
)


class CoinOrderError(RuntimeError):
    """Raised when a deterministic, order-only update cannot be proved safe."""


@dataclass(frozen=True)
class OrderSpec:
    detail_url: str
    period_label: str
    denomination: str
    years: str
    desired_order: int

    @property
    def label(self) -> str:
        return f"{self.denomination} ({self.years})" if self.years else self.denomination


@dataclass(frozen=True)
class OrderUpdate:
    spec: OrderSpec
    record: dict[str, Any]


@dataclass
class CoinOrderPlan:
    country: str
    specs: list[OrderSpec]
    updates: list[OrderUpdate]
    unchanged: list[OrderUpdate]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Agrupa moedas normais da mesma denominação e ordena as emissões "
            "cronologicamente, alterando apenas Coin.ordem."
        )
    )
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--country", default="")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-live", action="store_true")
    modes.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Confirma explicitamente --apply.")
    parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    parser.add_argument(
        "--rate-limit-delay",
        type=float,
        default=DEFAULT_RATE_LIMIT_DELAY_SECONDS,
    )
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    return parser.parse_args(argv)


def load_catalog(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CoinOrderError(f"Catálogo inválido ou ilegível: {path}") from exc
    if not isinstance(payload, dict):
        raise CoinOrderError("O catálogo tem de ser um objeto JSON.")
    return payload


def catalogue_order_specs(catalogue: dict[str, Any]) -> tuple[str, list[OrderSpec]]:
    country = str(catalogue.get("country") or "").strip()
    if not country:
        raise CoinOrderError("O país está ausente no catálogo.")

    entries = ordered_coin_entries(
        flatten_coins(catalogue),
        value_then_issue=catalogue.get("coinOrder") == VALUE_THEN_ISSUE_ORDER,
    )
    fallback_orders = fallback_catalog_orders(entries)
    specs_by_url: dict[str, OrderSpec] = {}
    for (period, coin), desired_order in zip(entries, fallback_orders, strict=True):
        detail_url = str(coin.get("detailUrl") or "").strip()
        period_label = build_notes(period, coin)
        denomination = str(coin.get("denomination") or "Moeda").strip()
        years = str(coin.get("issuePeriod") or "").strip()
        if not detail_url:
            raise CoinOrderError(f"URL uCoin ausente em {denomination} ({years}).")
        spec = OrderSpec(
            detail_url,
            period_label,
            denomination,
            years,
            desired_order,
        )
        previous = specs_by_url.get(detail_url)
        if previous is not None and previous != spec:
            raise CoinOrderError(
                f"URL uCoin repetido com dados contraditórios: {detail_url}."
            )
        specs_by_url.setdefault(detail_url, spec)
    return country, list(specs_by_url.values())


def build_live_plan(
    base_client: Any,
    catalogue: dict[str, Any],
    *,
    country: str = "",
) -> CoinOrderPlan:
    catalog_country, specs = catalogue_order_specs(catalogue)
    selected_country = country.strip() or catalog_country
    live = existing_records_for_country(base_client.for_entity("Coin"), selected_country)

    live_by_url: dict[str, list[dict[str, Any]]] = {}
    for record in live:
        detail_url = str(record.get("url_ucoin") or "").strip()
        if detail_url:
            live_by_url.setdefault(detail_url, []).append(record)

    expected_urls = {spec.detail_url for spec in specs}
    untracked = [
        record
        for record in live
        if str(record.get("url_ucoin") or "").strip() not in expected_urls
    ]
    if untracked:
        raise CoinOrderError(
            f"Existem {len(untracked)} moedas no Site sem correspondência exata no catálogo; "
            "nenhuma ordem foi alterada."
        )

    updates: list[OrderUpdate] = []
    unchanged: list[OrderUpdate] = []
    for spec in specs:
        matches = live_by_url.get(spec.detail_url, [])
        if len(matches) != 1:
            raise CoinOrderError(
                f"{spec.label}: esperava exatamente uma Coin por URL uCoin; "
                f"encontrei {len(matches)}."
            )
        item = OrderUpdate(spec, matches[0])
        if matches[0].get("ordem") == spec.desired_order:
            unchanged.append(item)
        else:
            updates.append(item)
    return CoinOrderPlan(selected_country, specs, updates, unchanged)


def grouped_blocks(
    specs: list[OrderSpec],
) -> list[tuple[str, str, list[OrderSpec]]]:
    blocks: list[tuple[str, str, list[OrderSpec]]] = []
    for spec in specs:
        same_period = blocks and blocks[-1][0].casefold() == spec.period_label.casefold()
        same_denomination = (
            blocks and blocks[-1][1].casefold() == spec.denomination.casefold()
        )
        if same_period and same_denomination:
            blocks[-1][2].append(spec)
        else:
            blocks.append((spec.period_label, spec.denomination, [spec]))
    return blocks


def print_plan(plan: CoinOrderPlan) -> None:
    print(f"Ordem das moedas normais — {plan.country}")
    print(f"Moedas associadas exatamente por URL uCoin: {len(plan.specs)}")
    print(f"Já na ordem correta: {len(plan.unchanged)}")
    print(f"Ordens a corrigir: {len(plan.updates)}")
    print("Outros campos a alterar: 0")
    print("\nBlocos com várias emissões da mesma denominação e período:")
    for period_label, denomination, specs in grouped_blocks(plan.specs):
        if len(specs) < 2:
            continue
        periods = ", ".join(spec.years for spec in specs)
        print(
            f"- {period_label or 'Sem período'} — {denomination}: "
            f"{len(specs)} moedas · ordens "
            f"{specs[0].desired_order}–{specs[-1].desired_order} · {periods}"
        )


def get_record(client: Any, record_id: str) -> dict[str, Any]:
    result = client.request("GET", f"{client.base_url}/{quote(record_id)}")
    if not isinstance(result, dict):
        raise CoinOrderError(f"GET de {record_id} devolveu {result!r}.")
    return result


def protected_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key not in {"ordem", "updated_date"}
    }


def apply_plan(base_client: Any, plan: CoinOrderPlan) -> int:
    reader = base_client.for_entity("Coin")
    writer = base_client.for_entity("Coin")
    writer.max_retries = 0
    started_at = time.monotonic()
    total_request_duration = 0.0

    with MutationRun(
        lambda: mark_numisvault_stats_stale(base_client),
        mutation_label="Coin order",
    ) as mutations:
        for position, item in enumerate(plan.updates, start=1):
            record_id = str(item.record.get("id") or "").strip()
            if not record_id:
                raise CoinOrderError(f"{item.spec.label}: ID Base44 ausente.")
            before = get_record(reader, record_id)
            request_started = time.monotonic()
            update_error: Exception | None = None
            try:
                writer.update(record_id, {"ordem": item.spec.desired_order})
            except Exception as exc:  # noqa: BLE001 - reconcile ambiguous PUT
                update_error = exc
            request_duration = time.monotonic() - request_started
            total_request_duration += request_duration
            after = get_record(reader, record_id)
            if after.get("ordem") != item.spec.desired_order:
                raise CoinOrderError(
                    f"{item.spec.label}: ordem {item.spec.desired_order} não confirmada: "
                    f"{update_error}."
                ) from update_error
            mutations.record_success()
            if protected_fields(before) != protected_fields(after):
                raise CoinOrderError(f"{item.spec.label}: outro campo além de ordem mudou.")

            remaining = len(plan.updates) - position
            average = total_request_duration / position
            eta = "concluído" if not remaining else f"~{format_duration(average * remaining)}"
            print(
                f"Ordenado: {position}/{len(plan.updates)} "
                f"({position / len(plan.updates):.0%}) — {plan.country} — "
                f"{item.spec.label} — ordem {item.spec.desired_order} "
                f"[demorou nesta: {format_duration(request_duration, precise=True)} | "
                f"decorrido: {format_duration(time.monotonic() - started_at)} | "
                f"restante: {eta}]",
                flush=True,
            )
    return len(plan.updates)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply and not args.yes:
        raise CoinOrderError("--apply exige --yes; nenhuma alteração foi efetuada.")
    catalogue = load_catalog(args.catalog)
    catalog_country, specs = catalogue_order_specs(catalogue)
    if not (args.check_live or args.apply):
        print(f"País: {args.country or catalog_country}")
        print(f"Moedas únicas no catálogo: {len(specs)}")
        print("Modo local: nenhuma chamada ao Site Base44.")
        return 0

    base_client = create_client(args)
    plan = build_live_plan(base_client, catalogue, country=args.country)
    print_plan(plan)
    if args.check_live:
        print("Site Base44: nenhuma alteração efetuada.")
        return 0

    changed = apply_plan(base_client, plan)
    verified = build_live_plan(base_client, catalogue, country=args.country)
    if verified.updates:
        raise CoinOrderError(
            f"A verificação final ainda encontrou {len(verified.updates)} ordens incorretas."
        )
    print(f"Concluído e verificado: {changed} ordens corrigidas; outros campos alterados: 0.")
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except (CoinOrderError, ValueError, RuntimeError) as exc:
        print(f"Erro: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
