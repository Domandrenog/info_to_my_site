from __future__ import annotations

import unittest

from scripts.ucoin_catalog import (
    classify_circulation_type,
    filter_periods_by_coin_type,
)


def coin(label: str, circulation_type: str | None) -> dict[str, object]:
    return {
        "denomination": {"displayName": label},
        "circulationType": circulation_type,
    }


class UCoinCoinTypeFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.periods = [
            {
                "fullTitle": "Portugal › República Portuguesa › 1969 - 1986",
                "coins": [
                    coin("1 escudo", "Circulação normal"),
                    coin("25 escudos", "Comemorativa"),
                    coin("1000 escudos", "Moeda de coleção"),
                    coin("Prova", "Tokens (provas)"),
                    coin("Sem tipo", None),
                ],
            }
        ]

    def test_classifies_portuguese_ucoin_labels(self) -> None:
        self.assertEqual(classify_circulation_type("Circulação normal"), "normal")
        self.assertEqual(classify_circulation_type("Comemorativa"), "commemorative")
        self.assertEqual(classify_circulation_type("Moeda de coleção"), "collection")
        self.assertEqual(classify_circulation_type("Tokens (provas)"), "unknown")

    def test_normal_filter_is_fail_closed(self) -> None:
        periods, summary = filter_periods_by_coin_type(self.periods, "normal")

        self.assertEqual(
            [item["denomination"]["displayName"] for item in periods[0]["coins"]],
            ["1 escudo"],
        )
        self.assertEqual(summary["selected"], 1)
        self.assertEqual(summary["excluded"], 4)
        self.assertEqual(summary["foundByType"]["unknown"], 2)

    def test_commemorative_filter_excludes_collection_and_proof(self) -> None:
        periods, summary = filter_periods_by_coin_type(
            self.periods, "commemorative"
        )

        self.assertEqual(
            [item["denomination"]["displayName"] for item in periods[0]["coins"]],
            ["25 escudos"],
        )
        self.assertEqual(summary["selected"], 1)

    def test_all_requires_explicit_selection(self) -> None:
        periods, summary = filter_periods_by_coin_type(self.periods, "all")

        self.assertEqual(len(periods[0]["coins"]), 5)
        self.assertEqual(summary["excluded"], 0)


if __name__ == "__main__":
    unittest.main()
