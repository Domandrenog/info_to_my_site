from __future__ import annotations

import unittest

from scripts import update_eua_presidential_dollars as presidential


class UpdateEuaPresidentialDollarsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan = presidential.load_plan(presidential.DEFAULT_PLAN)

    def live_records(self) -> list[dict[str, object]]:
        planned = [
            {
                "id": item["id"],
                "country": "EUA",
                "name": "1 dólar",
                "notes": "Presidential Dollars",
                "year": item["year"],
                **item["current"],
            }
            for item in self.plan["updates"]
        ]
        outside = [
            {
                "id": f"outside-{index}",
                "country": "EUA",
                "name": "25 cents",
                "notes": "50 State Quarters",
                "year": "1999",
                "commemorative_name": f"State {index}",
                "ordem": index,
            }
            for index in range(presidential.EXPECTED_USA_SPECIAL_COINS - len(planned))
        ]
        return [*planned, *outside]

    def test_committed_plan_has_chronological_ordinals(self) -> None:
        updates = self.plan["updates"]

        self.assertEqual([item["president_number"] for item in updates], [*range(1, 39), 40, 41])
        self.assertEqual([item["set"]["ordem"] for item in updates], list(range(50, 90)))
        self.assertEqual(updates[0]["set"]["commemorative_name"], "1st - George Washington")
        self.assertEqual(updates[21]["set"]["commemorative_name"], "22nd - Grover Cleveland")
        self.assertEqual(updates[23]["set"]["commemorative_name"], "24th - Grover Cleveland")
        self.assertEqual(updates[-1]["set"]["commemorative_name"], "41st - George Herbert Walker Bush")

    def test_reconcile_accepts_mixed_initial_and_final_states(self) -> None:
        live = self.live_records()
        live[0].update(self.plan["updates"][0]["set"])

        pending, complete = presidential.reconcile_live(self.plan, live)

        self.assertEqual(len(pending), 39)
        self.assertEqual(len(complete), 1)

    def test_reconcile_rejects_unapproved_field_state(self) -> None:
        live = self.live_records()
        live[0]["commemorative_name"] = "Washington"

        with self.assertRaisesRegex(presidential.PresidentialDollarError, "nem no estado"):
            presidential.reconcile_live(self.plan, live)

    def test_ordinal_suffixes_cover_exceptions(self) -> None:
        self.assertEqual(presidential.ordinal(1), "1st")
        self.assertEqual(presidential.ordinal(2), "2nd")
        self.assertEqual(presidential.ordinal(3), "3rd")
        self.assertEqual(presidential.ordinal(11), "11th")
        self.assertEqual(presidential.ordinal(12), "12th")
        self.assertEqual(presidential.ordinal(13), "13th")
        self.assertEqual(presidential.ordinal(21), "21st")


if __name__ == "__main__":
    unittest.main()
