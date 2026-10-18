#!/usr/bin/env python3
"""Move the 19 known Portugal commemoratives from Coin to SpecialCoin.

The migration is deliberately bound to a verified complete Base44 backup and
to an immutable set of uCoin type IDs.  It first creates and verifies all
SpecialCoin records, then (and only then) deletes the corresponding legacy
Coin records.  The current audited set has no variants or sightings; the
script fails closed if either relation appears before execution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from scripts.admin_stats_sync import MutationRun, mark_numisvault_stats_stale
from scripts.execute_eua_commemorative_migration import (
    MigrationExecutionError,
    create_and_verify_one,
    fetch_all,
    no_retry_entity_client,
    payload_differences,
    special_coin_identity,
)
from scripts.import_base44_coins import create_client


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BACKUP_ROOT = PROJECT_ROOT / "backups"
DEFAULT_STATE_ROOT = DEFAULT_BACKUP_ROOT / "migrations"
SOURCE_EXPORT = Path("europa/portugal/normal.json")
COUNTRY = "Portugal"
CONTINENT = "Europa"
EXPECTED_ITEMS = 19
TARGET_COPY_FIELDS = (
    "country",
    "continent",
    "condition",
    "image_frente",
    "image_verso",
    "adquirida_por",
    "data_aquisicao",
    "local_compra",
    "valor_pago",
    "moeda_valor",
    "url_numista",
    "url_ucoin",
    "hidden",
)
SYSTEM_FIELDS = {
    "id",
    "created_date",
    "updated_date",
    "created_by",
    "created_by_id",
    "is_sample",
}

# Order is explicit and immutable: programmes stay together and coins are
# chronological inside each programme (denomination breaks same-year ties).
SELECTION = (
    (76446, "Infante D. Henrique \"O Navegador\"", "ECU"),
    (76454, "João II", "ECU"),
    (76577, "Bartolomeu Dias", "ECU"),
    (76456, "Tesouros Numismáticos - Português de D. Manuel I", "ECU"),
    (76250, "Vasco da Gama", "ECU"),
    (76238, "Pedro Alvares Cabral", "ECU"),
    (76581, "Filipa de Lencastre", "ECU"),
    (76237, "Fernão de Magalhães", "ECU"),
    (76582, "Isabel de Portugal", "ECU"),
    (76251, "Afonso de Albuquerque", "ECU"),
    (108505, "Rainhas da Europa – D. Leonor de Portugal", "ECU"),
    (76583, "Rainhas da Europa – D. Leonor de Portugal", "ECU"),
    (21647, "Região Autónoma dos Açores", "Região Autónoma dos Açores"),
    (21512, "Açores", "Região Autónoma dos Açores"),
    (17784, "Açores", "Região Autónoma dos Açores"),
    (16923, "Portugal Universal – Antero de Quental", "Região Autónoma dos Açores"),
    (16924, "Autonomia dos Açores", "Região Autónoma dos Açores"),
    (29018, "Região Autónoma da Madeira", "Região Autónoma da Madeira"),
    (21513, "Região Autónoma da Madeira", "Região Autónoma da Madeira"),
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audita ou migra exatamente as 19 comemorativas portuguesas "
            "misturadas em Coin para SpecialCoin."
        )
    )
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--state", type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-live", action="store_true")
    modes.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--request-delay", type=float, default=0.15)
    parser.add_argument("--rate-limit-delay", type=float, default=60.0)
    parser.add_argument("--max-retries", type=int, default=3)
    return parser.parse_args(argv)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationExecutionError(f"JSON inválido ou ilegível: {path}") from exc


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_backup(args: argparse.Namespace) -> Path:
    if args.backup is not None:
        result = args.backup.resolve()
    else:
        pointer = args.backup_root / "LATEST"
        backup_name = pointer.read_text(encoding="utf-8").strip()
        if not backup_name or Path(backup_name).name != backup_name:
            raise MigrationExecutionError(f"Ponteiro de backup inválido: {pointer}")
        result = (args.backup_root / backup_name).resolve()
    if not result.is_dir():
        raise MigrationExecutionError(f"Backup inexistente: {result}")
    return result


def verified_source_entries(backup: Path) -> list[dict[str, Any]]:
    manifest = read_json(backup / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("status") != "complete":
        raise MigrationExecutionError("O backup não está marcado como completo.")
    scope = manifest.get("scope")
    if not isinstance(scope, dict) or not scope.get("complete_item_views"):
        raise MigrationExecutionError("O backup não garante relações completas.")
    exports = manifest.get("country_exports")
    matches = [
        item
        for item in exports or []
        if isinstance(item, dict) and item.get("file") == SOURCE_EXPORT.as_posix()
    ]
    if len(matches) != 1:
        raise MigrationExecutionError(
            f"Era esperado um export {SOURCE_EXPORT}; encontrados: {len(matches)}."
        )
    metadata = matches[0]
    if (
        metadata.get("country") != COUNTRY
        or metadata.get("continent") != CONTINENT
        or metadata.get("kind") != "normal"
    ):
        raise MigrationExecutionError("Metadados do export de Portugal inválidos.")
    source_path = backup / SOURCE_EXPORT
    if not source_path.is_file() or sha256_file(source_path) != metadata.get("sha256"):
        raise MigrationExecutionError("O export de Portugal não corresponde ao manifest.")
    entries = read_json(source_path)
    if not isinstance(entries, list) or metadata.get("records") != len(entries):
        raise MigrationExecutionError("Contagem do export de Portugal inválida.")
    return entries


def ucoin_tid(value: Any) -> int | None:
    match = re.search(r"[?&]tid=(\d+)", str(value or ""))
    return int(match.group(1)) if match else None


def build_plan(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expected = {tid for tid, _subject, _programme in SELECTION}
    by_tid: dict[int, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("entity") != "Coin":
            raise MigrationExecutionError("O export contém uma entrada que não é Coin.")
        record = entry.get("record")
        variants = entry.get("variants")
        sightings = entry.get("sightings")
        if not isinstance(record, dict) or not isinstance(variants, list) or not isinstance(sightings, list):
            raise MigrationExecutionError("Entrada Coin incompleta no backup.")
        tid = ucoin_tid(record.get("url_ucoin"))
        if tid not in expected:
            continue
        if tid in by_tid:
            raise MigrationExecutionError(f"uCoin tid repetido no Site: {tid}")
        if record.get("country") != COUNTRY or record.get("continent") != CONTINENT:
            raise MigrationExecutionError(f"País/continente inesperado no tid {tid}.")
        if variants or sightings or record.get("has_variants"):
            raise MigrationExecutionError(
                f"O tid {tid} ganhou variantes ou sinalizações; é necessária nova revisão."
            )
        by_tid[tid] = entry
    missing = sorted(expected - by_tid.keys())
    if missing or len(by_tid) != EXPECTED_ITEMS:
        raise MigrationExecutionError(f"Seleção incompleta; tids em falta: {missing}")

    plan: list[dict[str, Any]] = []
    identities: set[tuple[str, str, str, str]] = set()
    for order, (tid, subject, programme) in enumerate(SELECTION, start=1):
        entry = by_tid[tid]
        source = entry["record"]
        payload = {field: deepcopy(source.get(field)) for field in TARGET_COPY_FIELDS}
        payload.update(
            {
                "name": str(source.get("name") or "").strip(),
                "commemorative_name": subject,
                "year": str(source.get("years") or "").strip(),
                "notes": programme,
                "ordem": order,
                "has_variants": False,
            }
        )
        if SYSTEM_FIELDS & payload.keys():
            raise MigrationExecutionError(f"Campos de sistema no payload do tid {tid}.")
        identity = special_coin_identity(payload)
        if identity in identities:
            raise MigrationExecutionError(f"Identidade SpecialCoin repetida: {identity}")
        identities.add(identity)
        plan.append(
            {
                "tid": tid,
                "order": order,
                "source_coin_id": str(source.get("id") or ""),
                "source_record": deepcopy(source),
                "target_payload": payload,
            }
        )
    return plan


def default_state_path(backup: Path) -> Path:
    return DEFAULT_STATE_ROOT / f"portugal-commemorativas-{backup.name}.json"


def load_state(path: Path, *, backup: Path, plan: list[dict[str, Any]]) -> dict[str, Any]:
    fingerprint = hashlib.sha256(canonical_json(plan)).hexdigest()
    if not path.exists():
        return {
            "format_version": 1,
            "backup": backup.name,
            "backup_manifest_sha256": sha256_file(backup / "manifest.json"),
            "plan_sha256": fingerprint,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "targets": {},
            "deleted_sources": {},
            "status": "planned",
        }
    state = read_json(path)
    if (
        not isinstance(state, dict)
        or state.get("backup") != backup.name
        or state.get("backup_manifest_sha256") != sha256_file(backup / "manifest.json")
        or state.get("plan_sha256") != fingerprint
        or not isinstance(state.get("targets"), dict)
        or not isinstance(state.get("deleted_sources"), dict)
    ):
        raise MigrationExecutionError("O checkpoint não corresponde a este plano/backup.")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
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
            raise MigrationExecutionError(f"Falhou a verificação do checkpoint {path}.")
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def records_by_id(records: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = str(record.get("id") or "").strip()
        if not record_id or record_id in result:
            raise MigrationExecutionError(f"{label}: ID ausente ou repetido {record_id!r}.")
        result[record_id] = record
    return result


def assert_source_snapshot(
    plan: list[dict[str, Any]], live_coins: list[dict[str, Any]], deleted: set[str]
) -> None:
    live_by_id = records_by_id(live_coins, "Coin Portugal live")
    for item in plan:
        source_id = item["source_coin_id"]
        actual = live_by_id.get(source_id)
        if source_id in deleted:
            if actual is not None:
                raise MigrationExecutionError(f"Coin já apagada reapareceu: {source_id}")
            continue
        if actual != item["source_record"]:
            raise MigrationExecutionError(
                f"Coin de origem {source_id} mudou desde o backup; migração interrompida."
            )


def fetch_all_stable(client: Any, query: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    page_size = 1000
    skip = 0
    while True:
        page = client.filter(
            query, limit=page_size, skip=skip, sort="id"
        )
        if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
            raise MigrationExecutionError(f"Resposta Base44 inesperada: {page!r}")
        for row in page:
            record_id = str(row.get("id") or "")
            if not record_id or record_id in seen:
                raise MigrationExecutionError(
                    f"Paginação live devolveu ID ausente/repetido: {record_id!r}"
                )
            seen.add(record_id)
            records.append(row)
        if len(page) < page_size:
            return records
        skip += page_size


def assert_no_source_relations(base_client: Any, plan: list[dict[str, Any]]) -> None:
    source_ids = {item["source_coin_id"] for item in plan}
    variants = [
        row
        for row in fetch_all_stable(base_client.for_entity("CoinVariant"), {})
        if str(row.get("coin_id") or "") in source_ids
    ]
    sightings = [
        row
        for row in fetch_all_stable(base_client.for_entity("CoinSighting"), {})
        if str(row.get("coin_id") or "") in source_ids
        or str(row.get("item_id") or "") in source_ids
    ]
    if variants or sightings:
        raise MigrationExecutionError(
            f"As origens ganharam {len(variants)} variantes e "
            f"{len(sightings)} sinalizações; migração interrompida."
        )


def reconcile_targets(
    plan: list[dict[str, Any]], live: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    if any(record.get("country") != COUNTRY for record in live):
        raise MigrationExecutionError("A consulta de SpecialCoin devolveu outro país.")
    planned_identities = {
        special_coin_identity(item["target_payload"]): item for item in plan
    }
    by_identity: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for record in live:
        by_identity.setdefault(special_coin_identity(record), []).append(record)
    unexpected = sorted(set(by_identity) - set(planned_identities))
    if unexpected:
        raise MigrationExecutionError(
            f"Já existem SpecialCoin portuguesas fora deste plano: {unexpected[:3]}"
        )
    pending: list[dict[str, Any]] = []
    existing: dict[int, dict[str, Any]] = {}
    for identity, item in planned_identities.items():
        matches = by_identity.get(identity, [])
        if len(matches) > 1:
            raise MigrationExecutionError(f"SpecialCoin repetida: {identity}")
        if not matches:
            pending.append(item)
            continue
        actual = matches[0]
        differences = payload_differences(item["target_payload"], actual)
        if differences:
            raise MigrationExecutionError(
                f"SpecialCoin existente diverge no tid {item['tid']}: "
                + json.dumps(differences, ensure_ascii=False, sort_keys=True)
            )
        existing[item["tid"]] = actual
    pending.sort(key=lambda item: item["order"])
    return pending, existing


def format_duration(seconds: float) -> str:
    rounded = max(0, round(seconds))
    minutes, remainder = divmod(rounded, 60)
    return f"{minutes}m {remainder:02d}s" if minutes else f"{remainder}s"


def progress(label: str, position: int, total: int, started: float) -> str:
    elapsed = time.monotonic() - started
    remaining = total - position
    eta = elapsed / position * remaining if position else 0
    return (
        f"{position}/{total} ({position / total * 100:.1f}%) — {label} "
        f"[faltam: {remaining} | decorrido: {format_duration(elapsed)} | "
        f"restante: {'concluído' if not remaining else '~' + format_duration(eta)}]"
    )


def recover_interrupted_deletions(
    *,
    plan: list[dict[str, Any]],
    live_coins: list[dict[str, Any]],
    confirmed_targets: dict[int, dict[str, Any]],
    state: dict[str, Any],
) -> int:
    """Checkpoint sources proven absent after an interrupted DELETE response."""

    live_ids = {str(record.get("id") or "") for record in live_coins}
    recovered = 0
    for item in plan:
        source_id = item["source_coin_id"]
        if source_id in live_ids or source_id in state["deleted_sources"]:
            continue
        if item["tid"] not in confirmed_targets:
            raise MigrationExecutionError(
                f"Coin de origem {source_id} desapareceu sem SpecialCoin confirmada."
            )
        state["deleted_sources"][source_id] = {
            "tid": item["tid"],
            "result": "recovered_missing_after_interrupted_delete",
            "verified_at": utc_now(),
            "source_record": item["source_record"],
        }
        recovered += 1
    return recovered


def delete_and_verify(
    *, read_client: Any, write_client: Any, record_id: str
) -> str:
    error: Exception | None = None
    try:
        write_client.request("DELETE", f"{write_client.base_url}/{quote(record_id)}")
    except Exception as exc:  # noqa: BLE001 - reconcile ambiguous DELETE
        error = exc
    matches = read_client.filter({"id": record_id}, limit=2, skip=0)
    if isinstance(matches, list) and not matches:
        return "deleted" if error is None else "recovered_after_ambiguous_delete"
    if error is not None:
        raise MigrationExecutionError(
            f"DELETE de {record_id} falhou e a Coin continua presente: {error}"
        ) from error
    raise MigrationExecutionError(f"Coin {record_id} continua presente após DELETE.")


def print_plan(plan: list[dict[str, Any]], backup: Path) -> None:
    print("\n" + "=" * 72)
    print("MIGRAÇÃO PORTUGAL — COIN → SPECIALCOIN")
    print("=" * 72)
    print(f"Backup: {backup.name}")
    for item in plan:
        payload = item["target_payload"]
        print(
            f"{item['order']:02d}. {payload['notes']} — {payload['name']} — "
            f"{payload['commemorative_name']} ({payload['year']})"
        )
    print("SpecialCoin a garantir: 19")
    print("Coin antigas a remover apenas depois da verificação: 19")
    print("CoinVariant/CoinSighting a migrar: 0")


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply and not args.yes:
        raise MigrationExecutionError("--apply exige --yes.")
    backup = resolve_backup(args)
    plan = build_plan(verified_source_entries(backup))
    state_path = args.state or default_state_path(backup)
    state = load_state(state_path, backup=backup, plan=plan)
    print_plan(plan, backup)
    if not args.check_live and not args.apply:
        print("Site Base44: nenhuma leitura live e nenhuma alteração efetuada.")
        return 0

    base_client = create_client(args)
    live_coins = fetch_all(base_client.for_entity("Coin"), {"country": COUNTRY})
    live_special = fetch_all(
        base_client.for_entity("SpecialCoin"), {"country": COUNTRY}
    )
    pending, existing = reconcile_targets(plan, live_special)
    created_phase_required = bool(pending)
    recovered = recover_interrupted_deletions(
        plan=plan,
        live_coins=live_coins,
        confirmed_targets=existing,
        state=state,
    )
    if recovered:
        state["status"] = "deleting_sources"
        save_state(state_path, state)
        print(f"Eliminações interrompidas reconciliadas: {recovered}")
    deleted = set(state["deleted_sources"])
    assert_source_snapshot(plan, live_coins, deleted)
    if created_phase_required:
        assert_no_source_relations(base_client, plan)
    print(f"Coin de origem confirmadas: {EXPECTED_ITEMS - len(deleted)}/{EXPECTED_ITEMS}")
    print(f"SpecialCoin confirmadas: {len(existing)}/{EXPECTED_ITEMS}")
    print(f"SpecialCoin ainda por criar: {len(pending)}")
    print(f"Coin antigas já removidas: {len(deleted)}/{EXPECTED_ITEMS}")
    if args.check_live:
        print("Site Base44: nenhuma alteração efetuada.")
        return 0

    special_read = base_client.for_entity("SpecialCoin")
    special_write = no_retry_entity_client(base_client, "SpecialCoin")
    started = time.monotonic()
    with MutationRun(
        lambda: mark_numisvault_stats_stale(base_client),
        mutation_label="SpecialCoin Portugal record",
    ) as mutations:
        for position, item in enumerate(pending, start=1):
            actual, result = create_and_verify_one(
                read_client=special_read,
                write_client=special_write,
                payload=item["target_payload"],
                mutation_success_fn=mutations.record_success,
            )
            state["targets"][str(item["tid"])] = {
                "id": actual["id"],
                "source_coin_id": item["source_coin_id"],
                "result": result,
                "verified_at": utc_now(),
            }
            state["status"] = "creating_targets"
            save_state(state_path, state)
            payload = item["target_payload"]
            print(
                "Criado e verificado: "
                + progress(
                    f"Portugal — {payload['name']} — {payload['commemorative_name']}",
                    position,
                    len(pending),
                    started,
                )
            )

    live_special = fetch_all(special_read, {"country": COUNTRY})
    remaining_targets, confirmed_targets = reconcile_targets(plan, live_special)
    if remaining_targets or len(confirmed_targets) != EXPECTED_ITEMS:
        raise MigrationExecutionError("As 19 SpecialCoin não ficaram confirmadas.")
    state["status"] = "targets_verified"
    save_state(state_path, state)
    print("Fase SpecialCoin confirmada: 19/19.")

    # Re-check every source immediately before the destructive phase.
    live_coins = fetch_all(base_client.for_entity("Coin"), {"country": COUNTRY})
    assert_source_snapshot(plan, live_coins, set(state["deleted_sources"]))
    assert_no_source_relations(base_client, plan)

    coin_read = base_client.for_entity("Coin")
    coin_write = no_retry_entity_client(base_client, "Coin")
    pending_deletions = [
        item for item in plan if item["source_coin_id"] not in state["deleted_sources"]
    ]
    started = time.monotonic()
    with MutationRun(
        lambda: mark_numisvault_stats_stale(base_client),
        mutation_label="legacy Portugal Coin record",
    ) as mutations:
        for position, item in enumerate(pending_deletions, start=1):
            result = delete_and_verify(
                read_client=coin_read,
                write_client=coin_write,
                record_id=item["source_coin_id"],
            )
            mutations.record_success()
            state["deleted_sources"][item["source_coin_id"]] = {
                "tid": item["tid"],
                "result": result,
                "verified_at": utc_now(),
                "source_record": item["source_record"],
            }
            state["status"] = "deleting_sources"
            save_state(state_path, state)
            print(
                "Removido e verificado: "
                + progress(
                    f"Portugal — Coin antiga tid={item['tid']}",
                    position,
                    len(pending_deletions),
                    started,
                )
            )

    final_coins = fetch_all(base_client.for_entity("Coin"), {"country": COUNTRY})
    assert_source_snapshot(plan, final_coins, set(state["deleted_sources"]))
    final_special = fetch_all(special_read, {"country": COUNTRY})
    pending, confirmed = reconcile_targets(plan, final_special)
    if pending or len(confirmed) != EXPECTED_ITEMS or len(state["deleted_sources"]) != EXPECTED_ITEMS:
        raise MigrationExecutionError("A verificação final 1:1 falhou.")
    state["status"] = "complete"
    state["completed_at"] = utc_now()
    state["summary"] = {
        "special_coins": EXPECTED_ITEMS,
        "deleted_legacy_coins": EXPECTED_ITEMS,
        "variants": 0,
        "sightings": 0,
        "remaining_portugal_normal_coins": len(final_coins),
    }
    save_state(state_path, state)
    print("\nMIGRAÇÃO CONCLUÍDA E VERIFICADA 1:1")
    print(f"SpecialCoin Portugal: {len(final_special)}")
    print(f"Coin Portugal restantes: {len(final_coins)}")
    print("Variantes/sinalizações perdidas: 0")
    print(f"Checkpoint privado: {state_path.relative_to(PROJECT_ROOT)}")
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except (MigrationExecutionError, OSError) as exc:
        print(f"Erro: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
