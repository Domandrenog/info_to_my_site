#!/usr/bin/env python3
"""Audit and safely correct USA collection links in Base44.

The approved plan is built from three read-only sources:

* a verified Base44 backup;
* the current uCoin commemorative catalogue;
* the external-image provenance recorded by All_Coins.

The executor can only update ``SpecialCoin.url_ucoin`` and
``SpecialCoin.image_verso``.  One independently verified record (Steve Jobs)
also needs ``image_frente`` because its old front actually belongs to Cray-1.
The script cannot create or delete records.  Each write is reconciled against
exact live preconditions, read back, verified and checkpointed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from scripts.admin_stats_sync import MutationRun, mark_numisvault_stats_stale
from scripts.execute_eua_commemorative_migration import (
    MigrationExecutionError,
    fetch_all,
    get_record,
    no_retry_entity_client,
)
from scripts.import_base44_coins import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_RATE_LIMIT_DELAY_SECONDS,
    DEFAULT_REQUEST_DELAY_SECONDS,
    create_client,
)


DEFAULT_PLAN = Path(
    "info/paises/america/eua/special-coin-ucoin-corrections-approved.json"
)
DEFAULT_STATE = Path(
    "backups/migrations/eua-special-coin-ucoin-corrections-state.json"
)
DEFAULT_EXTERNAL_LINKS = Path(
    "../All_Coins/fotos/paises/America/EUA/normal/links-externos.txt"
)
EXPECTED_KIND = "eua_special_coin_ucoin_corrections"
EXPECTED_USA_RECORDS = 245
EXPECTED_UPDATES = 238
EXPECTED_URL_UPDATES = 238
EXPECTED_BACK_UPDATES = 157
EXPECTED_FRONT_UPDATES = 1
EXPECTED_EXACT_IMAGE_MATCHES = 221
EXPECTED_TID_OVERRIDES = 17
ALLOWED_FIELDS = frozenset({"url_ucoin", "image_frente", "image_verso"})
IDENTITY_FIELDS = ("country", "name", "commemorative_name", "year", "ordem")


# These catalogue associations cannot use the current catalogue sample image:
# uCoin changed the sample photo, or the original source was a Base44 upload.
# The type IDs were individually verified against the official detail pages.
TID_OVERRIDES: dict[tuple[str, str, str], tuple[int, str]] = {
    ("1 cent", "Primeira infância em Kentucky", "2009"): (11524, "reverse"),
    ("1 cent", "Anos de formação em Indiana", "2009"): (11525, "reverse"),
    ("1 cent", "Vida Profissional em Illinois", "2009"): (11526, "reverse"),
    ("1 cent", "Independência dos EUA", "2026"): (189803, "obverse"),
    ("5 cents", "Lousiana Purchase", "2004"): (11553, "reverse"),
    ("5 cents", "Ocean in View", "2005"): (11556, "reverse"),
    ("5 cents", "Independência dos EUA", "2026"): (189801, "reverse"),
    ("10 cents", "Independência dos EUA", "2026"): (189800, "reverse"),
    ("25 cents", "250º Aniversário - Mayflower", "2026"): (189804, "obverse"),
    ("25 cents", "250º Aniversário - Guerra Revolucionária", "2026"): (
        189805,
        "obverse",
    ),
    ("25 cents", "250º Ani. - Declaração de Independência", "2026"): (
        189806,
        "reverse",
    ),
    ("25 cents", "250º Aniversário - Constituição dos EUA", "2026"): (
        189807,
        "obverse",
    ),
    ("25 cents", "250º Aniversário - Discurso de Gettysburg", "2026"): (
        189808,
        "obverse",
    ),
    ("50 cents", "250º Aniversário - Independência dos EUA", "2026"): (
        189802,
        "obverse",
    ),
    ("1 dólar", "James Madison", "2007"): (2984, "obverse"),
    ("1 dólar", "Polly Cooper", "2026"): (189799, "reverse"),
    ("1 dólar", "California. Steve Jobs", "2026"): (189798, "reverse"),
}

# Both original links are Base44 uploads, so their relationship cannot be
# proven from URL structure.  The local source images were visually checked:
# Lincoln pairs with the shield, and Liberty pairs with the torch.
VISUALLY_VERIFIED_EXISTING_BACKS = {
    ("1 cent", "Independência dos EUA", "2026"),
    ("50 cents", "250º Aniversário - Independência dos EUA", "2026"),
}

# Some uCoin catalogue entries replace an older one-sided sample with a newer
# complete pair.  In those cases the missing side must come from the current
# catalogue pair; deriving it from the old sample asset can produce a URL that
# never existed.  Polly Cooper is the confirmed example: 82978124-2 was a
# valid reverse, but 82978124-1 returns 404.  The current type 189799 exposes
# the verified Sacagawea obverse as 83847230-1.
OFFICIAL_CATALOG_BACK_SIDES = {
    ("1 dólar", "Polly Cooper", "2026"): "obverse",
}

# The first correction run wrote this inferred-but-nonexistent URL before the
# browser download exposed the 404.  Accept it only as a known intermediate
# value so the corrected plan can replace it; no other unexpected live value
# is allowed through reconciliation.
APPROVED_INTERMEDIATE_FIELD_VALUES = {
    (
        "6ac90a623b0299311facfb57",
        "image_verso",
    ): frozenset(
        {
            "https://i.ucoin.net/coin/82/978/82978124-1/usa-1-dollar-2026.jpg",
        }
    ),
}

# This old record is labelled Steve Jobs but both external-image provenance and
# the downloaded front point to the Cray-1 type.  The official Steve Jobs page
# is tid=189798; use its thematic reverse as the collection front and its
# Liberty obverse as the back.
FULL_PHOTO_CORRECTIONS = {
    ("1 dólar", "California. Steve Jobs", "2026"): {
        "front_side": "reverse",
        "back_side": "obverse",
    }
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Cria, verifica ou aplica o plano aprovado de URLs uCoin e versos "
            "das moedas de coleção dos EUA."
        )
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--build-plan",
        action="store_true",
        help="Gera o plano aprovado apenas a partir de fontes locais.",
    )
    modes.add_argument(
        "--check-live",
        action="store_true",
        help="Compara o plano com o Site Base44 sem escrever.",
    )
    modes.add_argument(
        "--apply",
        action="store_true",
        help="Aplica apenas as correções ainda pendentes do plano.",
    )
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--ucoin-catalog", type=Path)
    parser.add_argument(
        "--external-links", type=Path, default=DEFAULT_EXTERNAL_LINKS
    )
    parser.add_argument(
        "--yes", action="store_true", help="Confirma explicitamente --apply."
    )
    parser.add_argument(
        "--request-delay", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS
    )
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


def write_verified_json(path: Path, value: Any, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json(value))
            handle.flush()
            os.fsync(handle.fileno())
        if read_json(temporary) != value:
            raise MigrationExecutionError(f"Falhou a verificação ao guardar {path}.")
        if private:
            temporary.chmod(0o600)
        os.replace(temporary, path)
        if private:
            path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def normalize_image_url(value: str) -> str:
    return re.sub(r"-(1|2)s(?=/)", r"-\1", str(value or "").strip())


def image_asset_id(value: str) -> str | None:
    match = re.search(r"/([0-9]+)-(?:1|2)s?/", str(value or ""))
    return match.group(1) if match else None


def image_side(value: str) -> str | None:
    match = re.search(r"-(1|2)s?/", str(value or ""))
    if not match:
        return None
    return "obverse" if match.group(1) == "1" else "reverse"


def counterpart_image_url(value: str) -> str:
    source = str(value or "").strip()
    match = re.search(r"-(1|2)s?(?=/)", source)
    if not match:
        raise MigrationExecutionError(
            f"Não foi possível obter a outra face desta imagem: {source}"
        )
    other = "2" if match.group(1) == "1" else "1"
    return source[: match.start()] + f"-{other}" + source[match.end() :]


def parse_external_links(path: Path) -> dict[str, dict[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise MigrationExecutionError(f"Não foi possível ler {path}.") from exc
    result: dict[str, dict[str, str]] = {}
    current_key: str | None = None
    for raw_line in lines:
        if raw_line and not raw_line[0].isspace() and raw_line.endswith(":"):
            current_key = raw_line[:-1].strip()
            if not current_key or current_key in result:
                raise MigrationExecutionError(
                    f"Chave externa ausente ou repetida em {path}: {current_key!r}"
                )
            result[current_key] = {}
            continue
        if current_key and ": " in raw_line:
            field, value = raw_line.strip().split(": ", 1)
            result[current_key][field] = value.strip()
    return result


def load_ucoin_coins(path: Path) -> list[dict[str, Any]]:
    catalog = read_json(path)
    periods = catalog.get("periods") if isinstance(catalog, dict) else None
    if not isinstance(periods, list) or len(periods) != 1:
        raise MigrationExecutionError("O catálogo uCoin não contém um período único.")
    coins = periods[0].get("coins") if isinstance(periods[0], dict) else None
    if not isinstance(coins, list) or not coins:
        raise MigrationExecutionError("O catálogo uCoin não contém moedas.")
    return coins


def record_identity(record: dict[str, Any]) -> dict[str, Any]:
    return {field: record.get(field) for field in IDENTITY_FIELDS}


def short_identity(record: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(record.get("name") or ""),
        str(record.get("commemorative_name") or ""),
        str(record.get("year") or ""),
    )


def ucoin_tid(value: str) -> int | None:
    values = parse_qs(urlparse(str(value or "")).query).get("tid", [])
    try:
        return int(values[0]) if values else None
    except (TypeError, ValueError):
        return None


def build_plan(
    *,
    backup_path: Path,
    ucoin_catalog_path: Path,
    external_links_path: Path,
) -> dict[str, Any]:
    backup = read_json(backup_path)
    if not isinstance(backup, list):
        raise MigrationExecutionError("O backup de SpecialCoin não é uma lista.")
    usa = [record for record in backup if record.get("country") == "EUA"]
    if len(usa) != EXPECTED_USA_RECORDS:
        raise MigrationExecutionError(
            f"Esperavam-se {EXPECTED_USA_RECORDS} SpecialCoin dos EUA; "
            f"o backup contém {len(usa)}."
        )
    pending = [record for record in usa if not str(record.get("url_ucoin") or "").strip()]
    if len(pending) != EXPECTED_UPDATES:
        raise MigrationExecutionError(
            f"Esperavam-se {EXPECTED_UPDATES} moedas sem URL; existem {len(pending)}."
        )

    external_links = parse_external_links(external_links_path)
    coins = load_ucoin_coins(ucoin_catalog_path)
    by_tid: dict[int, dict[str, Any]] = {}
    by_image: dict[str, list[tuple[dict[str, Any], str]]] = {}
    for coin in coins:
        try:
            tid = int(coin["ucoinTypeId"])
            images = coin["images"]
        except (KeyError, TypeError, ValueError) as exc:
            raise MigrationExecutionError("Entrada uCoin incompleta.") from exc
        if tid in by_tid:
            raise MigrationExecutionError(f"uCoin type ID repetido: {tid}")
        by_tid[tid] = coin
        for side in ("obverse", "reverse"):
            by_image.setdefault(normalize_image_url(images[side]), []).append(
                (coin, side)
            )

    updates: list[dict[str, Any]] = []
    exact_matches = 0
    override_matches = 0
    paired_backs = 0
    corrected_backs = 0
    visual_backs = 0
    seen_urls: set[str] = set()
    for record in sorted(pending, key=lambda item: int(item.get("ordem") or 0)):
        record_id = str(record.get("id") or "").strip()
        front_path = urlparse(str(record.get("image_frente") or "")).path
        source_key = Path(front_path).stem
        source = external_links.get(source_key)
        if not record_id or not source or not source.get("frente") or not source.get("tras"):
            raise MigrationExecutionError(
                f"Falta a proveniência externa de {short_identity(record)}."
            )

        source_front = source["frente"]
        source_back = source["tras"]
        identity = short_identity(record)
        matches = by_image.get(normalize_image_url(source_front), [])
        method: str
        if identity in TID_OVERRIDES:
            tid, front_side = TID_OVERRIDES[identity]
            coin = by_tid.get(tid)
            if coin is None:
                raise MigrationExecutionError(
                    f"O type ID verificado {tid} já não existe no catálogo."
                )
            method = "verified_ucoin_type_id"
            override_matches += 1
        elif len(matches) == 1:
            coin, front_side = matches[0]
            method = "exact_external_front_image"
            exact_matches += 1
        else:
            raise MigrationExecutionError(
                f"Associação uCoin ambígua para {short_identity(record)}: "
                f"{len(matches)} correspondências."
            )

        detail_url = str(coin.get("detailUrl") or "").strip()
        tid = int(coin["ucoinTypeId"])
        if not detail_url or detail_url in seen_urls:
            raise MigrationExecutionError(f"URL uCoin ausente ou repetido: {detail_url!r}")
        seen_urls.add(detail_url)

        current = {"url_ucoin": record.get("url_ucoin")}
        target = {"url_ucoin": detail_url}
        front_asset = image_asset_id(source_front)
        back_asset = image_asset_id(source_back)
        if identity in FULL_PHOTO_CORRECTIONS:
            correction = FULL_PHOTO_CORRECTIONS[identity]
            images = coin.get("images") or {}
            current["image_frente"] = record.get("image_frente")
            current["image_verso"] = record.get("image_verso")
            target["image_frente"] = normalize_image_url(
                str(images.get(correction["front_side"]) or "")
            )
            target["image_verso"] = normalize_image_url(
                str(images.get(correction["back_side"]) or "")
            )
            if not target["image_frente"] or not target["image_verso"]:
                raise MigrationExecutionError(
                    f"Faltam as fotografias oficiais de {identity}."
                )
            corrected_backs += 1
        elif identity in OFFICIAL_CATALOG_BACK_SIDES:
            images = coin.get("images") or {}
            current["image_verso"] = record.get("image_verso")
            target["image_verso"] = normalize_image_url(
                str(images.get(OFFICIAL_CATALOG_BACK_SIDES[identity]) or "")
            )
            if not target["image_verso"]:
                raise MigrationExecutionError(
                    f"Falta o verso oficial atual de {identity}."
                )
            corrected_backs += 1
        elif front_asset and back_asset and front_asset == back_asset:
            paired_backs += 1
        elif front_asset and back_asset and front_asset != back_asset:
            current["image_verso"] = record.get("image_verso")
            target["image_verso"] = counterpart_image_url(source_front)
            corrected_backs += 1
        elif short_identity(record) in VISUALLY_VERIFIED_EXISTING_BACKS:
            visual_backs += 1
        else:
            raise MigrationExecutionError(
                f"Não foi possível validar o par de imagens de {short_identity(record)}."
            )

        updates.append(
            {
                "id": record_id,
                "identity": record_identity(record),
                "match": {
                    "method": method,
                    "ucoin_type_id": tid,
                    "front_side": front_side,
                    "source_key": source_key,
                    "source_external_front": source_front,
                    "source_external_back": source_back,
                },
                "current": current,
                "set": target,
            }
        )

    assigned_tids = {int(item["match"]["ucoin_type_id"]) for item in updates}
    if len(assigned_tids) != EXPECTED_UPDATES:
        raise MigrationExecutionError("O plano não associa 238 type IDs uCoin distintos.")
    planned_ids = {item["id"] for item in updates}
    outside = [record for record in usa if str(record.get("id")) not in planned_ids]
    outside_urls = [str(record.get("url_ucoin") or "").strip() for record in outside]
    outside_current_tids = {
        tid for url in outside_urls if (tid := ucoin_tid(url)) in by_tid
    }
    missing_catalog_tids = set(by_tid) - assigned_tids
    if missing_catalog_tids != outside_current_tids or missing_catalog_tids != {
        189796,
        189797,
        195362,
    }:
        raise MigrationExecutionError(
            "A cobertura 1:1 do catálogo atual divergiu: "
            f"tipos restantes={sorted(missing_catalog_tids)}, "
            f"tipos novos={sorted(outside_current_tids)}."
        )
    all_final_urls = [item["set"]["url_ucoin"] for item in updates] + outside_urls
    if len(all_final_urls) != EXPECTED_USA_RECORDS or len(set(all_final_urls)) != len(
        all_final_urls
    ):
        raise MigrationExecutionError(
            "O plano final não contém 245 URLs uCoin distintos."
        )

    summary = {
        "usa_special_coins": len(usa),
        "records_to_update": len(updates),
        "url_ucoin_fields": sum("url_ucoin" in item["set"] for item in updates),
        "image_frente_fields": sum("image_frente" in item["set"] for item in updates),
        "image_verso_fields": sum("image_verso" in item["set"] for item in updates),
        "exact_external_front_matches": exact_matches,
        "verified_type_id_overrides": override_matches,
        "already_paired_backs": paired_backs,
        "visually_verified_existing_backs": visual_backs,
        "current_period_catalog_types": len(by_tid),
        "current_period_types_assigned_1_to_1": len(by_tid),
    }
    expected_summary = {
        "usa_special_coins": EXPECTED_USA_RECORDS,
        "records_to_update": EXPECTED_UPDATES,
        "url_ucoin_fields": EXPECTED_URL_UPDATES,
        "image_frente_fields": EXPECTED_FRONT_UPDATES,
        "image_verso_fields": EXPECTED_BACK_UPDATES,
        "exact_external_front_matches": EXPECTED_EXACT_IMAGE_MATCHES,
        "verified_type_id_overrides": EXPECTED_TID_OVERRIDES,
        "already_paired_backs": 79,
        "visually_verified_existing_backs": 2,
        "current_period_catalog_types": 241,
        "current_period_types_assigned_1_to_1": 241,
    }
    if summary != expected_summary:
        raise MigrationExecutionError(
            "Os totais do plano divergiram da revisão aprovada: "
            + json.dumps(summary, ensure_ascii=False, sort_keys=True)
        )

    return {
        "format_version": 1,
        "kind": EXPECTED_KIND,
        "status": "approved_by_user",
        "created_at": utc_now(),
        "source": {
            "backup": str(backup_path),
            "backup_sha256": sha256_file(backup_path),
            "ucoin_catalog": str(ucoin_catalog_path),
            "ucoin_catalog_sha256": sha256_file(ucoin_catalog_path),
            "external_links": str(external_links_path),
            "external_links_sha256": sha256_file(external_links_path),
        },
        "safety": {
            "entity": "SpecialCoin",
            "country": "EUA",
            "allowed_update_fields": sorted(ALLOWED_FIELDS),
            "creates": 0,
            "deletions": 0,
            "front_image_updates": EXPECTED_FRONT_UPDATES,
        },
        "summary": summary,
        "updates": updates,
    }


def load_plan(path: Path) -> tuple[dict[str, Any], str]:
    plan = read_json(path)
    if not isinstance(plan, dict):
        raise MigrationExecutionError("O plano aprovado não é um objeto JSON.")
    if plan.get("kind") != EXPECTED_KIND or plan.get("status") != "approved_by_user":
        raise MigrationExecutionError("O plano não está aprovado para esta operação.")
    expected_safety = {
        "entity": "SpecialCoin",
        "country": "EUA",
        "allowed_update_fields": sorted(ALLOWED_FIELDS),
        "creates": 0,
        "deletions": 0,
        "front_image_updates": EXPECTED_FRONT_UPDATES,
    }
    if plan.get("safety") != expected_safety:
        raise MigrationExecutionError("As garantias de segurança do plano divergiram.")
    updates = plan.get("updates")
    if not isinstance(updates, list) or len(updates) != EXPECTED_UPDATES:
        raise MigrationExecutionError(
            f"O plano tem de conter exatamente {EXPECTED_UPDATES} atualizações."
        )
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    back_updates = 0
    front_updates = 0
    for item in updates:
        if not isinstance(item, dict):
            raise MigrationExecutionError("O plano contém uma atualização inválida.")
        record_id = str(item.get("id") or "").strip()
        identity = item.get("identity")
        current = item.get("current")
        target = item.get("set")
        if not record_id or record_id in seen_ids:
            raise MigrationExecutionError(f"ID ausente ou repetido: {record_id!r}")
        seen_ids.add(record_id)
        if not isinstance(identity, dict) or set(identity) != set(IDENTITY_FIELDS):
            raise MigrationExecutionError(f"Identidade inválida em {record_id}.")
        if identity.get("country") != "EUA":
            raise MigrationExecutionError(f"País inválido em {record_id}.")
        if not isinstance(current, dict) or not isinstance(target, dict):
            raise MigrationExecutionError(f"Campos inválidos em {record_id}.")
        if set(current) != set(target) or not set(target) <= ALLOWED_FIELDS:
            raise MigrationExecutionError(f"Campos não permitidos em {record_id}.")
        if "url_ucoin" not in target or any(current[k] == target[k] for k in target):
            raise MigrationExecutionError(f"Correção vazia ou incompleta em {record_id}.")
        url = str(target["url_ucoin"] or "").strip()
        if not url or url in seen_urls:
            raise MigrationExecutionError(f"URL uCoin ausente ou repetido em {record_id}.")
        seen_urls.add(url)
        back_updates += int("image_verso" in target)
        front_updates += int("image_frente" in target)
    if back_updates != EXPECTED_BACK_UPDATES:
        raise MigrationExecutionError(
            f"Esperavam-se {EXPECTED_BACK_UPDATES} correções de verso; "
            f"o plano contém {back_updates}."
        )
    if front_updates != EXPECTED_FRONT_UPDATES:
        raise MigrationExecutionError(
            f"Esperava-se {EXPECTED_FRONT_UPDATES} correção de frente; "
            f"o plano contém {front_updates}."
        )
    return plan, sha256_file(path)


def load_state(path: Path, plan_sha256: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "format_version": 1,
            "plan_sha256": plan_sha256,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "updated_records": {},
            "safety": {"creates": 0, "deletions": 0},
        }
    state = read_json(path)
    if not isinstance(state, dict) or state.get("plan_sha256") != plan_sha256:
        raise MigrationExecutionError("O checkpoint não corresponde ao plano aprovado.")
    if (
        not isinstance(state.get("updated_records"), dict)
        or state.get("safety") != {"creates": 0, "deletions": 0}
    ):
        raise MigrationExecutionError("O checkpoint não mantém as garantias do plano.")
    return state


def reconcile_live(
    plan: dict[str, Any], live: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(live) != EXPECTED_USA_RECORDS:
        raise MigrationExecutionError(
            f"Esperavam-se {EXPECTED_USA_RECORDS} SpecialCoin dos EUA no Site; "
            f"existem {len(live)}."
        )
    by_id: dict[str, dict[str, Any]] = {}
    for record in live:
        record_id = str(record.get("id") or "").strip()
        if not record_id or record_id in by_id:
            raise MigrationExecutionError(f"ID live ausente ou repetido: {record_id!r}")
        by_id[record_id] = record

    pending: list[dict[str, Any]] = []
    complete: list[dict[str, Any]] = []
    planned_ids = {item["id"] for item in plan["updates"]}
    for item in plan["updates"]:
        record = by_id.get(item["id"])
        if record is None:
            raise MigrationExecutionError(
                f"A SpecialCoin planeada {item['id']} desapareceu do Site."
            )
        actual_identity = record_identity(record)
        if actual_identity != item["identity"]:
            raise MigrationExecutionError(
                f"A identidade de {item['id']} mudou: "
                + json.dumps(actual_identity, ensure_ascii=False, sort_keys=True)
            )
        patch: dict[str, Any] = {}
        for field, target_value in item["set"].items():
            actual_value = record.get(field)
            if actual_value == target_value:
                continue
            allowed_current_values = {
                item["current"][field],
                *APPROVED_INTERMEDIATE_FIELD_VALUES.get(
                    (item["id"], field), frozenset()
                ),
            }
            if actual_value not in allowed_current_values:
                raise MigrationExecutionError(
                    f"Conflito em {item['id']}.{field}: valor live inesperado."
                )
            patch[field] = target_value
        if patch:
            pending.append({**item, "patch": patch})
        else:
            complete.append(item)

    outside = [record for record in live if record["id"] not in planned_ids]
    if len(outside) != EXPECTED_USA_RECORDS - EXPECTED_UPDATES:
        raise MigrationExecutionError("O conjunto live fora do plano mudou.")
    missing_outside = [
        record for record in outside if not str(record.get("url_ucoin") or "").strip()
    ]
    if missing_outside:
        raise MigrationExecutionError(
            "Existem novas moedas fora do plano sem URL uCoin: "
            + ", ".join(str(record.get("id")) for record in missing_outside)
        )

    effective_urls = {
        item["id"]: item["set"]["url_ucoin"] for item in plan["updates"]
    }
    effective_urls.update(
        {
            str(record["id"]): str(record.get("url_ucoin") or "").strip()
            for record in outside
        }
    )
    urls = list(effective_urls.values())
    if any(not url for url in urls) or len(set(urls)) != EXPECTED_USA_RECORDS:
        raise MigrationExecutionError(
            "O estado final planeado não teria 245 URLs uCoin únicos."
        )
    return pending, complete


def format_duration(seconds: float) -> str:
    rounded = max(0, round(seconds))
    minutes, seconds_part = divmod(rounded, 60)
    return f"{minutes}m {seconds_part:02d}s" if minutes else f"{seconds_part}s"


def update_and_verify(
    *,
    read_client: Any,
    write_client: Any,
    item: dict[str, Any],
    mutation_success_fn: Callable[[], None],
) -> tuple[dict[str, Any], str]:
    update_error: Exception | None = None
    try:
        write_client.update(item["id"], item["patch"])
        mutation_success_fn()
    except Exception as exc:  # noqa: BLE001 - reconcile ambiguous PUT by GET
        update_error = exc
    actual = get_record(read_client, item["id"])
    if record_identity(actual) != item["identity"] or any(
        actual.get(field) != value for field, value in item["set"].items()
    ):
        if update_error is not None:
            raise MigrationExecutionError(
                f"A atualização de {item['id']} falhou e o estado live não "
                f"corresponde ao plano: {update_error}"
            ) from update_error
        raise MigrationExecutionError(
            f"A SpecialCoin {item['id']} não corresponde ao plano após PUT."
        )
    if update_error is not None:
        mutation_success_fn()
        return actual, "recovered_after_ambiguous_update"
    return actual, "updated"


def print_summary(plan: dict[str, Any]) -> None:
    summary = plan["summary"]
    print("\n" + "=" * 72)
    print("PLANO APROVADO — LIGAÇÕES UCOIN DA COLEÇÃO DOS EUA")
    print("=" * 72)
    print(f"SpecialCoin dos EUA: {summary['usa_special_coins']}")
    print(f"Moedas a atualizar: {summary['records_to_update']}")
    print(f"URLs uCoin a preencher: {summary['url_ucoin_fields']}")
    print(f"Versos realmente incorretos a corrigir: {summary['image_verso_fields']}")
    print(
        "Versos já corretos e preservados: "
        f"{summary['already_paired_backs'] + summary['visually_verified_existing_backs']}"
    )
    print(
        "Imagens da frente a alterar: "
        f"{summary['image_frente_fields']} (Steve Jobs estava com a frente do Cray-1)"
    )
    print("Criações: 0")
    print("Eliminações: 0")


def print_live_status(
    live: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    complete: list[dict[str, Any]],
) -> None:
    pending_fields = {
        field: sum(field in item["patch"] for item in pending)
        for field in sorted(ALLOWED_FIELDS)
    }
    urls_present = sum(bool(str(record.get("url_ucoin") or "").strip()) for record in live)
    print(f"SpecialCoin dos EUA no Site: {len(live)}")
    print(f"Com URL uCoin agora: {urls_present}/{len(live)}")
    print(f"Moedas já exatamente corrigidas: {len(complete)}/{EXPECTED_UPDATES}")
    print(f"Moedas ainda pendentes: {len(pending)}")
    print(f"URLs uCoin ainda pendentes: {pending_fields['url_ucoin']}")
    print(f"Versos ainda pendentes: {pending_fields['image_verso']}")
    print(f"Imagens da frente ainda pendentes: {pending_fields['image_frente']}")


def apply_updates(
    *,
    plan: dict[str, Any],
    plan_sha256: str,
    state_path: Path,
    base_client: Any,
) -> int:
    state = load_state(state_path, plan_sha256)
    read_client = base_client.for_entity("SpecialCoin")
    write_client = no_retry_entity_client(base_client, "SpecialCoin")
    live = fetch_all(read_client, {"country": "EUA"})
    pending, complete = reconcile_live(plan, live)
    print_live_status(live, pending, complete)
    if not pending:
        print("Nenhuma correção por aplicar.")
        return 0

    print(f"\nA aplicar exatamente {len(pending)} atualizações em SpecialCoin.")
    started_at = time.monotonic()
    total_item_duration = 0.0
    with MutationRun(
        lambda: mark_numisvault_stats_stale(base_client),
        mutation_label="SpecialCoin record",
    ) as mutations:
        for position, item in enumerate(pending, start=1):
            item_started_at = time.monotonic()
            actual, result = update_and_verify(
                read_client=read_client,
                write_client=write_client,
                item=item,
                mutation_success_fn=mutations.record_success,
            )
            item_duration = time.monotonic() - item_started_at
            total_item_duration += item_duration
            state["updated_records"][item["id"]] = {
                "identity": item["identity"],
                "patch": item["patch"],
                "result": result,
                "verified_record": actual,
                "verified_at": utc_now(),
            }
            state["updated_at"] = utc_now()
            write_verified_json(state_path, state, private=True)
            remaining = len(pending) - position
            eta = total_item_duration / position * remaining
            identity = item["identity"]
            fields = ", ".join(item["patch"])
            print(
                f"Atualizado: {position}/{len(pending)} "
                f"({position / len(pending) * 100:.1f}%) — EUA — "
                f"{identity['name']} — {identity['commemorative_name']} "
                f"({identity['year']}) "
                f"[faltam: {remaining} | demorou nesta: {format_duration(item_duration)} | "
                f"decorrido: {format_duration(time.monotonic() - started_at)} | "
                f"restante: {'concluído' if not remaining else '~' + format_duration(eta)}] "
                f"— {fields}"
            )
    return len(pending)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply and not args.yes:
        raise MigrationExecutionError(
            "O modo de escrita exige --yes; nenhuma alteração foi efetuada."
        )
    if args.build_plan:
        if args.backup is None or args.ucoin_catalog is None:
            raise MigrationExecutionError(
                "--build-plan exige --backup e --ucoin-catalog."
            )
        plan = build_plan(
            backup_path=args.backup,
            ucoin_catalog_path=args.ucoin_catalog,
            external_links_path=args.external_links,
        )
        write_verified_json(args.plan, plan)
        print_summary(plan)
        print(f"Plano guardado e verificado: {args.plan}")
        print("Site Base44: nenhuma alteração efetuada.")
        return 0

    plan, plan_sha256 = load_plan(args.plan)
    print_summary(plan)
    if not args.check_live and not args.apply:
        print("Site Base44: nenhuma leitura ou alteração efetuada.")
        return 0

    base_client = create_client(args)
    if args.check_live:
        live = fetch_all(base_client.for_entity("SpecialCoin"), {"country": "EUA"})
        pending, complete = reconcile_live(plan, live)
        print_live_status(live, pending, complete)
        print("Site Base44: nenhuma alteração efetuada.")
        return 0

    updated = apply_updates(
        plan=plan,
        plan_sha256=plan_sha256,
        state_path=args.state,
        base_client=base_client,
    )
    live = fetch_all(base_client.for_entity("SpecialCoin"), {"country": "EUA"})
    pending, complete = reconcile_live(plan, live)
    print("\nVerificação final:")
    print_live_status(live, pending, complete)
    print(f"SpecialCoin atualizadas e verificadas nesta execução: {updated}")
    print("Criações: 0")
    print("Eliminações: 0")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MigrationExecutionError as exc:
        print(f"Erro: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1)
