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
    def test_resequence_programmes_orders_each_programme_chronologically(self) -> None:
        programmes = {
            "Programa": [
                {
                    "year": "2024",
                    "denomination": "2 euro",
                    "commemorativeName": "Tema recente",
                    "order": 1,
                },
                {
                    "year": "2007",
                    "denomination": "2 euro",
                    "commemorativeName": "Tema antigo",
                    "order": 2,
                },
            ]
        }

        preparer.resequence_programmes(["Programa"], programmes)

        self.assertEqual(
            [(coin["year"], coin["order"]) for coin in programmes["Programa"]],
            [("2007", 1), ("2024", 2)],
        )

    def test_single_year_uses_official_url_not_historical_year_in_label(self) -> None:
        coin = {
            "detailUrl": "https://pt.ucoin.net/coin/portugal-2-euro-2024/?tid=1",
            "issuePeriod": {
                "displayValue": "Revolução de 25 de Abril de 1974",
                "startYear": 1974,
                "endYear": 1974,
            },
        }

        self.assertEqual(preparer.single_year(coin), "2024")

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
                "temas oficiais das moedas novas ainda não estão completos",
            ):
                preparer.build_catalog(
                    raw,
                    subjects,
                    existing,
                    country="Portugal",
                    site_country="Portugal",
                    continent="Europa",
                )

    def test_partial_subject_collection_can_be_prepared_explicitly(self) -> None:
        with TemporaryDirectory() as temporary:
            raw, subjects, existing = self.write_sources(
                Path(temporary), complete=False
            )
            catalogue = preparer.build_catalog(
                raw,
                subjects,
                existing,
                country="Portugal",
                site_country="Portugal",
                continent="Europa",
                allow_partial=True,
            )

        self.assertEqual(catalogue["summary"]["coins"], 1)
        self.assertEqual(catalogue["summary"]["newCoins"], 0)
        self.assertEqual(catalogue["summary"]["remainingCoins"], 1)
        self.assertFalse(catalogue["summary"]["complete"])
        self.assertTrue(catalogue["source"]["partial"])

    def test_preserved_coin_does_not_need_to_be_recollected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw, subjects, existing = self.write_sources(root)
            raw_payload = json.loads(raw.read_text(encoding="utf-8"))
            subjects.write_text(
                json.dumps(subject_output(raw_payload, include_second=True)),
                encoding="utf-8",
            )
            subject_payload = json.loads(subjects.read_text(encoding="utf-8"))
            subject_payload["items"] = subject_payload["items"][1:]
            subject_payload["completed"] = 1
            subjects.write_text(json.dumps(subject_payload), encoding="utf-8")

            catalogue = preparer.build_catalog(
                raw,
                subjects,
                existing,
                country="Portugal",
                site_country="Portugal",
                continent="Europa",
            )

        self.assertEqual(catalogue["summary"]["preservedCoins"], 1)
        self.assertEqual(catalogue["summary"]["newCoins"], 1)

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

    def test_partial_approval_requires_explicit_flag(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw, subjects, existing = self.write_sources(root, complete=False)
            pending = root / "pending.json"
            catalogue = preparer.build_catalog(
                raw,
                subjects,
                existing,
                country="Portugal",
                site_country="Portugal",
                continent="Europa",
                allow_partial=True,
            )
            pending.write_bytes(preparer.json_bytes(catalogue))
            args = argparse.Namespace(
                pending=pending,
                catalog=raw,
                subjects=subjects,
                existing_catalog=existing,
                approved_output=root / "approved.json",
                country="Portugal",
                site_country="Portugal",
                continent="Europa",
                allow_partial=False,
            )

            with self.assertRaisesRegex(
                preparer.SpecialCatalogPreparationError, "catálogo é parcial"
            ):
                preparer.approve_pending(args)

            args.allow_partial = True
            self.assertEqual(preparer.approve_pending(args), 0)


if __name__ == "__main__":
    unittest.main()
