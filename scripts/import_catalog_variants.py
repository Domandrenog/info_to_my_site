#!/usr/bin/env python3
"""Create missing CoinVariant or SpecialCoinVariant records from a catalogue.

The flow is deliberately create-only for variants. Existing variants are never
updated or deleted, and a parent is marked with ``has_variants=true`` only after
all requested tags have been confirmed.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from scripts.admin_stats_sync import MutationRun, mark_numisvault_stats_stale


class CatalogVariantError(RuntimeError):
    """Raised when a variant import cannot be proven safe."""


@dataclass(frozen=True)
class VariantSpec:
    detail_url: str
    label: str
    variants: tuple[tuple[str, int], ...]


@dataclass
class ParentVariantPlan:
    spec: VariantSpec
    parent: dict[str, Any]
    existing_by_tag: dict[str, dict[str, Any]] = field(default_factory=dict)
    missing: list[tuple[str, int]] = field(default_factory=list)
    flag_pending: bool = False


@dataclass
class VariantImportPlan:
    catalog_type: str
    country: str
    parents: list[ParentVariantPlan]

    @property
    def expected(self) -> int:
        return sum(len(parent.spec.variants) for parent in self.parents)

    @property
    def existing(self) -> int:
        return sum(len(parent.spec.variants) - len(parent.missing) for parent in self.parents)

    @property
    def missing(self) -> int:
        return sum(len(parent.missing) for parent in self.parents)

    @property
    def flags_pending(self) -> int:
        return sum(parent.flag_pending for parent in self.parents)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Cria apenas variantes em falta a partir de app-catalog.json ou "
            "special-app-catalog.json."
        )
    )
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument(
        "--catalog-type",
        choices=("auto", "normal", "special"),
        default="auto",
    )
    parser.add_argument("--country", default="")
    parser.add_argument(
        "--detail-url",
        action="append",
        default=[],
        help="Limita a operação ao URL uCoin exato; pode ser repetido.",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-live", action="store_true")
    modes.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Confirma explicitamente --apply.")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--request-delay", type=float, default=1.5)
    parser.add_argument("--rate-limit-delay", type=float, default=30.0)
    parser.add_argument("--max-retries", type=int, default=5)
    return parser.parse_args(argv)


def load_catalog(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogVariantError(f"Catálogo inválido ou ilegível: {path}") from exc
    if not isinstance(payload, dict):
        raise CatalogVariantError("O catálogo tem de ser um objeto JSON.")
    return payload


def detect_catalog_type(catalogue: dict[str, Any], configured: str = "auto") -> str:
    if configured != "auto":
        return configured
    if isinstance(catalogue.get("periods"), list):
        return "normal"
    if isinstance(catalogue.get("programmes"), list):
        return "special"
    raise CatalogVariantError("Não foi possível distinguir catálogo normal de coleção.")


def normalized_variants(coin: dict[str, Any], label: str) -> tuple[tuple[str, int], ...]:
    raw = coin.get("variants")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CatalogVariantError(f"Variantes inválidas em {label}.")
    variants: list[tuple[str, int]] = []
    tags: set[str] = set()
    for index, item in enumerate(raw):
        if isinstance(item, str):
            tag = item.strip()
            order = index
        elif isinstance(item, dict):
            tag = str(item.get("tag") or "").strip()
            raw_order = item.get("order", item.get("ordem", index))
            order = raw_order if isinstance(raw_order, int) else index
        else:
            raise CatalogVariantError(f"Variante inválida em {label}.")
        if not tag or tag in tags:
            raise CatalogVariantError(f"Tag de variante vazia ou repetida em {label}.")
        tags.add(tag)
        variants.append((tag, order))
    # One row is the base issue and should not open a variant selector.
    return tuple(variants) if len(variants) >= 2 else ()


def catalogue_specs(
    catalogue: dict[str, Any], catalog_type: str
) -> tuple[str, list[VariantSpec]]:
    specs_by_url: dict[str, VariantSpec] = {}
    seen_by_url: dict[str, tuple[str, tuple[tuple[str, int], ...]]] = {}
    if catalog_type == "normal":
        country = str(catalogue.get("country") or "").strip()
        groups = catalogue.get("periods", [])
        coin_groups = [group.get("coins", []) for group in groups if isinstance(group, dict)]
    else:
        country = str(catalogue.get("siteCountry") or catalogue.get("country") or "").strip()
        groups = catalogue.get("programmes", [])
        coin_groups = [group.get("coins", []) for group in groups if isinstance(group, dict)]
    if not country:
        raise CatalogVariantError("O país está ausente no catálogo.")

    for coins in coin_groups:
        if not isinstance(coins, list):
            continue
        for coin in coins:
            if not isinstance(coin, dict):
                continue
            detail_url = str(coin.get("detailUrl") or "").strip()
            denomination = str(coin.get("denomination") or "Moeda").strip()
            years = str(
                coin.get("issuePeriod")
                or coin.get("year")
                or coin.get("commemorativeName")
                or ""
            ).strip()
            label = f"{denomination} ({years})" if years else denomination
            variants = normalized_variants(coin, label)
            if not detail_url:
                if variants:
                    raise CatalogVariantError(f"URL uCoin ausente em {label}.")
                continue
            identity = (label, variants)
            previous = seen_by_url.get(detail_url)
            if previous is not None:
                if previous != identity:
                    raise CatalogVariantError(
                        f"Dados de variantes contraditórios para {detail_url}: "
                        f"{previous[0]} versus {label}."
                    )
                continue
            seen_by_url[detail_url] = identity
            if variants:
                specs_by_url[detail_url] = VariantSpec(detail_url, label, variants)
    return country, list(specs_by_url.values())


def fetch_all(client: Any, query: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    skip = 0
    while True:
        result = client.filter(query, limit=1000, skip=skip)
        if not isinstance(result, list):
            raise CatalogVariantError(f"Resposta Base44 inesperada: {result!r}")
        records.extend(item for item in result if isinstance(item, dict))
        if len(result) < 1000:
            return records
        skip += 1000


def parent_and_variant_contract(catalog_type: str) -> tuple[str, str, str]:
    if catalog_type == "normal":
        return "Coin", "CoinVariant", "coin_id"
    if catalog_type == "special":
        return "SpecialCoin", "SpecialCoinVariant", "special_coin_id"
    raise CatalogVariantError(f"Tipo de catálogo inválido: {catalog_type}")


def build_live_plan(
    base_client: Any,
    catalogue: dict[str, Any],
    *,
    catalog_type: str = "auto",
    country: str = "",
    allowed_urls: set[str] | None = None,
) -> VariantImportPlan:
    selected_type = detect_catalog_type(catalogue, catalog_type)
    catalog_country, specs = catalogue_specs(catalogue, selected_type)
    selected_country = country.strip() or catalog_country
    if allowed_urls is not None:
        specs = [spec for spec in specs if spec.detail_url in allowed_urls]
    parent_entity, variant_entity, parent_field = parent_and_variant_contract(selected_type)
    parents = fetch_all(base_client.for_entity(parent_entity), {"country": selected_country})
    parents_by_url: dict[str, list[dict[str, Any]]] = {}
    for parent in parents:
        url = str(parent.get("url_ucoin") or "").strip()
        if url:
            parents_by_url.setdefault(url, []).append(parent)

    variant_client = base_client.for_entity(variant_entity)
    plans: list[ParentVariantPlan] = []
    for spec in specs:
        matches = parents_by_url.get(spec.detail_url, [])
        if len(matches) != 1:
            raise CatalogVariantError(
                f"{spec.label}: esperava exatamente um pai por URL uCoin; encontrei {len(matches)}."
            )
        parent = matches[0]
        parent_id = str(parent.get("id") or "").strip()
        if not parent_id:
            raise CatalogVariantError(f"{spec.label}: o pai não tem ID.")
        live_variants = fetch_all(variant_client, {parent_field: parent_id})
        existing_by_tag: dict[str, dict[str, Any]] = {}
        for live in live_variants:
            if str(live.get(parent_field) or "") != parent_id:
                raise CatalogVariantError(f"{spec.label}: variante ligada ao pai errado.")
            tag = str(live.get("tag") or "").strip()
            if not tag or tag in existing_by_tag:
                raise CatalogVariantError(f"{spec.label}: tag Base44 vazia ou repetida.")
            existing_by_tag[tag] = live
        missing = [item for item in spec.variants if item[0] not in existing_by_tag]
        plans.append(
            ParentVariantPlan(
                spec=spec,
                parent=parent,
                existing_by_tag=existing_by_tag,
                missing=missing,
                flag_pending=parent.get("has_variants") is not True,
            )
        )
    return VariantImportPlan(selected_type, selected_country, plans)


def print_plan(plan: VariantImportPlan) -> None:
    label = "moedas normais" if plan.catalog_type == "normal" else "moedas de coleção"
    complete = [
        parent
        for parent in plan.parents
        if not parent.missing and not parent.flag_pending
    ]
    selector_only = [
        parent
        for parent in plan.parents
        if not parent.missing and parent.flag_pending
    ]
    variants_and_selector = [parent for parent in plan.parents if parent.missing]
    print(f"Variantes — {label} — {plan.country}")
    print(f"Moedas com variantes no catálogo: {len(plan.parents)}")
    print(f"Variantes pedidas: {plan.expected}")
    print(f"Já existentes e preservadas: {plan.existing}")
    print(f"Em falta para criar: {plan.missing}")
    print(f"Pais a marcar com has_variants: {plan.flags_pending}")
    print(f"Já completamente corretas: {len(complete)}")
    print("Atualizações/remoções de variantes existentes: 0")

    if selector_only:
        print("\nApenas ativar o seletor (todas as variantes já existem):")
        for parent in selector_only:
            print(f"- {parent.spec.label}: {len(parent.spec.variants)} variantes")
    if variants_and_selector:
        print("\nCriar variantes em falta:")
        for parent in variants_and_selector:
            suffix = " e ativar o seletor" if parent.flag_pending else ""
            print(
                f"- {parent.spec.label}: {len(parent.missing)} em falta"
                f"{suffix}"
            )


def get_record(client: Any, record_id: str) -> dict[str, Any]:
    result = client.request("GET", f"{client.base_url}/{quote(record_id)}")
    if not isinstance(result, dict):
        raise CatalogVariantError(f"GET de {record_id} devolveu {result!r}.")
    return result


def chunk(values: list[tuple[str, int]], size: int) -> list[list[tuple[str, int]]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def apply_plan(base_client: Any, plan: VariantImportPlan, *, batch_size: int = 25) -> tuple[int, int]:
    if batch_size < 1 or batch_size > 50:
        raise CatalogVariantError("--batch-size tem de estar entre 1 e 50.")
    parent_entity, variant_entity, parent_field = parent_and_variant_contract(plan.catalog_type)
    parent_client = base_client.for_entity(parent_entity)
    parent_write = base_client.for_entity(parent_entity)
    variant_client = base_client.for_entity(variant_entity)
    variant_write = base_client.for_entity(variant_entity)
    # A timed-out POST/PUT may already have succeeded remotely. Never retry a
    # mutation blindly; reconcile it with a fresh read instead.
    parent_write.max_retries = 0
    variant_write.max_retries = 0
    created = 0
    flags_updated = 0

    with MutationRun(
        lambda: mark_numisvault_stats_stale(base_client),
        mutation_label=f"{variant_entity}/{parent_entity} record",
    ) as mutations:
        for position, parent_plan in enumerate(plan.parents, start=1):
            parent_id = str(parent_plan.parent["id"])
            for batch in chunk(parent_plan.missing, batch_size):
                payloads = [
                    {
                        parent_field: parent_id,
                        "tag": tag,
                        "condition": "Não Tenho",
                        "ordem": order,
                    }
                    for tag, order in batch
                ]
                write_error: Exception | None = None
                write_succeeded = False
                try:
                    variant_write.bulk_create(payloads)
                    write_succeeded = True
                    mutations.record_success(len(payloads))
                except Exception as exc:  # noqa: BLE001 - reconcile ambiguous POST
                    write_error = exc
                after_batch = fetch_all(variant_client, {parent_field: parent_id})
                after_by_tag = {
                    str(item.get("tag") or "").strip(): item for item in after_batch
                }
                batch_tags = {tag for tag, _order in batch}
                confirmed_batch_tags = batch_tags & set(after_by_tag)
                if not write_succeeded and confirmed_batch_tags:
                    # Even a partial ambiguous POST changed raw data and must
                    # invalidate aggregate statistics before surfacing the error.
                    mutations.record_success(len(confirmed_batch_tags))
                if not batch_tags.issubset(after_by_tag):
                    missing_after = sorted(batch_tags - set(after_by_tag))
                    raise CatalogVariantError(
                        f"{parent_plan.spec.label}: lote não confirmado; "
                        f"em falta={missing_after}, erro={write_error}."
                    ) from write_error
                for payload in payloads:
                    actual = after_by_tag[payload["tag"]]
                    differences = {
                        key: {"expected": value, "actual": actual.get(key)}
                        for key, value in payload.items()
                        if actual.get(key) != value
                    }
                    if differences:
                        raise CatalogVariantError(
                            f"{parent_plan.spec.label}: variante criada diverge: "
                            + json.dumps(differences, ensure_ascii=False, sort_keys=True)
                        )
                if not write_succeeded and not confirmed_batch_tags:
                    mutations.record_success(len(payloads))
                created += len(payloads)

            confirmed = fetch_all(variant_client, {parent_field: parent_id})
            confirmed_by_tag = {
                str(item.get("tag") or "").strip(): item for item in confirmed
            }
            expected_tags = {tag for tag, _order in parent_plan.spec.variants}
            if not expected_tags.issubset(confirmed_by_tag):
                missing = sorted(expected_tags - set(confirmed_by_tag))
                raise CatalogVariantError(
                    f"{parent_plan.spec.label}: variantes não confirmadas: {missing}."
                )
            if parent_plan.flag_pending:
                before = get_record(parent_client, parent_id)
                update_error: Exception | None = None
                update_succeeded = False
                try:
                    parent_write.update(parent_id, {"has_variants": True})
                    update_succeeded = True
                    mutations.record_success()
                except Exception as exc:  # noqa: BLE001 - reconcile ambiguous PUT
                    update_error = exc
                after = get_record(parent_client, parent_id)
                if after.get("has_variants") is not True:
                    raise CatalogVariantError(
                        f"{parent_plan.spec.label}: has_variants não foi confirmado: "
                        f"{update_error}."
                    ) from update_error
                if not update_succeeded:
                    mutations.record_success()
                protected_before = {
                    key: value
                    for key, value in before.items()
                    if key not in {"has_variants", "updated_date"}
                }
                protected_after = {
                    key: value
                    for key, value in after.items()
                    if key not in {"has_variants", "updated_date"}
                }
                if protected_before != protected_after:
                    raise CatalogVariantError(
                        f"{parent_plan.spec.label}: outro campo do pai mudou."
                    )
                flags_updated += 1
            print(
                f"Variantes {position}/{len(plan.parents)} — {plan.country} — "
                f"{parent_plan.spec.label} — criadas {len(parent_plan.missing)}",
                flush=True,
            )
    return created, flags_updated


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply and not args.yes:
        raise CatalogVariantError("--apply exige --yes; nenhuma alteração foi efetuada.")
    catalogue = load_catalog(args.catalog)
    selected_type = detect_catalog_type(catalogue, args.catalog_type)
    catalog_country, specs = catalogue_specs(catalogue, selected_type)
    requested_urls = {value.strip() for value in args.detail_url if value.strip()}
    known_urls = {spec.detail_url for spec in specs}
    unknown_urls = sorted(requested_urls - known_urls)
    if unknown_urls:
        raise CatalogVariantError(
            "Os seguintes URLs não têm variantes no catálogo: "
            + ", ".join(unknown_urls)
        )
    if requested_urls:
        specs = [spec for spec in specs if spec.detail_url in requested_urls]
    if not (args.check_live or args.apply):
        expected = sum(len(spec.variants) for spec in specs)
        print(f"Tipo: {selected_type}")
        print(f"País: {args.country or catalog_country}")
        print(f"Moedas com variantes: {len(specs)}")
        print(f"Variantes no catálogo: {expected}")
        print("Modo local: nenhuma chamada ao Site Base44.")
        return 0

    from scripts.import_base44_coins import create_client

    base_client = create_client(args)
    plan = build_live_plan(
        base_client,
        catalogue,
        catalog_type=selected_type,
        country=args.country,
        allowed_urls=requested_urls or None,
    )
    print_plan(plan)
    if args.check_live:
        print("Site Base44: nenhuma alteração efetuada.")
        return 0
    created, flags = apply_plan(base_client, plan, batch_size=args.batch_size)
    print(f"Concluído: {created} variantes criadas; {flags} pais atualizados.")
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except (CatalogVariantError, ValueError, RuntimeError) as exc:
        print(f"Erro: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
