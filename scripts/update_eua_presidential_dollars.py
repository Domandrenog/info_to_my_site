#!/usr/bin/env python3
"""Order the USA Presidential Dollars chronologically and prefix ordinals.

Only ``SpecialCoin.commemorative_name`` and ``SpecialCoin.ordem`` may be
updated.  The operation is idempotent, validates the exact 40-record programme
before writing, does not retry ambiguous PUT requests, and verifies every
result with a fresh GET.
"""

from __future__ import annotations

import argparse
import json
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
    / "info/paises/america/eua/presidential-dollars-order-approved.json"
)
EXPECTED_USA_SPECIAL_COINS = 245
EXPECTED_RECORDS = 40
EXPECTED_NUMBERS = [*range(1, 39), 40, 41]
EXPECTED_ORDERS = list(range(50, 90))
ALLOWED_FIELDS = {"commemorative_name", "ordem"}


class PresidentialDollarError(RuntimeError):
    """Raised when the approved programme cannot be proved safe to update."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Organiza os 40 Presidential Dollars dos EUA por presidente e "
            "adiciona 1st, 2nd, 3rd, etc. antes do nome."
        )
    )
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--check-live",
        action="store_true",
        help="Valida o estado do Site Base44 sem escrever.",
    )
    modes.add_argument(
        "--apply",
        action="store_true",
        help="Aplica e verifica apenas os campos aprovados.",
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
        raise PresidentialDollarError(f"Plano inválido ou ilegível: {path}") from exc


def ordinal(number: int) -> str:
    if 10 < number % 100 < 14:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def expected_target_name(item: dict[str, Any]) -> str:
    current_name = str(item["current"]["commemorative_name"])
    base_name = current_name.removesuffix(" (1st)").removesuffix(" (2nd)")
    return f"{ordinal(item['president_number'])} - {base_name}"


def load_plan(path: Path) -> dict[str, Any]:
    plan = read_json(path)
    if not isinstance(plan, dict):
        raise PresidentialDollarError("O plano não é um objeto JSON.")
    if plan.get("kind") != "eua_presidential_dollars_chronological_order":
        raise PresidentialDollarError("O plano não é dos Presidential Dollars.")
    if plan.get("country") != "EUA" or plan.get("programme") != "Presidential Dollars":
        raise PresidentialDollarError("O país ou programa do plano é inválido.")
    updates = plan.get("updates")
    if not isinstance(updates, list) or len(updates) != EXPECTED_RECORDS:
        raise PresidentialDollarError("O plano tem de conter exatamente 40 moedas.")

    ids: set[str] = set()
    numbers: list[int] = []
    orders: list[int] = []
    target_names: set[str] = set()
    for item in updates:
        if not isinstance(item, dict):
            raise PresidentialDollarError("O plano contém uma entrada inválida.")
        record_id = str(item.get("id") or "").strip()
        if not record_id or record_id in ids:
            raise PresidentialDollarError(f"ID ausente ou repetido: {record_id!r}")
        ids.add(record_id)
        if set(item.get("current", {})) != ALLOWED_FIELDS:
            raise PresidentialDollarError(f"Estado atual inválido em {record_id}.")
        if set(item.get("set", {})) != ALLOWED_FIELDS:
            raise PresidentialDollarError(f"Patch inválido em {record_id}.")
        if item["set"]["commemorative_name"] != expected_target_name(item):
            raise PresidentialDollarError(f"Ordinal ou nome incorreto em {record_id}.")
        numbers.append(item.get("president_number"))
        orders.append(item["set"]["ordem"])
        target_names.add(item["set"]["commemorative_name"])
        if not str(item.get("year") or "").strip():
            raise PresidentialDollarError(f"Ano em falta em {record_id}.")

    if numbers != EXPECTED_NUMBERS:
        raise PresidentialDollarError("A sequência presidencial aprovada é inválida.")
    if orders != EXPECTED_ORDERS:
        raise PresidentialDollarError("A sequência de ordem tem de ser 50–89.")
    if len(target_names) != EXPECTED_RECORDS:
        raise PresidentialDollarError("Os nomes finais não são únicos.")
    summary = plan.get("summary", {})
    if summary.get("missing_president_numbers") != [39]:
        raise PresidentialDollarError("O plano tem de documentar a ausência do 39th.")
    return plan


def records_by_id(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = str(record.get("id") or "").strip()
        if not record_id or record_id in result:
            raise PresidentialDollarError(f"ID live vazio ou repetido: {record_id!r}")
        result[record_id] = record
    return result


def approved_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {field: record.get(field) for field in ALLOWED_FIELDS}


def reconcile_live(
    plan: dict[str, Any], live: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(live) != EXPECTED_USA_SPECIAL_COINS:
        raise PresidentialDollarError(
            f"Esperavam-se 245 SpecialCoin dos EUA; existem {len(live)}."
        )
    live_by_id = records_by_id(live)
    planned_ids = {item["id"] for item in plan["updates"]}
    programme_ids = {
        str(record["id"])
        for record in live
        if record.get("notes") == "Presidential Dollars"
    }
    if programme_ids != planned_ids:
        missing = sorted(planned_ids - programme_ids)
        unexpected = sorted(programme_ids - planned_ids)
        raise PresidentialDollarError(
            "O conjunto Presidential Dollars mudou "
            f"(em falta={missing[:5]}, inesperados={unexpected[:5]})."
        )

    pending: list[dict[str, Any]] = []
    complete: list[dict[str, Any]] = []
    for item in plan["updates"]:
        actual = live_by_id[item["id"]]
        invariant = {
            "country": actual.get("country"),
            "name": actual.get("name"),
            "notes": actual.get("notes"),
            "year": actual.get("year"),
        }
        expected_invariant = {
            "country": "EUA",
            "name": "1 dólar",
            "notes": "Presidential Dollars",
            "year": item["year"],
        }
        if invariant != expected_invariant:
            raise PresidentialDollarError(
                f"A identidade de {item['id']} mudou: {invariant}."
            )
        current = approved_fields(actual)
        if current == item["set"]:
            complete.append(item)
        elif current == item["current"]:
            pending.append(item)
        else:
            raise PresidentialDollarError(
                f"{item['id']} não está nem no estado inicial nem no final: {current}."
            )
    return pending, complete


def get_record(client: Base44Client, record_id: str) -> dict[str, Any]:
    result = client.request("GET", f"{client.base_url}/{quote(record_id)}")
    if not isinstance(result, dict):
        raise PresidentialDollarError(f"GET de {record_id} devolveu {result!r}.")
    return result


def update_and_verify(
    *,
    read_client: Base44Client,
    write_client: Base44Client,
    item: dict[str, Any],
    mutation_success_fn: Callable[[], None],
) -> str:
    update_error: Exception | None = None
    try:
        write_client.update(item["id"], item["set"])
        mutation_success_fn()
    except Exception as exc:  # noqa: BLE001 - reconcile ambiguous PUT with GET
        update_error = exc
    actual = get_record(read_client, item["id"])
    if approved_fields(actual) != item["set"]:
        if update_error is not None:
            raise PresidentialDollarError(
                f"PUT de {item['id']} falhou e o estado final não foi confirmado: "
                f"{update_error}"
            ) from update_error
        raise PresidentialDollarError(
            f"A SpecialCoin {item['id']} não corresponde ao plano após PUT."
        )
    if update_error is not None:
        mutation_success_fn()
        return "recovered_after_ambiguous_update"
    return "updated"


def print_plan(plan: dict[str, Any]) -> None:
    print("PRESIDENTIAL DOLLARS — ORDEM CRONOLÓGICA")
    print(f"Moedas no plano: {len(plan['updates'])}")
    print("Campos permitidos: commemorative_name, ordem")
    print("Intervalo de ordem preservado: 50–89")
    print("39th: ausente do conjunto atual; não será inventado")
    print("Criações: 0 · Eliminações: 0")
    first = plan["updates"][0]["set"]["commemorative_name"]
    last = plan["updates"][-1]["set"]["commemorative_name"]
    print(f"Primeiro: {first}")
    print(f"Último: {last}")


def apply_updates(
    *,
    plan: dict[str, Any],
    pending: list[dict[str, Any]],
    base_client: Base44Client,
) -> int:
    if not pending:
        print("Nenhuma alteração pendente.")
        return 0
    read_client = base_client.for_entity("SpecialCoin")
    write_client = no_retry_entity_client(base_client, "SpecialCoin")
    started = time.monotonic()
    durations = 0.0
    with MutationRun(
        lambda: mark_numisvault_stats_stale(base_client),
        mutation_label="SpecialCoin record",
    ) as mutations:
        for position, item in enumerate(pending, start=1):
            item_started = time.monotonic()
            update_and_verify(
                read_client=read_client,
                write_client=write_client,
                item=item,
                mutation_success_fn=mutations.record_success,
            )
            item_duration = time.monotonic() - item_started
            durations += item_duration
            remaining = len(pending) - position
            eta = durations / position * remaining
            print(
                f"Atualizado: {position}/{len(pending)} "
                f"({position / len(pending) * 100:.1f}%) — "
                f"{item['set']['commemorative_name']} ({item['year']}) "
                f"[faltam: {remaining} | nesta: {format_duration(item_duration)} | "
                f"decorrido: {format_duration(time.monotonic() - started)} | "
                f"restante: {'concluído' if not remaining else '~' + format_duration(eta)}]"
            )
    return len(pending)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply and not args.yes:
        raise PresidentialDollarError("--apply exige --yes.")
    plan_path = args.plan
    if not plan_path.is_absolute():
        plan_path = PROJECT_ROOT / plan_path
    plan = load_plan(plan_path)
    print_plan(plan)
    if not (args.check_live or args.apply):
        print("Modo local: nenhuma chamada à API e nenhuma alteração.")
        return 0

    base_client = create_client(args)
    live = fetch_all(base_client.for_entity("SpecialCoin"), {"country": "EUA"})
    pending, complete = reconcile_live(plan, live)
    print(f"Já corretas: {len(complete)}/{EXPECTED_RECORDS}")
    print(f"Pendentes: {len(pending)}/{EXPECTED_RECORDS}")
    if args.check_live:
        print("Site Base44: nenhuma alteração efetuada.")
        return 0

    updated = apply_updates(plan=plan, pending=pending, base_client=base_client)
    final_live = fetch_all(base_client.for_entity("SpecialCoin"), {"country": "EUA"})
    final_pending, final_complete = reconcile_live(plan, final_live)
    if final_pending or len(final_complete) != EXPECTED_RECORDS:
        raise PresidentialDollarError("A verificação final não confirmou as 40 moedas.")
    print(f"Concluído: {updated} atualizadas; 40/40 confirmadas.")
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except PresidentialDollarError as exc:
        print(f"Erro: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
