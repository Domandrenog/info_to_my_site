#!/usr/bin/env python3
"""Delete only the validated legacy USA commemorative Coin records.

The cleanup is deliberately narrow and resumable.  It derives the exact 274
``CoinVariant`` and 238 ``Coin`` IDs from a fresh successful pre-cleanup audit,
checks the live Base44 snapshot against the backup before the first deletion,
deletes variants before parents, and writes a private checkpoint after every
confirmed mutation.  ``SpecialCoin``, ``SpecialCoinVariant`` and
``CoinSighting`` are read and verified but never mutated by this script.
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
from urllib.parse import quote

from scripts import audit_eua_pre_cleanup as pre_cleanup
from scripts.admin_stats_sync import mark_numisvault_stats_stale
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
BACKUP_ROOT = PROJECT_ROOT / "backups"
CLEANUP_ROOT = BACKUP_ROOT / "cleanup"
AUDIT_ROOT = BACKUP_ROOT / "audits"
EXPECTED_COIN_IDS = 238
EXPECTED_VARIANT_IDS = 274
READ_ONLY_ENTITIES = ("SpecialCoin", "SpecialCoinVariant", "CoinSighting")
MUTATED_ENTITIES = ("CoinVariant", "Coin")
REQUIRED_ENTITIES = (*MUTATED_ENTITIES, *READ_ONLY_ENTITIES)


class CleanupError(RuntimeError):
    """Raised whenever the exact safe deletion set cannot be proven."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Remove apenas as 274 CoinVariant e 238 Coin comemorativas antigas "
            "dos EUA já validadas pela auditoria pré-limpeza."
        )
    )
    parser.add_argument(
        "--backup",
        type=Path,
        default=None,
        help="Backup completo; por omissão usa backups/LATEST.",
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        default=None,
        help="Artefacto da migração; por omissão usa backups/migrations/LATEST.",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--check-live",
        action="store_true",
        help="Compara o plano com o Site Base44, sem apagar.",
    )
    modes.add_argument(
        "--apply",
        action="store_true",
        help="Apaga somente os IDs aprovados, com checkpoint e verificação.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirma explicitamente --apply.",
    )
    parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    parser.add_argument(
        "--rate-limit-delay",
        type=float,
        default=DEFAULT_RATE_LIMIT_DELAY_SECONDS,
    )
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    return parser.parse_args(argv)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CleanupError(f"JSON inválido ou ilegível: {path}") from exc


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_value(value: Any) -> str:
    return hashlib.sha256(json_bytes(value)).hexdigest()


def write_private_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        if read_json(temporary) != value:
            raise CleanupError(f"Falhou a verificação antes de guardar {path}.")
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def resolve_directory(path: Path | None, *, root: Path) -> Path:
    destination = path or pre_cleanup.resolve_pointer(root)
    if not destination.is_absolute():
        destination = PROJECT_ROOT / destination
    destination = destination.resolve()
    if not destination.is_dir():
        raise CleanupError(f"Pasta em falta: {destination}")
    return destination


