#!/usr/bin/env python3
"""Finalize Portugal's normal catalogue after the commemorative migration.

The old generated catalogue contains the 19 records moved to ``SpecialCoin``
and two byte-for-byte duplicate normal entries.  This command removes exactly
that audited commemorative set, deduplicates only identical uCoin records and
regenerates the normal catalogue, availability statistics and spreadsheet.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from scripts.import_base44_coins import VALUE_THEN_ISSUE_ORDER
from scripts.migrate_portugal_commemoratives import SELECTION, ucoin_tid
from ucoin_to_mysite.generate_resume_json import (
    statistics_document,
    validate_final_availability_catalogue,
    validate_normal_app_catalogue_contract,
    validate_resume_catalogue,
    write_coins_excel,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_COUNTRY_DIR = PROJECT_ROOT / "info/paises/europa/portugal"
EXPECTED_NORMAL_COINS = 128


class PortugalCatalogError(RuntimeError):
    """Raised when the legacy catalogue is not the audited Portugal dataset."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Retira do app-catalog normal as 19 comemorativas portuguesas, "
            "remove duplicados exatos e regenera stats/Excel."
        )
    )
    parser.add_argument("--country-dir", type=Path, default=DEFAULT_COUNTRY_DIR)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortugalCatalogError(f"JSON inválido ou ilegível: {path}") from exc


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def finalize_catalogue(
    catalogue: dict[str, Any], *, expected_normal_coins: int = EXPECTED_NORMAL_COINS
) -> tuple[dict[str, Any], dict[str, int]]:
    if catalogue.get("country") != "Portugal" or not isinstance(
        catalogue.get("periods"), list
    ):
        raise PortugalCatalogError("O catálogo não é o app-catalog de Portugal.")

    selected = {tid for tid, _subject, _programme in SELECTION}
    found_selected: set[int] = set()
    seen_by_url: dict[str, dict[str, Any]] = {}
    periods: list[dict[str, Any]] = []
    duplicate_count = 0

    for original_period in catalogue["periods"]:
        if not isinstance(original_period, dict) or not isinstance(
            original_period.get("coins"), list
        ):
            raise PortugalCatalogError("Período inválido no catálogo de Portugal.")
        retained: list[dict[str, Any]] = []
        for original_coin in original_period["coins"]:
            if not isinstance(original_coin, dict):
                raise PortugalCatalogError("Moeda inválida no catálogo de Portugal.")
            detail_url = str(original_coin.get("detailUrl") or "").strip()
            if not detail_url:
                raise PortugalCatalogError("Moeda sem detailUrl no catálogo de Portugal.")
            tid = ucoin_tid(detail_url)
            if tid in selected:
                if tid in found_selected:
                    raise PortugalCatalogError(f"Comemorativa repetida no catálogo: tid={tid}.")
                found_selected.add(tid)
                continue
            previous = seen_by_url.get(detail_url)
            if previous is not None:
                if previous != original_coin:
                    raise PortugalCatalogError(
                        f"URL uCoin repetido com dados diferentes: {detail_url}."
                    )
                duplicate_count += 1
                continue
            coin = deepcopy(original_coin)
            seen_by_url[detail_url] = coin
            retained.append(coin)
        if retained:
            period = deepcopy(original_period)
            period["coins"] = retained
            periods.append(period)

    if found_selected not in (set(), selected):
        missing = sorted(selected - found_selected)
        raise PortugalCatalogError(
            f"Conjunto comemorativo parcial; tids em falta: {missing}."
        )
    if len(seen_by_url) != expected_normal_coins:
        raise PortugalCatalogError(
            f"Esperava {expected_normal_coins} moedas normais únicas; "
            f"encontrei {len(seen_by_url)}."
        )

    result = {
        "catalogType": "normal",
        "ucoinCoinType": "normal",
        "coinOrder": VALUE_THEN_ISSUE_ORDER,
        "country": "Portugal",
        "periods": periods,
    }
    validate_normal_app_catalogue_contract(result)
    validate_resume_catalogue(result, expected_normal_coins)
    validate_final_availability_catalogue(result)
    return result, {
        "normal_coins": len(seen_by_url),
        "commemoratives_removed": len(found_selected),
        "duplicates_removed": duplicate_count,
    }


def write_outputs(country_dir: Path, catalogue: dict[str, Any]) -> None:
    app_path = country_dir / "app-catalog.json"
    stats_path = country_dir / "availability-statistics.json"
    excel_path = country_dir / "coins-availability.xlsx"
    write_atomic(app_path, json_bytes(catalogue))
    write_atomic(stats_path, json_bytes(statistics_document(catalogue)))

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{excel_path.name}.", suffix=".tmp", dir=country_dir
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        write_coins_excel(str(temporary), catalogue)
        os.replace(temporary, excel_path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    app_path = args.country_dir / "app-catalog.json"
    catalogue, summary = finalize_catalogue(read_json(app_path))
    if args.check:
        if app_path.read_bytes() != json_bytes(catalogue):
            print(f"Desatualizado: {app_path}")
            return 1
        print(f"Atualizado: {app_path} — {summary['normal_coins']} moedas normais")
        return 0
    write_outputs(args.country_dir, catalogue)
    print(f"Catálogo normal finalizado: {app_path}")
    print(f"Moedas normais únicas: {summary['normal_coins']}")
    print(f"Comemorativas removidas: {summary['commemoratives_removed']}")
    print(f"Duplicados exatos removidos: {summary['duplicates_removed']}")
    print("Ordem: valor da denominação e depois cronologia da emissão")
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except (PortugalCatalogError, ValueError, OSError) as exc:
        print(f"Erro: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
