from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from scripts import import_base44_special_coins as importer


class FakeEntityClient:
    def __init__(self, store: dict[str, list[dict]], entity: str) -> None:
        self.store = store
        self.entity = entity

    def filter(self, query, limit=1000, skip=0, **_kwargs):
        matches = [
            copy.deepcopy(record)
            for record in self.store[self.entity]
            if all(record.get(key) == value for key, value in query.items())
        ]
        return matches[skip : skip + limit]

    def bulk_create(self, payloads):
        for payload in payloads:
            self.store[self.entity].append(
                {
                    **copy.deepcopy(payload),
                    "id": f"special-{len(self.store[self.entity]) + 1}",
                }
            )


class FakeBaseClient:
    def __init__(self, store: dict[str, list[dict]]) -> None:
        self.store = store

    def for_entity(self, entity):
        return FakeEntityClient(self.store, entity)


def catalog() -> dict:
    return {
        "formatVersion": 1,
        "catalogType": "special",
        "reviewStatus": "approved",
        "country": "Portugal",
        "siteCountry": "Portugal",
        "continent": "Europa",
        "programmes": [
            {
                "title": "República Portuguesa",
                "coins": [
                    {
                        "denomination": "100 escudos",
                        "commemorativeName": "Tema A",
                        "year": "2000",
                        "order": 1,
                        "detailUrl": "https://pt.ucoin.net/coin/a/?tid=1",
                        "numistaUrl": "",
                        "obverseImage": "https://img/a1.jpg",
                        "reverseImage": "https://img/a2.jpg",
                        "hidden": False,
                        "hasVariants": False,
                        "variants": [],
                    },
                    {
                        "denomination": "200 escudos",
                        "commemorativeName": "Tema B",
                        "year": "2001",
                        "order": 2,
                        "detailUrl": "https://pt.ucoin.net/coin/b/?tid=2",
                        "numistaUrl": "",
                        "obverseImage": "https://img/b1.jpg",
                        "reverseImage": "https://img/b2.jpg",
                        "hidden": False,
                        "hasVariants": False,
                        "variants": [],
                    },
                ],
            }
        ],
    }


def existing_first(*, country: str = "Portugal") -> dict:
    payload = importer.payload_from_coin(
        catalog(),
        "República Portuguesa",
        catalog()["programmes"][0]["coins"][0],
    )
    return {
        **payload,
        "id": "special-existing",
        "country": country,
        "condition": "Tenho",
        "adquirida_por": "Eu",
    }


class ImportBase44SpecialCoinsTests(unittest.TestCase):
    def test_plan_preserves_existing_personal_fields_and_creates_only_missing(self) -> None:
        live = [existing_first()]

        plan = importer.build_plan(catalog(), live)

        self.assertEqual(len(plan.existing), 1)
        self.assertEqual(len(plan.pending), 1)
        self.assertEqual(plan.existing[0][1]["condition"], "Tenho")
        self.assertEqual(plan.pending[0]["commemorative_name"], "Tema B")

    def test_apply_creates_missing_without_updating_existing(self) -> None:
        original = existing_first()
        store = {"SpecialCoin": [copy.deepcopy(original)], "AdminStats": []}
        base = FakeBaseClient(store)
        plan = importer.build_plan(catalog(), store["SpecialCoin"])

        with (
            patch.object(
                importer,
                "no_retry_entity_client",
                side_effect=lambda client, entity: client.for_entity(entity),
            ),
            patch.object(importer, "mark_numisvault_stats_stale"),
        ):
            created = importer.apply_plan(base, plan)

        self.assertEqual(created, 1)
        self.assertEqual(store["SpecialCoin"][0], original)
        self.assertEqual(store["SpecialCoin"][1]["condition"], "Não Tenho")
        self.assertEqual(store["SpecialCoin"][1]["has_variants"], False)

    def test_url_owned_by_another_country_is_rejected(self) -> None:
        live = [existing_first(country="EUA")]

        with self.assertRaisesRegex(
            importer.MigrationExecutionError,
            "já pertence ao país EUA",
        ):
            importer.build_plan(catalog(), live)

    def test_pending_catalog_cannot_be_imported(self) -> None:
        pending = catalog()
        pending["reviewStatus"] = "pending"

        with self.assertRaisesRegex(
            importer.SpecialCatalogPreparationError,
            "ainda não foi aprovado",
        ):
            importer.catalogue_payloads(pending)


if __name__ == "__main__":
    unittest.main()
