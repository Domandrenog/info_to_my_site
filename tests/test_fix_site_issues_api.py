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
    def test_verified_creation_updates_decision_and_missing_found_json(self) -> None:
        detail_url = "https://pt.ucoin.net/coin/china-1-jiao-1980-1986"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            decisions_path = root / "decisions.json"
            missing_found_path = root / "missing-found.json"
            fix_site_issues_api.save_name_match_decision(
                decisions_path,
                "china",
                {
                    "ucoinUrl": detail_url,
                    "catalogName": "1 jiao",
                    "catalogYears": "1980 - 1986",
                    "sameCoin": "new_record",
                    "createRecord": "yes",
                    "applyStatus": "pending_create",
                },
            )
            missing_found_path.write_text(
                json.dumps(
                    {
                        "country": "china",
                        "missing": [
                            {
                                "status": "confirmed_create",
                                "ucoinUrl": detail_url,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            results = [
                {
                    "record_id": "record-new",
                    "ucoin_url": detail_url,
                    "fields": ["create"],
                    "status": "created",
                    "message": "criação confirmada no Site Base44",
                }
            ]

            fix_site_issues_api.update_creation_application_status(decisions_path, results)
            fix_site_issues_api.confirm_created_matches(missing_found_path, results)
            decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
            missing_found = json.loads(missing_found_path.read_text(encoding="utf-8"))

        self.assertEqual(decisions["decisions"][0]["applyStatus"], "created")
        self.assertEqual(decisions["decisions"][0]["siteRecordId"], "record-new")
        self.assertEqual(missing_found["missing"][0]["status"], "created")
        self.assertEqual(missing_found["missing"][0]["record_id"], "record-new")

    def test_name_decision_json_records_verified_application_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "decisions.json"
            fix_site_issues_api.save_name_match_decision(
                path,
                "pais",
                {
                    "ucoinUrl": "https://pt.ucoin.net/coin/example",
                    "siteRecordId": "record-1",
                    "catalogName": "10 cêntimos",
                    "catalogYears": "2020",
                    "sameCoin": "yes",
                    "rename": "yes",
                    "proposedName": "10 cêntimos",
                    "updateYears": "yes",
                    "proposedYears": "2020",
                    "applyStatus": "pending",
                },
            )

            fix_site_issues_api.update_name_match_application_status(
                path,
                [
                    {
                        "record_id": "record-1",
                        "fields": ["name", "years"],
                        "status": "applied",
                        "message": "name, years",
                    }
                ],
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["decisions"][0]["applyStatus"], "applied")
        self.assertEqual(payload["decisions"][0]["applyMessage"], "name, years")
        self.assertIn("appliedAt", payload["decisions"][0])

    def test_successful_name_update_confirms_pending_association(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "missing-found.json"
            path.write_text(
                json.dumps(
                    {
                        "country": "pais",
                        "missing": [
                            {
                                "status": "connected_pending_name_update",
                                "ucoinUrl": "https://pt.ucoin.net/coin/example",
                                "apiUrl": "https://base44.test/entities/Coin/record-1",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            fix_site_issues_api.confirm_applied_name_matches(
                path,
                [
                    {
                        "record_id": "record-1",
                        "fields": ["name"],
                        "status": "applied",
                    }
                ],
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["missing"][0]["status"], "connected")

    def test_successful_year_update_confirms_pending_metadata_association(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "missing-found.json"
            path.write_text(
                json.dumps(
                    {
                        "country": "coreia-do-sul",
                        "missing": [
                            {
                                "status": "connected_pending_metadata_update",
                                "ucoinUrl": "https://pt.ucoin.net/coin/example",
                                "apiUrl": "https://base44.test/entities/Coin/record-1",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            fix_site_issues_api.confirm_applied_name_matches(
                path,
                [
                    {
                        "record_id": "record-1",
                        "fields": ["years"],
                        "status": "applied",
                    }
                ],
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["missing"][0]["status"], "connected")

    def test_abbreviated_denomination_units_are_equivalent_for_matching(self) -> None:
        self.assertEqual(fix_site_issues_api.normalize_key("10 cents"), "10 cent")
        self.assertEqual(fix_site_issues_api.normalize_key("10 cêntimos"), "10 cent")
        self.assertEqual(fix_site_issues_api.normalize_key("10 cent"), "10 cent")

    def test_reconcile_can_confirm_abbreviated_name_and_propose_full_name(self) -> None:
        missing = [
            {
                "denomination": "10 cêntimos",
                "ucoinUrl": "https://pt.ucoin.net/coin/example",
                "period": {"title": "País › República › 2020"},
                "coin": {"issuePeriod": "2020"},
            }
        ]
        site_record = {
            "id": "record-1",
            "name": "10 cents",
            "years": "2020",
            "url_ucoin": "",
            "notes": "República",
        }
        updates: list[dict[str, object]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            decisions_path = Path(temp_dir) / "name-match-decisions.json"
            with (
                patch.object(
                    fix_site_issues_api.checker,
                    "api_records_for_country",
                    return_value=([site_record], None),
                ),
                patch("builtins.input", side_effect=["s", "s"]),
                redirect_stdout(io.StringIO()) as output,
            ):
                result = fix_site_issues_api.reconcile_missing(
                    country_name="País",
                    missing_creates=missing,
                    plan_updates=updates,
                    associations_path=Path(temp_dir) / "associations.json",
                    missing_found_path=Path(temp_dir) / "missing-found.json",
                    interactive=True,
                    max_candidates=5,
                    name_decisions_path=decisions_path,
                    decision_country_slug="pais",
                )
            decisions = json.loads(decisions_path.read_text(encoding="utf-8"))

        self.assertEqual(result["unresolved"], [])
        self.assertEqual(result["all"][0]["status"], "connected_pending_metadata_update")
        self.assertEqual(updates[0]["current"]["name"], "10 cents")
        self.assertEqual(updates[0]["set"]["name"], "10 cêntimos")
        self.assertIn("Catálogo local: 10 cêntimos (2020)", output.getvalue())
        self.assertIn("uCoin: https://pt.ucoin.net/coin/example", output.getvalue())
        self.assertIn("Site Base44 agora: 10 cents", output.getvalue())
        self.assertEqual(decisions["country"], "pais")
        self.assertEqual(decisions["decisions"][0]["sameCoin"], "yes")
        self.assertEqual(decisions["decisions"][0]["rename"], "yes")
        self.assertEqual(decisions["decisions"][0]["updateYears"], "not_needed")
        self.assertEqual(decisions["decisions"][0]["applyStatus"], "pending")

    def test_reconcile_can_confirm_match_and_propose_updated_years(self) -> None:
        missing = [
            {
                "denomination": "1 won",
                "ucoinUrl": "https://pt.ucoin.net/coin/south-korea-1-won-1983-2026",
                "period": {"title": "Coreia do Sul › República › 1983-2026"},
                "coin": {"issuePeriod": "1983 - 2026"},
            }
        ]
        site_record = {
            "id": "record-1",
            "name": "1 won",
            "years": "1983-2025",
            "url_ucoin": "",
            "notes": "República",
        }
        updates: list[dict[str, object]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            decisions_path = Path(temp_dir) / "name-match-decisions.json"
            with (
                patch.object(
                    fix_site_issues_api.checker,
                    "api_records_for_country",
                    return_value=([site_record], None),
                ),
                patch("builtins.input", side_effect=["1", "s"]),
                redirect_stdout(io.StringIO()) as output,
            ):
                result = fix_site_issues_api.reconcile_missing(
                    country_name="Coreia do Sul",
                    missing_creates=missing,
                    plan_updates=updates,
                    associations_path=Path(temp_dir) / "associations.json",
                    missing_found_path=Path(temp_dir) / "missing-found.json",
                    interactive=True,
                    max_candidates=5,
                    name_decisions_path=decisions_path,
                    decision_country_slug="coreia-do-sul",
                )
            decisions = json.loads(decisions_path.read_text(encoding="utf-8"))

        self.assertEqual(result["unresolved"], [])
        self.assertEqual(result["all"][0]["status"], "connected_pending_metadata_update")
        self.assertEqual(updates[0]["current"]["years"], "1983-2025")
        self.assertEqual(updates[0]["set"]["years"], "1983 - 2026")
        self.assertNotIn("name", updates[0]["set"])
        self.assertIn(
            "uCoin: https://pt.ucoin.net/coin/south-korea-1-won-1983-2026",
            output.getvalue(),
        )
        self.assertIn("Site Base44 agora: 1983-2025", output.getvalue())
        self.assertEqual(decisions["decisions"][0]["rename"], "not_needed")
        self.assertEqual(decisions["decisions"][0]["updateYears"], "yes")
        self.assertEqual(decisions["decisions"][0]["proposedYears"], "1983 - 2026")
        self.assertEqual(decisions["decisions"][0]["applyStatus"], "pending")

    def test_reconcile_can_confirm_that_missing_coin_must_be_created(self) -> None:
        missing_item = {
            "denomination": "1 jiao",
            "ucoinUrl": "https://pt.ucoin.net/coin/china-1-jiao-1980-1986/?tid=5892",
            "period": {"title": "China › República Popular › 1980-1986"},
            "coin": {
                "denomination": "1 jiao",
                "issuePeriod": "1980 - 1986",
                "availability": "scarce",
            },
            "ordem": 1,
        }
        site_records = [
            {"id": f"record-{index}", "name": name, "years": years}
            for index, (name, years) in enumerate(
                [
                    ("1 jiao", "2019-2025"),
                    ("1 jiao", "2005-2018"),
                    ("1 jiao", "1999-2003"),
                    ("1 jiao", "1991-2000"),
                    ("5 jiao", "2019-2025"),
                ],
                start=1,
            )
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            decisions_path = Path(temp_dir) / "name-match-decisions.json"
            with (
                patch.object(
                    fix_site_issues_api.checker,
                    "api_records_for_country",
                    return_value=(site_records, None),
                ),
                patch("builtins.input", return_value="6"),
                redirect_stdout(io.StringIO()) as output,
            ):
                result = fix_site_issues_api.reconcile_missing(
                    country_name="China",
                    missing_creates=[missing_item],
                    plan_updates=[],
                    associations_path=Path(temp_dir) / "associations.json",
                    missing_found_path=Path(temp_dir) / "missing-found.json",
                    interactive=True,
                    max_candidates=5,
                    name_decisions_path=decisions_path,
                    decision_country_slug="china",
                )
            decisions = json.loads(decisions_path.read_text(encoding="utf-8"))

        self.assertEqual(result["unresolved"], [])
        self.assertEqual(result["creates"], [missing_item])
        self.assertEqual(result["all"][0]["status"], "confirmed_create")
        self.assertIn("6) Não existe no Site Base44 — criar moeda nova", output.getvalue())
        self.assertIn("0) Não associar por agora", output.getvalue())
        self.assertEqual(decisions["decisions"][0]["sameCoin"], "new_record")
        self.assertEqual(decisions["decisions"][0]["createRecord"], "yes")
        self.assertEqual(decisions["decisions"][0]["applyStatus"], "pending_create")

    def test_rejected_match_is_also_saved_in_decisions_json(self) -> None:
        missing = [
            {
                "denomination": "10 cêntimos",
                "ucoinUrl": "https://pt.ucoin.net/coin/example",
                "period": {},
                "coin": {"issuePeriod": "2020"},
            }
        ]
        site_record = {"id": "record-1", "name": "10 cent", "years": "2020"}

        with tempfile.TemporaryDirectory() as temp_dir:
            decisions_path = Path(temp_dir) / "name-match-decisions.json"
            with (
                patch.object(
                    fix_site_issues_api.checker,
                    "api_records_for_country",
                    return_value=([site_record], None),
                ),
                patch("builtins.input", return_value="n"),
                redirect_stdout(io.StringIO()),
            ):
                result = fix_site_issues_api.reconcile_missing(
                    country_name="País",
                    missing_creates=missing,
                    plan_updates=[],
                    associations_path=Path(temp_dir) / "associations.json",
                    missing_found_path=Path(temp_dir) / "missing-found.json",
                    interactive=True,
                    max_candidates=5,
                    name_decisions_path=decisions_path,
                    decision_country_slug="pais",
                )
            decisions = json.loads(decisions_path.read_text(encoding="utf-8"))

        self.assertEqual(result["unresolved"], [{"denomination": "10 cêntimos", "ucoinUrl": "https://pt.ucoin.net/coin/example"}])
        self.assertEqual(result["all"][0]["status"], "rejected")
        self.assertEqual(decisions["decisions"][0]["sameCoin"], "no")
        self.assertEqual(decisions["decisions"][0]["siteName"], "10 cent")

    def test_consolidate_updates_merges_duplicates_and_excludes_conflicting_fields(self) -> None:
        duplicate_updates = [
            {
                "record_id": "record-1",
                "denomination": "1 fen",
                "current": {"notes": "", "url_ucoin": ""},
                "set": {"notes": "República Popular", "url_ucoin": "https://example.test/1"},
            },
            {
                "record_id": "record-1",
                "denomination": "1 fen",
                "current": {"notes": "", "url_ucoin": ""},
                "set": {"notes": "República Popular", "url_ucoin": "https://example.test/2"},
            },
        ]

        updates, conflicts = fix_site_issues_api.consolidate_updates(duplicate_updates)

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["set"], {"notes": "República Popular"})
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["field"], "url_ucoin")
        self.assertEqual(
            conflicts[0]["values"],
            ["https://example.test/1", "https://example.test/2"],
        )

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

    def test_restrict_notes_scope_keeps_other_selected_fields(self) -> None:
        plan = {
            "updates": [
                {
                    "country_slug": "bahamas",
                    "current": {"notes": "", "url_ucoin": ""},
                    "set": {"notes": "Bahamas", "url_ucoin": "https://example.test/bahamas"},
                },
                {
                    "country_slug": "brasil",
                    "current": {"notes": "", "url_ucoin": ""},
                    "set": {"notes": "Brasil", "url_ucoin": "https://example.test/brasil"},
                },
            ]
        }

        selected = fix_site_issues_api.restrict_update_field_to_countries(plan, "notes", {"bahamas"})

        self.assertEqual(
            selected["updates"][0]["set"],
            {"notes": "Bahamas", "url_ucoin": "https://example.test/bahamas"},
        )
        self.assertEqual(
            selected["updates"][1]["set"],
            {"url_ucoin": "https://example.test/brasil"},
        )
        self.assertEqual(selected["updates"][1]["current"], {"url_ucoin": ""})

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
                patch.object(
                    fix_site_issues_api.checker,
                    "api_records_for_all_countries",
                    return_value=(
                        [
                            {"country": "País Teste", "notes": ""},
                            {"country": "País Teste", "notes": "República"},
                        ],
                        None,
                    ),
                ) as api,
                patch.object(fix_site_issues_api.checker, "compare_country", return_value=report) as compare,
            ):
                plan = fix_site_issues_api.collect_global_fix_plan(paises_dir, Path(temp_dir) / "All_Coins")

        api.assert_called_once_with()
        self.assertEqual(compare.call_count, 1)
        self.assertEqual(plan["updates"][0]["country"], "País Teste")
        self.assertEqual(plan["manual_counts"], {"photos": 2})
        self.assertEqual(
            plan["notes_status"],
            [
                {
                    "country": "País Teste",
                    "country_slug": "pais-teste",
                    "total": 2,
                    "with_notes": 1,
                    "without_notes": 1,
                    "fixable_missing_notes": 1,
                    "state": "partial",
                }
            ],
        )


class FixSiteIssuesApplyTests(unittest.TestCase):
    def test_apply_creates_only_confirmed_coin_and_verifies_it(self) -> None:
        class FakeClient:
            base_url = "https://base44.test/entities/Coin"

            def __init__(self) -> None:
                self.created_record = None
                self.bulk_calls = 0

            def bulk_create(self, records):
                self.bulk_calls += 1
                self.created_record = dict(records[0])
                return [{**self.created_record, "id": "record-new"}]

            def filter(self, query, limit=1, skip=0):
                if self.created_record is None:
                    return []
                return [{**self.created_record, "id": "record-new"}]

        client = FakeClient()
        args = argparse.Namespace(
            request_delay=0,
            rate_limit_delay=0,
            max_retries=0,
            continent="Ásia",
            condition="Não Tenho",
        )
        detail_url = "https://pt.ucoin.net/coin/china-1-jiao-1980-1986/?tid=5892"
        plan = {
            "updates": [],
            "creates": [
                {
                    "denomination": "1 jiao",
                    "issuePeriod": "1980 - 1986",
                    "ucoinUrl": detail_url,
                    "period": {"title": "China › República Popular da China › 1980-1986"},
                    "coin": {
                        "denomination": "1 jiao",
                        "issuePeriod": "1980 - 1986",
                        "availability": "scarce",
                        "detailUrl": detail_url,
                        "obverseImage": "https://i.ucoin.net/front.jpg",
                        "reverseImage": "https://i.ucoin.net/back.jpg",
                    },
                    "ordem": 1,
                }
            ],
        }

        with (
            patch.object(fix_site_issues_api.import_base44_coins, "create_client", return_value=client),
            redirect_stdout(io.StringIO()) as output,
        ):
            application_results: list[dict[str, object]] = []
            result = fix_site_issues_api.apply_plan(
                args,
                "China",
                plan,
                application_results,
            )

        self.assertEqual(result, {"updated": 0, "created": 1, "skipped": 0})
        self.assertEqual(client.created_record["name"], "1 jiao")
        self.assertEqual(client.created_record["years"], "1980 - 1986")
        self.assertEqual(client.created_record["url_ucoin"], detail_url)
        self.assertEqual(application_results[0]["status"], "created")
        self.assertEqual(application_results[0]["record_id"], "record-new")
        self.assertIn("Criado: 1/1 (100,0%) — China — 1 jiao (1980 - 1986)", output.getvalue())

        with (
            patch.object(fix_site_issues_api.import_base44_coins, "create_client", return_value=client),
            redirect_stdout(io.StringIO()) as second_output,
        ):
            second_results: list[dict[str, object]] = []
            second_run = fix_site_issues_api.apply_plan(args, "China", plan, second_results)

        self.assertEqual(second_run, {"updated": 0, "created": 0, "skipped": 1})
        self.assertEqual(client.bulk_calls, 1)
        self.assertEqual(second_results[0]["status"], "already_exists")
        self.assertIn("Já existe: 1/1 (100,0%)", second_output.getvalue())

    def test_apply_can_change_only_the_confirmed_name_and_years(self) -> None:
        class FakeClient:
            base_url = "https://base44.test/entities/Coin"

            def __init__(self) -> None:
                self.record = {
                    "id": "record-1",
                    "name": "10 cents",
                    "years": "2019",
                    "notes": "República",
                    "image_frente": "https://images.test/front.jpg",
                    "image_verso": "https://images.test/back.jpg",
                }

            def request(self, method, url):
                return dict(self.record)

            def update(self, record_id, payload):
                self.record = dict(payload)

        client = FakeClient()
        args = argparse.Namespace(request_delay=0, rate_limit_delay=0, max_retries=0, continent="", condition="")
        plan = {
            "updates": [
                {
                    "country": "País",
                    "denomination": "10 cêntimos",
                    "issuePeriod": "2020",
                    "record_id": "record-1",
                    "current": {"name": "10 cents", "years": "2019"},
                    "set": {"name": "10 cêntimos", "years": "2020"},
                }
            ],
            "creates": [],
        }

        with (
            patch.object(fix_site_issues_api.import_base44_coins, "create_client", return_value=client),
            redirect_stdout(io.StringIO()),
        ):
            application_results: list[dict[str, object]] = []
            result = fix_site_issues_api.apply_plan(args, "", plan, application_results)

        self.assertEqual(result, {"updated": 1, "created": 0, "skipped": 0})
        self.assertEqual(client.record["name"], "10 cêntimos")
        self.assertEqual(client.record["years"], "2020")
        self.assertEqual(client.record["notes"], "República")
        self.assertEqual(client.record["image_frente"], "https://images.test/front.jpg")
        self.assertEqual(client.record["image_verso"], "https://images.test/back.jpg")
        self.assertEqual(
            application_results,
            [
                {
                    "record_id": "record-1",
                    "fields": ["name", "years"],
                    "status": "applied",
                    "message": "name, years",
                }
            ],
        )

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
                    "country": "Portugal",
                    "denomination": "1 escudo",
                    "issuePeriod": "1950",
                    "record_id": "record-1",
                    "current": {"notes": ""},
                    "set": {"notes": "República"},
                }
            ],
            "creates": [],
        }

        with (
            patch.object(fix_site_issues_api.import_base44_coins, "create_client", return_value=client),
            redirect_stdout(io.StringIO()) as output,
        ):
            result = fix_site_issues_api.apply_plan(args, "", plan)

        self.assertEqual(result, {"updated": 1, "created": 0, "skipped": 0})
        self.assertEqual(client.payloads[0]["notes"], "República")
        self.assertEqual(client.payloads[0]["url_ucoin"], "https://old.test")
        self.assertEqual(client.payloads[0]["image_frente"], "https://images.test/front.jpg")
        self.assertEqual(client.payloads[0]["image_verso"], "https://images.test/back.jpg")
        self.assertIn("Atualizado: 1/1 (100,0%) — Portugal — 1 escudo (1950)", output.getvalue())
        self.assertIn("faltam: 0", output.getvalue())
        self.assertIn("restante: concluído", output.getvalue())

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
