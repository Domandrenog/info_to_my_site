from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import main


PLANS = [
    {
        "country": "Bahamas",
        "continent": "América",
        "ucoin_country_link_name": "bahamas",
        "first_year": 1966,
    },
    {
        "country": "Brasil",
        "continent": "América",
        "ucoin_country_link_name": "brazil",
        "first_year": 1994,
    },
]


class MainTrackingActionTests(unittest.TestCase):
    @patch("main.run_step", return_value=0)
    @patch("main.title")
    @patch("main.ask_text", side_effect=["Magic Kingdom", "2", "", "Orlando"])
    def test_presscoins_collection_passes_selected_availability(
        self,
        _ask_text,
        _title,
        run_step,
    ) -> None:
        main.action_collect_presscoins_souvenirs()

        command = run_step.call_args.args[1]
        availability_index = command.index("--availability")
        self.assertEqual(command[availability_index + 1], "All")
        self.assertNotIn("--search", command)

    @patch("main.run_step", return_value=0)
    @patch("main.title")
    @patch("main.ask_text", return_value="info/souvenirs/catalog.json")
    def test_souvenir_import_action_can_preview_without_apply(
        self,
        _ask_text,
        _title,
        run_step,
    ) -> None:
        main.action_import_presscoins_souvenirs(apply=False)

        command = run_step.call_args.args[1]
        self.assertIn("scripts.import_base44_souvenirs", command)
        self.assertNotIn("--apply", command)

    @patch("main.run_step", return_value=0)
    @patch("main.title")
    @patch("main.ask_text", return_value="info/souvenirs/catalog.json")
    def test_souvenir_import_action_applies_only_when_selected(
        self,
        _ask_text,
        _title,
        run_step,
    ) -> None:
        main.action_import_presscoins_souvenirs(apply=True)

        command = run_step.call_args.args[1]
        self.assertEqual(command[-1], "--apply")

    @patch("main.menu_atualizar_site")
    @patch("main.title")
    @patch("main.ask_text", side_effect=["4", "5"])
    def test_base44_update_menu_is_inside_normal_coins(
        self,
        _ask_text,
        _title,
        update_site_menu,
    ) -> None:
        with redirect_stdout(io.StringIO()) as output:
            main.menu_importar_ucoin()

        self.assertIn("4) Atualizar Site Base44", output.getvalue())
        update_site_menu.assert_called_once_with()

    @patch("main.menu_presscoins_usa")
    @patch("main.title")
    @patch("main.ask_text", side_effect=["1", "3"])
    def test_souvenirs_menu_groups_entries_by_source_site(
        self,
        _ask_text,
        _title,
        presscoins_menu,
    ) -> None:
        with redirect_stdout(io.StringIO()) as output:
            main.menu_souvenirs()

        self.assertIn("1) Presscoins — USA", output.getvalue())
        self.assertIn("2) PennyCollector.com", output.getvalue())
        presscoins_menu.assert_called_once_with()

    @patch("main.menu_pressedcoins_disney_orlando")
    @patch("main.title")
    @patch("main.ask_text", side_effect=["1", "2"])
    def test_presscoins_menu_exposes_disney_orlando(
        self,
        _ask_text,
        _title,
        pressedcoins_menu,
    ) -> None:
        with redirect_stdout(io.StringIO()) as output:
            main.menu_presscoins_usa()

        self.assertIn("1) Disney Orlando", output.getvalue())
        pressedcoins_menu.assert_called_once_with()

    @patch("main.run_step", return_value=0)
    @patch("main.title")
    @patch(
        "main.ask_text",
        return_value="http://locations.pennycollector.com/Details.aspx?location=1851",
    )
    def test_pennycollector_accepts_location_link(self, _ask_text, _title, run_step) -> None:
        main.action_collect_pennycollector_location()

        command = run_step.call_args.args[1]
        self.assertIn("scripts.pennycollector_souvenirs", command)
        self.assertIn("Details.aspx?location=1851", command[-1])
        self.assertNotIn("--apply", command)

    @patch("main.run_step", return_value=0)
    @patch("main.title")
    @patch("main.ask_text", return_value="8143")
    def test_pennycollector_location_includes_retired_by_default(
        self, _ask_text, _title, run_step
    ) -> None:
        main.action_collect_pennycollector_location()

        command = run_step.call_args.args[1]
        self.assertIn("8143", command)
        self.assertNotIn("--current-only", command)

    @patch("main.run_step", return_value=0)
    @patch("main.title")
    @patch(
        "main.ask_text",
        return_value="http://locations.pennycollector.com/Locations.aspx?area=14",
    )
    def test_pennycollector_accepts_area_link(self, _ask_text, _title, run_step) -> None:
        main.action_collect_pennycollector_area()

        command = run_step.call_args.args[1]
        self.assertIn("scripts.pennycollector_areas", command)
        self.assertIn("http://locations.pennycollector.com/Locations.aspx?area=14", command)
        self.assertIn("--interactive", command)
        self.assertNotIn("--apply", command)

    @patch("main.title")
    @patch("main.ask_text", side_effect=["7"])
    def test_pennycollector_menu_exposes_complete_review_and_import_flow(
        self,
        _ask_text,
        _title,
    ) -> None:
        with redirect_stdout(io.StringIO()) as output:
            main.menu_pennycollector()

        menu = output.getvalue()
        self.assertIn("1) Recolher localização por link ou ID", menu)
        self.assertIn("2) Recolher área por link ou ID", menu)
        self.assertIn("3) Rever e aprovar catálogo recolhido", menu)
        self.assertIn("4) Verificar o que falta no Site Base44", menu)
        self.assertIn("5) Importar apenas souvenirs aprovados e em falta", menu)
        self.assertIn("6) Corrigir tipos de tokens e medalhões", menu)
        self.assertIn("7) Voltar", menu)
        self.assertNotIn("Kennedy Space Center", menu)

    @patch("main.action_import_pennycollector_souvenirs")
    @patch("builtins.input", return_value="")
    @patch("main.title")
    @patch("main.ask_text", side_effect=["4", "7"])
    def test_pennycollector_menu_keeps_verification_result_visible(
        self,
        _ask_text,
        _title,
        wait_for_continue,
        verify_souvenirs,
    ) -> None:
        main.menu_pennycollector()

        verify_souvenirs.assert_called_once_with(apply=False)
        wait_for_continue.assert_called_once_with("\nCarrega Enter para continuar...")

    @patch("main.run_step", return_value=0)
    @patch("main.title")
    @patch(
        "main.ask_text",
        side_effect=["2", "info/pennycollector-catalog.json"],
    )
    def test_pennycollector_review_uses_separate_approval_step(
        self, _ask_text, _title, run_step
    ) -> None:
        main.action_review_pennycollector_souvenirs()

        command = run_step.call_args.args[1]
        self.assertIn("scripts.review_pennycollector_souvenirs", command)
        self.assertIn("info/pennycollector-catalog.json", command)
        self.assertNotIn("--apply", command)

    @patch("main.run_step", return_value=0)
    @patch("main.title")
    @patch(
        "main.ask_text",
        side_effect=["2", "info/pennycollector-catalog-final.json"],
    )
    def test_pennycollector_import_requires_final_catalog_and_apply_flag(
        self, _ask_text, _title, run_step
    ) -> None:
        main.action_import_pennycollector_souvenirs(apply=True)

        command = run_step.call_args.args[1]
        self.assertIn("scripts.import_base44_souvenirs", command)
        self.assertIn("info/pennycollector-catalog-final.json", command)
        self.assertIn("--apply", command)

    @patch("main.run_step", return_value=0)
    @patch("main.title")
    @patch("main.ask_text", return_value="1")
    def test_pennycollector_review_can_process_all_catalogs(
        self, _ask_text, _title, run_step
    ) -> None:
        main.action_review_pennycollector_souvenirs()

        command = run_step.call_args.args[1]
        self.assertIn("--all-catalogs", command)
        self.assertIn("info/souvenirs", command)
        self.assertNotIn("--input", command)

    @patch("main.run_step", return_value=0)
    @patch("main.title")
    @patch("main.ask_text", return_value="1")
    def test_pennycollector_import_can_process_all_souvenir_catalogs(
        self, _ask_text, _title, run_step
    ) -> None:
        main.action_import_pennycollector_souvenirs(apply=True)

        command = run_step.call_args.args[1]
        self.assertIn("--all-catalogs", command)
        self.assertIn("info/souvenirs", command)
        self.assertIn("--apply", command)

    @patch("main.run_step", return_value=0)
    @patch("main.title")
    def test_pennycollector_type_correction_uses_explicit_apply_flow(
        self, _title, run_step
    ) -> None:
        main.action_fix_pennycollector_souvenir_types()

        command = run_step.call_args.args[1]
        self.assertIn("scripts.fix_pennycollector_souvenir_types", command)
        self.assertIn("info/souvenirs", command)
        self.assertIn("--apply", command)

    def test_unconfirmed_associations_are_grouped_by_country(self) -> None:
        payload = [
            {
                "country": "filipinas",
                "country_name": "Filipinas",
                "coins_with_issues": [
                    {
                        "denomination": "10 piso",
                        "issues": [{"type": "missing_api_coin_record"}],
                    },
                    {
                        "denomination": "20 piso",
                        "issues": [{"type": "missing_image_url"}],
                    },
                ],
            },
            {
                "country": "bahamas",
                "country_name": "Bahamas",
                "coins_with_issues": [
                    {
                        "denomination": "1 cent",
                        "issues": [{"type": "missing_api_coin_record"}],
                    }
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "differences.json"
            report_path.write_text(json.dumps(payload), encoding="utf-8")

            countries = main.unconfirmed_association_countries(report_path)

        self.assertEqual(
            countries,
            [
                {"country": "filipinas", "country_name": "Filipinas", "coin_count": 1},
                {"country": "bahamas", "country_name": "Bahamas", "coin_count": 1},
            ],
        )

    @patch("main.run_association_review", return_value=0)
    @patch("main.ask_text", side_effect=["1", "todos"])
    @patch(
        "main.unconfirmed_association_countries",
        return_value=[
            {"country": "filipinas", "country_name": "Filipinas", "coin_count": 2},
            {"country": "bahamas", "country_name": "Bahamas", "coin_count": 1},
        ],
    )
    def test_association_review_can_process_all_countries(
        self,
        _countries,
        _ask_text,
        run_review,
    ) -> None:
        with redirect_stdout(io.StringIO()) as output:
            main.offer_unconfirmed_association_review(Path("differences.json"))

        self.assertIn("3 moedas em 2 países", output.getvalue())
        self.assertEqual(
            [call.args[0] for call in run_review.call_args_list],
            ["filipinas", "bahamas"],
        )

    @patch("main.subprocess.run", return_value=subprocess.CompletedProcess([], 0))
    def test_association_review_applies_only_confirmed_name_and_year_updates(self, run) -> None:
        with redirect_stdout(io.StringIO()):
            result = main.run_association_review("filipinas")

        self.assertEqual(result, 0)
        command = run.call_args.args[0]
        self.assertIn("--reconcile-missing-interactive", command)
        update_fields_index = command.index("--update-fields")
        self.assertEqual(command[update_fields_index + 1 : update_fields_index + 3], ["name", "years"])
        self.assertIn("--apply", command)
        self.assertIn("--skip-create-missing", command)

    @patch("main.run_step", return_value=0)
    @patch("main.add_browser_mode", side_effect=lambda command: command.append("--attach-cdp"))
    @patch("main.ask_text", return_value="1")
    @patch("main.load_missing_photo_entries", return_value=[{"denomination": "10 cêntimos"}])
    def test_photo_recheck_option_runs_only_after_selection(
        self,
        _load_entries,
        _ask_text,
        _browser_mode,
        run_step,
    ) -> None:
        report_path = main.Path("info/paises/all-differences.json")

        with redirect_stdout(io.StringIO()) as output:
            main.offer_ucoin_photo_recheck(report_path)

        self.assertIn("1 moeda sem fotografias", output.getvalue())
        run_step.assert_called_once_with(
            "Rever fotografias no uCoin",
            [
                main.sys.executable,
                "-m",
                "scripts.recheck_ucoin_photos",
                "--input",
                str(report_path),
                "--attach-cdp",
            ],
        )

    @patch("main.run_step")
    @patch("main.ask_text", return_value="0")
    @patch("main.load_missing_photo_entries", return_value=[{"denomination": "10 cêntimos"}])
    def test_photo_recheck_option_can_be_skipped(self, _load_entries, _ask_text, run_step) -> None:
        with redirect_stdout(io.StringIO()):
            main.offer_ucoin_photo_recheck(main.Path("report.json"))

        run_step.assert_not_called()

    @patch("main.load_image_source_replacements", return_value=[])
    @patch("main.run_ucoin_photo_recheck")
    @patch("main.review_unconfirmed_association_countries")
    @patch("main.ask_text", side_effect=["2", "1"])
    @patch(
        "main.load_missing_photo_entries",
        return_value=[{"denomination": "10 cêntimos"}],
    )
    @patch(
        "main.unconfirmed_association_countries",
        return_value=[
            {"country": "filipinas", "country_name": "Filipinas", "coin_count": 2},
        ],
    )
    def test_difference_followups_keep_name_and_photo_reviews_available(
        self,
        _countries,
        _photo_entries,
        _ask_text,
        review_associations,
        recheck_photos,
        _source_replacements,
    ) -> None:
        countries = [
            {"country": "filipinas", "country_name": "Filipinas", "coin_count": 2},
        ]
        review_associations.return_value = ["filipinas"]

        with redirect_stdout(io.StringIO()) as output:
            main.offer_difference_followups(Path("differences.json"))

        displayed = output.getvalue()
        self.assertIn("1) Rever nomes semelhantes no Site Base44 — 2 moedas em 1 país", displayed)
        self.assertIn("2) Rever fotografias indisponíveis no uCoin — 1 moeda", displayed)
        self.assertIn("0) Terminar", displayed)
        recheck_photos.assert_called_once_with(Path("differences.json"))
        review_associations.assert_called_once_with(countries)

    @patch("main.load_image_source_replacements", return_value=[])
    @patch("main.review_unconfirmed_association_countries", side_effect=[["coreia-do-sul"], ["bahamas"]])
    @patch("main.ask_text", side_effect=["1", "1"])
    @patch("main.load_missing_photo_entries", return_value=[])
    @patch(
        "main.unconfirmed_association_countries",
        return_value=[
            {"country": "coreia-do-sul", "country_name": "Coreia do Sul", "coin_count": 6},
            {"country": "bahamas", "country_name": "Bahamas", "coin_count": 2},
        ],
    )
    def test_difference_followups_offer_remaining_countries_after_each_review(
        self,
        _countries,
        _photo_entries,
        _ask_text,
        review_associations,
        _source_replacements,
    ) -> None:
        with redirect_stdout(io.StringIO()) as output:
            main.offer_difference_followups(Path("differences.json"))

        first_countries = review_associations.call_args_list[0].args[0]
        second_countries = review_associations.call_args_list[1].args[0]
        self.assertEqual(
            [item["country"] for item in first_countries],
            ["coreia-do-sul", "bahamas"],
        )
        self.assertEqual(
            [item["country"] for item in second_countries],
            ["bahamas"],
        )
        self.assertIn("8 moedas em 2 países", output.getvalue())
        self.assertIn("2 moedas em 1 país", output.getvalue())

    @patch("main.run_image_source_review", return_value=0)
    @patch(
        "main.load_image_source_replacements",
        return_value=[
            {
                "country": "filipinas",
                "denomination": f"moeda-{index // 2}",
                "issuePeriod": "2020",
                "side": "frente" if index % 2 == 0 else "tras",
                "current": f"https://base44.app/{index}.jpg",
                "proposed": f"https://i.ucoin.net/{index}.jpg",
            }
            for index in range(38)
        ],
    )
    @patch("main.load_missing_photo_entries", return_value=[])
    @patch("main.unconfirmed_association_countries", return_value=[])
    @patch("main.ask_text", return_value="1")
    def test_difference_followups_offer_image_source_replacements(
        self,
        _ask_text,
        _countries,
        _photo_entries,
        _source_replacements,
        run_source_review,
    ) -> None:
        report_path = Path("differences.json")

        with redirect_stdout(io.StringIO()) as output:
            main.offer_difference_followups(report_path)

        self.assertIn("1) Rever origens das fotografias — 19 moedas / 38 links", output.getvalue())
        run_source_review.assert_called_once_with(report_path)

    @patch("main.subprocess.run", return_value=subprocess.CompletedProcess([], 1))
    @patch("main.ask_yes_no", return_value=True)
    @patch("main.title")
    def test_run_step_accepts_difference_exit_code(self, _title, _confirm, run) -> None:
        with redirect_stdout(io.StringIO()) as output:
            result = main.run_step("Check diferenças", ["checker"], accepted_exit_codes={0, 1})

        self.assertEqual(result, 1)
        self.assertIn("foram encontradas diferencas", output.getvalue())
        run.assert_called_once_with(["checker"], cwd=main.PROJECT_DIR, check=False)

    @patch("main.subprocess.run")
    @patch("main.ask_yes_no", return_value=False)
    @patch("main.add_browser_mode", side_effect=lambda args: args.append("--attach-cdp"))
    @patch("main.ask_text", return_value="todos")
    @patch("main.print_tracking_plans")
    @patch("main.load_tracking_plans", return_value=(PLANS, None))
    @patch("main.title")
    def test_cancelled_plan_does_not_start_collection(
        self,
        _title,
        _load,
        _print_plans,
        _ask_text,
        _browser,
        _confirm,
        run,
    ) -> None:
        with redirect_stdout(io.StringIO()):
            main.action_collect_missing_country_tracking()

        run.assert_not_called()

    @patch("main.subprocess.run")
    @patch("main.ask_yes_no", return_value=True)
    @patch("main.add_browser_mode", side_effect=lambda args: args.append("--attach-cdp"))
    @patch("main.ask_text", return_value="todos")
    @patch("main.print_tracking_plans")
    @patch("main.load_tracking_plans", return_value=(PLANS, None))
    @patch("main.title")
    def test_confirmed_plan_runs_every_selected_country_once(
        self,
        _title,
        _load,
        _print_plans,
        _ask_text,
        _browser,
        _confirm,
        run,
    ) -> None:
        with redirect_stdout(io.StringIO()):
            main.action_collect_missing_country_tracking()

        self.assertEqual(run.call_count, 3)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][commands[0].index("--start-year") + 1], "1966")
        self.assertEqual(commands[1][commands[1].index("--start-year") + 1], "1994")
        self.assertTrue(all("--no-wait-for-final" in command for command in commands[:2]))
        self.assertEqual(
            commands[2],
            [
                main.sys.executable,
                "-m",
                "scripts.manage_pending_rarities",
                "--wait-for-final",
                "--cleanup-intermediate",
            ],
        )

    @patch("main.subprocess.run")
    @patch("main.pending_rarity_catalogues", return_value=[main.Path("pending.json")])
    @patch("main.load_tracking_plans", return_value=([], None))
    @patch("main.title")
    def test_collection_action_resumes_pending_rarity_stage(self, _title, _load, _pending, run) -> None:
        with redirect_stdout(io.StringIO()):
            main.action_collect_missing_country_tracking()

        run.assert_called_once_with(
            [
                main.sys.executable,
                "-m",
                "scripts.manage_pending_rarities",
                "--wait-for-final",
                "--cleanup-intermediate",
            ],
            cwd=main.PROJECT_DIR,
            check=True,
        )

    @patch("main.fix_site_issues_api.apply_plan", return_value={"updated": 1, "created": 0, "skipped": 0})
    @patch("main.ask_yes_no", return_value=True)
    @patch("main.ask_text", return_value="1")
    @patch("main.fix_site_issues_api.collect_global_fix_plan")
    @patch("main.title")
    def test_autofix_only_applies_the_selected_available_field(
        self,
        _title,
        collect,
        _ask_text,
        _confirm,
        apply_plan,
    ) -> None:
        collect.return_value = {
            "updates": [
                {
                    "country": "Portugal",
                    "denomination": "1 escudo",
                    "issuePeriod": "1950",
                    "record_id": "record-1",
                    "current": {"url_ucoin": "", "notes": ""},
                    "set": {"url_ucoin": "https://pt.ucoin.net/coin/example", "notes": "República"},
                }
            ],
            "missing": [],
            "errors": [],
            "manual_counts": {"photos": 0},
        }

        with redirect_stdout(io.StringIO()) as output:
            main.action_autofix_issues()

        applied_plan = apply_plan.call_args.args[2]
        self.assertEqual(applied_plan["updates"][0]["set"], {"url_ucoin": "https://pt.ucoin.net/coin/example"})
        self.assertNotIn("fotografias a rever", output.getvalue().lower())

    @patch("main.fix_site_issues_api.apply_plan", return_value={"updated": 1, "created": 0, "skipped": 0})
    @patch("main.ask_yes_no", return_value=True)
    @patch("main.ask_text", side_effect=["1", "1"])
    @patch("main.fix_site_issues_api.collect_global_fix_plan")
    @patch("main.title")
    def test_autofix_can_apply_only_safe_year_extensions(
        self,
        _title,
        collect,
        _ask_text,
        _confirm,
        apply_plan,
    ) -> None:
        collect.return_value = {
            "updates": [
                {
                    "country": "Brasil",
                    "country_slug": "brasil",
                    "denomination": "1 real",
                    "record_id": "record-1",
                    "safe_year_extension": True,
                    "current": {"years": "2002-2024"},
                    "set": {"years": "2002 - 2026"},
                },
                {
                    "country": "Malásia",
                    "country_slug": "malasia",
                    "denomination": "10 sen",
                    "record_id": "record-2",
                    "safe_year_extension": False,
                    "current": {"years": "1989-2011"},
                    "set": {"years": "1967 - 1988"},
                },
            ],
            "notes_status": [],
            "missing": [],
            "errors": [],
            "manual_counts": {"photos": 0},
        }

        with redirect_stdout(io.StringIO()) as output:
            main.action_autofix_issues()

        applied_updates = apply_plan.call_args.args[2]["updates"]
        self.assertEqual([item["country"] for item in applied_updates], ["Brasil"])
        self.assertIn("Apenas prolongar o ano final", output.getvalue())
        self.assertNotIn("Malásia:", output.getvalue())

    @patch("main.fix_site_issues_api.apply_plan", return_value={"updated": 1, "created": 0, "skipped": 0})
    @patch("main.ask_yes_no", return_value=True)
    @patch("main.ask_text", side_effect=["1", "1"])
    @patch("main.fix_site_issues_api.collect_global_fix_plan")
    @patch("main.title")
    def test_autofix_can_limit_notes_to_safe_proposals(
        self,
        _title,
        collect,
        _ask_text,
        _confirm,
        apply_plan,
    ) -> None:
        collect.return_value = {
            "updates": [
                {
                    "country": "Bahamas",
                    "country_slug": "bahamas",
                    "denomination": "1 cent",
                    "record_id": "record-1",
                    "safe_note": False,
                    "current": {"notes": ""},
                    "set": {"notes": "Bahamas"},
                },
                {
                    "country": "Brasil",
                    "country_slug": "brasil",
                    "denomination": "1 real",
                    "record_id": "record-2",
                    "safe_note": True,
                    "current": {"notes": ""},
                    "set": {"notes": "República Federativa do Brasil"},
                },
            ],
            "notes_status": [
                {
                    "country": "Bahamas",
                    "country_slug": "bahamas",
                    "total": 24,
                    "with_notes": 0,
                    "without_notes": 24,
                    "fixable_missing_notes": 1,
                    "safe_missing_notes": 0,
                    "review_missing_notes": 1,
                    "state": "none",
                },
                {
                    "country": "Brasil",
                    "country_slug": "brasil",
                    "total": 14,
                    "with_notes": 10,
                    "without_notes": 4,
                    "fixable_missing_notes": 1,
                    "safe_missing_notes": 1,
                    "review_missing_notes": 0,
                    "state": "partial",
                },
                {
                    "country": "Canadá",
                    "country_slug": "canada",
                    "total": 200,
                    "with_notes": 200,
                    "without_notes": 0,
                    "fixable_missing_notes": 0,
                    "safe_missing_notes": 0,
                    "review_missing_notes": 0,
                    "state": "complete",
                },
            ],
            "missing": [],
            "errors": [],
            "manual_counts": {"photos": 0},
        }

        with redirect_stdout(io.StringIO()) as output:
            main.action_autofix_issues()

        applied_updates = apply_plan.call_args.args[2]["updates"]
        self.assertEqual([item["country"] for item in applied_updates], ["Brasil"])
        self.assertIn("Bahamas: nenhuma (0/24 moedas com notes", output.getvalue())
        self.assertIn("Brasil: parcial (10/14 moedas com notes", output.getvalue())
        self.assertIn("Apenas correções seguras", output.getvalue())
        self.assertNotIn("Canadá: completa", output.getvalue())


if __name__ == "__main__":
    unittest.main()
