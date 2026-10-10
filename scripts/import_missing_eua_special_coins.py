#!/usr/bin/env python3
"""Create the explicitly approved missing USA commemorative coins.

This executor is intentionally create-only for collection data.  It creates
``SpecialCoin`` and ``SpecialCoinVariant`` records from a reviewed catalogue,
verifies each write, and stores a private resumable checkpoint.  It cannot
update or delete any existing collection record.  As required after external
raw-data mutations, it may only set ``AdminStats.needs_rebuild`` to true.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.admin_stats_sync import MutationRun, mark_numisvault_stats_stale
from scripts.execute_eua_commemorative_migration import (
    MigrationExecutionError,
    create_and_verify_one,
    create_and_verify_variant,
    fetch_all,
    no_retry_entity_client,
    payload_differences,
    special_coin_identity,
    special_variant_identity,
)
from scripts.import_base44_coins import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_RATE_LIMIT_DELAY_SECONDS,
    DEFAULT_REQUEST_DELAY_SECONDS,
    create_client,
)


DEFAULT_CATALOG = Path(
    "info/paises/america/eua/special-coins-missing-approved.json"
)
DEFAULT_STATE = Path(
    "backups/migrations/eua-missing-special-coins-execution-state.json"
)
EXPECTED_KIND = "eua_missing_special_coins"
EXPECTED_ITEMS = 7
EXPECTED_VARIANTS = 24
BASELINE_USA_SPECIAL_COINS = 238
SYSTEM_FIELDS = {
    "id",
    "created_date",
    "updated_date",
    "created_by",
    "created_by_id",
    "is_sample",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Valida ou cria as sete comemorativas aprovadas dos EUA e apenas "
            "as variantes não-S. Nunca atualiza nem apaga registos existentes."
        )
    )
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--check-live",
        action="store_true",
        help="Compara o catálogo aprovado com o Base44 sem escrever.",
    )
    modes.add_argument(
        "--apply-all-create-only",
        action="store_true",
        help="Cria e verifica pais e variantes ainda em falta.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirma explicitamente o modo create-only.",
    )
    parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    parser.add_argument(
        "--rate-limit-delay", type=float, default=DEFAULT_RATE_LIMIT_DELAY_SECONDS
    )
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    return parser.parse_args(argv)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationExecutionError(f"JSON inválido ou ilegível: {path}") from exc


def is_s_variant(tag: str) -> bool:
    normalized = " ".join(tag.upper().split())
    return normalized == "S" or normalized.endswith(" S")


def load_approved_catalog(path: Path) -> tuple[dict[str, Any], str]:
    catalog = read_json(path)
    if not isinstance(catalog, dict):
        raise MigrationExecutionError("O catálogo aprovado não é um objeto JSON.")
    if catalog.get("kind") != EXPECTED_KIND or catalog.get("status") != "approved_by_user":
        raise MigrationExecutionError("O catálogo não está aprovado para esta operação.")
    items = catalog.get("items")
    if not isinstance(items, list) or len(items) != EXPECTED_ITEMS:
        raise MigrationExecutionError(
            f"O catálogo tem de conter exatamente {EXPECTED_ITEMS} moedas."
        )

    keys: set[str] = set()
    identities: set[tuple[str, str, str, str]] = set()
    urls: set[str] = set()
    orders: set[int] = set()
    variant_count = 0
    for item in items:
        if not isinstance(item, dict):
            raise MigrationExecutionError("O catálogo contém uma moeda inválida.")
        key = str(item.get("key") or "").strip()
        payload = item.get("payload")
        variants = item.get("variants")
        if not key or key in keys:
            raise MigrationExecutionError(f"Chave ausente ou repetida: {key!r}")
        keys.add(key)
        if not isinstance(payload, dict) or not isinstance(variants, list):
            raise MigrationExecutionError(f"Payload ou variantes inválidos em {key}.")
        forbidden = sorted(SYSTEM_FIELDS & payload.keys())
        if forbidden:
            raise MigrationExecutionError(f"Campos de sistema em {key}: {forbidden}")
        if payload.get("country") != "EUA" or payload.get("continent") != "América":
            raise MigrationExecutionError(f"País ou continente inválido em {key}.")
        identity = special_coin_identity(payload)
        if identity in identities:
            raise MigrationExecutionError(f"Identidade repetida: {identity}")
        identities.add(identity)
        url = str(payload.get("url_ucoin") or "").strip()
        if not url or url in urls:
            raise MigrationExecutionError(f"URL uCoin ausente ou repetido em {key}.")
        urls.add(url)
        order = payload.get("ordem")
        if not isinstance(order, int) or order in orders:
            raise MigrationExecutionError(f"Ordem inválida ou repetida em {key}: {order!r}")
        orders.add(order)
        if bool(payload.get("has_variants")) != bool(variants):
            raise MigrationExecutionError(f"has_variants não corresponde a {key}.")

        tags: set[str] = set()
        for variant in variants:
            if not isinstance(variant, dict):
                raise MigrationExecutionError(f"Variante inválida em {key}.")
            tag = str(variant.get("tag") or "").strip()
            if not tag or tag in tags:
                raise MigrationExecutionError(f"Tag ausente ou repetida em {key}: {tag!r}")
            if is_s_variant(tag):
                raise MigrationExecutionError(
                    f"A variante S está proibida pelo catálogo aprovado: {key} / {tag}"
                )
            tags.add(tag)
            if SYSTEM_FIELDS & variant.keys() or "special_coin_id" in variant:
                raise MigrationExecutionError(f"Campos proibidos na variante {key} / {tag}.")
        variant_count += len(variants)

    if variant_count != EXPECTED_VARIANTS:
        raise MigrationExecutionError(
            f"Eram esperadas {EXPECTED_VARIANTS} variantes; existem {variant_count}."
        )
    return catalog, sha256_file(path)


def write_private_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = utc_now()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json(state))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        if read_json(temporary) != state:
            raise MigrationExecutionError(f"Falhou a verificação ao guardar {path}.")
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_state(path: Path, catalog_sha256: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "format_version": 1,
            "catalog_sha256": catalog_sha256,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "special_coins": {},
            "special_coin_variants": {},
            "safety": {"updates": 0, "deletions": 0},
        }
    state = read_json(path)
    if not isinstance(state, dict) or state.get("catalog_sha256") != catalog_sha256:
        raise MigrationExecutionError(
            "O checkpoint não corresponde ao catálogo aprovado atual."
        )
    if (
        not isinstance(state.get("special_coins"), dict)
        or not isinstance(state.get("special_coin_variants"), dict)
        or state.get("safety") != {"updates": 0, "deletions": 0}
    ):
        raise MigrationExecutionError("O checkpoint não garante uma operação create-only.")
    return state


def checkpoint_parent(
    state: dict[str, Any], item: dict[str, Any], actual: dict[str, Any], result: str
) -> None:
    state["special_coins"][item["key"]] = {
        "id": actual["id"],
        "identity": list(special_coin_identity(actual)),
        "result": result,
        "verified_record": actual,
        "verified_at": utc_now(),
    }


def variant_checkpoint_key(item_key: str, tag: str) -> str:
    return f"{item_key}::{tag}"


def checkpoint_variant(
    state: dict[str, Any], plan: dict[str, Any], actual: dict[str, Any], result: str
) -> None:
    key = variant_checkpoint_key(plan["item_key"], plan["payload"]["tag"])
    state["special_coin_variants"][key] = {
        "id": actual["id"],
        "special_coin_id": plan["payload"]["special_coin_id"],
        "tag": plan["payload"]["tag"],
        "result": result,
        "verified_record": actual,
        "verified_at": utc_now(),
    }


def reconcile_parents(
    catalog: dict[str, Any],
    state: dict[str, Any],
    live: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], dict[str, Any]]]]:
    by_identity: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    by_url: dict[str, list[dict[str, Any]]] = {}
    for record in live:
        by_identity.setdefault(special_coin_identity(record), []).append(record)
        url = str(record.get("url_ucoin") or "").strip()
        if url:
            by_url.setdefault(url, []).append(record)

    pending: list[dict[str, Any]] = []
    existing: list[tuple[dict[str, Any], dict[str, Any]]] = []
    planned_identities = {
        special_coin_identity(item["payload"]) for item in catalog["items"]
    }
    unplanned_live = [
        record for record in live if special_coin_identity(record) not in planned_identities
    ]
    if len(unplanned_live) != BASELINE_USA_SPECIAL_COINS:
        raise MigrationExecutionError(
            "A base live dos EUA mudou: esperavam-se "
            f"{BASELINE_USA_SPECIAL_COINS} SpecialCoin anteriores e existem "
            f"{len(unplanned_live)}. Faz um novo backup e revê o plano."
        )

    occupied_orders = {
        record.get("ordem"): record
        for record in unplanned_live
        if isinstance(record.get("ordem"), int)
    }
    for item in catalog["items"]:
        payload = item["payload"]
        identity = special_coin_identity(payload)
        matches = by_identity.get(identity, [])
        if len(matches) > 1:
            raise MigrationExecutionError(f"Existem {len(matches)} moedas para {identity}.")
        url_matches = by_url.get(str(payload["url_ucoin"]), [])
        if not matches and url_matches:
            raise MigrationExecutionError(
                f"O URL de {item['key']} já pertence a outra SpecialCoin."
            )
        checkpoint = state["special_coins"].get(item["key"])
        if not matches:
            if checkpoint:
                raise MigrationExecutionError(
                    f"O destino guardado de {item['key']} desapareceu do Base44."
                )
            collision = occupied_orders.get(payload["ordem"])
            if collision is not None:
                raise MigrationExecutionError(
                    f"A ordem {payload['ordem']} de {item['key']} já está ocupada."
                )
            pending.append(item)
            continue
        actual = matches[0]
        if url_matches != matches:
            raise MigrationExecutionError(f"O URL de {item['key']} não é único.")
        differences = payload_differences(payload, actual)
        if differences:
            raise MigrationExecutionError(
                f"A SpecialCoin existente de {item['key']} diverge do catálogo: "
                + json.dumps(differences, ensure_ascii=False, sort_keys=True)
            )
        actual_id = str(actual.get("id") or "").strip()
        if not actual_id:
            raise MigrationExecutionError(f"A SpecialCoin de {item['key']} não tem ID.")
        if checkpoint and checkpoint.get("id") != actual_id:
            raise MigrationExecutionError(f"O ID de {item['key']} diverge do checkpoint.")
        existing.append((item, actual))
    return pending, existing


def build_variant_plans(
    catalog: dict[str, Any], parent_by_key: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    for item in catalog["items"]:
        parent = parent_by_key.get(item["key"])
        parent_id = str((parent or {}).get("id") or "").strip()
        if not parent_id:
            raise MigrationExecutionError(f"Falta o pai confirmado de {item['key']}.")
        for variant in item["variants"]:
            plans.append(
                {
                    "item_key": item["key"],
                    "parent": parent,
                    "payload": {**variant, "special_coin_id": parent_id},
                }
            )
    if len(plans) != EXPECTED_VARIANTS:
        raise MigrationExecutionError("O plano de variantes ficou incompleto.")
    return plans


def reconcile_variants(
    plans: list[dict[str, Any]],
    state: dict[str, Any],
    live: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], dict[str, Any]]]]:
    parent_ids = {plan["payload"]["special_coin_id"] for plan in plans}
    relevant = [r for r in live if str(r.get("special_coin_id") or "") in parent_ids]
    by_identity: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in relevant:
        by_identity.setdefault(special_variant_identity(record), []).append(record)
    planned_identities = {special_variant_identity(plan["payload"]) for plan in plans}
    unexpected = sorted(set(by_identity) - planned_identities)
    if unexpected:
        raise MigrationExecutionError(
            f"Existem variantes não aprovadas nos novos pais: {unexpected[:5]}"
        )

    pending: list[dict[str, Any]] = []
    existing: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for plan in plans:
        payload = plan["payload"]
        tag = payload["tag"]
        key = variant_checkpoint_key(plan["item_key"], tag)
        matches = by_identity.get(special_variant_identity(payload), [])
        if len(matches) > 1:
            raise MigrationExecutionError(
                f"Existem {len(matches)} variantes para {plan['item_key']} / {tag}."
            )
        checkpoint = state["special_coin_variants"].get(key)
        if not matches:
            if checkpoint:
                raise MigrationExecutionError(f"A variante guardada {key} desapareceu.")
            pending.append(plan)
            continue
        actual = matches[0]
        differences = payload_differences(payload, actual)
        if differences:
            raise MigrationExecutionError(
                f"A variante existente {key} diverge do catálogo: "
                + json.dumps(differences, ensure_ascii=False, sort_keys=True)
            )
        actual_id = str(actual.get("id") or "").strip()
        if not actual_id:
            raise MigrationExecutionError(f"A variante {key} não tem ID.")
        if checkpoint and checkpoint.get("id") != actual_id:
            raise MigrationExecutionError(f"O ID da variante {key} diverge do checkpoint.")
        existing.append((plan, actual))
    return pending, existing


def format_duration(seconds: float) -> str:
    rounded = max(0, round(seconds))
    minutes, seconds_part = divmod(rounded, 60)
    return f"{minutes}m {seconds_part:02d}s" if minutes else f"{seconds_part}s"


def progress_line(label: str, position: int, total: int, started_at: float) -> str:
    elapsed = time.monotonic() - started_at
    remaining = total - position
    eta = elapsed / position * remaining if position else 0.0
    return (
        f"{position}/{total} ({position / total * 100:.1f}%) — {label} "
        f"[faltam: {remaining} | decorrido: {format_duration(elapsed)} | "
        f"restante: {'concluído' if not remaining else '~' + format_duration(eta)}]"
    )


def recover_checkpoints(
    state_path: Path,
    state: dict[str, Any],
    existing_parents: list[tuple[dict[str, Any], dict[str, Any]]],
    existing_variants: list[tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    changed = False
    for item, actual in existing_parents:
        if item["key"] not in state["special_coins"]:
            checkpoint_parent(state, item, actual, "recovered_exact_existing")
            changed = True
    for plan, actual in existing_variants:
        key = variant_checkpoint_key(plan["item_key"], plan["payload"]["tag"])
        if key not in state["special_coin_variants"]:
            checkpoint_variant(state, plan, actual, "recovered_exact_existing")
            changed = True
    if changed:
        write_private_state(state_path, state)


def print_plan(catalog: dict[str, Any], state: dict[str, Any]) -> None:
    print("\n" + "=" * 72)
    print("PLANO CREATE-ONLY — 7 COMEMORATIVAS EM FALTA DOS EUA")
    print("=" * 72)
    for item in catalog["items"]:
        payload = item["payload"]
        tags = ", ".join(variant["tag"] for variant in item["variants"])
        print(
            f"- {payload['name']} — {payload['commemorative_name']} "
            f"({payload['year']}) · variantes: {tags}"
        )
    print(f"Pais: {len(catalog['items'])}")
    print(f"Variantes não-S: {sum(len(item['variants']) for item in catalog['items'])}")
    print(f"Checkpoint de pais: {len(state['special_coins'])}")
    print(f"Checkpoint de variantes: {len(state['special_coin_variants'])}")
    print("Atualizações de moedas ou variantes disponíveis neste script: 0")
    print("Eliminações de moedas ou variantes disponíveis neste script: 0")
    print("AdminStats: needs_rebuild será marcado após criações confirmadas")
    print("Site Base44: nenhuma leitura ou alteração efetuada.")


def check_live(
    catalog: dict[str, Any], state: dict[str, Any], base_client: Any
) -> tuple[
    list[dict[str, Any]],
    list[tuple[dict[str, Any], dict[str, Any]]],
    list[dict[str, Any]],
    list[tuple[dict[str, Any], dict[str, Any]]],
]:
    live_parents = fetch_all(base_client.for_entity("SpecialCoin"), {"country": "EUA"})
    pending_parents, existing_parents = reconcile_parents(catalog, state, live_parents)
    parent_by_key = {item["key"]: actual for item, actual in existing_parents}
    if pending_parents:
        return pending_parents, existing_parents, [], []
    plans = build_variant_plans(catalog, parent_by_key)
    live_variants = fetch_all(base_client.for_entity("SpecialCoinVariant"), {})
    pending_variants, existing_variants = reconcile_variants(plans, state, live_variants)
    return pending_parents, existing_parents, pending_variants, existing_variants


def apply_all(
    catalog: dict[str, Any],
    state_path: Path,
    state: dict[str, Any],
    base_client: Any,
) -> tuple[int, int]:
    parent_read = base_client.for_entity("SpecialCoin")
    parent_write = no_retry_entity_client(base_client, "SpecialCoin")
    live_parents = fetch_all(parent_read, {"country": "EUA"})
    pending_parents, existing_parents = reconcile_parents(catalog, state, live_parents)
    parent_by_key = {item["key"]: actual for item, actual in existing_parents}
    recover_checkpoints(state_path, state, existing_parents, [])

    created_parents = 0
    created_variants = 0
    with MutationRun(
        lambda: mark_numisvault_stats_stale(base_client),
        mutation_label="SpecialCoin/SpecialCoinVariant record",
    ) as mutations:
        if pending_parents:
            print(f"\nSpecialCoin a criar: {len(pending_parents)}")
            started_at = time.monotonic()
            for position, item in enumerate(pending_parents, start=1):
                actual, result = create_and_verify_one(
                    read_client=parent_read,
                    write_client=parent_write,
                    payload=item["payload"],
                    mutation_success_fn=mutations.record_success,
                )
                checkpoint_parent(state, item, actual, result)
                write_private_state(state_path, state)
                parent_by_key[item["key"]] = actual
                created_parents += 1
                payload = item["payload"]
                label = f"{payload['name']} — {payload['commemorative_name']}"
                print(progress_line(label, position, len(pending_parents), started_at))
        else:
            print("Nenhuma SpecialCoin por criar.")

        plans = build_variant_plans(catalog, parent_by_key)
        variant_read = base_client.for_entity("SpecialCoinVariant")
        variant_write = no_retry_entity_client(base_client, "SpecialCoinVariant")
        live_variants = fetch_all(variant_read, {})
        pending_variants, existing_variants = reconcile_variants(
            plans, state, live_variants
        )
        recover_checkpoints(state_path, state, [], existing_variants)
        if pending_variants:
            print(f"\nSpecialCoinVariant a criar: {len(pending_variants)}")
            started_at = time.monotonic()
            for position, plan in enumerate(pending_variants, start=1):
                actual, result = create_and_verify_variant(
                    read_client=variant_read,
                    write_client=variant_write,
                    payload=plan["payload"],
                    mutation_success_fn=mutations.record_success,
                )
                checkpoint_variant(state, plan, actual, result)
                write_private_state(state_path, state)
                created_variants += 1
                parent = plan["parent"]
                label = (
                    f"{parent['name']} — {parent['commemorative_name']} "
                    f"— {plan['payload']['tag']}"
                )
                print(progress_line(label, position, len(pending_variants), started_at))
        else:
            print("Nenhuma SpecialCoinVariant por criar.")
    return created_parents, created_variants


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply_all_create_only and not args.yes:
        raise MigrationExecutionError(
            "O modo create-only exige --yes; nenhuma alteração foi efetuada."
        )
    catalog, catalog_sha256 = load_approved_catalog(args.catalog)
    state = load_state(args.state, catalog_sha256)
    if not args.check_live and not args.apply_all_create_only:
        print_plan(catalog, state)
        return 0

    base_client = create_client(args)
    if args.check_live:
        pending_parents, existing_parents, pending_variants, existing_variants = (
            check_live(catalog, state, base_client)
        )
        print(f"SpecialCoin dos EUA no Site: {BASELINE_USA_SPECIAL_COINS + len(existing_parents)}")
        print(f"Novas moedas confirmadas: {len(existing_parents)}/{EXPECTED_ITEMS}")
        print(f"Novas moedas ainda por criar: {len(pending_parents)}")
        if pending_parents:
            print(
                "Variantes: serão verificadas depois de existirem todos os pais "
                f"({EXPECTED_VARIANTS} planeadas)."
            )
        else:
            print(f"Variantes confirmadas: {len(existing_variants)}/{EXPECTED_VARIANTS}")
            print(f"Variantes ainda por criar: {len(pending_variants)}")
        print("Variantes S planeadas: 0")
        print("Site Base44: nenhuma alteração efetuada.")
        return 0

    created_parents, created_variants = apply_all(
        catalog, args.state, state, base_client
    )
    print(f"\nSpecialCoin criadas e verificadas: {created_parents}")
    print(f"SpecialCoinVariant criadas e verificadas: {created_variants}")
    print("Variantes S criadas: 0")
    print("Moedas ou variantes atualizadas/apagadas: 0")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MigrationExecutionError as exc:
        print(f"Erro: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1)
