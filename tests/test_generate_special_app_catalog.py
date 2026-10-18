from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts import generate_special_app_catalog as generator


CATALOG_PATH = Path("info/paises/america/eua/special-app-catalog.json")
PORTUGAL_CATALOG_PATH = Path(
    "info/paises/europa/portugal/special-app-catalog.json"
)
PRIVATE_FIELDS = {
    "id",
    "special_coin_id",
    "condition",
    "adquirida_por",
    "data_aquisicao",
    "local_compra",
    "valor_pago",
    "moeda_valor",
    "created_by",
    "created_by_id",
    "created_date",
    "updated_date",
    "is_sample",
    "sightings",
}


def backup_entry(
    *,
    parent_id: str = "special-1",
    name: str = "25 cents",
    subject: str = "Delaware",
    year: str = "1999",
    order: int = 2,
    programme: str = "50 State Quarters",
) -> dict:
    return {
        "entity": "SpecialCoin",
        "record": {
            "id": parent_id,
            "country": "EUA",
            "continent": "América",
            "name": name,
            "commemorative_name": subject,
            "year": year,
            "ordem": order,
            "url_ucoin": f"https://pt.ucoin.net/coin/{parent_id}",
            "url_numista": None,
            "image_frente": f"https://example.test/{parent_id}-front.jpg",
            "image_verso": f"https://example.test/{parent_id}-back.jpg",
            "notes": programme,
            "hidden": False,
            "has_variants": True,
            "condition": "Tenho",
            "adquirida_por": "Utilizador privado",
        },
        "variants": [
            {
                "id": f"{parent_id}-variant",
                "special_coin_id": parent_id,
                "tag": "D",
                "ordem": 0,
                "condition": "Tenho",
            }
        ],
        "sightings": [{"username": "privado"}],
    }


class GenerateSpecialAppCatalogTests(unittest.TestCase):
    def test_build_catalog_keeps_descriptive_data_and_strips_private_state(self) -> None:
        catalog = generator.build_catalog([backup_entry()])

        self.assertEqual(catalog["catalogType"], "special")
        self.assertEqual(catalog["summary"], {"programmes": 1, "coins": 1, "variants": 1})
        coin = catalog["programmes"][0]["coins"][0]
        self.assertEqual(coin["commemorativeName"], "Delaware")
        self.assertEqual(coin["variants"], [{"tag": "D", "order": 0}])
        serialized = json.dumps(catalog, ensure_ascii=False)
        for field in PRIVATE_FIELDS:
            self.assertNotIn(f'"{field}"', serialized)

    def test_build_catalog_rejects_variant_linked_to_another_parent(self) -> None:
        entry = backup_entry()
        entry["variants"][0]["special_coin_id"] = "wrong-parent"

        with self.assertRaisesRegex(generator.SpecialCatalogError, "pai errado"):
            generator.build_catalog([entry])

    def test_build_catalog_supports_portugal_without_changing_usa_defaults(self) -> None:
        entry = backup_entry()
        entry["record"].update(
            {
                "country": "Portugal",
                "continent": "Europa",
                "name": "2½ ECUs",
                "commemorative_name": 'Infante D. Henrique "O Navegador"',
                "year": "1991",
                "notes": "ECU",
            }
        )

        catalog = generator.build_catalog(
            [entry],
            country_name="Portugal",
            site_country="Portugal",
            continent="Europa",
        )

        self.assertEqual(catalog["country"], "Portugal")
        self.assertEqual(catalog["siteCountry"], "Portugal")
        self.assertEqual(catalog["continent"], "Europa")
        self.assertEqual(catalog["programmes"][0]["title"], "ECU")

    def test_build_catalog_preserves_known_missing_photos(self) -> None:
        entry = backup_entry()
        entry["record"]["image_frente"] = ""
        entry["record"]["image_verso"] = ""

        catalog = generator.build_catalog([entry])

        coin = catalog["programmes"][0]["coins"][0]
        self.assertEqual(coin["obverseImage"], "")
        self.assertEqual(coin["reverseImage"], "")

    def test_committed_catalog_has_the_complete_current_scope(self) -> None:
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

        self.assertEqual(catalog["country"], "Estados Unidos da América")
        self.assertEqual(catalog["siteCountry"], "EUA")
        self.assertEqual(
            catalog["summary"], {"programmes": 11, "coins": 245, "variants": 298}
        )
        coins = [
            coin
            for programme in catalog["programmes"]
            for coin in programme["coins"]
        ]
        self.assertEqual(len(coins), 245)
        self.assertEqual(sum(len(coin["variants"]) for coin in coins), 298)

        serialized = json.dumps(catalog, ensure_ascii=False)
        for field in PRIVATE_FIELDS:
            self.assertNotIn(f'"{field}"', serialized)

    def test_presidential_dollars_are_chronological_with_ordinals(self) -> None:
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        programme = next(
            item
            for item in catalog["programmes"]
            if item["title"] == "Presidential Dollars"
        )
        names = [coin["commemorativeName"] for coin in programme["coins"]]

        self.assertEqual(len(names), 40)
        self.assertEqual(names[0], "1st - George Washington")
        self.assertEqual(names[21], "22nd - Grover Cleveland")
        self.assertEqual(names[23], "24th - Grover Cleveland")
        self.assertEqual(names[-1], "41st - George Herbert Walker Bush")

    def test_committed_portugal_catalog_has_the_migrated_collection(self) -> None:
        catalog = json.loads(PORTUGAL_CATALOG_PATH.read_text(encoding="utf-8"))

        self.assertEqual(catalog["country"], "Portugal")
        self.assertEqual(catalog["siteCountry"], "Portugal")
        self.assertEqual(catalog["continent"], "Europa")
        self.assertEqual(
            catalog["summary"], {"programmes": 3, "coins": 19, "variants": 0}
        )
        self.assertEqual(
            [programme["title"] for programme in catalog["programmes"]],
            ["ECU", "Região Autónoma dos Açores", "Região Autónoma da Madeira"],
        )
        coins = [
            coin
            for programme in catalog["programmes"]
            for coin in programme["coins"]
        ]
        self.assertEqual([coin["order"] for coin in coins], list(range(1, 20)))
        self.assertEqual(
            sum(not coin["obverseImage"] or not coin["reverseImage"] for coin in coins),
            1,
        )
        serialized = json.dumps(catalog, ensure_ascii=False)
        for field in PRIVATE_FIELDS:
            self.assertNotIn(f'"{field}"', serialized)


if __name__ == "__main__":
    unittest.main()
