#!/usr/bin/env python3
"""Create only the missing variants of the 30 normal USA coins.

The approved plan is generated from the live uCoin mintage tables.  Existing
``CoinVariant`` records are immutable in this flow.  Missing variants are
created with ``condition=Não Tenho`` and each parent ``Coin.has_variants`` is
set only after its complete expected variant set has been verified.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from scripts.admin_stats_sync import MutationRun, mark_numisvault_stats_stale
from scripts.execute_eua_commemorative_migration import (
    fetch_all,
    format_duration,
    no_retry_entity_client,
)
from scripts.import_base44_coins import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_RATE_LIMIT_DELAY_SECONDS,
    DEFAULT_REQUEST_DELAY_SECONDS,
    Base44Client,
    create_client,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PLAN = (
    PROJECT_ROOT / "info/paises/america/eua/normal-coin-variants-approved.json"
)
EXPECTED_PARENTS = 30
EXPECTED_VARIANTS = 1239
EXPECTED_PROOF_EXCLUSIONS = 260
PLAN_KIND = "eua_normal_coin_variants"
NEW_CONDITION = "Não Tenho"
IGNORED_PARENT_FIELDS = {"has_variants", "updated_date"}
VERIFY_ATTEMPTS = 6
VERIFY_PAUSE_SECONDS = 2.0
APPLY_LOCK = Path("/tmp/info-to-my-site-eua-normal-coin-variants.lock")


class NormalCoinVariantError(RuntimeError):
    """Raised when the create-only variant operation cannot be proved safe."""


@contextmanager
def exclusive_apply_lock() -> Any:
    """Prevent two local create-only executions from overlapping."""

    handle = APPLY_LOCK.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise NormalCoinVariantError(
                "Já existe uma importação de variantes dos EUA em execução."
            ) from exc
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Cria apenas as variantes em falta nas 30 moedas normais dos EUA; "
            "não altera variantes existentes."
        )
    )
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--check-live",
        action="store_true",
        help="Reconcilia o plano com o Site Base44 sem escrever.",
    )
    modes.add_argument(
        "--apply",
        action="store_true",
        help="Cria e verifica apenas as variantes ainda em falta.",
    )
    parser.add_argument("--yes", action="store_true", help="Confirma --apply.")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    parser.add_argument(
        "--rate-limit-delay",
        type=float,
        default=DEFAULT_RATE_LIMIT_DELAY_SECONDS,
    )
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    return parser.parse_args(argv)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NormalCoinVariantError(f"Plano inválido ou ilegível: {path}") from exc


def load_plan(path: Path) -> dict[str, Any]:
    plan = read_json(path)
    if not isinstance(plan, dict):
        raise NormalCoinVariantError("O plano não é um objeto JSON.")
    if plan.get("kind") != PLAN_KIND:
        raise NormalCoinVariantError("O plano não é das variantes normais dos EUA.")
    if plan.get("status") != "approved_by_user" or plan.get("country") != "EUA":
        raise NormalCoinVariantError("O plano não está aprovado para o país EUA.")
    if plan.get("summary") != {
        "parents": EXPECTED_PARENTS,
        "variants": EXPECTED_VARIANTS,
        "proof_only_excluded": EXPECTED_PROOF_EXCLUSIONS,
    }:
        raise NormalCoinVariantError("O resumo do plano não tem as contagens aprovadas.")
    safety = plan.get("safety")
    if not isinstance(safety, dict) or safety != {
        "coin_updates_allowed": ["has_variants"],
        "coin_variant_creates_maximum": EXPECTED_VARIANTS,
        "coin_variant_updates": 0,
        "coin_variant_deletes": 0,
        "coin_creates": 0,
        "coin_deletes": 0,
    }:
        raise NormalCoinVariantError("As garantias de segurança do plano são inválidas.")
    policy = plan.get("policy")
    if not isinstance(policy, dict) or policy.get("new_variant_condition") != NEW_CONDITION:
        raise NormalCoinVariantError("A condição das novas variantes é inválida.")

    items = plan.get("items")
    if not isinstance(items, list) or len(items) != EXPECTED_PARENTS:
        raise NormalCoinVariantError("O plano tem de conter exatamente 30 moedas.")
    ids: set[str] = set()
    urls: set[str] = set()
    total_variants = 0
    total_proof = 0
    for item in items:
        if not isinstance(item, dict):
            raise NormalCoinVariantError("O plano contém uma moeda inválida.")
        coin_id = str(item.get("coin_id") or "").strip()
        url_ucoin = str(item.get("url_ucoin") or "").strip()
        name = str(item.get("name") or "").strip()
        years = str(item.get("years") or "").strip()
        variants = item.get("variants")
        proof_only = item.get("excluded_proof_only")
        if not coin_id or coin_id in ids:
            raise NormalCoinVariantError(f"Coin ID ausente ou repetido: {coin_id!r}")
        if not url_ucoin or url_ucoin in urls:
            raise NormalCoinVariantError(f"URL uCoin ausente ou repetido: {url_ucoin!r}")
        if not name or not years:
            raise NormalCoinVariantError(f"Coin sem nome ou anos: {coin_id}")
        if not isinstance(variants, list) or len(variants) < 2:
            raise NormalCoinVariantError(f"Variantes insuficientes em {coin_id}.")
        if any(not isinstance(tag, str) or not tag.strip() for tag in variants):
            raise NormalCoinVariantError(f"Tag de variante inválida em {coin_id}.")
        if len(variants) != len(set(variants)):
            raise NormalCoinVariantError(f"Tags repetidas em {coin_id}.")
        if not isinstance(proof_only, list) or any(
            not isinstance(tag, str) or not tag.strip() for tag in proof_only
        ):
            raise NormalCoinVariantError(f"Exclusões Proof inválidas em {coin_id}.")
        if set(variants) & set(proof_only):
            raise NormalCoinVariantError(f"Uma variante normal também é Proof em {coin_id}.")
        ids.add(coin_id)
        urls.add(url_ucoin)
        total_variants += len(variants)
        total_proof += len(proof_only)
    if total_variants != EXPECTED_VARIANTS or total_proof != EXPECTED_PROOF_EXCLUSIONS:
        raise NormalCoinVariantError("As contagens detalhadas divergem do resumo do plano.")
    return plan


def records_by_id(records: list[dict[str, Any]], *, label: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = str(record.get("id") or "").strip()
        if not record_id or record_id in indexed:
            raise NormalCoinVariantError(f"{label}: ID ausente ou repetido: {record_id!r}")
        indexed[record_id] = record
    return indexed


def variants_by_tag(
    records: list[dict[str, Any]], *, coin_id: str
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        if str(record.get("coin_id") or "") != coin_id:
            raise NormalCoinVariantError(f"CoinVariant ligada ao pai errado em {coin_id}.")
        tag = str(record.get("tag") or "").strip()
        if not tag or tag in indexed:
            raise NormalCoinVariantError(
                f"CoinVariant com tag vazia ou repetida em {coin_id}: {tag!r}"
            )
        indexed[tag] = record
    return indexed


def fetch_parent_variants(client: Base44Client, coin_id: str) -> list[dict[str, Any]]:
    return fetch_all(client, {"coin_id": coin_id})


def protected_parent_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {
        field: value
        for field, value in record.items()
        if field not in IGNORED_PARENT_FIELDS
    }


def get_record(client: Base44Client, record_id: str) -> dict[str, Any]:
    result = client.request("GET", f"{client.base_url}/{quote(record_id)}")
    if not isinstance(result, dict):
        raise NormalCoinVariantError(f"GET de {record_id} devolveu {result!r}.")
    return result


def variant_payload(item: dict[str, Any], tag: str) -> dict[str, Any]:
    return {
        "coin_id": item["coin_id"],
        "tag": tag,
        "condition": NEW_CONDITION,
        "ordem": item["variants"].index(tag),
    }


def payload_differences(payload: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    return {
        field: {"expected": value, "actual": actual.get(field)}
        for field, value in payload.items()
        if actual.get(field) != value
    }


def reconcile_live(
    plan: dict[str, Any],
    live_parents: list[dict[str, Any]],
    live_variants: list[dict[str, Any]],
) -> dict[str, Any]:
    if len(live_parents) != EXPECTED_PARENTS:
        raise NormalCoinVariantError(
            f"Esperavam-se 30 Coin normais dos EUA; existem {len(live_parents)}."
        )
    parent_by_id = records_by_id(live_parents, label="Coin EUA")
    planned_ids = {item["coin_id"] for item in plan["items"]}
    if set(parent_by_id) != planned_ids:
        missing = sorted(planned_ids - set(parent_by_id))
        unexpected = sorted(set(parent_by_id) - planned_ids)
        raise NormalCoinVariantError(
            f"O conjunto Coin mudou (em falta={missing[:5]}, inesperados={unexpected[:5]})."
        )

    relevant = [
        record
        for record in live_variants
        if str(record.get("coin_id") or "") in planned_ids
    ]
    variants_by_parent: dict[str, list[dict[str, Any]]] = {
        coin_id: [] for coin_id in planned_ids
    }
    for record in relevant:
        variants_by_parent[str(record["coin_id"])].append(record)

    item_states: list[dict[str, Any]] = []
    total_existing = 0
    total_pending = 0
    flags_pending = 0
    for item in plan["items"]:
        coin_id = item["coin_id"]
        parent = parent_by_id[coin_id]
        if parent.get("country") != "EUA":
            raise NormalCoinVariantError(f"País inesperado em {coin_id}.")
        if parent.get("url_ucoin") != item["url_ucoin"]:
            raise NormalCoinVariantError(f"URL uCoin mudou em {coin_id}.")
        if parent.get("name") != item["name"] or parent.get("years") != item["years"]:
            raise NormalCoinVariantError(f"Nome ou anos mudaram em {coin_id}.")

        by_tag = variants_by_tag(variants_by_parent[coin_id], coin_id=coin_id)
        expected = set(item["variants"])
        unexpected_tags = sorted(set(by_tag) - expected)
        if unexpected_tags:
            raise NormalCoinVariantError(
                f"Variantes inesperadas em {coin_id}: {unexpected_tags[:10]}"
            )
        existing_tags = [tag for tag in item["variants"] if tag in by_tag]
        pending_tags = [tag for tag in item["variants"] if tag not in by_tag]
        total_existing += len(existing_tags)
        total_pending += len(pending_tags)
        flag_pending = parent.get("has_variants") is not True
        flags_pending += int(flag_pending)
        item_states.append(
            {
                "item": item,
                "parent": parent,
                "variants_by_tag": by_tag,
                "existing_tags": existing_tags,
                "pending_tags": pending_tags,
                "flag_pending": flag_pending,
            }
        )

    if total_existing + total_pending != EXPECTED_VARIANTS:
        raise NormalCoinVariantError("A reconciliação não cobre as 1.239 variantes.")
    return {
        "items": item_states,
        "existing": total_existing,
        "pending": total_pending,
        "flags_pending": flags_pending,
        "all_live_variant_count": len(live_variants),
    }


def print_plan(plan: dict[str, Any], state: dict[str, Any] | None = None) -> None:
    print("VARIANTES — MOEDAS NORMAIS DOS EUA")
    print(f"Moedas no plano: {len(plan['items'])}")
    print(f"Variantes válidas no uCoin: {EXPECTED_VARIANTS}")
    print(f"Linhas exclusivamente Proof excluídas: {EXPECTED_PROOF_EXCLUSIONS}")
    print("Novas variantes: condition=Não Tenho")
    print("CoinVariant existentes: nenhuma alteração e nenhuma eliminação")
    print("Coin: apenas has_variants pode mudar; nenhuma criação ou eliminação")
    if state is not None:
        print(f"Já existentes no Site Base44: {state['existing']}/{EXPECTED_VARIANTS}")
        print(f"Ainda por criar: {state['pending']}/{EXPECTED_VARIANTS}")
        print(f"Pais a marcar com variantes: {state['flags_pending']}/{EXPECTED_PARENTS}")


def create_batch_and_verify(
    *,
    item: dict[str, Any],
    tags: list[str],
    read_client: Base44Client,
    write_client: Base44Client,
    mutations: MutationRun,
) -> int:
    before = variants_by_tag(
        fetch_parent_variants(read_client, item["coin_id"]),
        coin_id=item["coin_id"],
    )
    expected = set(item["variants"])
    if set(before) - expected:
        raise NormalCoinVariantError(f"Variantes inesperadas antes do POST em {item['coin_id']}.")
    if any(tag in before for tag in tags):
        raise NormalCoinVariantError(f"O lote contém uma variante que já existe em {item['coin_id']}.")
    payloads = [variant_payload(item, tag) for tag in tags]
    creation_error: Exception | None = None
    successful_request = False
    try:
        write_client.bulk_create(payloads)
        successful_request = True
        # The write endpoint answered successfully.  Record it immediately so
        # AdminStats is still invalidated if the following verification GET
        # encounters an unrelated network failure.
        mutations.record_success(len(payloads))
    except Exception as exc:  # noqa: BLE001 - reconcile an ambiguous POST
        creation_error = exc

    after: dict[str, dict[str, Any]] = {}
    new_tags: set[str] = set()
    for attempt in range(VERIFY_ATTEMPTS):
        after = variants_by_tag(
            fetch_parent_variants(read_client, item["coin_id"]),
            coin_id=item["coin_id"],
        )
        if set(after) - expected:
            raise NormalCoinVariantError(
                f"Variantes inesperadas após POST em {item['coin_id']}."
            )
        new_tags = set(after) - set(before)
        if new_tags == set(tags):
            break
        if not new_tags.issubset(set(tags)):
            break
        if attempt + 1 < VERIFY_ATTEMPTS:
            time.sleep(VERIFY_PAUSE_SECONDS)
    if not successful_request and new_tags:
        mutations.record_success(len(new_tags))
    if new_tags != set(tags):
        detail = (
            f"esperadas={sorted(tags)}, confirmadas={sorted(new_tags)}, "
            f"erro={creation_error}"
        )
        raise NormalCoinVariantError(
            f"O lote não foi criado integralmente em {item['coin_id']}: {detail}"
        ) from creation_error
    for payload in payloads:
        differences = payload_differences(payload, after[payload["tag"]])
        if differences:
            raise NormalCoinVariantError(
                "CoinVariant criada diverge do payload: "
                + json.dumps(differences, ensure_ascii=False, sort_keys=True)
            )
    return len(new_tags)


def set_parent_flag_and_verify(
    *,
    item: dict[str, Any],
    read_client: Base44Client,
    write_client: Base44Client,
    mutations: MutationRun,
) -> bool:
    before = get_record(read_client, item["coin_id"])
    if before.get("has_variants") is True:
        return False
    update_error: Exception | None = None
    successful_request = False
    try:
        write_client.update(item["coin_id"], {"has_variants": True})
        successful_request = True
        mutations.record_success()
    except Exception as exc:  # noqa: BLE001 - reconcile an ambiguous PUT
        update_error = exc
    after: dict[str, Any] = {}
    for attempt in range(VERIFY_ATTEMPTS):
        after = get_record(read_client, item["coin_id"])
        if after.get("has_variants") is True:
            break
        if attempt + 1 < VERIFY_ATTEMPTS:
            time.sleep(VERIFY_PAUSE_SECONDS)
    if after.get("has_variants") is not True:
        raise NormalCoinVariantError(
            f"Não foi possível confirmar has_variants em {item['coin_id']}: {update_error}"
        ) from update_error
    if not successful_request:
        mutations.record_success()
    if protected_parent_fields(after) != protected_parent_fields(before):
        raise NormalCoinVariantError(f"Um campo Coin protegido mudou em {item['coin_id']}.")
    return True


def apply_missing(
    *,
    plan: dict[str, Any],
    initial_state: dict[str, Any],
    base_client: Base44Client,
    batch_size: int,
) -> tuple[int, int]:
    if batch_size < 1 or batch_size > 50:
        raise NormalCoinVariantError("--batch-size tem de estar entre 1 e 50.")
    variant_read = base_client.for_entity("CoinVariant")
    variant_write = no_retry_entity_client(base_client, "CoinVariant")
    parent_read = base_client.for_entity("Coin")
    parent_write = no_retry_entity_client(base_client, "Coin")
    total_pending = initial_state["pending"]
    total_created = 0
    flags_updated = 0
    started = time.monotonic()
    durations = 0.0

    with MutationRun(
        lambda: mark_numisvault_stats_stale(base_client),
        mutation_label="CoinVariant/Coin record",
    ) as mutations:
        for item_state in initial_state["items"]:
            item = item_state["item"]
            pending = item_state["pending_tags"]
            for offset in range(0, len(pending), batch_size):
                tags = pending[offset : offset + batch_size]
                batch_started = time.monotonic()
                created = create_batch_and_verify(
                    item=item,
                    tags=tags,
                    read_client=variant_read,
                    write_client=variant_write,
                    mutations=mutations,
                )
                total_created += created
                duration = time.monotonic() - batch_started
                durations += duration
                remaining = total_pending - total_created
                processed_batches = max(1, (total_created + batch_size - 1) // batch_size)
                eta = durations / processed_batches * ((remaining + batch_size - 1) // batch_size)
                print(
                    f"Criadas: {total_created}/{total_pending} "
                    f"({total_created / total_pending * 100:.1f}%) — EUA — "
                    f"{item['name']} ({item['years']}) — lote {created} "
                    f"[faltam: {remaining} | nesta: {format_duration(duration)} | "
                    f"decorrido: {format_duration(time.monotonic() - started)} | "
                    f"restante: {'concluído' if not remaining else '~' + format_duration(eta)}]"
                )

            final_variants = variants_by_tag(
                fetch_parent_variants(variant_read, item["coin_id"]),
                coin_id=item["coin_id"],
            )
            if set(final_variants) != set(item["variants"]):
                raise NormalCoinVariantError(
                    f"O conjunto final do pai está incompleto em {item['coin_id']}."
                )
            if set_parent_flag_and_verify(
                item=item,
                read_client=parent_read,
                write_client=parent_write,
                mutations=mutations,
            ):
                flags_updated += 1
    return total_created, flags_updated


def live_state(plan: dict[str, Any], base_client: Base44Client) -> dict[str, Any]:
    parents = fetch_all(base_client.for_entity("Coin"), {"country": "EUA"})
    variant_client = base_client.for_entity("CoinVariant")
    variants: list[dict[str, Any]] = []
    # A global CoinVariant listing spans several pages.  During eventual
    # consistency, offset pagination can transiently repeat or skip records.
    # Each USA parent fits on one page, so parent-scoped reads are stable and
    # also avoid depending on unrelated countries.
    for item in plan["items"]:
        variants.extend(fetch_parent_variants(variant_client, item["coin_id"]))
    return reconcile_live(plan, parents, variants)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply and not args.yes:
        raise NormalCoinVariantError("--apply exige --yes.")
    plan_path = args.plan if args.plan.is_absolute() else PROJECT_ROOT / args.plan
    plan = load_plan(plan_path)
    if not (args.check_live or args.apply):
        print_plan(plan)
        print("Modo local: nenhuma chamada à API e nenhuma alteração.")
        return 0

    if args.check_live:
        base_client = create_client(args)
        state = live_state(plan, base_client)
        print_plan(plan, state)
        print("Site Base44: nenhuma alteração efetuada.")
        return 0

    with exclusive_apply_lock():
        base_client = create_client(args)
        state = live_state(plan, base_client)
        print_plan(plan, state)
        created, flags_updated = apply_missing(
            plan=plan,
            initial_state=state,
            base_client=base_client,
            batch_size=args.batch_size,
        )
        final_state = live_state(plan, base_client)
        if final_state["pending"] or final_state["existing"] != EXPECTED_VARIANTS:
            raise NormalCoinVariantError(
                "A verificação final não confirmou as 1.239 variantes."
            )
        if final_state["flags_pending"]:
            raise NormalCoinVariantError(
                "A verificação final encontrou has_variants pendentes."
            )
    print(
        f"Concluído: {created} CoinVariant criadas; {flags_updated} pais atualizados; "
        "1.239/1.239 variantes confirmadas."
    )
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except (NormalCoinVariantError, ValueError, RuntimeError) as exc:
        print(f"Erro: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
