from __future__ import annotations

import unittest

from scripts.plan_missing_country_tracking import (
    build_collection_command,
    build_tracking_plans,
    record_year_bounds,
    select_tracking_plans,
    year_coverage,
)


class MissingCountryTrackingPlanTests(unittest.TestCase):
    def test_year_coverage_uses_first_start_and_latest_end_year(self) -> None:
        records = [
            {"name": "1 cent", "years": "1966-2015"},
            {"name": "15 cent", "years": "2018"},
            {"name": "Sem data", "years": ""},
        ]

        self.assertEqual(record_year_bounds(records[0]), (1966, 2015))
        self.assertEqual(
            year_coverage(records),
            {
                "first_year": 1966,
                "last_year": 2018,
                "latest_coins": ["15 cent (2018)"],
                "records_without_years": 1,
            },
        )

    def test_build_plan_uses_api_continent_and_ucoin_alias(self) -> None:
        grouped = {
            "coreia-do-sul": [
                {"country": "Coreia do Sul", "continent": "Ásia", "name": "500 won", "years": "2023"}
            ]
        }

        plans = build_tracking_plans(grouped, set())

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["country_slug"], "coreia-do-sul")
        self.assertEqual(plans[0]["ucoin_country_link_name"], "south_korea")
        self.assertEqual(plans[0]["output_dir"], "info/paises/asia/coreia-do-sul")

    def test_select_tracking_plans_accepts_all_or_indexes(self) -> None:
        plans = [{"country": "A"}, {"country": "B"}, {"country": "C"}]

        self.assertEqual(select_tracking_plans(plans, "todos"), plans)
        self.assertEqual(select_tracking_plans(plans, "3,1,3"), [plans[2], plans[0]])
        with self.assertRaisesRegex(ValueError, "Índice fora"):
            select_tracking_plans(plans, "4")

    def test_collection_command_is_preview_only_and_uses_full_api_coverage(self) -> None:
        plan = {
            "country": "Bahamas",
            "continent": "América",
            "ucoin_country_link_name": "bahamas",
            "first_year": 1966,
        }

        command = build_collection_command(plan, browser_args=["--attach-cdp"])

        self.assertIn("--no-wait-for-final", command)
        self.assertEqual(command[command.index("--start-year") + 1], "1966")
        self.assertNotIn("scripts.import_base44_coins", command)


if __name__ == "__main__":
    unittest.main()
