from __future__ import annotations

import argparse
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts import prepare_special_app_catalog as preparer


def technical_catalog() -> dict:
    def coin(tid: int, year: int, denomination: str) -> dict:
        return {
            "denomination": {"displayName": denomination},
            "issuePeriod": {
                "displayValue": str(year),
                "startYear": year,
                "endYear": year,
            },
            "ucoinTypeId": tid,
            "detailUrl": f"https://pt.ucoin.net/coin/portugal-test-{year}/?tid={tid}",
            "images": {
                "obverse": f"https://img.test/{tid}-1.jpg",
                "reverse": f"https://img.test/{tid}-2.jpg",
            },
            "variants": [],
        }

    return {
        "country": "Portugal",
        "periods": [
            {
                "country": "Portugal",
                "rulerOrPeriodName": "República Portuguesa",
                "coins": [coin(1, 2000, "100 escudos"), coin(2, 2001, "200 escudos")],
            }
        ],
    }


def subject_output(raw: dict, *, include_second: bool = True) -> dict:
    coins = raw["periods"][0]["coins"]
    selected = coins if include_second else coins[:1]
    items = []
    for index, coin in enumerate(selected, start=1):
        items.append(
            {
                "ucoinTypeId": coin["ucoinTypeId"],
                "detailUrl": coin["detailUrl"],
                "finalUrl": coin["detailUrl"],
                "status": 200,
                "catalogCountry": "Portugal",
                "periodCountry": "Portugal",
                "documentTitle": "uCoin",
                "h1": f"Portugal {coin['denomination']['displayName']}, 200{index - 1}",
                "subject": f"Tema oficial {index}",
                "headings": [f"Tema oficial {index}"],
            }
        )
    return {
        "kind": "ucoin_detail_subjects",
        "source_catalog": "raw.json",
        "expected": len(coins),
        "completed": len(items),
        "items": items,
    }


def existing_catalog(raw: dict) -> dict:
    first = raw["periods"][0]["coins"][0]
    return {
        "formatVersion": 1,
        "catalogType": "special",
        "country": "Portugal",
        "siteCountry": "Portugal",
        "continent": "Europa",
        "programmes": [
            {
                "title": "Programa preservado",
                "coins": [
                    {
                        "denomination": "100 escudos",
                        "commemorativeName": "Nome já aprovado",
                        "year": "2000",
                        "order": 1,
                        "detailUrl": first["detailUrl"],
                        "numistaUrl": "",
                        "obverseImage": "https://approved.test/front.jpg",
                        "reverseImage": "https://approved.test/back.jpg",
                        "hidden": False,
                        "hasVariants": False,
                        "variants": [],
                    }
                ],
            }
        ],
    }


class PrepareSpecialAppCatalogTests(unittest.TestCase):
    def write_sources(self, root: Path, *, complete: bool = True) -> tuple[Path, Path, Path]:
        raw = technical_catalog()
        raw_path = root / "raw.json"
        subjects_path = root / "subjects.json"
        existing_path = root / "special-app-catalog.json"
        raw_path.write_text(json.dumps(raw), encoding="utf-8")
        subjects_path.write_text(
            json.dumps(subject_output(raw, include_second=complete)),
            encoding="utf-8",
        )
        existing_path.write_text(json.dumps(existing_catalog(raw)), encoding="utf-8")
        return raw_path, subjects_path, existing_path

    def test_build_preserves_existing_and_adds_official_subject(self) -> None:
        with TemporaryDirectory() as temporary:
            raw, subjects, existing = self.write_sources(Path(temporary))
            catalogue = preparer.build_catalog(
                raw,
                subjects,
                existing,
                country="Portugal",
                site_country="Portugal",
                continent="Europa",
            )

        self.assertEqual(catalogue["reviewStatus"], "pending")
        self.assertEqual(catalogue["summary"]["preservedCoins"], 1)
        self.assertEqual(catalogue["summary"]["newCoins"], 1)
        coins = [coin for _programme, coin in preparer.iter_special_coins(catalogue)]
        self.assertEqual(coins[0]["commemorativeName"], "Nome já aprovado")
        self.assertEqual(coins[0]["obverseImage"], "https://approved.test/front.jpg")
        self.assertEqual(coins[1]["commemorativeName"], "Tema oficial 2")
        self.assertEqual(coins[1]["order"], 2)

    def test_incomplete_subject_collection_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            raw, subjects, existing = self.write_sources(Path(temporary), complete=False)
            with self.assertRaisesRegex(
                preparer.SpecialCatalogPreparationError,
                "temas oficiais ainda não estão completos",
            ):
                preparer.build_catalog(
                    raw,
                    subjects,
                    existing,
                    country="Portugal",
                    site_country="Portugal",
                    continent="Europa",
                )

    def test_approval_requires_unchanged_sources(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw, subjects, existing = self.write_sources(root)
            pending = root / "pending.json"
            approved = root / "approved.json"
            catalogue = preparer.build_catalog(
                raw,
                subjects,
                existing,
                country="Portugal",
                site_country="Portugal",
                continent="Europa",
            )
            pending.write_bytes(preparer.json_bytes(catalogue))
            args = argparse.Namespace(
                pending=pending,
                catalog=raw,
                subjects=subjects,
                existing_catalog=existing,
                approved_output=approved,
                country="Portugal",
                site_country="Portugal",
                continent="Europa",
            )

            self.assertEqual(preparer.approve_pending(args), 0)
            result = json.loads(approved.read_text(encoding="utf-8"))

        self.assertEqual(result["reviewStatus"], "approved")

    def test_approval_rejects_changed_existing_catalog(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw, subjects, existing = self.write_sources(root)
            pending = root / "pending.json"
            catalogue = preparer.build_catalog(
                raw,
                subjects,
                existing,
                country="Portugal",
                site_country="Portugal",
                continent="Europa",
            )
            pending.write_bytes(preparer.json_bytes(catalogue))
            existing.write_text("{}", encoding="utf-8")
            args = argparse.Namespace(
                pending=pending,
                catalog=raw,
                subjects=subjects,
                existing_catalog=existing,
                approved_output=root / "approved.json",
                country="Portugal",
                site_country="Portugal",
                continent="Europa",
            )

            with self.assertRaisesRegex(
                preparer.SpecialCatalogPreparationError,
                "fontes mudaram",
            ):
                preparer.approve_pending(args)


if __name__ == "__main__":
    unittest.main()
