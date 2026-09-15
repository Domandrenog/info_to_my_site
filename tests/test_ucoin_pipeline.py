import argparse
import unittest
from pathlib import Path

from scripts import ucoin_pipeline


def args(**overrides):
    values = {
        "country": "India",
        "period": None,
        "country_link_name": "",
        "start_year": None,
        "output_dir": "",
        "catalog_output": "",
        "attach_cdp": False,
        "cdp_url": "http://127.0.0.1:9222",
        "manual_session": False,
        "no_manual_session": False,
        "headless": False,
        "timeout": 60,
        "max_pages": 50,
        "retries": 2,
        "sequential_fallback": False,
        "no_wait_for_final": False,
        "cleanup_intermediate": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class UCoinPipelineTests(unittest.TestCase):
    def test_default_catalog_path_uses_country_slug(self) -> None:
        self.assertEqual(ucoin_pipeline.default_catalog_path(args(country="Índia")), Path("paises/india/ucoin-catalog.json"))

    def test_scrape_command_passes_cdp_manual_and_start_year(self) -> None:
        command = ucoin_pipeline.build_scrape_command(args(start_year=1957, attach_cdp=True, manual_session=True))
        self.assertIn("--start-year", command)
        self.assertIn("1957", command)
        self.assertIn("--attach-cdp", command)
        self.assertIn("--manual-session", command)

    def test_scrape_command_passes_country_link_name_when_provided(self) -> None:
        command = ucoin_pipeline.build_scrape_command(args(country_link_name="belarus"))
        self.assertIn("--country-link-name", command)
        self.assertIn("belarus", command)

    def test_generate_command_waits_for_final_by_default(self) -> None:
        command = ucoin_pipeline.build_generate_command(args(), Path("paises/india/ucoin-catalog.json"))
        self.assertIn("--wait-for-final", command)

    def test_generate_command_can_stop_after_pending_catalogue(self) -> None:
        command = ucoin_pipeline.build_generate_command(args(no_wait_for_final=True), Path("paises/india/ucoin-catalog.json"))
        self.assertNotIn("--wait-for-final", command)

    def test_generate_command_can_clean_intermediate_files(self) -> None:
        command = ucoin_pipeline.build_generate_command(args(cleanup_intermediate=True), Path("paises/india/ucoin-catalog.json"))
        self.assertIn("--cleanup-intermediate", command)


if __name__ == "__main__":
    unittest.main()
