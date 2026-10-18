from __future__ import annotations

import copy
import unittest
from urllib.parse import unquote
from unittest.mock import patch

from scripts import import_catalog_variants as importer


class FakeEntityClient:
    def __init__(self, store: dict[str, list[dict]], entity: str) -> None:
        self.store = store
        self.entity = entity
        self.base_url = f"https://base44.test/{entity}"
        self.max_retries = 5

    def filter(self, query, limit=1000, skip=0):
        matches = [
            copy.deepcopy(record)
            for record in self.store[self.entity]
            if all(record.get(key) == value for key, value in query.items())
        ]
        return matches[skip : skip + limit]

    def bulk_create(self, payloads):
        if self.store.get("_fail_after_first") and self.entity in {
            "CoinVariant",
            "SpecialCoinVariant",
        }:
            payload = payloads[0]
            self.store[self.entity].append(
                {**copy.deepcopy(payload), "id": f"{self.entity}-partial"}
            )
            raise RuntimeError("ambiguous timeout")
        for payload in payloads:
            self.store[self.entity].append(
                {**copy.deepcopy(payload), "id": f"{self.entity}-{len(self.store[self.entity]) + 1}"}
            )

    def update(self, record_id, payload):
        record = next(item for item in self.store[self.entity] if item["id"] == record_id)
        record.update(copy.deepcopy(payload))

    def request(self, method, url, payload=None):
        record_id = unquote(url.rsplit("/", 1)[-1])
        return copy.deepcopy(
            next(item for item in self.store[self.entity] if item["id"] == record_id)
        )


class FakeBaseClient:
    def __init__(self, store: dict[str, list[dict]]) -> None:
        self.store = store

    def for_entity(self, entity):
        return FakeEntityClient(self.store, entity)


def normal_catalog() -> dict:
    return {
        "country": "Portugal",
        "periods": [
            {
                "coins": [
                    {
                        "denomination": "1 euro",
                        "issuePeriod": "2020 - 2026",
                        "detailUrl": "https://pt.ucoin.net/coin/portugal-1-euro/?tid=1",
                        "hasVariants": True,
                        "variants": [
                            {"tag": "2020", "order": 0},
                            {"tag": "2021", "order": 1},
                        ],
                    }
                ]
            }
        ],
    }


