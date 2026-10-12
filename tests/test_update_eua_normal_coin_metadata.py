from __future__ import annotations

import unittest

from scripts import update_eua_normal_coin_metadata as normal_metadata


class UpdateEuaNormalCoinMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan = normal_metadata.load_plan(normal_metadata.DEFAULT_PLAN)

    def live_records(self) -> list[dict[str, object]]:
        return [
            {
                "id": item["id"],
                "country": "EUA",
                "url_ucoin": item["url_ucoin"],
                "condition": "Não Tenho",
                "image_frente": f"front-{index}",
                "image_verso": f"back-{index}",
                "updated_date": "before",
                **item["current"],
            }
            for index, item in enumerate(self.plan["updates"])
        ]

    def test_committed_plan_covers_all_normal_coins(self) -> None:
        updates = self.plan["updates"]

        self.assertEqual(len(updates), normal_metadata.EXPECTED_RECORDS)
        self.assertEqual(len({item["id"] for item in updates}), normal_metadata.EXPECTED_RECORDS)
        self.assertEqual(len({item["url_ucoin"] for item in updates}), normal_metadata.EXPECTED_RECORDS)
        self.assertTrue(all(item["set"]["name"] in normal_metadata.EXPECTED_NAMES for item in updates))
        self.assertTrue(all(item["set"]["notes"] == normal_metadata.EXPECTED_NOTES for item in updates))
        self.assertTrue(
            all(item["accepted_intermediate"]["notes"] != normal_metadata.EXPECTED_NOTES for item in updates)
        )

    def test_reconcile_accepts_mixed_initial_and_final_states(self) -> None:
        live = self.live_records()
        pending_before, complete_before = normal_metadata.reconcile_live(self.plan, live)
        item = pending_before[0]
        record = next(record for record in live if record["id"] == item["id"])
        record.update(item["set"])

        pending, complete = normal_metadata.reconcile_live(self.plan, live)

        self.assertEqual(len(pending), len(pending_before) - 1)
        self.assertEqual(len(complete), len(complete_before) + 1)

    def test_reconcile_accepts_the_short_lived_series_note_state(self) -> None:
        live = self.live_records()
        by_id = {record["id"]: record for record in live}
        for item in self.plan["updates"]:
            by_id[item["id"]].update(item["accepted_intermediate"])

        pending, complete = normal_metadata.reconcile_live(self.plan, live)

        self.assertEqual(len(pending), normal_metadata.EXPECTED_RECORDS)
        self.assertEqual(len(complete), 0)

    def test_reconcile_rejects_unapproved_metadata_state(self) -> None:
        live = self.live_records()
        live[0]["notes"] = "Outra série"

        with self.assertRaisesRegex(normal_metadata.NormalCoinMetadataError, "não está no estado"):
            normal_metadata.reconcile_live(self.plan, live)

    def test_protected_fields_ignore_only_metadata_and_updated_date(self) -> None:
        before = self.live_records()[0]
        after = {
            **before,
            **self.plan["updates"][0]["set"],
            "updated_date": "after",
        }

        self.assertEqual(
            normal_metadata.protected_fields(before),
            normal_metadata.protected_fields(after),
        )
        after["condition"] = "Tenho"
        self.assertNotEqual(
            normal_metadata.protected_fields(before),
            normal_metadata.protected_fields(after),
        )

    def test_live_update_sends_only_fields_that_really_differ(self) -> None:
        item = self.plan["updates"][0]
        before = {
            "id": item["id"],
            "country": "EUA",
            "url_ucoin": item["url_ucoin"],
            "condition": "Não Tenho",
            **item["accepted_intermediate"],
        }
        state = dict(before)

        class WriteClient:
            payload: dict[str, object] | None = None

            def update(self, record_id, payload):
                self.payload = dict(payload)
                state.update(payload)

        class ReadClient:
            base_url = "https://example.test/Coin"

            def request(self, method, path):
                return dict(state)

        write_client = WriteClient()
        status, fields = normal_metadata.update_and_verify(
            read_client=ReadClient(),
            write_client=write_client,
            item=item,
            before=before,
            mutation_success_fn=lambda: None,
        )

        self.assertEqual(status, "updated")
        self.assertEqual(fields, ["notes"])
        self.assertEqual(write_client.payload, {"notes": normal_metadata.EXPECTED_NOTES})


if __name__ == "__main__":
    unittest.main()
