#!/usr/bin/env python3
"""Normalize the 30 USA normal Coin records without recreating anything.

Only ``Coin.name``, ``Coin.years`` and ``Coin.notes`` may be updated.  The
approved plan contains the exact before/after state for every normal USA coin.
Its approved correction restores ``notes`` to the political period used by the
normal-country pipeline.  The executor is idempotent, accepts the short-lived
series-note state, uses non-retrying PUT requests, verifies each record after
writing and proves that all other fields stayed untouched.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any, Callable
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
    PROJECT_ROOT
    / "info/paises/america/eua/normal-coin-metadata-approved.json"
)
EXPECTED_RECORDS = 30
ALLOWED_FIELDS = {"name", "years", "notes"}
EXPECTED_NAMES = {"1 cêntimo", "5 cêntimos", "1 dime", "¼ dólar", "½ dólar", "1 dólar"}
EXPECTED_NOTES = "Estados Unidos da América"
IGNORED_PROTECTED_FIELDS = ALLOWED_FIELDS | {"updated_date"}


class NormalCoinMetadataError(RuntimeError):
    """Raised when the approved normalization cannot be proved safe."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Uniformiza denominações, anos e séries das 30 moedas normais "
            "dos EUA sem criar ou eliminar registos."
        )
    )
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--check-live",
        action="store_true",
        help="Valida o plano contra o Site Base44 sem escrever.",
    )
    modes.add_argument(
        "--apply",
        action="store_true",
        help="Aplica e verifica apenas name/years/notes.",
    )
    parser.add_argument("--yes", action="store_true", help="Confirma --apply.")
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
        raise NormalCoinMetadataError(f"Plano inválido ou ilegível: {path}") from exc


def load_plan(path: Path) -> dict[str, Any]:
    plan = read_json(path)
    if not isinstance(plan, dict):
        raise NormalCoinMetadataError("O plano não é um objeto JSON.")
    if plan.get("kind") != "eua_normal_coin_metadata_normalization":
        raise NormalCoinMetadataError("O plano não é da normalização Coin dos EUA.")
    if plan.get("status") != "approved_by_user" or plan.get("country") != "EUA":
        raise NormalCoinMetadataError("O plano não está aprovado para o país EUA.")
    safety = plan.get("safety")
    if not isinstance(safety, dict):
        raise NormalCoinMetadataError("O plano não documenta as garantias de segurança.")
    if set(safety.get("allowed_fields") or []) != ALLOWED_FIELDS:
        raise NormalCoinMetadataError("Os campos permitidos do plano são inválidos.")
    if safety.get("creates") != 0 or safety.get("deletes") != 0:
        raise NormalCoinMetadataError("O plano não garante zero criações e eliminações.")

    correction = plan.get("correction")
    if not isinstance(correction, dict):
        raise NormalCoinMetadataError("O plano não contém a correção aprovada de notes.")
    if correction.get("status") != "approved_by_user":
        raise NormalCoinMetadataError("A correção de notes não está aprovada.")
    if correction.get("final_notes") != EXPECTED_NOTES:
        raise NormalCoinMetadataError("A época política final de notes é inválida.")
    if correction.get("supersedes_notes_in_updates_set") is not True:
        raise NormalCoinMetadataError("A correção não substitui explicitamente as notes intermédias.")

    updates = plan.get("updates")
    if not isinstance(updates, list) or len(updates) != EXPECTED_RECORDS:
        raise NormalCoinMetadataError("O plano tem de conter exatamente 30 moedas.")

    ids: set[str] = set()
    urls: set[str] = set()
    for item in updates:
        if not isinstance(item, dict):
            raise NormalCoinMetadataError("O plano contém uma entrada inválida.")
        record_id = str(item.get("id") or "").strip()
        url_ucoin = str(item.get("url_ucoin") or "").strip()
        if not record_id or record_id in ids:
            raise NormalCoinMetadataError(f"ID ausente ou repetido: {record_id!r}")
        if not url_ucoin or url_ucoin in urls:
            raise NormalCoinMetadataError(f"URL uCoin ausente ou repetido: {url_ucoin!r}")
        ids.add(record_id)
        urls.add(url_ucoin)

        current = item.get("current")
        target = item.get("set")
        if not isinstance(current, dict) or set(current) != ALLOWED_FIELDS:
            raise NormalCoinMetadataError(f"Estado atual inválido em {record_id}.")
        if not isinstance(target, dict) or set(target) != ALLOWED_FIELDS:
            raise NormalCoinMetadataError(f"Patch inválido em {record_id}.")
        if current == target:
            raise NormalCoinMetadataError(f"Entrada sem alterações em {record_id}.")
        if target["name"] not in EXPECTED_NAMES:
            raise NormalCoinMetadataError(f"Denominação final inválida em {record_id}.")
        if not re.fullmatch(r"\d{4}( - \d{4})?", str(target["years"])):
            raise NormalCoinMetadataError(f"Anos finais inválidos em {record_id}.")
        if not str(target["notes"]).strip():
            raise NormalCoinMetadataError(f"Notes intermédias inválidas em {record_id}.")

        # ``set`` originally held series labels. They were applied briefly,
        # then the user confirmed that normal-coin notes must keep the ruler
        # of the catalogue period, consistently with every other country.
        item["accepted_intermediate"] = dict(target)
        item["set"] = {**target, "notes": EXPECTED_NOTES}
        if item["set"]["notes"] != EXPECTED_NOTES:
            raise NormalCoinMetadataError(f"Notes finais inválidas em {record_id}.")
    return plan


