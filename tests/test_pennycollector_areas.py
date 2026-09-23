from __future__ import annotations

import unittest
from pathlib import Path

from scripts.pennycollector_areas import (
    build_catalog,
    default_output_directory,
    parse_area,
    preview_html,
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


if __name__ == "__main__":
    unittest.main()
