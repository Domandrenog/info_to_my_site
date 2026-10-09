#!/usr/bin/env python3
"""Safely create USA SpecialCoin parents from an approved private manifest.

The executor is deliberately limited to creating SpecialCoin parent records.
It has no source-deletion mode and does not yet mutate variants or sightings.
Every successful creation is verified and checkpointed by old/new ID.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from scripts.admin_stats_sync import MutationRun, mark_numisvault_stats_stale
from scripts.import_base44_coins import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_RATE_LIMIT_DELAY_SECONDS,
    DEFAULT_REQUEST_DELAY_SECONDS,
    Base44Client,
    create_client,
)


DEFAULT_MIGRATION_ROOT = Path("backups/migrations")
STATE_FILENAME = "execution-state.json"
EXPECTED_MANIFEST_KIND = "eua_coin_to_special_coin_migration"
EXPECTED_MANIFEST_ITEMS = 238
SYSTEM_FIELDS = {
    "id",
    "created_date",
    "updated_date",
    "created_by",
    "created_by_id",
    "is_sample",
}
BOOLEAN_DEFAULT_FIELDS = {"has_variants", "hidden"}


class MigrationExecutionError(RuntimeError):
    """Raised when a safe, idempotent continuation cannot be proven."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Valida e cria apenas os pais SpecialCoin do manifesto dos EUA. "
            "Não apaga Coin e não altera variantes ou descobertas."
        )
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        help="Pasta do manifesto. Por omissão lê backups/migrations/LATEST.",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--check-live",
        action="store_true",
        help="Compara o plano com SpecialCoin no Base44, sem escrever.",
    )
    modes.add_argument(
        "--probe-incomplete",
        action="store_true",
        help="Cria, verifica e apaga uma SpecialCoin temporária com estado Incompleto.",
    )
    modes.add_argument(
        "--apply-canary",
        action="store_true",
        help="Cria e verifica exatamente uma moeda real simples; não apaga a Coin antiga.",
    )
    modes.add_argument(
        "--apply-all-create-only",
        action="store_true",
        help="Cria e verifica todos os pais ainda em falta; não apaga Coin.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirma explicitamente um dos modos que escreve no Base44.",
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


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationExecutionError(f"JSON inválido ou ilegível: {path}") from exc


def parse_checksums(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise MigrationExecutionError(f"Não foi possível ler {path}.") from exc
    result: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2 or len(parts[0]) != 64 or not parts[1]:
            raise MigrationExecutionError(f"Checksum inválido em {path}: {line!r}")
        checksum, relative_path = parts
        if relative_path in result:
            raise MigrationExecutionError(f"Checksum repetido: {relative_path}")
        result[relative_path] = checksum
    return result


def resolve_artifact_directory(
    artifact: Path | None, *, root: Path = DEFAULT_MIGRATION_ROOT
) -> Path:
    if artifact is not None:
        destination = artifact
    else:
        latest_path = root / "LATEST"
        try:
            name = latest_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise MigrationExecutionError(
                f"Não foi possível ler o ponteiro de migração: {latest_path}"
            ) from exc
        if not name or Path(name).name != name:
            raise MigrationExecutionError(f"Ponteiro de migração inválido: {latest_path}")
        destination = root / name
    if not destination.is_dir():
        raise MigrationExecutionError(f"A pasta do manifesto não existe: {destination}")
    return destination


def load_verified_manifest(artifact: Path) -> tuple[dict[str, Any], str]:
    manifest_path = artifact / "manifest.json"
    summary_path = artifact / "summary.json"
    checksums = parse_checksums(artifact / "SHA256SUMS")
    for filename, path in (("manifest.json", manifest_path), ("summary.json", summary_path)):
        if checksums.get(filename) != sha256_file(path):
            raise MigrationExecutionError(f"O checksum de {filename} não corresponde.")

    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise MigrationExecutionError("O manifesto não é um objeto.")
    if manifest.get("kind") != EXPECTED_MANIFEST_KIND:
        raise MigrationExecutionError("O manifesto não é da migração Coin -> SpecialCoin dos EUA.")
    if manifest.get("status") != "planned_read_only":
        raise MigrationExecutionError("O manifesto não está no estado planeado read-only.")
    items = manifest.get("items")
    if not isinstance(items, list) or len(items) != EXPECTED_MANIFEST_ITEMS:
        raise MigrationExecutionError(
            f"O manifesto tem de conter exatamente {EXPECTED_MANIFEST_ITEMS} itens."
        )

    source_ids: set[str] = set()
    identities: set[tuple[str, str, str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            raise MigrationExecutionError("O manifesto contém um item inválido.")
        source_id = str(item.get("source_coin_id") or "").strip()
        if not source_id or source_id in source_ids:
            raise MigrationExecutionError(f"ID de origem ausente ou repetido: {source_id!r}")
        source_ids.add(source_id)
        if item.get("target_special_coin_id") is not None:
            raise MigrationExecutionError(
                f"O manifesto imutável já contém um ID de destino em {source_id}."
            )
        if item.get("source_coin_deletion_authorized") is not False:
            raise MigrationExecutionError(
                f"O item {source_id} não bloqueia explicitamente a eliminação da origem."
            )
        payload = item.get("target_special_coin_payload")
        if not isinstance(payload, dict):
            raise MigrationExecutionError(f"O item {source_id} não tem payload de destino.")
        forbidden = sorted(SYSTEM_FIELDS & payload.keys())
        if forbidden or "rarity" in payload:
            raise MigrationExecutionError(
                f"O payload {source_id} contém campos proibidos: {forbidden or ['rarity']}"
            )
        identity = special_coin_identity(payload)
        if identity in identities:
            raise MigrationExecutionError(f"Identidade de destino repetida: {identity}")
        identities.add(identity)

    return manifest, sha256_file(manifest_path)


def special_coin_identity(record: dict[str, Any]) -> tuple[str, str, str, str]:
    identity = tuple(
        str(record.get(field) or "").strip()
        for field in ("country", "name", "commemorative_name", "year")
    )
    if not all(identity):
        raise MigrationExecutionError(f"Identidade SpecialCoin incompleta: {identity}")
    return identity  # type: ignore[return-value]


def normalized_field(record: dict[str, Any], field: str) -> Any:
    if field in BOOLEAN_DEFAULT_FIELDS:
        return bool(record.get(field))
    return record.get(field)


def payload_differences(
    payload: dict[str, Any], actual: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    return {
        field: {"expected": expected, "actual": normalized_field(actual, field)}
        for field, expected in payload.items()
        if normalized_field(actual, field) != expected
    }


def load_or_initialize_state(
    artifact: Path, *, manifest_sha256: str
) -> dict[str, Any]:
    path = artifact / STATE_FILENAME
    if not path.exists():
        return {
            "format_version": 1,
            "manifest_sha256": manifest_sha256,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "capabilities": {},
            "created_special_coins": {},
            "safety": {
                "source_coin_deletions": 0,
                "source_variant_mutations": 0,
                "source_sighting_mutations": 0,
            },
        }
    state = read_json(path)
    if not isinstance(state, dict):
        raise MigrationExecutionError(f"Estado de execução inválido: {path}")
    if state.get("manifest_sha256") != manifest_sha256:
        raise MigrationExecutionError(
            "O execution-state.json pertence a outra versão do manifesto."
        )
    if not isinstance(state.get("created_special_coins"), dict):
        raise MigrationExecutionError("O estado não contém created_special_coins válido.")
    safety = state.get("safety")
    if not isinstance(safety, dict) or any(
        safety.get(field) != 0
        for field in (
            "source_coin_deletions",
            "source_variant_mutations",
            "source_sighting_mutations",
        )
    ):
        raise MigrationExecutionError("O estado não garante origens intocadas.")
    return state


def write_private_json_atomic(path: Path, value: Any) -> None:
    content = json_bytes(value)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        if read_json(temporary) != value:
            raise MigrationExecutionError(f"Falhou a verificação antes de guardar {path}.")
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def save_state(artifact: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    write_private_json_atomic(artifact / STATE_FILENAME, state)


def list_usa_special_coins(client: Any) -> list[dict[str, Any]]:
    return fetch_all(client, {"country": "EUA"})


def fetch_all(client: Any, query: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    skip = 0
    page_size = 1000
    while True:
        result = client.filter(query, limit=page_size, skip=skip)
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise MigrationExecutionError(f"Resposta Base44 inesperada: {result!r}")
        records.extend(result)
        if len(result) < page_size:
            return records
        skip += page_size


def _records_by_id(records: list[dict[str, Any]], *, label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = str(record.get("id") or "").strip()
        if not record_id:
            raise MigrationExecutionError(f"{label}: registo sem ID.")
        if record_id in result:
            raise MigrationExecutionError(f"{label}: ID repetido {record_id}.")
        result[record_id] = record
    return result


def verify_exact_record_set(
    *,
    label: str,
    expected: list[dict[str, Any]],
    actual: list[dict[str, Any]],
) -> None:
    expected_by_id = _records_by_id(expected, label=f"{label} esperado")
    actual_by_id = _records_by_id(actual, label=f"{label} live")
    if expected_by_id.keys() != actual_by_id.keys():
        missing = sorted(expected_by_id.keys() - actual_by_id.keys())
        unexpected = sorted(actual_by_id.keys() - expected_by_id.keys())
        raise MigrationExecutionError(
            f"{label}: o conjunto live diverge do backup "
            f"(em falta={missing[:5]}, inesperados={unexpected[:5]})."
        )
    for record_id, expected_record in expected_by_id.items():
        actual_record = actual_by_id[record_id]
        if expected_record != actual_record:
            changed_fields = sorted(
                field
                for field in expected_record.keys() | actual_record.keys()
                if expected_record.get(field) != actual_record.get(field)
            )
            raise MigrationExecutionError(
                f"{label} {record_id} mudou desde o backup: {changed_fields}. "
                "Cria um backup e manifesto novos antes de continuar."
            )


def source_entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    selected = [
        {
            "record": item.get("source_record"),
            "variants": [variant.get("source_record") for variant in item.get("variants", [])],
            "sightings": [sighting.get("source_record") for sighting in item.get("sightings", [])],
        }
        for item in manifest["items"]
    ]
    excluded = manifest.get("excluded_normal_coins")
    if not isinstance(excluded, list):
        raise MigrationExecutionError("O manifesto não contém as 12 moedas normais excluídas.")
    entries = [*selected, *excluded]
    if len(entries) != 250:
        raise MigrationExecutionError(
            f"O snapshot de origem deveria conter 250 Coin; contém {len(entries)}."
        )
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("record"), dict)
            or not isinstance(entry.get("variants"), list)
            or not isinstance(entry.get("sightings"), list)
            or any(not isinstance(value, dict) for value in entry["variants"])
            or any(not isinstance(value, dict) for value in entry["sightings"])
        ):
            raise MigrationExecutionError("O manifesto contém uma origem incompleta.")
    return entries


def verify_live_source_snapshot(
    manifest: dict[str, Any], base_client: Any
) -> dict[str, int]:
    entries = source_entries(manifest)
    expected_coins = [entry["record"] for entry in entries]
    source_coin_ids = {record["id"] for record in expected_coins}
    expected_variants = [variant for entry in entries for variant in entry["variants"]]
    expected_sightings = [sighting for entry in entries for sighting in entry["sightings"]]

    live_coins = fetch_all(base_client.for_entity("Coin"), {"country": "EUA"})
    all_live_variants = fetch_all(base_client.for_entity("CoinVariant"), {})
    all_live_sightings = fetch_all(base_client.for_entity("CoinSighting"), {})
    live_variants = [
        record
        for record in all_live_variants
        if str(record.get("coin_id") or "") in source_coin_ids
    ]
    live_sightings = [
        record
        for record in all_live_sightings
        if str(record.get("coin_id") or "") in source_coin_ids
        or str(record.get("item_id") or "") in source_coin_ids
    ]
    verify_exact_record_set(label="Coin EUA", expected=expected_coins, actual=live_coins)
    verify_exact_record_set(
        label="CoinVariant EUA", expected=expected_variants, actual=live_variants
    )
    verify_exact_record_set(
        label="CoinSighting EUA", expected=expected_sightings, actual=live_sightings
    )
    return {
        "coins": len(live_coins),
        "variants": len(live_variants),
        "sightings": len(live_sightings),
    }


def index_live_records(
    records: list[dict[str, Any]],
) -> dict[tuple[str, str, str, str], list[dict[str, Any]]]:
    result: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for record in records:
        identity = special_coin_identity(record)
        result.setdefault(identity, []).append(record)
    return result


def reconcile_items(
    manifest: dict[str, Any],
    state: dict[str, Any],
    live_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], dict[str, Any]]]]:
    live_by_identity = index_live_records(live_records)
    state_records = state["created_special_coins"]
    pending: list[dict[str, Any]] = []
    existing: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in manifest["items"]:
        source_id = item["source_coin_id"]
        payload = item["target_special_coin_payload"]
        matches = live_by_identity.get(special_coin_identity(payload), [])
        if len(matches) > 1:
            raise MigrationExecutionError(
                f"Existem {len(matches)} SpecialCoin com a identidade de {source_id}."
            )
        checkpoint = state_records.get(source_id)
        if not matches:
            if checkpoint:
                raise MigrationExecutionError(
                    f"O checkpoint de {source_id} aponta para uma SpecialCoin que já não existe."
                )
            pending.append(item)
            continue
        actual = matches[0]
        target_id = str(actual.get("id") or "").strip()
        if not target_id:
            raise MigrationExecutionError(f"SpecialCoin sem ID para a origem {source_id}.")
        differences = payload_differences(payload, actual)
        if differences:
            raise MigrationExecutionError(
                f"A SpecialCoin {target_id} diverge do plano {source_id}: "
                + json.dumps(differences, ensure_ascii=False, sort_keys=True)
            )
        if checkpoint and checkpoint.get("target_special_coin_id") != target_id:
            raise MigrationExecutionError(
                f"O ID live de {source_id} não corresponde ao checkpoint."
            )
        existing.append((item, actual))
    return pending, existing


def checkpoint_record(
    state: dict[str, Any],
    item: dict[str, Any],
    actual: dict[str, Any],
    *,
    result: str,
) -> None:
    source_id = item["source_coin_id"]
    state["created_special_coins"][source_id] = {
        "target_special_coin_id": actual["id"],
        "identity": list(special_coin_identity(actual)),
        "payload_sha256": sha256_bytes(json_bytes(item["target_special_coin_payload"])),
        "verified_record": actual,
        "result": result,
        "verified_at": utc_now(),
    }


def record_reconciled_existing(
    artifact: Path,
    state: dict[str, Any],
    existing: list[tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    changed = False
    for item, actual in existing:
        if item["source_coin_id"] not in state["created_special_coins"]:
            checkpoint_record(state, item, actual, result="recovered_exact_existing")
            changed = True
    if changed:
        save_state(artifact, state)


def query_identity(client: Any, payload: dict[str, Any]) -> list[dict[str, Any]]:
    country, name, commemorative_name, year = special_coin_identity(payload)
    result = client.filter(
        {
            "country": country,
            "name": name,
            "commemorative_name": commemorative_name,
            "year": year,
        },
        limit=10,
        skip=0,
    )
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise MigrationExecutionError(f"Resposta de verificação inesperada: {result!r}")
    return result


def create_and_verify_one(
    *,
    read_client: Any,
    write_client: Any,
    payload: dict[str, Any],
    mutation_success_fn: Callable[[], None] = lambda: None,
) -> tuple[dict[str, Any], str]:
    creation_error: Exception | None = None
    try:
        write_client.bulk_create([payload])
        mutation_success_fn()
    except Exception as exc:  # noqa: BLE001 - reconcile ambiguous POST before failing
        creation_error = exc

    matches = query_identity(read_client, payload)
    if len(matches) != 1:
        if creation_error is not None:
            raise MigrationExecutionError(
                "A criação falhou e não foi possível reconciliar um único registo: "
                f"{creation_error}"
            ) from creation_error
        raise MigrationExecutionError(
            f"A criação devolveu {len(matches)} correspondências em vez de uma."
        )
    if creation_error is not None:
        # A unique exact match proves the apparently failed POST did mutate the
        # service, so AdminStats must still be marked stale.
        mutation_success_fn()
    actual = matches[0]
    differences = payload_differences(payload, actual)
    if differences:
        raise MigrationExecutionError(
            "O registo criado não corresponde ao payload: "
            + json.dumps(differences, ensure_ascii=False, sort_keys=True)
        )
    result = "created" if creation_error is None else "recovered_after_ambiguous_create"
    return actual, result


def format_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.1f} s".replace(".", ",")
    minutes, remaining = divmod(round(seconds), 60)
    return f"{minutes}m {remaining:02d}s"


def choose_canary(pending: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [
        item
        for item in pending
        if not item.get("variants")
        and not item.get("sightings")
        and item["target_special_coin_payload"].get("condition") != "Incompleto"
    ]
    if not candidates:
        raise MigrationExecutionError("Não existe uma moeda simples pendente para canário.")
    return candidates[0]


def execute_creations(
    *,
    artifact: Path,
    manifest: dict[str, Any],
    state: dict[str, Any],
    read_client: Any,
    write_client: Any,
    admin_client: Any,
    canary_only: bool,
) -> int:
    live_records = list_usa_special_coins(read_client)
    pending, existing = reconcile_items(manifest, state, live_records)
    record_reconciled_existing(artifact, state, existing)
    if canary_only:
        items_to_create = [choose_canary(pending)] if pending else []
    else:
        capability = state.get("capabilities", {}).get("incomplete_condition", {})
        if any(
            item["target_special_coin_payload"].get("condition") == "Incompleto"
            for item in pending
        ) and capability.get("verified") is not True:
            raise MigrationExecutionError(
                "O estado Incompleto ainda não foi confirmado. Executa --probe-incomplete."
            )
        items_to_create = pending

    if not items_to_create:
        print("Nenhuma SpecialCoin por criar neste âmbito.")
        return 0

    print(f"SpecialCoin já confirmadas: {len(existing)}")
    print(f"SpecialCoin a criar agora: {len(items_to_create)}")
    started_at = time.monotonic()
    total_item_duration = 0.0
    with MutationRun(
        lambda: mark_numisvault_stats_stale(admin_client),
        mutation_label="SpecialCoin parent record",
    ) as mutations:
        for position, item in enumerate(items_to_create, start=1):
            item_started_at = time.monotonic()
            actual, result = create_and_verify_one(
                read_client=read_client,
                write_client=write_client,
                payload=item["target_special_coin_payload"],
                mutation_success_fn=mutations.record_success,
            )
            checkpoint_record(state, item, actual, result=result)
            save_state(artifact, state)
            item_duration = time.monotonic() - item_started_at
            total_item_duration += item_duration
            remaining = len(items_to_create) - position
            eta = (total_item_duration / position) * remaining if position else 0.0
            payload = item["target_special_coin_payload"]
            print(
                f"Criado e verificado: {position}/{len(items_to_create)} "
                f"({position / len(items_to_create) * 100:.1f}%) — EUA — "
                f"{payload['name']} — {payload['commemorative_name']} ({payload['year']}) "
                f"[faltam: {remaining} | nesta: {format_duration(item_duration)} | "
                f"decorrido: {format_duration(time.monotonic() - started_at)} | "
                f"restante: {'concluído' if not remaining else '~' + format_duration(eta)}]"
            )
    return len(items_to_create)


def probe_payload(marker: str) -> dict[str, Any]:
    return {
        "country": "EUA",
        "continent": "América",
        "name": "1 cent",
        "commemorative_name": marker,
        "year": "1900",
        "condition": "Incompleto",
        "has_variants": False,
        "notes": "Teste temporário de compatibilidade; apagar imediatamente",
        "hidden": True,
    }


def run_incomplete_probe(
    *,
    artifact: Path,
    state: dict[str, Any],
    read_client: Any,
    write_client: Any,
    admin_client: Any,
) -> dict[str, Any]:
    marker = f"__probe_incompleto_{uuid.uuid4().hex}__"
    payload = probe_payload(marker)
    state.setdefault("capabilities", {})["incomplete_condition"] = {
        "verified": False,
        "probe_marker": marker,
        "started_at": utc_now(),
        "cleanup_verified": False,
    }
    save_state(artifact, state)
    created_record: dict[str, Any] | None = None
    with MutationRun(
        lambda: mark_numisvault_stats_stale(admin_client),
        mutation_label="temporary SpecialCoin probe",
    ) as mutations:
        try:
            created_record, _result = create_and_verify_one(
                read_client=read_client,
                write_client=write_client,
                payload=payload,
                mutation_success_fn=mutations.record_success,
            )
            if created_record.get("condition") != "Incompleto":
                raise MigrationExecutionError(
                    "O Base44 não devolveu o estado Incompleto sem alterações."
                )
        finally:
            matches = query_identity(read_client, payload)
            for match in matches:
                record_id = str(match.get("id") or "").strip()
                if not record_id:
                    raise MigrationExecutionError(
                        "O probe temporário existe mas não tem ID para limpeza."
                    )
                write_client.request("DELETE", f"{write_client.base_url}/{quote(record_id)}")
                mutations.record_success()
            remaining = query_identity(read_client, payload)
            if remaining:
                raise MigrationExecutionError(
                    f"A limpeza do probe deixou {len(remaining)} registo(s) no Base44."
                )

    capability = {
        "verified": True,
        "probe_marker": marker,
        "verified_condition": "Incompleto",
        "temporary_record_id": created_record.get("id") if created_record else None,
        "verified_at": utc_now(),
        "cleanup_verified": True,
    }
    state["capabilities"]["incomplete_condition"] = capability
    save_state(artifact, state)
    return capability


def actual_clients(args: argparse.Namespace) -> tuple[Base44Client, Base44Client, Base44Client]:
    base_client = create_client(args)
    read_client = base_client.for_entity("SpecialCoin")
    # POST/DELETE are never retried automatically: after an ambiguous result the
    # executor reconciles by exact identity, preventing duplicate creations.
    write_client = Base44Client(
        app_id=base_client.app_id,
        api_key=base_client.api_key,
        server_url=base_client.server_url,
        request_delay_seconds=base_client.request_delay_seconds,
        rate_limit_delay_seconds=base_client.rate_limit_delay_seconds,
        max_retries=0,
        entity_name="SpecialCoin",
    )
    return base_client, read_client, write_client


def print_offline_plan(
    artifact: Path, manifest: dict[str, Any], state: dict[str, Any]
) -> None:
    completed = len(state["created_special_coins"])
    pending = len(manifest["items"]) - completed
    print("\n" + "=" * 72)
    print("PLANO CREATE-ONLY — SPECIALCOIN EUA")
    print("=" * 72)
    print(f"Manifesto: {artifact / 'manifest.json'}")
    print(f"Pais planeados: {len(manifest['items'])}")
    print(f"Checkpoint local: {completed}")
    print(f"Ainda sem checkpoint: {pending}")
    print("Eliminações de Coin disponíveis neste script: 0")
    print("Alterações de variantes/descobertas disponíveis neste script: 0")
    print("Base44: nenhuma leitura ou alteração efetuada.")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    write_mode = args.probe_incomplete or args.apply_canary or args.apply_all_create_only
    if write_mode and not args.yes:
        raise MigrationExecutionError(
            "Um modo de escrita exige --yes; nenhuma alteração foi efetuada."
        )
    artifact = resolve_artifact_directory(args.artifact)
    manifest, manifest_sha256 = load_verified_manifest(artifact)
    state = load_or_initialize_state(artifact, manifest_sha256=manifest_sha256)
    if not (
        args.check_live
        or args.probe_incomplete
        or args.apply_canary
        or args.apply_all_create_only
    ):
        print_offline_plan(artifact, manifest, state)
        return 0

    base_client, read_client, write_client = actual_clients(args)
    if args.check_live:
        source_counts = verify_live_source_snapshot(manifest, base_client)
        live = list_usa_special_coins(read_client)
        pending, existing = reconcile_items(manifest, state, live)
        print(
            "Origens intactas: "
            f"{source_counts['coins']} Coin · "
            f"{source_counts['variants']} CoinVariant · "
            f"{source_counts['sightings']} CoinSighting"
        )
        print(f"SpecialCoin dos EUA no Site: {len(live)}")
        print(f"Correspondências exatas do manifesto: {len(existing)}")
        print(f"Pais ainda por criar: {len(pending)}")
        print("Site Base44: nenhuma alteração efetuada.")
        return 0
    if args.probe_incomplete:
        capability = run_incomplete_probe(
            artifact=artifact,
            state=state,
            read_client=read_client,
            write_client=write_client,
            admin_client=base_client,
        )
        print(
            "Estado Incompleto confirmado e probe temporário removido: "
            f"{capability['temporary_record_id']}"
        )
        return 0

    source_counts = verify_live_source_snapshot(manifest, base_client)
    print(
        "Snapshot de origem confirmado antes de criar: "
        f"{source_counts['coins']} Coin · "
        f"{source_counts['variants']} CoinVariant · "
        f"{source_counts['sightings']} CoinSighting"
    )
    created = execute_creations(
        artifact=artifact,
        manifest=manifest,
        state=state,
        read_client=read_client,
        write_client=write_client,
        admin_client=base_client,
        canary_only=args.apply_canary,
    )
    print(f"SpecialCoin criadas e verificadas nesta execução: {created}")
    print("Coin antigas apagadas: 0")
    print("Variantes ou descobertas alteradas: 0")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MigrationExecutionError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        raise SystemExit(1)