def records_by_id(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = str(record.get("id") or "").strip()
        if not record_id or record_id in result:
            raise NormalCoinMetadataError(f"ID live vazio ou repetido: {record_id!r}")
        result[record_id] = record
    return result


def approved_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {field: record.get(field) for field in ALLOWED_FIELDS}


def protected_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {
        field: value
        for field, value in record.items()
        if field not in IGNORED_PROTECTED_FIELDS
    }


def reconcile_live(
    plan: dict[str, Any], live: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(live) != EXPECTED_RECORDS:
        raise NormalCoinMetadataError(
            f"Esperavam-se 30 Coin normais dos EUA; existem {len(live)}."
        )
    live_by_id = records_by_id(live)
    planned_ids = {str(item["id"]) for item in plan["updates"]}
    if set(live_by_id) != planned_ids:
        missing = sorted(planned_ids - set(live_by_id))
        unexpected = sorted(set(live_by_id) - planned_ids)
        raise NormalCoinMetadataError(
            "O conjunto Coin dos EUA mudou "
            f"(em falta={missing[:5]}, inesperados={unexpected[:5]})."
        )

    pending: list[dict[str, Any]] = []
    complete: list[dict[str, Any]] = []
    for item in plan["updates"]:
        actual = live_by_id[item["id"]]
        if actual.get("country") != "EUA":
            raise NormalCoinMetadataError(f"País inesperado em {item['id']}.")
        if actual.get("url_ucoin") != item["url_ucoin"]:
            raise NormalCoinMetadataError(f"URL uCoin mudou em {item['id']}.")
        fields = approved_fields(actual)
        if fields == item["set"]:
            complete.append(item)
        elif fields in (item["current"], item["accepted_intermediate"]):
            pending.append(item)
        else:
            raise NormalCoinMetadataError(
                f"{item['id']} não está no estado inicial, intermédio ou final: {fields}."
            )
    return pending, complete


def get_record(client: Base44Client, record_id: str) -> dict[str, Any]:
    result = client.request("GET", f"{client.base_url}/{quote(record_id)}")
    if not isinstance(result, dict):
        raise NormalCoinMetadataError(f"GET de {record_id} devolveu {result!r}.")
    return result


def update_and_verify(
    *,
    read_client: Base44Client,
    write_client: Base44Client,
    item: dict[str, Any],
    before: dict[str, Any],
    mutation_success_fn: Callable[[], None],
) -> tuple[str, list[str]]:
    patch = {
        field: value
        for field, value in item["set"].items()
        if before.get(field) != value
    }
    if not patch:
        raise NormalCoinMetadataError(
            f"A Coin {item['id']} foi marcada como pendente sem diferenças reais."
        )
    update_error: Exception | None = None
    try:
        write_client.update(item["id"], patch)
        mutation_success_fn()
    except Exception as exc:  # noqa: BLE001 - reconcile ambiguous PUT with GET
        update_error = exc
    actual = get_record(read_client, item["id"])
    if approved_fields(actual) != item["set"]:
        if update_error is not None:
            raise NormalCoinMetadataError(
                f"PUT de {item['id']} falhou e o estado final não foi confirmado: "
                f"{update_error}"
            ) from update_error
        raise NormalCoinMetadataError(
            f"A Coin {item['id']} não corresponde ao plano após PUT."
        )
    if protected_fields(actual) != protected_fields(before):
        raise NormalCoinMetadataError(
            f"Um campo protegido foi alterado em {item['id']}."
        )
    if update_error is not None:
        mutation_success_fn()
        return "recovered_after_ambiguous_update", sorted(patch)
    return "updated", sorted(patch)


def changed_fields(item: dict[str, Any]) -> list[str]:
    return [field for field in sorted(ALLOWED_FIELDS) if item["current"][field] != item["set"][field]]


def print_plan(plan: dict[str, Any]) -> None:
    field_changes = sum(len(changed_fields(item)) for item in plan["updates"])
    print("MOEDAS NORMAIS DOS EUA — DENOMINAÇÕES, ÉPOCAS E NOTES")
    print(f"Moedas no plano: {len(plan['updates'])}")
    print(f"Campos a alterar: {field_changes}")
    print("Campos permitidos: name, years, notes")
    print("Criações: 0 · Eliminações: 0")
    print("URLs, fotografias, estado, variantes e descobertas: preservados")
    print("Denominações finais: " + ", ".join(sorted(EXPECTED_NAMES)))
    print(f"Notes finais em todas as moedas: {EXPECTED_NOTES}")


def apply_updates(
    *,
    pending: list[dict[str, Any]],
    live: list[dict[str, Any]],
    base_client: Base44Client,
) -> int:
    if not pending:
        print("Nenhuma alteração pendente.")
        return 0
    live_by_id = records_by_id(live)
    read_client = base_client.for_entity("Coin")
    write_client = no_retry_entity_client(base_client, "Coin")
    started = time.monotonic()
    durations = 0.0
    with MutationRun(
        lambda: mark_numisvault_stats_stale(base_client),
        mutation_label="Coin record",
    ) as mutations:
        for position, item in enumerate(pending, start=1):
            item_started = time.monotonic()
            _, actual_changed_fields = update_and_verify(
                read_client=read_client,
                write_client=write_client,
                item=item,
                before=live_by_id[item["id"]],
                mutation_success_fn=mutations.record_success,
            )
            item_duration = time.monotonic() - item_started
            durations += item_duration
            remaining = len(pending) - position
            eta = durations / position * remaining
            print(
                f"Atualizado: {position}/{len(pending)} "
                f"({position / len(pending) * 100:.1f}%) — EUA — "
                f"{item['set']['name']} ({item['set']['years']}) — "
                f"{item['set']['notes']} — {', '.join(actual_changed_fields)} "
                f"[faltam: {remaining} | nesta: {format_duration(item_duration)} | "
                f"decorrido: {format_duration(time.monotonic() - started)} | "
                f"restante: {'concluído' if not remaining else '~' + format_duration(eta)}]"
            )
    return len(pending)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply and not args.yes:
        raise NormalCoinMetadataError("--apply exige --yes.")
    plan_path = args.plan
    if not plan_path.is_absolute():
        plan_path = PROJECT_ROOT / plan_path
    plan = load_plan(plan_path)
    print_plan(plan)
    if not (args.check_live or args.apply):
        print("Modo local: nenhuma chamada à API e nenhuma alteração.")
        return 0

    base_client = create_client(args)
    live = fetch_all(base_client.for_entity("Coin"), {"country": "EUA"})
    pending, complete = reconcile_live(plan, live)
    print(f"Já corretas: {len(complete)}/{EXPECTED_RECORDS}")
    print(f"Pendentes: {len(pending)}/{EXPECTED_RECORDS}")
    if args.check_live:
        print("Site Base44: nenhuma alteração efetuada.")
        return 0

    updated = apply_updates(pending=pending, live=live, base_client=base_client)
    final_live = fetch_all(base_client.for_entity("Coin"), {"country": "EUA"})
    final_pending, final_complete = reconcile_live(plan, final_live)
    if final_pending or len(final_complete) != EXPECTED_RECORDS:
        raise NormalCoinMetadataError("A verificação final não confirmou as 30 moedas.")
    print(f"Concluído: {updated} atualizadas; 30/30 confirmadas.")
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except NormalCoinMetadataError as exc:
        print(f"Erro: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