def parse_checksums(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CleanupError(f"Não foi possível ler {path}.") from exc
    for line in lines:
        parts = line.split("  ", 1)
        if len(parts) != 2 or len(parts[0]) != 64 or not parts[1]:
            raise CleanupError(f"Checksum inválido em {path}: {line!r}")
        checksum, relative = parts
        if relative in result:
            raise CleanupError(f"Checksum repetido em {path}: {relative}")
        result[relative] = checksum
    return result


def verified_backup_entities(backup: Path) -> dict[str, list[dict[str, Any]]]:
    manifest_path = backup / "manifest.json"
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("status") != "complete":
        raise CleanupError(f"O backup não está completo: {backup}")
    checksums = parse_checksums(backup / "SHA256SUMS")
    required_files = ["manifest.json", *[f"entities/{name}.json" for name in REQUIRED_ENTITIES]]
    for relative in required_files:
        path = backup / relative
        expected = checksums.get(relative)
        if not expected or sha256_file(path) != expected:
            raise CleanupError(f"Checksum em falta ou incorreto: {relative}")

    records: dict[str, list[dict[str, Any]]] = {}
    for entity in REQUIRED_ENTITIES:
        payload = read_json(backup / "entities" / f"{entity}.json")
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise CleanupError(f"Entidade inválida no backup: {entity}")
        records[entity] = payload
    return records


def records_by_id(records: list[dict[str, Any]], *, label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = str(record.get("id") or "").strip()
        if not record_id or record_id in result:
            raise CleanupError(f"{label}: ID vazio ou repetido: {record_id!r}")
        result[record_id] = record
    return result


def validated_plan(
    *, backup: Path, artifact: Path
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    backup_records = verified_backup_entities(backup)
    report = pre_cleanup.audit(backup=backup, artifact=artifact)
    if report.get("status") != "ready_for_cleanup_review" or report.get("blockers"):
        raise CleanupError(
            "A auditoria pré-limpeza não autorizou revisão: "
            + ", ".join(report.get("blockers") or [str(report.get("status"))])
        )
    candidates = report.get("cleanup_candidates", {})
    coin_ids = candidates.get("coin_ids")
    variant_ids = candidates.get("coin_variant_ids")
    if (
        not isinstance(coin_ids, list)
        or len(coin_ids) != EXPECTED_COIN_IDS
        or len(set(coin_ids)) != EXPECTED_COIN_IDS
        or not isinstance(variant_ids, list)
        or len(variant_ids) != EXPECTED_VARIANT_IDS
        or len(set(variant_ids)) != EXPECTED_VARIANT_IDS
    ):
        raise CleanupError("A auditoria não contém exatamente 238 Coin e 274 CoinVariant.")

    coin_by_id = records_by_id(backup_records["Coin"], label="Coin backup")
    variant_by_id = records_by_id(backup_records["CoinVariant"], label="CoinVariant backup")
    if not set(coin_ids) <= coin_by_id.keys() or not set(variant_ids) <= variant_by_id.keys():
        raise CleanupError("A lista de limpeza contém IDs ausentes do backup verificado.")
    if any(str(coin_by_id[value].get("country") or "") != "EUA" for value in coin_ids):
        raise CleanupError("A lista de limpeza contém Coin fora dos EUA.")
    if any(
        str(variant_by_id[value].get("coin_id") or "") not in set(coin_ids)
        for value in variant_ids
    ):
        raise CleanupError("A lista de limpeza contém CoinVariant de outro conjunto.")
    return report, backup_records


def expected_after_missing(
    backup_records: list[dict[str, Any]], missing_ids: set[str]
) -> list[dict[str, Any]]:
    return [
        record
        for record in backup_records
        if str(record.get("id") or "") not in missing_ids
    ]


def verify_exact_records(
    *, label: str, expected: list[dict[str, Any]], actual: list[dict[str, Any]]
) -> None:
    expected_by_id = records_by_id(expected, label=f"{label} esperado")
    actual_by_id = records_by_id(actual, label=f"{label} live")
    if expected_by_id.keys() != actual_by_id.keys():
        missing = sorted(expected_by_id.keys() - actual_by_id.keys())
        unexpected = sorted(actual_by_id.keys() - expected_by_id.keys())
        raise CleanupError(
            f"{label}: conjunto live divergente "
            f"(em falta={missing[:5]}, inesperados={unexpected[:5]})."
        )
    for record_id, expected_record in expected_by_id.items():
        actual_record = actual_by_id[record_id]
        if expected_record != actual_record:
            fields = sorted(
                field
                for field in expected_record.keys() | actual_record.keys()
                if expected_record.get(field) != actual_record.get(field)
            )
            raise CleanupError(f"{label} {record_id} mudou desde o backup: {fields}.")


def fetch_relevant_entities(base_client: Base44Client) -> dict[str, list[dict[str, Any]]]:
    return {
        entity: fetch_all(base_client.for_entity(entity), {})
        for entity in REQUIRED_ENTITIES
    }


def verify_live_snapshot(
    *,
    backup_records: dict[str, list[dict[str, Any]]],
    live_records: dict[str, list[dict[str, Any]]],
    missing_variants: set[str],
    missing_coins: set[str],
) -> None:
    missing_by_entity = {
        "CoinVariant": missing_variants,
        "Coin": missing_coins,
        "SpecialCoin": set(),
        "SpecialCoinVariant": set(),
        "CoinSighting": set(),
    }
    for entity in REQUIRED_ENTITIES:
        verify_exact_records(
            label=entity,
            expected=expected_after_missing(
                backup_records[entity], missing_by_entity[entity]
            ),
            actual=live_records[entity],
        )


def candidate_fingerprint(report: dict[str, Any]) -> str:
    return sha256_value(report["cleanup_candidates"])


def state_path_for_backup(backup: Path) -> Path:
    return CLEANUP_ROOT / f"eua-legacy-commemoratives-{backup.name}.json"


def initial_state(*, backup: Path, artifact: Path, report: dict[str, Any]) -> dict[str, Any]:
    return {
        "format_version": 1,
        "kind": "eua_legacy_commemorative_cleanup",
        "status": "preflight_verified",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "backup": str(backup.relative_to(PROJECT_ROOT)),
        "migration_artifact": str(artifact.relative_to(PROJECT_ROOT)),
        "candidate_fingerprint": candidate_fingerprint(report),
        "expected": {
            "CoinVariant": EXPECTED_VARIANT_IDS,
            "Coin": EXPECTED_COIN_IDS,
        },
        "deleted_coin_variants": {},
        "deleted_coins": {},
        "post_verification": None,
        "admin_stats_marked_stale": False,
        "completed_at": None,
    }


def load_or_initialize_state(
    *, path: Path, backup: Path, artifact: Path, report: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    if not path.exists():
        return initial_state(backup=backup, artifact=artifact, report=report), False
    state = read_json(path)
    if not isinstance(state, dict):
        raise CleanupError(f"Checkpoint inválido: {path}")
    expected_values = {
        "kind": "eua_legacy_commemorative_cleanup",
        "backup": str(backup.relative_to(PROJECT_ROOT)),
        "migration_artifact": str(artifact.relative_to(PROJECT_ROOT)),
        "candidate_fingerprint": candidate_fingerprint(report),
    }
    for field, expected in expected_values.items():
        if state.get(field) != expected:
            raise CleanupError(f"Checkpoint incompatível no campo {field}.")
    for field in ("deleted_coin_variants", "deleted_coins"):
        if not isinstance(state.get(field), dict):
            raise CleanupError(f"Checkpoint sem {field} válido.")
    return state, True


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    write_private_json_atomic(path, state)


def missing_candidate_ids(
    live_records: dict[str, list[dict[str, Any]]], report: dict[str, Any]
) -> tuple[set[str], set[str]]:
    live_variant_ids = records_by_id(
        live_records["CoinVariant"], label="CoinVariant live"
    ).keys()
    live_coin_ids = records_by_id(live_records["Coin"], label="Coin live").keys()
    candidates = report["cleanup_candidates"]
    return (
        set(candidates["coin_variant_ids"]) - live_variant_ids,
        set(candidates["coin_ids"]) - live_coin_ids,
    )


def checkpoint_recovered_missing(
    *,
    state: dict[str, Any],
    missing_variants: set[str],
    missing_coins: set[str],
) -> None:
    for record_id in sorted(missing_variants):
        state["deleted_coin_variants"].setdefault(
            record_id,
            {"result": "recovered_missing", "verified_at": utc_now()},
        )
    for record_id in sorted(missing_coins):
        state["deleted_coins"].setdefault(
            record_id,
            {"result": "recovered_missing", "verified_at": utc_now()},
        )


def delete_one(
    *,
    record_id: str,
    write_client: Base44Client,
    read_client: Base44Client,
) -> str:
    deletion_error: Exception | None = None
    try:
        write_client.request("DELETE", f"{write_client.base_url}/{quote(record_id)}")
    except Exception as exc:  # noqa: BLE001 - reconcile an ambiguous DELETE
        deletion_error = exc
    if deletion_error is None:
        return "deleted"

    remaining = records_by_id(fetch_all(read_client, {}), label="reconciliação live")
    if record_id not in remaining:
        return "recovered_after_ambiguous_delete"
    raise CleanupError(
        f"DELETE de {record_id} falhou e o registo ainda existe: {deletion_error}"
    ) from deletion_error


def delete_batch(
    *,
    entity: str,
    candidate_ids: list[str],
    completed: dict[str, Any],
    base_client: Base44Client,
    state: dict[str, Any],
    state_path: Path,
    global_completed_before: int,
    global_total: int,
) -> int:
    pending = [record_id for record_id in candidate_ids if record_id not in completed]
    if not pending:
        print(f"{entity}: nenhum registo pendente.")
        return 0
    read_client = base_client.for_entity(entity)
    write_client = no_retry_entity_client(base_client, entity)
    started = time.monotonic()
    durations = 0.0
    for position, record_id in enumerate(pending, start=1):
        item_started = time.monotonic()
        result = delete_one(
            record_id=record_id,
            write_client=write_client,
            read_client=read_client,
        )
        completed[record_id] = {"result": result, "verified_at": utc_now()}
        save_state(state_path, state)
        item_duration = time.monotonic() - item_started
        durations += item_duration
        global_position = global_completed_before + position
        remaining = global_total - global_position
        eta = (durations / position) * (len(pending) - position)
        print(
            f"Removido: {global_position}/{global_total} "
            f"({global_position / global_total * 100:.1f}%) — {entity} — {record_id} "
            f"[faltam: {remaining} | nesta: {format_duration(item_duration)} | "
            f"fase: {format_duration(time.monotonic() - started)} | "
            f"restante da fase: {'concluído' if position == len(pending) else '~' + format_duration(eta)}]",
            flush=True,
        )
    return len(pending)


def assert_candidates_absent(
    *, entity: str, live_records: list[dict[str, Any]], candidate_ids: list[str]
) -> None:
    remaining = set(candidate_ids) & records_by_id(live_records, label=f"{entity} live").keys()
    if remaining:
        raise CleanupError(f"{entity}: continuam presentes {len(remaining)} candidatos.")


def completion_report(
    *,
    backup: Path,
    artifact: Path,
    state_path: Path,
    state: dict[str, Any],
    backup_records: dict[str, list[dict[str, Any]]],
    final_live: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    return {
        "format_version": 1,
        "kind": "eua_legacy_commemorative_cleanup_result",
        "status": "complete",
        "created_at": utc_now(),
        "backup": str(backup.relative_to(PROJECT_ROOT)),
        "migration_artifact": str(artifact.relative_to(PROJECT_ROOT)),
        "checkpoint": str(state_path.relative_to(PROJECT_ROOT)),
        "deleted": {
            "CoinVariant": len(state["deleted_coin_variants"]),
            "Coin": len(state["deleted_coins"]),
        },
        "preserved": {
            "CoinVariant": len(final_live["CoinVariant"]),
            "Coin": len(final_live["Coin"]),
            "SpecialCoinVariant": len(final_live["SpecialCoinVariant"]),
            "SpecialCoin": len(final_live["SpecialCoin"]),
            "CoinSighting": len(final_live["CoinSighting"]),
        },
        "expected_after_cleanup": {
            "CoinVariant": len(backup_records["CoinVariant"]) - EXPECTED_VARIANT_IDS,
            "Coin": len(backup_records["Coin"]) - EXPECTED_COIN_IDS,
            "SpecialCoinVariant": len(backup_records["SpecialCoinVariant"]),
            "SpecialCoin": len(backup_records["SpecialCoin"]),
            "CoinSighting": len(backup_records["CoinSighting"]),
        },
        "admin_stats_marked_stale": state["admin_stats_marked_stale"],
        "no_sightings_reference_deleted_coins": True,
    }


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply and not args.yes:
        raise CleanupError("--apply exige --yes.")
    backup = resolve_directory(args.backup, root=BACKUP_ROOT)
    artifact = resolve_directory(args.artifact, root=pre_cleanup.DEFAULT_MIGRATION_ROOT)
    report, backup_records = validated_plan(backup=backup, artifact=artifact)

    print("CLEANUP DAS COMEMORATIVAS ANTIGAS DOS EUA")
    print(f"Backup verificado: {backup.relative_to(PROJECT_ROOT)}")
    print(f"CoinVariant aprovadas para remover: {EXPECTED_VARIANT_IDS}")
    print(f"Coin aprovadas para remover: {EXPECTED_COIN_IDS}")
    print("SpecialCoin/SpecialCoinVariant/CoinSighting a alterar: 0")
    if not (args.check_live or args.apply):
        print("Modo local: nenhuma chamada à API e nenhum DELETE.")
        return 0

    base_client = create_client(args)
    live = fetch_relevant_entities(base_client)
    state_path = state_path_for_backup(backup)
    state, resumed = load_or_initialize_state(
        path=state_path,
        backup=backup,
        artifact=artifact,
        report=report,
    )
    missing_variants, missing_coins = missing_candidate_ids(live, report)
    if not resumed and (missing_variants or missing_coins):
        raise CleanupError(
            "O Site já não corresponde ao backup e ainda não existe checkpoint "
            "desta limpeza. É necessária nova auditoria."
        )
    verify_live_snapshot(
        backup_records=backup_records,
        live_records=live,
        missing_variants=missing_variants,
        missing_coins=missing_coins,
    )
    checkpoint_recovered_missing(
        state=state,
        missing_variants=missing_variants,
        missing_coins=missing_coins,
    )
    save_state(state_path, state)
    print(
        "Preflight live confirmado: "
        f"{len(live['CoinVariant'])} CoinVariant · {len(live['Coin'])} Coin · "
        f"{len(live['SpecialCoinVariant'])} SpecialCoinVariant · "
        f"{len(live['SpecialCoin'])} SpecialCoin · "
        f"{len(live['CoinSighting'])} CoinSighting"
    )
    if args.check_live:
        print("DELETE executado: não")
        return 0

    candidates = report["cleanup_candidates"]
    total = EXPECTED_VARIANT_IDS + EXPECTED_COIN_IDS
    already_done = len(state["deleted_coin_variants"]) + len(state["deleted_coins"])
    print(f"A remover agora: {total - already_done}/{total}")
    delete_batch(
        entity="CoinVariant",
        candidate_ids=candidates["coin_variant_ids"],
        completed=state["deleted_coin_variants"],
        base_client=base_client,
        state=state,
        state_path=state_path,
        global_completed_before=len(state["deleted_coin_variants"]),
        global_total=total,
    )
    live_variants = fetch_all(base_client.for_entity("CoinVariant"), {})
    assert_candidates_absent(
        entity="CoinVariant",
        live_records=live_variants,
        candidate_ids=candidates["coin_variant_ids"],
    )
    print("Fase CoinVariant confirmada: 274/274 removidas.")

    completed_before_coins = len(state["deleted_coin_variants"]) + len(state["deleted_coins"])
    delete_batch(
        entity="Coin",
        candidate_ids=candidates["coin_ids"],
        completed=state["deleted_coins"],
        base_client=base_client,
        state=state,
        state_path=state_path,
        global_completed_before=completed_before_coins,
        global_total=total,
    )

    final_live = fetch_relevant_entities(base_client)
    assert_candidates_absent(
        entity="Coin",
        live_records=final_live["Coin"],
        candidate_ids=candidates["coin_ids"],
    )
    verify_live_snapshot(
        backup_records=backup_records,
        live_records=final_live,
        missing_variants=set(candidates["coin_variant_ids"]),
        missing_coins=set(candidates["coin_ids"]),
    )
    source_ids = set(candidates["coin_ids"])
    bad_sightings = [
        row
        for row in final_live["CoinSighting"]
        if str(row.get("coin_id") or "") in source_ids
        or str(row.get("item_id") or "") in source_ids
    ]
    if bad_sightings:
        raise CleanupError("Existem sinalizações ligadas a uma Coin eliminada.")

    mark_numisvault_stats_stale(base_client)
    state["admin_stats_marked_stale"] = True
    state["post_verification"] = {
        entity: len(records) for entity, records in final_live.items()
    }
    state["status"] = "complete"
    state["completed_at"] = utc_now()
    save_state(state_path, state)

    result = completion_report(
        backup=backup,
        artifact=artifact,
        state_path=state_path,
        state=state,
        backup_records=backup_records,
        final_live=final_live,
    )
    report_path = AUDIT_ROOT / f"eua-cleanup-{backup.name}.json"
    write_private_json_atomic(report_path, result)
    print("CLEANUP CONCLUÍDO E VERIFICADO")
    print(f"CoinVariant removidas: {result['deleted']['CoinVariant']}")
    print(f"Coin removidas: {result['deleted']['Coin']}")
    print(f"SpecialCoin preservadas: {result['preserved']['SpecialCoin']}")
    print(
        "SpecialCoinVariant preservadas: "
        f"{result['preserved']['SpecialCoinVariant']}"
    )
    print(f"CoinSighting preservadas: {result['preserved']['CoinSighting']}")
    print(f"Relatório: {report_path.relative_to(PROJECT_ROOT)}")
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except CleanupError as exc:
        print(f"Erro: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
