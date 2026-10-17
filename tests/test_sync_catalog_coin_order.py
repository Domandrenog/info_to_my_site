from __future__ import annotations

import copy
import unittest
from unittest.mock import patch
from urllib.parse import unquote

from scripts import sync_catalog_coin_order as syncer


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


def catalog() -> dict:
    return {
        "country": "Portugal",
        "periods": [
            {
                "ruler": "República Portuguesa",
                "coins": [
                    {
                        "denomination": "1 escudo",
                        "issuePeriod": "1986 - 2001",
                        "detailUrl": "https://ucoin.test/one-new",
                    },
                    {
                        "denomination": "5 escudos",
                        "issuePeriod": "1986 - 2001",
                        "detailUrl": "https://ucoin.test/five",
                    },
                ],
            },
            {
                "ruler": "ECU",
                "coins": [
                    {
                        "denomination": "1 escudo",
                        "issuePeriod": "1991",
                        "detailUrl": "https://ucoin.test/ecu-one",
                    },
                ],
            },
            {
                "ruler": "República Portuguesa",
                "coins": [
                    {
                        "denomination": "1 escudo",
                        "issuePeriod": "1927 - 1968",
                        "detailUrl": "https://ucoin.test/one-old",
                    },
                ]
            }
        ],
    }


def store() -> dict[str, list[dict]]:
    return {
        "Coin": [
            {
                "id": "new",
                "country": "Portugal",
                "name": "1 escudo",
                "years": "1986 - 2001",
                "url_ucoin": "https://ucoin.test/one-new",
                "ordem": 1,
            },
            {
                "id": "five",
                "country": "Portugal",
                "name": "5 escudos",
                "years": "1986 - 2001",
                "url_ucoin": "https://ucoin.test/five",
                "ordem": 2,
            },
            {
                "id": "old",
                "country": "Portugal",
                "name": "1 escudo",
                "years": "1927 - 1968",
                "url_ucoin": "https://ucoin.test/one-old",
                "ordem": 3,
            },
            {
                "id": "ecu",
                "country": "Portugal",
                "name": "1 escudo",
                "years": "1991",
                "url_ucoin": "https://ucoin.test/ecu-one",
                "ordem": 4,
            },
        ],
        "AdminStats": [{"id": "stats", "needs_rebuild": True}],
    }


class SyncCatalogCoinOrderTests(unittest.TestCase):
    def test_build_plan_groups_equal_denominations_chronologically(self) -> None:
        plan = syncer.build_live_plan(FakeBaseClient(store()), catalog())

        self.assertEqual(
            [(item.spec.label, item.spec.desired_order) for item in plan.updates],
            [
                ("1 escudo (1927 - 1968)", 1),
                ("1 escudo (1986 - 2001)", 2),
                ("5 escudos (1986 - 2001)", 3),
            ],
        )
        self.assertEqual(plan.specs[-1].period_label, "ECU")
        self.assertEqual(plan.specs[-1].desired_order, 4)

    def test_apply_changes_only_order(self) -> None:
        data = store()
        base = FakeBaseClient(data)
        plan = syncer.build_live_plan(base, catalog())
        before = {
            item["id"]: {key: value for key, value in item.items() if key != "ordem"}
            for item in data["Coin"]
        }

        with patch.object(syncer, "mark_numisvault_stats_stale") as marker:
            changed = syncer.apply_plan(base, plan)

        self.assertEqual(changed, 3)
        marker.assert_called_once_with(base)
        self.assertEqual(
            {item["id"]: item["ordem"] for item in data["Coin"]},
            {"old": 1, "new": 2, "five": 3, "ecu": 4},
        )
        self.assertEqual(
            {
                item["id"]: {key: value for key, value in item.items() if key != "ordem"}
                for item in data["Coin"]
            },
            before,
        )

    def test_site_only_coin_stops_before_writes(self) -> None:
        data = store()
        data["Coin"].append(
            {
                "id": "extra",
                "country": "Portugal",
                "url_ucoin": "https://ucoin.test/extra",
                "ordem": 4,
            }
        )

        with self.assertRaisesRegex(syncer.CoinOrderError, "sem correspondência exata"):
            syncer.build_live_plan(FakeBaseClient(data), catalog())


if __name__ == "__main__":
    unittest.main()
