from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.pennycollector_areas import (
    build_catalog,
    collect_locations,
    default_output_directory,
    interactive_area_menu,
    location_candidates,
    parse_area,
    parse_location_selection,
    pressed_design_count,
    preview_html,
    sync_existing_location_catalogs,
)


SAMPLE_HTML = """
<select id="MyReportLocation_CountryList"><option selected="selected">United States</option></select>
<select id="MyReportLocation_StateList"><option selected="selected">FL</option></select>
<table id="DG"><tr class="tbllist_header"><td>Location</td><td>City</td><td>Designs</td><td>Images</td><td>Updated</td></tr>
<tr><td>Kennedy Space Center<br><span>Space Commerce Way</span></td><td>Merritt Island</td><td>48p</td><td><a href="Details.aspx?location=1851"><img src="images/camera.gif"></a></td><td>12/28/25</td></tr>
<tr class="gone"><td><s>Old Place</s><br><span>Old Road</span></td><td>Orlando</td><td>Gone</td><td><a href="Details.aspx?location=99"><img src="images/camerano.gif"></a></td><td>01/01/20</td></tr>
<tr class="outoforder"><td>Repair Place<br><span>Main Road</span></td><td>Miami</td><td>4p</td><td><a href="Details.aspx?location=100"><img src="images/camera.gif"></a></td><td>02/02/26</td></tr></table>
"""


class PennyCollectorAreasTests(unittest.TestCase):
    def test_area_page_extracts_locations_and_states(self) -> None:
        locations, metadata = parse_area(SAMPLE_HTML)

        self.assertEqual(len(locations), 3)
        self.assertEqual(metadata["area_name"], "Florida")
        self.assertEqual(locations[0].location_id, "1851")
        self.assertEqual(locations[0].address, "Space Commerce Way")
        self.assertTrue(locations[0].has_images)
        self.assertEqual(locations[1].status, "Gone")
        self.assertFalse(locations[1].has_images)
        self.assertEqual(locations[2].status, "Out of Order")

    def test_area_catalog_is_discovery_only(self) -> None:
        locations, metadata = parse_area(SAMPLE_HTML)
        catalog = build_catalog(locations, metadata, area_id="14")

        self.assertEqual(catalog["status"], "discovery_only")
        self.assertFalse(catalog["base44_updated"])
        self.assertFalse(catalog["import_ready"])
        self.assertEqual(catalog["source"]["status_counts"]["Active"], 1)
        self.assertIn("Kennedy Space Center", preview_html(catalog))

    def test_florida_area_has_separate_discovery_path(self) -> None:
        self.assertEqual(
            default_output_directory({"country": "United States", "area_name": "Florida"}),
            Path("info/souvenirs/america/eua/areas/florida"),
        )

    def test_all_site_design_codes_count_as_collectible_candidates(self) -> None:
        self.assertEqual(pressed_design_count("4p"), 4)
        self.assertEqual(pressed_design_count("4n 1d 2q 3m 4e 5t"), 19)
        self.assertEqual(pressed_design_count("3e 1t"), 4)
        self.assertEqual(pressed_design_count("Gone"), 0)

    def test_area_filters_pressed_locations_and_accepts_ids_or_links(self) -> None:
        locations, metadata = parse_area(SAMPLE_HTML)
        catalog = build_catalog(locations, metadata, area_id="14")

        self.assertEqual(
            [location["location_id"] for location in location_candidates(catalog, "active")],
            ["1851"],
        )
        self.assertEqual(
            parse_location_selection(
                "1851, http://locations.pennycollector.com/Details.aspx?location=100",
                catalog,
            ),
            ["1851", "100"],
        )
        with self.assertRaisesRegex(ValueError, "não pertencem"):
            parse_location_selection("999", catalog)

    def test_existing_location_catalog_is_reused(self) -> None:
        locations, metadata = parse_area(SAMPLE_HTML)
        catalog = build_catalog(locations, metadata, area_id="14")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            existing = root / "merritt-island" / "kennedy" / "pennycollector-catalog.json"
            existing.parent.mkdir(parents=True)
            existing.write_text(
                '{"source": {"site": "PennyCollector", "location_id": "1851"}}',
                encoding="utf-8",
            )

            found = sync_existing_location_catalogs(catalog, root)

        self.assertEqual(found["1851"], existing)
        self.assertEqual(catalog["locations"][0]["collection_status"], "already_collected")
        self.assertEqual(catalog["collection"]["collected"], 1)

    @patch("scripts.pennycollector_areas.write_location_outputs")
    @patch("scripts.pennycollector_areas.default_location_output_directory")
    @patch("scripts.pennycollector_areas.build_location_catalog", return_value={"items": []})
    @patch("scripts.pennycollector_areas.parse_designs", return_value=([object()], {}))
    def test_collect_location_persists_area_progress(
        self,
        _parse_designs,
        _build_location_catalog,
        location_output_directory,
        write_location_outputs,
    ) -> None:
        locations, metadata = parse_area(SAMPLE_HTML)
        catalog = build_catalog(locations, metadata, area_id="14")
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            location_output_directory.return_value = output_dir / "location"
            write_location_outputs.return_value = (
                output_dir / "location" / "pennycollector-catalog.json",
                output_dir / "location" / "preview.html",
            )

            stats = collect_locations(
                catalog,
                ["1851"],
                area_output_dir=output_dir / "area",
                force=True,
                fetcher=lambda _url: "<html></html>",
            )

            self.assertTrue((output_dir / "area" / "pennycollector-area.json").exists())
        self.assertEqual(stats, {"collected": 1, "skipped": 0, "failed": 0})
        _parse_designs.assert_called_once_with(
            "<html></html>", include_retired=True
        )
        self.assertEqual(catalog["locations"][0]["collection_status"], "collected")

    @patch("scripts.pennycollector_areas.collect_locations")
    @patch("scripts.pennycollector_areas.write_outputs")
    @patch("scripts.pennycollector_areas.sync_existing_location_catalogs", return_value={})
    def test_interactive_menu_collects_exact_selected_ids(
        self,
        _sync_existing,
        _write_outputs,
        collect,
    ) -> None:
        locations, metadata = parse_area(SAMPLE_HTML)
        catalog = build_catalog(locations, metadata, area_id="14")
        collect.return_value = {"collected": 1, "skipped": 0, "failed": 0}
        answers = iter(["1", "1851", "", "5"])

        interactive_area_menu(
            catalog,
            Path("unused"),
            input_fn=lambda _prompt: next(answers),
        )

        self.assertEqual(collect.call_args.args[1], ["1851"])


if __name__ == "__main__":
    unittest.main()
