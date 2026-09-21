from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import fix_site_issues_api


class FixSiteIssuesPlanTests(unittest.TestCase):
    def test_build_plan_keeps_current_and_proposed_values(self) -> None:
        report = {
            "coins_with_issues": [
                {
                    "denomination": "1 escudo",
                    "issuePeriod": "1950",
                    "ucoinUrl": "https://pt.ucoin.net/coin/example",
                    "siteUrl": "https://base44.test/entities/Coin/record-1",
                    "issues": [
                        {"field": "notes", "value": "", "missing_value": "República"},
                        {
                            "field": "url_ucoin",
                            "value": "https://old.test/coin",
                            "missing_value": "https://pt.ucoin.net/coin/example",
                        },
                    ],
                }
            ]
        }

        plan = fix_site_issues_api.build_fix_plan(report, {})

        self.assertEqual(
            plan["updates"][0]["current"],
            {"notes": "", "url_ucoin": "https://old.test/coin"},
        )
        self.assertEqual(
            plan["updates"][0]["set"],
            {"notes": "República", "url_ucoin": "https://pt.ucoin.net/coin/example"},
        )
        self.assertEqual(plan["updates"][0]["issuePeriod"], "1950")

    def test_selection_keeps_only_the_explicit_field(self) -> None:
        plan = {
            "updates": [
                {
                    "record_id": "record-1",
                    "current": {"notes": "", "url_ucoin": ""},
                    "set": {"notes": "República", "url_ucoin": "https://example.test"},
                }
            ],
            "missing": [{"denomination": "2 escudos"}],
        }

        selected = fix_site_issues_api.select_update_fields(plan, ["url_ucoin"])

        self.assertEqual(selected["updates"][0]["current"], {"url_ucoin": ""})
        self.assertEqual(selected["updates"][0]["set"], {"url_ucoin": "https://example.test"})
        self.assertEqual(selected["missing"], plan["missing"])

    def test_collect_global_plan_reuses_one_api_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paises_dir = Path(temp_dir) / "paises"
            country_dir = paises_dir / "europa" / "pais-teste"
            country_dir.mkdir(parents=True)
            (country_dir / "app-catalog.json").write_text(
                json.dumps(
                    {
                        "country": "País Teste",
                        "periods": [
                            {
                                "title": "País Teste › República › 1950",
                                "coins": [
                                    {
                                        "denomination": "1 escudo",
                                        "issuePeriod": "1950",
                                        "detailUrl": "https://pt.ucoin.net/coin/example",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            report = {
                "country": "pais-teste",
                "country_name": "País Teste",
                "summary": {"by_type": {"missing_notes": 1, "missing_image_url": 2}},
                "coins_with_issues": [
                    {
                        "denomination": "1 escudo",
                        "issuePeriod": "1950",
                        "ucoinUrl": "https://pt.ucoin.net/coin/example",
                        "siteUrl": "https://base44.test/entities/Coin/record-1",
                        "issues": [{"field": "notes", "value": "", "missing_value": "República"}],
                    }
                ],
            }

            with (
                patch.object(fix_site_issues_api.checker, "api_records_for_all_countries", return_value=([], None)) as api,
                patch.object(fix_site_issues_api.checker, "compare_country", return_value=report) as compare,
            ):
                plan = fix_site_issues_api.collect_global_fix_plan(paises_dir, Path(temp_dir) / "All_Coins")

        api.assert_called_once_with()
        self.assertEqual(compare.call_count, 1)
        self.assertEqual(plan["updates"][0]["country"], "País Teste")
        self.assertEqual(plan["manual_counts"], {"photos": 2})


class FixSiteIssuesApplyTests(unittest.TestCase):
    def test_apply_changes_selected_field_and_verifies_preserved_fields(self) -> None:
        class FakeClient:
            base_url = "https://base44.test/entities/Coin"

            def __init__(self) -> None:
                self.record = {
                    "id": "record-1",
                    "name": "1 escudo",
                    "notes": "",
                    "url_ucoin": "https://old.test",
                    "image_frente": "https://images.test/front.jpg",
                    "image_verso": "https://images.test/back.jpg",
                }
                self.payloads = []

            def request(self, method, url):
                return dict(self.record)

            def update(self, record_id, payload):
                self.payloads.append(dict(payload))
                self.record = dict(payload)

        client = FakeClient()
        args = argparse.Namespace(request_delay=0, rate_limit_delay=0, max_retries=0, continent="", condition="")
        plan = {
            "updates": [
                {
                    "denomination": "1 escudo",
                    "record_id": "record-1",
                    "current": {"notes": ""},
                    "set": {"notes": "República"},
                }
            ],
            "creates": [],
        }

        with patch.object(fix_site_issues_api.import_base44_coins, "create_client", return_value=client):
            result = fix_site_issues_api.apply_plan(args, "", plan)

        self.assertEqual(result, {"updated": 1, "created": 0, "skipped": 0})
        self.assertEqual(client.payloads[0]["notes"], "República")
        self.assertEqual(client.payloads[0]["url_ucoin"], "https://old.test")
        self.assertEqual(client.payloads[0]["image_frente"], "https://images.test/front.jpg")
        self.assertEqual(client.payloads[0]["image_verso"], "https://images.test/back.jpg")

    def test_apply_skips_record_changed_since_preview(self) -> None:
        class FakeClient:
            base_url = "https://base44.test/entities/Coin"

            def __init__(self) -> None:
                self.updated = False

            def request(self, method, url):
                return {"id": "record-1", "notes": "Alterado por outra pessoa"}

            def update(self, record_id, payload):
                self.updated = True

        client = FakeClient()
        args = argparse.Namespace(request_delay=0, rate_limit_delay=0, max_retries=0, continent="", condition="")
        plan = {
            "updates": [
                {
                    "denomination": "1 escudo",
                    "record_id": "record-1",
                    "current": {"notes": ""},
                    "set": {"notes": "República"},
                }
            ],
            "creates": [],
        }

        with (
            patch.object(fix_site_issues_api.import_base44_coins, "create_client", return_value=client),
            redirect_stdout(io.StringIO()),
        ):
            result = fix_site_issues_api.apply_plan(args, "", plan)

        self.assertEqual(result, {"updated": 0, "created": 0, "skipped": 1})
        self.assertFalse(client.updated)


if __name__ == "__main__":
    unittest.main()
