from __future__ import annotations

import unittest

from scripts import migrate_portugal_commemoratives as migration


def source_entry(tid: int, order: int) -> dict:
    selected = next(item for item in migration.SELECTION if item[0] == tid)
    return {
        "entity": "Coin",
        "record": {
            "id": f"coin-{tid}",
            "country": "Portugal",
            "continent": "Europa",
            "name": "5 euro" if tid in {76581, 76582, 76583} else "2½ ECUs",
            "years": str(1990 + order),
            "condition": "Não Tenho",
            "image_frente": f"https://example.test/{tid}-front.jpg",
            "image_verso": f"https://example.test/{tid}-back.jpg",
            "adquirida_por": None,
            "data_aquisicao": None,
            "local_compra": None,
            "valor_pago": None,
            "moeda_valor": "EUR",
            "url_numista": "",
            "url_ucoin": f"https://pt.ucoin.net/coin/test/?tid={tid}",
            "hidden": False,
            "has_variants": False,
            "notes": selected[2],
            "ordem": order + 20,
            "rarity": "Histórica",
        },
        "variants": [],
        "sightings": [],
    }


class PortugalCommemorativeMigrationTests(unittest.TestCase):
    def entries(self) -> list[dict]:
        return [
            source_entry(tid, position)
            for position, (tid, _subject, _programme) in enumerate(
                migration.SELECTION, start=1
            )
        ]

    def test_plan_is_exact_ordered_and_preserves_collection_fields(self) -> None:
        plan = migration.build_plan(self.entries())

        self.assertEqual(len(plan), 19)
        self.assertEqual([item["order"] for item in plan], list(range(1, 20)))
        self.assertEqual(plan[0]["tid"], 76446)
        self.assertEqual(
            plan[0]["target_payload"]["commemorative_name"],
            'Infante D. Henrique "O Navegador"',
        )
        self.assertEqual(plan[6]["tid"], 76581)
        self.assertEqual(plan[-1]["tid"], 21513)
        self.assertNotIn("rarity", plan[0]["target_payload"])
        self.assertNotIn("id", plan[0]["target_payload"])
        self.assertEqual(plan[0]["target_payload"]["condition"], "Não Tenho")
        self.assertEqual(plan[0]["target_payload"]["ordem"], 1)

    def test_plan_fails_if_a_source_gains_variants_or_sightings(self) -> None:
        entries = self.entries()
        entries[0]["variants"] = [{"id": "variant-1"}]

        with self.assertRaisesRegex(
            migration.MigrationExecutionError, "ganhou variantes ou sinalizações"
        ):
            migration.build_plan(entries)

    def test_plan_fails_if_one_tid_is_missing(self) -> None:
        with self.assertRaisesRegex(
            migration.MigrationExecutionError, "Seleção incompleta"
        ):
            migration.build_plan(self.entries()[:-1])

    def test_interrupted_delete_is_recovered_only_with_confirmed_target(self) -> None:
        plan = migration.build_plan(self.entries())
        live_coins = [item["source_record"] for item in plan[1:]]
        state = {"deleted_sources": {}}

        recovered = migration.recover_interrupted_deletions(
            plan=plan,
            live_coins=live_coins,
            confirmed_targets={plan[0]["tid"]: {"id": "special-1"}},
            state=state,
        )

        self.assertEqual(recovered, 1)
        self.assertEqual(
            state["deleted_sources"][plan[0]["source_coin_id"]]["result"],
            "recovered_missing_after_interrupted_delete",
        )

    def test_interrupted_delete_without_target_fails_closed(self) -> None:
        plan = migration.build_plan(self.entries())

        with self.assertRaisesRegex(
            migration.MigrationExecutionError, "sem SpecialCoin confirmada"
        ):
            migration.recover_interrupted_deletions(
                plan=plan,
                live_coins=[item["source_record"] for item in plan[1:]],
                confirmed_targets={},
                state={"deleted_sources": {}},
            )


if __name__ == "__main__":
    unittest.main()
