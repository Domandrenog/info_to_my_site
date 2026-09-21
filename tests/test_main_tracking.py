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
    def test_association_review_applies_only_confirmed_name_updates(self, run) -> None:
        with redirect_stdout(io.StringIO()):
            result = main.run_association_review("filipinas")

        self.assertEqual(result, 0)
        command = run.call_args.args[0]
        self.assertIn("--reconcile-missing-interactive", command)
        self.assertEqual(command[command.index("--update-fields") + 1], "name")
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
    def test_autofix_can_limit_notes_to_countries_without_any_notes(
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
                    "current": {"notes": ""},
                    "set": {"notes": "Bahamas"},
                },
                {
                    "country": "Brasil",
                    "country_slug": "brasil",
                    "denomination": "1 real",
                    "record_id": "record-2",
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
                    "state": "none",
                },
                {
                    "country": "Brasil",
                    "country_slug": "brasil",
                    "total": 14,
                    "with_notes": 10,
                    "without_notes": 4,
                    "fixable_missing_notes": 1,
                    "state": "partial",
                },
            ],
            "missing": [],
            "errors": [],
            "manual_counts": {"photos": 0},
        }

        with redirect_stdout(io.StringIO()) as output:
            main.action_autofix_issues()

        applied_updates = apply_plan.call_args.args[2]["updates"]
        self.assertEqual([item["country"] for item in applied_updates], ["Bahamas"])
        self.assertIn("Bahamas: nenhuma (0/24 moedas com notes", output.getvalue())
        self.assertIn("Brasil: parcial (10/14 moedas com notes", output.getvalue())


if __name__ == "__main__":
    unittest.main()