class ImportCatalogVariantsTests(unittest.TestCase):
    def test_detects_normal_and_special_catalogues(self) -> None:
        self.assertEqual(importer.detect_catalog_type(normal_catalog()), "normal")
        self.assertEqual(
            importer.detect_catalog_type(
                {"siteCountry": "EUA", "programmes": []}
            ),
            "special",
        )

    def test_single_row_is_not_treated_as_a_variant_choice(self) -> None:
        catalogue = normal_catalog()
        catalogue["periods"][0]["coins"][0]["variants"] = [
            {"tag": "Sem marca", "order": 0}
        ]

        country, specs = importer.catalogue_specs(catalogue, "normal")

        self.assertEqual(country, "Portugal")
        self.assertEqual(specs, [])

    def test_identical_duplicate_ucoin_url_is_aggregated_once(self) -> None:
        catalogue = normal_catalog()
        duplicate = copy.deepcopy(catalogue["periods"][0]["coins"][0])
        catalogue["periods"].append({"coins": [duplicate]})

        country, specs = importer.catalogue_specs(catalogue, "normal")

        self.assertEqual(country, "Portugal")
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].detail_url, duplicate["detailUrl"])

    def test_conflicting_duplicate_ucoin_url_is_rejected(self) -> None:
        catalogue = normal_catalog()
        duplicate = copy.deepcopy(catalogue["periods"][0]["coins"][0])
        duplicate["variants"][1]["tag"] = "2022"
        catalogue["periods"].append({"coins": [duplicate]})

        with self.assertRaisesRegex(
            importer.CatalogVariantError, "Dados de variantes contraditórios"
        ):
            importer.catalogue_specs(catalogue, "normal")

    def test_normal_import_creates_only_missing_and_then_sets_parent_flag(self) -> None:
        store = {
            "Coin": [
                {
                    "id": "coin-1",
                    "country": "Portugal",
                    "url_ucoin": "https://pt.ucoin.net/coin/portugal-1-euro/?tid=1",
                    "has_variants": False,
                    "name": "1 euro",
                }
            ],
            "CoinVariant": [
                {
                    "id": "variant-1",
                    "coin_id": "coin-1",
                    "tag": "2020",
                    "condition": "Tenho",
                    "ordem": 99,
                }
            ],
            "SpecialCoin": [],
            "SpecialCoinVariant": [],
            "AdminStats": [{"id": "stats", "needs_rebuild": True}],
        }
        base = FakeBaseClient(store)
        plan = importer.build_live_plan(base, normal_catalog(), catalog_type="normal")

        self.assertEqual(plan.existing, 1)
        self.assertEqual(plan.missing, 1)
        created, flags = importer.apply_plan(base, plan)

        self.assertEqual((created, flags), (1, 1))
        self.assertEqual(store["CoinVariant"][0]["condition"], "Tenho")
        self.assertEqual(store["CoinVariant"][0]["ordem"], 99)
        created_variant = next(
            item for item in store["CoinVariant"] if item["tag"] == "2021"
        )
        self.assertEqual(created_variant["condition"], "Não Tenho")
        self.assertTrue(store["Coin"][0]["has_variants"])

    def test_special_catalog_uses_special_parent_relationship(self) -> None:
        catalogue = {
            "siteCountry": "EUA",
            "programmes": [
                {
                    "title": "Programme",
                    "coins": [
                        {
                            "denomination": "25 cents",
                            "year": "2026",
                            "detailUrl": "https://pt.ucoin.net/coin/usa-quarter/?tid=2",
                            "variants": [
                                {"tag": "P", "order": 0},
                                {"tag": "D", "order": 1},
                            ],
                        }
                    ],
                }
            ],
        }
        store = {
            "Coin": [],
            "CoinVariant": [],
            "SpecialCoin": [
                {
                    "id": "special-1",
                    "country": "EUA",
                    "url_ucoin": "https://pt.ucoin.net/coin/usa-quarter/?tid=2",
                    "has_variants": False,
                }
            ],
            "SpecialCoinVariant": [],
            "AdminStats": [{"id": "stats", "needs_rebuild": True}],
        }
        base = FakeBaseClient(store)

        plan = importer.build_live_plan(base, catalogue, catalog_type="special")
        created, flags = importer.apply_plan(base, plan)

        self.assertEqual((created, flags), (2, 1))
        self.assertEqual(
            {item["special_coin_id"] for item in store["SpecialCoinVariant"]},
            {"special-1"},
        )
        self.assertTrue(store["SpecialCoin"][0]["has_variants"])

    def test_country_override_cannot_escape_catalog_scope(self) -> None:
        catalogue = {
            "siteCountry": "EUA",
            "programmes": [],
        }

        with self.assertRaisesRegex(
            importer.CatalogVariantError,
            r"--country Portugal.*catálogo \(EUA\).*Nenhuma consulta",
        ):
            importer.build_live_plan(
                FakeBaseClient({}),
                catalogue,
                catalog_type="special",
                country="Portugal",
            )

    def test_partial_ambiguous_write_marks_stats_stale_before_stopping(self) -> None:
        store = {
            "Coin": [
                {
                    "id": "coin-1",
                    "country": "Portugal",
                    "url_ucoin": "https://pt.ucoin.net/coin/portugal-1-euro/?tid=1",
                    "has_variants": False,
                }
            ],
            "CoinVariant": [],
            "SpecialCoin": [],
            "SpecialCoinVariant": [],
            "AdminStats": [{"id": "stats", "needs_rebuild": False}],
            "_fail_after_first": True,
        }
        base = FakeBaseClient(store)
        plan = importer.build_live_plan(base, normal_catalog(), catalog_type="normal")

        with (
            patch.object(importer, "mark_numisvault_stats_stale") as marker,
            self.assertRaisesRegex(importer.CatalogVariantError, "lote não confirmado"),
        ):
            importer.apply_plan(base, plan)

        marker.assert_called_once_with(base)
        self.assertEqual(len(store["CoinVariant"]), 1)
        self.assertFalse(store["Coin"][0]["has_variants"])


if __name__ == "__main__":
    unittest.main()
