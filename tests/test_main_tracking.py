from __future__ import annotations

import io
import subprocess
import unittest
from contextlib import redirect_stdout
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


if __name__ == "__main__":
    unittest.main()
